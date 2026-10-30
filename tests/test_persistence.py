"""Cache, run ledger, daily usage and chunk vectors over a real index.

Each guards money: a lost cache row is a request paid twice, and a raw
exception out of a repository ends a run as "internal error" (exit 1) where
a StorageError stops it cleanly with exit code 9.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import threading
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from devdocs.errors import StorageError
from devdocs.index.cache import ResponseCache
from devdocs.index.chunk import Chunk, ChunkStore
from devdocs.index.db import Database
from devdocs.index.ledger import DailyUsage, RunLedger, config_fingerprint
from devdocs.llm import Completion, Finish, Request, Usage
from devdocs.llm.budget import Router
from devdocs.llm.catalog import Tier
from devdocs.llm.client import LLMClient
from devdocs.llm.http import RetryPolicy

# A file name that is not UTF-8, as os.walk yields it on Linux.
ODD = (b"caf" + bytes([0xE9]) + b".py").decode("utf-8", "surrogateescape")


def answer(model: str, text: str = "doc") -> Completion:
    return Completion(text, model, "gemini", Finish.STOP, Usage(1000, 200))


class Counting:
    """A ChatModel that answers everything and counts what it is paid for."""

    provider = "gemini"

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.lock = threading.Lock()

    def list_models(self) -> list[str]:
        return []

    def generate(self, request: Request) -> Completion:
        with self.lock:
            self.calls[request.prompt] = self.calls.get(request.prompt, 0) + 1
        return answer(request.model, f"doc for {request.prompt}")

    @property
    def total(self) -> int:
        return sum(self.calls.values())


def client_for(db: Database, model: Counting) -> LLMClient:
    return LLMClient(
        model,
        router=Router("gemini"),
        cache=ResponseCache(db),
        usage=DailyUsage(db),
        retry=RetryPolicy(base_delay=0),
        free_tier=False,
    )


def file_database(case: unittest.TestCase) -> Path:
    return Path(case.enterContext(tempfile.TemporaryDirectory())) / "devdocs.db"


class TestCacheKey(unittest.TestCase):
    def test_valid_text_keys_exactly_as_before(self) -> None:
        # A changed key would re-buy every cached answer.
        request = Request(model="gemini-2.5-flash-lite", system="SYS",
                          prompt="café ✓ 日本 😀", max_output_tokens=800,
                          temperature=0.2, effort=None)
        self.assertEqual(
            ResponseCache.key(request, provider="gemini"),
            "526e4306dd6a7c0ae63496726ae8a4a32e5fb69fd02629a1f3b72dcb8c962c85",
        )

    def test_undecodable_file_name_keys_stably(self) -> None:
        def key(prompt: str) -> str:
            return ResponseCache.key(Request(model="m", system="s", prompt=prompt),
                                     provider="gemini")

        self.assertEqual(key(f"File: {ODD}"), key(f"File: {ODD}"))
        self.assertNotEqual(key(f"File: {ODD}"), key("File: caf\udce8.py"))


class TestUndecodableText(unittest.TestCase):
    """Text holding lone surrogates is keyed, stored and matched exactly."""

    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)

    def test_prompt_is_priced_paid_once_then_cached(self) -> None:
        model = Counting()
        client = client_for(self.db, model)
        prompt = f"File: src/{ODD}\n\ndef f(): pass\n"
        quote = client.quote(Tier.FILE, "SYS", prompt)
        self.assertFalse(quote.cached)
        self.assertGreater(quote.cost, 0)
        first = client.complete(Tier.FILE, "SYS", prompt)
        again = client.complete(Tier.FILE, "SYS", prompt)
        self.assertEqual((first.cached, again.cached), (False, True))
        self.assertEqual(model.total, 1)
        self.assertTrue(client.quote(Tier.FILE, "SYS", prompt).cached)

    def test_answer_is_cached_exactly(self) -> None:
        cache = ResponseCache(self.db)
        self.assertTrue(cache.put("k", answer("m", f"see {ODD}")))
        hit = cache.get("k")
        assert hit is not None
        self.assertEqual(hit.text, f"see {ODD}")

    def test_chunks_track_the_file(self) -> None:
        # Regression: replace raised StorageError for such a name.
        store = ChunkStore(self.db)
        chunk = Chunk("c1", ODD, "", 1, 1, f"{ODD}\nx = 1")
        self.assertEqual(store.replace(ODD, [chunk]), (1, 0))
        self.assertEqual(store.replace(ODD, [chunk]), (0, 0))   # found, not added again
        store.store([("c1", [0.5])], "embed-1")
        self.assertEqual(store.missing([chunk], "embed-1", 1), [])

        moved = Chunk("c2", ODD, "", 1, 1, f"{ODD}\nx = 2")
        self.assertEqual(store.replace(ODD, [moved]), (1, 1))
        self.assertEqual(store.vectors("embed-1", 1, {"c1"}), ([], []))

    def test_pruning_asks_about_the_file_by_its_name(self) -> None:
        store = ChunkStore(self.db)
        store.replace(ODD, [Chunk("c1", ODD, "", 1, 1, "x = 1")])
        asked: list[str] = []

        def there(source: str) -> bool:
            asked.append(source)
            return True

        self.assertEqual(store.prune(there), 0)
        self.assertEqual(asked, [ODD])
        self.assertEqual(store.prune(lambda source: False), 1)
        self.assertIsNone(self.db.query_one("SELECT id FROM chunk"))

    def test_valid_text_is_still_stored_as_text(self) -> None:
        ChunkStore(self.db).replace("café.py", [Chunk("c1", "café.py", "", 1, 1, "x")])
        row = self.db.query_one("SELECT typeof(source), source FROM chunk")
        assert row is not None
        self.assertEqual((row[0], row[1]), ("text", "café.py"))

    def test_fingerprint_accepts_an_undecodable_path(self) -> None:
        self.assertEqual(len(config_fingerprint(f"/home/{ODD}", "gemini")), 16)

    def test_fingerprint_is_unchanged_for_valid_text(self) -> None:
        # A changed fingerprint would leave every interrupted run unresumable.
        fingerprint = config_fingerprint(
            "/home/café/日本", "gemini", "balanced", ("*.py",), ()
        )
        self.assertEqual(fingerprint, "6f8cda9bdaa7eb72")


class TestFailuresAreStorageErrors(unittest.TestCase):
    """Whatever SQLite refuses, repositories raise StorageError (exit 9)."""

    def setUp(self) -> None:
        self.db = Database(file_database(self))
        self.addCleanup(self.db.close)
        self.cache = ResponseCache(self.db)
        self.ledger = RunLedger(self.db)
        self.usage = DailyUsage(self.db)
        self.cache.put("k", answer("m"))
        self.run_id, _ = self.ledger.start("cfg")
        self.ledger.finish(self.run_id, "interrupted")
        self.usage.record("m")

    def refuse_everything(self) -> None:
        self.db.connection.set_authorizer(lambda *_: sqlite3.SQLITE_DENY)

    def test_every_repository_method(self) -> None:
        self.refuse_everything()
        calls: dict[str, Callable[[], object]] = {
            "ResponseCache.get": lambda: self.cache.get("k"),
            "ResponseCache.put": lambda: self.cache.put("k2", answer("m")),
            "ResponseCache.stats": self.cache.stats,
            "ResponseCache.clear": self.cache.clear,
            "RunLedger.start": lambda: self.ledger.start("cfg"),
            "RunLedger.start fresh": lambda: self.ledger.start("cfg", resume=False),
            "RunLedger.finish": lambda: self.ledger.finish(self.run_id),
            "DailyUsage.record": lambda: self.usage.record("m"),
            "DailyUsage.requests": lambda: self.usage.requests("m"),
        }
        for name, call in calls.items():
            with self.subTest(name), self.assertRaises(StorageError):
                call()

    def test_client_raises_storage_error_and_sends_nothing(self) -> None:
        self.refuse_everything()
        model = Counting()
        client = client_for(self.db, model)
        with self.assertRaises(StorageError):
            client.quote(Tier.FILE, "SYS", "a")
        with self.assertRaises(StorageError):
            client.complete(Tier.FILE, "SYS", "a")
        self.assertEqual(model.total, 0)

    def test_damaged_pages(self) -> None:
        for index in range(60):
            self.cache.put(f"k{index}", answer("m", "x" * 300))
        roots = {row["name"]: row["rootpage"] for row in self.db.query(
            "SELECT name, rootpage FROM sqlite_master WHERE rootpage > 1")}
        page_size = int(self.db.query("PRAGMA page_size")[0][0])
        self.db.close()

        # Garble the data pages but not the schema, as a torn write would.
        data = bytearray(self.db.path.read_bytes())
        for page in roots.values():
            start = (page - 1) * page_size
            data[start + 8:start + page_size] = b"\xee" * (page_size - 8)
        self.db.path.write_bytes(bytes(data))

        with Database(self.db.path) as db:
            reads: dict[str, Callable[[], object]] = {
                "ResponseCache.get": lambda: ResponseCache(db).get("k1"),
                "ResponseCache.stats": ResponseCache(db).stats,
                "RunLedger.start": lambda: RunLedger(db).start("cfg"),
                "DailyUsage.requests": lambda: DailyUsage(db).requests("m"),
            }
            for name, read in reads.items():
                with self.subTest(name), self.assertRaises(StorageError) as caught:
                    read()
                self.assertIn("rebuild the index", caught.exception.hint or "")


class TestInMemoryUnderAThreadPool(unittest.TestCase):
    def test_every_request_is_paid_once_and_counted(self) -> None:
        with Database(":memory:") as db:
            model = Counting()
            client = client_for(db, model)
            prompts = [f"file {index}" for index in range(48)]
            with ThreadPoolExecutor(8) as pool:
                first = list(pool.map(
                    lambda prompt: client.complete(Tier.FILE, "SYS", prompt), prompts
                ))
            again = [client.complete(Tier.FILE, "SYS", prompt) for prompt in prompts]

            self.assertEqual(model.total, len(prompts))
            self.assertTrue(all(completion.cached for completion in again))
            self.assertEqual(ResponseCache(db).stats()[0], len(prompts))
            served = first[0].model
            self.assertEqual(DailyUsage(db).requests(served), len(prompts))


class TestChunkVectors(unittest.TestCase):
    def test_vectors_are_kept_per_model(self) -> None:
        with Database(":memory:") as db:
            store = ChunkStore(db)
            chunk = Chunk("c1", "a.py", "f", 1, 3, "def f(): pass")
            store.replace("a.py", [chunk])
            self.assertEqual(store.missing([chunk], "embed-1", 3), [chunk])

            store.store([("c1", [0.5, 0.25, 0.125])], "embed-1")
            self.assertEqual(store.missing([chunk], "embed-1", 3), [])
            ids, vectors = store.vectors("embed-1", 3, {"c1"})
            self.assertEqual((ids, list(vectors[0])), (["c1"], [0.5, 0.25, 0.125]))
            # another model's vector is not this one's
            self.assertEqual(store.missing([chunk], "embed-2", 3), [chunk])
            # nor is one of a chunk not asked about
            self.assertEqual(store.vectors("embed-1", 3, {"other"}), ([], []))

    def test_only_a_file_that_is_gone_is_pruned(self) -> None:
        with Database(":memory:") as db:
            store = ChunkStore(db)
            for source in ("kept.py", "gone.py"):
                store.replace(source, [Chunk(source, source, "f", 1, 3, "pass")])
                store.store([(source, [0.5])], "embed-1")
            self.assertEqual(store.prune(lambda source: source == "kept.py"), 1)
            self.assertEqual(store.vectors("embed-1", 1, {"kept.py", "gone.py"})[0],
                             ["kept.py"])


class TestCliOnADamagedIndex(unittest.TestCase):
    def test_cache_stats_exits_with_the_storage_code(self) -> None:
        from devdocs.cli import main

        project = file_database(self).parent
        good = project / "good.db"
        Database(good).close()
        (project / ".devdocs").mkdir()
        (project / ".devdocs" / "devdocs.db").write_bytes(good.read_bytes()[:200])

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                contextlib.redirect_stdout(io.StringIO()):
            code = main(["cache", "stats", str(project)])
        self.assertEqual(code, StorageError.exit_code)
        self.assertIn("rebuild the index", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
