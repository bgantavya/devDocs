"""The whole pipeline over a small real project, with a scripted model.

No network: `TaggedModel` answers each prompt in the reply format the style
guard parses, so these tests exercise scan, parse, graph, plan, prompts, the
client, composition, rendering and writing end to end.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from devdocs.config import Config
from devdocs.errors import (
    AuthError,
    ConfigError,
    DevDocsError,
    ModelNotFound,
    ProviderError,
    QuotaExhausted,
    RateLimited,
)
from devdocs.generate import Estimate, Pipeline, Prepared, _retry
from devdocs.index.cache import ResponseCache
from devdocs.index.db import Database
from devdocs.index.embed import Embeddings
from devdocs.index.ledger import DailyUsage, quota_day
from devdocs.llm import Completion, Finish, Request, Usage
from devdocs.llm.budget import Budget, Router, token_ceiling
from devdocs.llm.catalog import find
from devdocs.llm.client import CallEvent, LLMClient
from devdocs.llm.http import HttpResponse, RetryPolicy
from devdocs.llm.providers import GeminiProvider
from devdocs.render import MANIFEST, Renderer, read_manifest
from devdocs.render.markdown import MarkdownRenderer
from devdocs.render.model import DocSet
from devdocs.render.pdf import PdfRenderer, external
from devdocs.render.pdf.check import PdfFile, check
from tests.test_render_writer import unnameable

PROJECT = {
    "pyproject.toml": '[project]\nname = "shop"\n',
    "shop/__init__.py": '"""The shop package."""\n',
    "shop/cart.py": (
        "from shop.money import Money\n\n\n"
        "class Cart:\n"
        "    def add(self, sku: str, price: Money) -> None:\n"
        "        self.items.append((sku, price))\n\n"
        "    def total(self) -> Money:\n"
        "        return sum(p for _, p in self.items)\n"
    ),
    "shop/money.py": (
        '"""Money as integer cents, so totals never drift."""\n\n'
        "class Money(int):\n"
        '    """An amount in cents."""\n'
    ),
    "shop/cli.py": (
        "from shop.cart import Cart\n\n\n"
        "def main() -> int:\n"
        "    # TODO: read the basket from argv\n"
        "    Cart()\n"
        "    return 0\n\n\n"
        'if __name__ == "__main__":\n'
        "    raise SystemExit(main())\n"
    ),
    "tests/test_cart.py": "from shop.cart import Cart\n\ndef test_add():\n    Cart()\n",
}


class TaggedModel:
    """Answers file, module and architecture prompts in the tagged format."""

    provider = "gemini"

    def __init__(self) -> None:
        self.calls: list[Request] = []
        self.interrupt_after: int | None = None

    def list_models(self) -> list[str]:
        return ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]

    def generate(self, request: Request) -> Completion:
        if self.interrupt_after is not None and len(self.calls) >= self.interrupt_after:
            raise KeyboardInterrupt
        self.calls.append(request)
        if "OVERVIEW" in request.system:
            text = ("OVERVIEW: A small shop with a cart and money types.\n"
                    "FLOW: The command line builds a cart.\n"
                    "FLOW: The cart totals its items in cents.\n")
        elif "RELATION" in request.system:
            text = ("PURPOSE: Holds the cart, money and the command line.\n"
                    "RELATION: The command line drives the cart.\n")
        else:
            match = re.search(r"^Describe: (.*)$", request.prompt, re.M)
            names = [n.strip() for n in match.group(1).split(",")] if match else []
            symbols = "".join(
                f"SYMBOL {n}: Does the {n.rsplit('.', 1)[-1]} step.\n"
                for n in names if n and not n.startswith("(")
            )
            text = f"PURPOSE: Implements part of the shop.\n{symbols}"
        return Completion(text, request.model, "gemini", Finish.STOP,
                          Usage(input_tokens=500, output_tokens=60))


PROSE = "Implements part of the shop."      # TaggedModel's answer for a file
NO_CALL_FITS = 0.0000001                    # a budget, in USD, that buys nothing


class PipelineCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        for relpath, text in PROJECT.items():
            path = self.tmp / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.db = Database(self.tmp / ".devdocs" / "devdocs.db")
        self.addCleanup(self.db.close)
        self.model: TaggedModel = TaggedModel()

    def pipeline(self, *, log: Any = None, warn: Any = None,
                 renderers: Sequence[Renderer] | None = None,
                 **overrides: object) -> Pipeline:
        config = Config(path=self.tmp, **overrides)  # type: ignore[arg-type]
        client = LLMClient(
            self.model,
            router=Router("gemini"),
            budget=Budget(config.max_cost, config.max_requests),
            cache=ResponseCache(self.db),
            usage=DailyUsage(self.db),
            retry=RetryPolicy(base_delay=0),
            free_tier=False,
        )
        self.client = client
        return Pipeline(config, client=client, db=self.db, log=log, warn=warn,
                        renderers=renderers)

    def docs(self) -> dict[str, str]:
        out = self.tmp / "docs"
        return {
            p.relative_to(out).as_posix(): p.read_text("utf-8")
            for p in sorted(out.rglob("*")) if p.is_file()
        }

    def last_run(self) -> str:
        """How the last run ended, as the ledger has it."""
        run = self.db.query_one(
            "SELECT status FROM run ORDER BY started_at DESC LIMIT 1")
        assert run is not None
        return str(run["status"])


class TestRun(PipelineCase):
    def test_full_run_writes_every_document(self) -> None:
        pipeline = self.pipeline()
        summary = pipeline.run(pipeline.prepare())
        docs = self.docs()
        for page in ("README.md", "architecture.md", "index.json",
                     "shop/cart.py.md", "shop/money.py.md", "shop/cli.py.md",
                     "shop/index.md"):
            self.assertIn(page, docs)
        self.assertNotIn("tests/test_cart.py.md", docs)   # tests are not documented
        self.assertGreater(summary.prose, 0)
        self.assertEqual(summary.failed, [])
        self.assertEqual(summary.requests, len(self.model.calls))

        cart = docs["shop/cart.py.md"]
        self.assertIn("Implements part of the shop.", cart)
        self.assertIn("def add(self, sku: str, price: Money) -> None", cart)
        cli = docs["shop/cli.py.md"]
        self.assertIn("read the basket from argv", cli)        # known issue
        manifest = json.loads(docs["index.json"])
        self.assertIn("shop/cart.py.md", manifest["files"])
        self.assertEqual(manifest["run"]["requests"], summary.requests)

    def test_second_run_is_free_and_identical(self) -> None:
        first = self.pipeline()
        first.run(first.prepare())
        before = self.docs()
        calls = len(self.model.calls)

        second = self.pipeline()
        summary = second.run(second.prepare())
        self.assertEqual(len(self.model.calls), calls)
        self.assertEqual(summary.cost, 0.0)
        self.assertGreater(summary.cache_hits, 0)
        after = self.docs()
        changed = [p for p in before if before[p] != after[p]]
        self.assertEqual(changed, ["index.json"])            # only the run record

    def test_request_ceiling_leaves_the_rest_as_stubs(self) -> None:
        pipeline = self.pipeline(max_requests=1)
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(summary.prose, 1)
        # Too small for an overview, so the module is the most valuable call.
        self.assertIn("RELATION", self.model.calls[0].system)
        self.assertIn("shop/money.py.md", self.docs())

    def test_interrupt_keeps_what_was_paid_for(self) -> None:
        self.model.interrupt_after = 1
        pipeline = self.pipeline(concurrency=1)
        with self.assertRaises(KeyboardInterrupt):
            pipeline.run(pipeline.prepare())
        self.assertIn("README.md", self.docs())
        self.model.interrupt_after = None
        again = self.pipeline(concurrency=1)
        summary = again.run(again.prepare())
        self.assertTrue(summary.resumed)
        self.assertGreater(summary.cache_hits, 0)


class OnTheWire(TaggedModel):
    """Answers `tags` only once all are out at once; the first sets `stop`,
    as a Ctrl-C would, and the others answer after it."""

    def __init__(self, *tags: str) -> None:
        super().__init__()
        self.tags = tags
        self.out = threading.Barrier(len(tags), timeout=5)
        self.stop = threading.Event()   # the run's, once its client exists

    def generate(self, request: Request) -> Completion:
        if request.tag in self.tags:
            self.out.wait()
            if request.tag == self.tags[0]:
                self.stop.set()
            else:
                self.stop.wait(5)
        return super().generate(request)


class DownAtTheStop(TaggedModel):
    """The stop comes while the module prompt's call, this project's last,
    is out, and that call fails with a 503 a retry might cure."""

    def __init__(self) -> None:
        super().__init__()
        self.stop = threading.Event()   # the run's, once its client exists

    def generate(self, request: Request) -> Completion:
        if "RELATION" in request.system:
            self.stop.set()
            raise ProviderError("down", status=503)
        return super().generate(request)


class Limited(TaggedModel):
    """shop/cart.py's first call is told to come back in 30 s; shop/cli.py's
    raises KeyboardInterrupt once it is, as Python's own Ctrl-C would."""

    def __init__(self) -> None:
        super().__init__()
        self.limited = threading.Event()

    def generate(self, request: Request) -> Completion:
        if request.tag == "shop/cart.py" and not self.limited.is_set():
            self.limited.set()
            raise RateLimited("slow down", retry_after=30, status=429)
        if request.tag == "shop/cli.py":
            self.limited.wait(5)
            raise KeyboardInterrupt
        return super().generate(request)


class TestStop(PipelineCase):
    """A stop request, set here as the CLI's signal handler sets it."""

    def test_calls_on_the_wire_finish_and_are_kept(self) -> None:
        model = OnTheWire("shop/cart.py", "shop/cli.py")
        self.model = model
        pipeline = self.pipeline(concurrency=2)
        model.stop = self.client.stop
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual(summary.stopped, "interrupted")
        self.assertEqual(sorted(r.tag for r in model.calls),
                         ["shop/cart.py", "shop/cli.py"])
        docs = self.docs()
        for page in ("shop/cart.py.md", "shop/cli.py.md"):
            self.assertIn(PROSE, docs[page])
        for page in ("shop/money.py.md", "shop/__init__.py.md"):
            self.assertNotIn(PROSE, docs[page])
        self.assertEqual(self.last_run(), "interrupted")

        again = self.pipeline(concurrency=2)
        summary = again.run(again.prepare())
        self.assertTrue(summary.resumed)
        self.assertEqual(summary.cache_hits, 2)

    def test_a_stop_still_takes_what_the_cache_holds(self) -> None:
        first = self.pipeline()
        first.run(first.prepare())
        cli = self.tmp / "shop" / "cli.py"
        cli.write_text(cli.read_text("utf-8").replace(
            "    Cart()\n", "    # build the basket\n    Cart().total()\n"
        ), encoding="utf-8")
        model = OnTheWire("shop/cli.py")
        self.model = model
        second = self.pipeline()
        model.stop = self.client.stop
        summary = second.run(second.prepare())
        self.assertEqual(summary.stopped, "interrupted")
        self.assertEqual([r.tag for r in model.calls], ["shop/cli.py"])
        docs = self.docs()
        for page in ("shop/cart.py.md", "shop/cli.py.md", "shop/money.py.md",
                     "shop/__init__.py.md"):
            self.assertIn(PROSE, docs[page])
        self.assertIn("Holds the cart, money and the command line.",
                      docs["shop/index.md"])
        self.assertEqual(self.last_run(), "interrupted")

    def test_a_stop_that_fails_the_last_call_leaves_the_run_to_resume(self) -> None:
        # The failed call is not retried once stopped, so its answer is still
        # to buy: the run must not be recorded as finished.
        model = DownAtTheStop()
        self.model = model
        pipeline = self.pipeline()
        model.stop = self.client.stop
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual(summary.stopped, "interrupted")
        self.assertEqual(self.last_run(), "interrupted")
        docs = self.docs()
        for page in ("shop/cart.py.md", "shop/cli.py.md", "shop/money.py.md",
                     "shop/__init__.py.md"):
            self.assertIn(PROSE, docs[page])
        self.assertNotIn("Holds the cart", docs["shop/index.md"])

    @unittest.skipIf(sys.platform == "win32", "POSIX signal masks")
    def test_workers_leave_stop_signals_to_the_main_thread(self) -> None:
        # Python runs a handler only on the main thread, and a signal the
        # kernel gave a worker instead would leave that thread's wait asleep.
        masks: list[set[signal.Signals]] = []

        class Masked(TaggedModel):
            def generate(self, request: Request) -> Completion:
                masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, []))
                return super().generate(request)

        self.model = Masked()
        pipeline = self.pipeline(concurrency=2)
        pipeline.run(pipeline.prepare())
        self.assertTrue(masks)
        for mask in masks:
            self.assertLessEqual({signal.SIGINT, signal.SIGTERM}, mask)

    def test_pythons_own_ctrl_c_stops_the_workers_too(self) -> None:
        # Regression: with no handler to set the stop, a Ctrl-C waited out
        # every retry the workers had begun, here a 30 s Retry-After.
        self.model = Limited()
        pipeline = self.pipeline(concurrency=2)
        prepared = pipeline.prepare()
        started = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            pipeline.run(prepared)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(self.client.stop.is_set())
        self.assertIn("README.md", self.docs())
        self.assertEqual(self.last_run(), "interrupted")


class FirstRender(MarkdownRenderer):
    """Markdown whose first render, the run's check of its output, comes
    with `act`: a stop request, say, or Python's own Ctrl-C."""

    def __init__(self, act: Callable[[], None]) -> None:
        super().__init__()
        self.act: Callable[[], None] | None = act

    def render(self, docset: DocSet) -> dict[str, bytes]:
        act, self.act = self.act, None
        if act is not None:
            act()
        return super().render(docset)


class NoFiles(TaggedModel):
    """Refuses every file prompt, so a run buys only the summing up."""

    def generate(self, request: Request) -> Completion:
        if "RELATION" not in request.system and "OVERVIEW" not in request.system:
            raise ProviderError("refused", status=400)
        return super().generate(request)


class Quota(TaggedModel):
    """Answers `answers` calls, then finds every model's daily quota spent."""

    def __init__(self, answers: int) -> None:
        super().__init__()
        self.answers = answers

    def generate(self, request: Request) -> Completion:
        if len(self.calls) >= self.answers:
            raise QuotaExhausted("daily quota spent", status=429, provider="gemini")
        return super().generate(request)


class Attentive(TaggedModel):
    """Sums a module up by what its prompt says of the files, as a model
    does: their prose, once bought, changes its answer."""

    def generate(self, request: Request) -> Completion:
        if "RELATION" in request.system and PROSE.rstrip(".") in request.prompt:
            self.calls.append(request)
            return Completion("PURPOSE: Holds the parts of a shop.\n", request.model,
                              "gemini", Finish.STOP,
                              Usage(input_tokens=500, output_tokens=60))
        return super().generate(request)


class TestCheckedFirst(PipelineCase):
    """What the write would refuse is refused before anything is bought."""

    def plant(self, relpath: str, text: str = "# Ours\n") -> None:
        page = self.tmp / "docs" / relpath
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(text, encoding="utf-8")

    def refused(self, pipeline: Pipeline) -> ConfigError:
        """The run's refusal, once it is checked that nothing was bought and
        no run recorded."""
        def runs() -> list[tuple[Any, ...]]:
            return [tuple(row) for row in self.db.query("SELECT * FROM run")]

        before, calls = runs(), len(self.model.calls)
        with self.assertRaises(ConfigError) as caught:
            pipeline.run(pipeline.prepare())
        self.assertEqual(self.model.calls[calls:], [])
        self.assertEqual(runs(), before)
        return caught.exception

    def stubbed(self) -> None:
        """A first run whose budget buys nothing, so every page is a stub."""
        first = self.pipeline(max_cost=NO_CALL_FITS)
        self.assertEqual(first.run(first.prepare()).prose, 0)
        self.assertEqual(self.model.calls, [])

    def test_a_file_devdocs_did_not_write_costs_nothing(self) -> None:
        # Regression: every call was paid for, then the write refused.
        self.plant("README.md")
        pipeline = self.pipeline()
        error = self.refused(pipeline)
        self.assertEqual(error.message, (
            f"refusing to replace 1 file in {pipeline.out} that devdocs has no "
            "record of writing: README.md"))
        self.assertEqual(error.hint,
                         "move it away or choose another --out; --force replaces it")
        self.assertEqual(self.docs(), {"README.md": "# Ours\n"})

        forced = self.pipeline()
        self.assertGreater(forced.run(forced.prepare(), force=True).prose, 0)
        self.assertIn("Generated by devdocs", self.docs()["README.md"])

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0,
                     "POSIX permission bits, which root ignores")
    def test_an_out_dir_devdocs_cannot_write_costs_nothing(self) -> None:
        docs = self.tmp / "docs"
        docs.mkdir()
        docs.chmod(0o555)
        self.addCleanup(docs.chmod, 0o755)
        pipeline = self.pipeline()
        error = self.refused(pipeline)
        self.assertEqual(error.message, f"cannot write index.json in {pipeline.out}: "
                                        f"{os.strerror(errno.EACCES)}")
        self.assertEqual(error.hint, f"devdocs needs write access to {pipeline.out}")

    def test_a_page_the_file_system_cannot_name_costs_nothing(self) -> None:
        # As macOS cannot name a Linux file's bytes that are not UTF-8.
        (self.tmp / "shop" / "odd.py").write_text('"""Odd."""\n', encoding="utf-8")
        pipeline = self.pipeline()
        with unnameable("odd.py"):
            error = self.refused(pipeline)
        self.assertEqual(error.message, (
            f"cannot write shop/odd.py.md in {pipeline.out}: the file system "
            f"cannot name it ({os.strerror(errno.EILSEQ)})"))
        self.assertFalse((self.tmp / "docs").exists())

    def test_pages_already_right_are_no_conflict_without_a_manifest(self) -> None:
        # The check composes with the prose the cache holds: stubs would make
        # devdocs's own pages, the manifest lost, look like another's. (Not
        # HTML: without its manifest, the site's scripts are scanned as source.)
        formats = ("markdown", "pdf")
        first = self.pipeline(formats=formats)
        first.run(first.prepare())
        (self.tmp / "docs" / "index.json").unlink()
        calls = len(self.model.calls)
        again = self.pipeline(formats=formats)
        summary = again.run(again.prepare())
        self.assertEqual(len(self.model.calls), calls)
        self.assertEqual(summary.written.written, ["index.json"])

    def test_stubs_whose_prose_is_to_buy_are_replaced_pages(self) -> None:
        # Regression: the check made these pages as the stubs they were, so
        # passed; the run bought their prose, then refused to write it.
        self.stubbed()
        (self.tmp / "docs" / "index.json").unlink()
        pipeline = self.pipeline()
        error = self.refused(pipeline)
        self.assertEqual(error.message, (
            f"refusing to replace 7 files in {pipeline.out} that devdocs has no "
            "record of writing: README.md, architecture.md, shop/__init__.py.md, "
            "shop/cart.py.md, shop/cli.py.md and 2 more"))

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0,
                     "POSIX permission bits, which root ignores")
    def test_stubs_whose_prose_is_to_buy_are_probed(self) -> None:
        # Regression: taken for unchanged, they went unprobed until paid for.
        self.stubbed()
        shop = self.tmp / "docs" / "shop"
        shop.chmod(0o555)
        self.addCleanup(shop.chmod, 0o755)
        pipeline = self.pipeline()
        error = self.refused(pipeline)
        self.assertEqual(error.message, f"cannot write shop/__init__.py.md in "
                                        f"{pipeline.out}: {os.strerror(errno.EACCES)}")

    def test_stubs_the_budget_keeps_stubs_are_no_conflict_without_a_manifest(
        self,
    ) -> None:
        # Regression: the check took their prose for bought; the run, which
        # buys none, would write them again as they are.
        self.stubbed()
        (self.tmp / "docs" / "index.json").unlink()
        again = self.pipeline(max_cost=NO_CALL_FITS)
        summary = again.run(again.prepare())
        self.assertEqual((summary.prose, self.model.calls), (0, []))
        self.assertEqual(summary.written.written, ["index.json"])

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0,
                     "POSIX permission bits, which root ignores")
    def test_stubs_the_budget_keeps_stubs_need_no_writing(self) -> None:
        # Regression: the check probed them, in a directory no write would touch.
        self.stubbed()
        shop = self.tmp / "docs" / "shop"
        shop.chmod(0o555)
        self.addCleanup(shop.chmod, 0o755)
        again = self.pipeline(max_cost=NO_CALL_FITS)
        summary = again.run(again.prepare())
        self.assertEqual((summary.prose, self.model.calls), (0, []))
        self.assertEqual(summary.written.unchanged[:1], ["README.md"])

    def test_only_what_the_budget_lets_in_is_a_replaced_page(self) -> None:
        # The module's answer cached, the limit lets in one file and the module
        # it rebuilds: these four pages change, as the write would say, and
        # the other files stay stubs.
        first = self.pipeline(max_requests=1)
        first.run(first.prepare())
        self.assertEqual([request.tag for request in self.model.calls], ["shop"])
        (self.tmp / "docs" / "index.json").unlink()
        pipeline = self.pipeline(max_requests=2)
        error = self.refused(pipeline)
        self.assertEqual(error.message, (
            f"refusing to replace 4 files in {pipeline.out} that devdocs has no "
            "record of writing: README.md, architecture.md, shop/cart.py.md, "
            "shop/index.md"))

    def test_a_module_the_budget_cannot_rebuild_is_no_replaced_page(self) -> None:
        # Each file would take the cached module along, past the one request:
        # none is bought, so no page changes and only the manifest is written.
        first = self.pipeline(max_requests=1)
        first.run(first.prepare())
        (self.tmp / "docs" / "index.json").unlink()
        self.model.calls.clear()
        pipeline = self.pipeline(max_requests=1)
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual((self.model.calls, summary.stopped), ([], ""))
        self.assertEqual(summary.written.written, ["index.json"])

    def at_the_edge(self, page: str) -> Pipeline:
        """A run whose budget fits shop/cart.py's prompt only without the
        context it carries: the run leaves its page a stub and buys
        shop/cli.py's with that money. `page`, as a stub, is all docs/ holds."""
        probe = self.pipeline(max_cost=0)
        prepared = probe.prepare()

        def costs(of: Prepared) -> dict[str, float]:
            return {item.key: item.cost
                    for item in probe.price(of, embedding=False).items}

        full, bare = costs(prepared), costs(replace(prepared, retriever=None))
        budget = (full["shop/cart.py"] + bare["shop/cart.py"]) / 2
        self.assertGreater(budget, full["shop/cli.py"])
        self.stubbed()
        docs = self.tmp / "docs"
        stub = (docs / page).read_text("utf-8")
        shutil.rmtree(docs)
        self.plant(page, stub)
        return self.pipeline(max_cost=budget)

    def test_a_stub_the_budget_will_replace_is_found_by_the_runs_prices(self) -> None:
        # Regression: priced without its context, shop/cart.py took the money
        # in the check, so this stub seemed to stay; the run paid, then refused.
        pipeline = self.at_the_edge("shop/cli.py.md")
        error = self.refused(pipeline)
        self.assertEqual(error.message, (
            f"refusing to replace 1 file in {pipeline.out} that devdocs has no "
            "record of writing: shop/cli.py.md"))

    def test_a_stub_the_budget_will_keep_is_found_by_the_runs_prices(self) -> None:
        pipeline = self.at_the_edge("shop/cart.py.md")
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual([request.tag for request in self.model.calls], ["shop/cli.py"])
        self.assertEqual(summary.prose, 1)

    def test_with_no_client_all_prose_is_still_to_buy(self) -> None:
        self.stubbed()
        (self.tmp / "docs" / "index.json").unlink()
        pipeline = Pipeline(Config(path=self.tmp))
        with self.assertRaises(ConfigError) as caught:
            pipeline.check(pipeline.prepare())
        self.assertIn("refusing to replace 7 files", caught.exception.message)

    def test_the_check_leaves_the_plan_to_the_run(self) -> None:
        for budget in (NO_CALL_FITS, 0.0):
            with self.subTest(budget=budget):
                pipeline = self.pipeline(max_cost=budget)
                prepared = pipeline.prepare()
                calls = prepared.plan.calls()
                self.assertGreater(calls, 0)
                pipeline.check(prepared)
                self.assertEqual(
                    (prepared.plan.calls(), prepared.prompts, prepared.cut),
                    (calls, {}, []))

    def test_a_page_summing_up_prose_to_buy_is_a_replaced_page(self) -> None:
        # Regression: the module's answer, cached from a run that had only
        # docstrings for its files, made the overview's summary of it look
        # final; the run bought the files' prose, so a new module answer.
        self.model = NoFiles()
        first = self.pipeline()
        self.assertEqual(len(first.run(first.prepare()).failed), 4)
        docs = self.tmp / "docs"
        overview = (docs / "architecture.md").read_bytes()
        shutil.rmtree(docs)
        self.plant("architecture.md", overview.decode("utf-8"))
        self.model = Attentive()
        pipeline = self.pipeline()
        error = self.refused(pipeline)
        self.assertEqual(error.message, (
            f"refusing to replace 1 file in {pipeline.out} that devdocs has no "
            "record of writing: architecture.md"))

    def refused_late(self, pipeline: Pipeline, prepared: Prepared | None = None) -> str:
        """The hint of the run's refusal of a page that appeared after the
        check, once its first call was answered, as another process might
        put it there."""
        def plant(event: CallEvent) -> None:
            if not (self.tmp / "docs" / "README.md").exists():
                self.plant("README.md")

        self.client.on_event = plant
        with self.assertRaises(ConfigError) as caught:
            pipeline.run(prepared or pipeline.prepare())
        self.assertGreater(len(self.model.calls), 0)
        return caught.exception.hint

    def test_a_write_refused_once_all_is_bought_costs_nothing_again(self) -> None:
        hint = self.refused_late(self.pipeline())
        self.assertEqual(hint, "move it away or choose another --out; --force "
                               "replaces it; the answers are cached, so running "
                               "again costs nothing")
        calls = len(self.model.calls)
        again = self.pipeline()
        self.assertEqual(again.run(again.prepare(), force=True).prose, 5)
        self.assertEqual(len(self.model.calls), calls)

    def test_a_write_refused_after_a_stop_says_the_rest_is_to_pay(self) -> None:
        # Regression: it said running again cost nothing, and not why the run
        # had stopped, its summary unshown.
        self.model = Quota(2)
        hint = self.refused_late(self.pipeline())
        self.assertEqual(hint, "move it away or choose another --out; --force "
                               "replaces it; stopped early (quota): the answers "
                               "bought are cached, so running again pays only for "
                               "the rest")
        bought = {request.tag for request in self.model.calls}
        self.model = TaggedModel()
        again = self.pipeline()
        again.run(again.prepare(), force=True)
        paid = {request.tag for request in self.model.calls}
        self.assertEqual((len(bought), len(paid), bought & paid), (2, 3, set()))

    def test_a_write_refused_after_a_budget_cut_says_the_rest_is_to_pay(self) -> None:
        # Regression: it said running again cost nothing, but the answers now
        # cached leave the limit room for what it cut.
        hint = self.refused_late(self.pipeline(max_requests=2))
        self.assertEqual(hint, "move it away or choose another --out; --force "
                               "replaces it; 3 items over budget: the answers "
                               "bought are cached, so running again pays only for "
                               "the rest")
        calls = len(self.model.calls)
        again = self.pipeline(max_requests=2)
        again.run(again.prepare(), force=True)
        self.assertGreater(len(self.model.calls), calls)

    def test_a_cut_an_estimate_made_is_told_of_too(self) -> None:
        pipeline = self.pipeline(max_requests=2)
        prepared = pipeline.prepare()
        self.assertEqual(len(pipeline.estimate(prepared).demoted), 3)
        self.assertIn("; 3 items over budget: ", self.refused_late(pipeline, prepared))

    def test_a_write_refused_after_failures_says_the_rest_is_to_pay(self) -> None:
        self.model = NoFiles()
        hint = self.refused_late(self.pipeline())
        self.assertTrue(hint.endswith(
            "; 4 items failed: the answers bought are cached, so running again "
            "pays only for the rest"), hint)

    def refused_at_the_write(self, pipeline: Pipeline) -> str:
        """The hint of the run's refusal of a page that appeared once the check
        had passed, whether or not anything was bought. Its renderer plants
        the page at its second render, the write's."""
        renders: list[int] = []
        plant = self.plant

        class Planting(MarkdownRenderer):
            def render(self, docset: DocSet) -> dict[str, bytes]:
                renders.append(1)
                if len(renders) == 2:
                    plant("README.md")
                return super().render(docset)

        pipeline.renderers = [Planting()]
        with self.assertRaises(ConfigError) as caught:
            pipeline.run(pipeline.prepare())
        return caught.exception.hint or ""

    # The writer's own hint, which every refusal below begins with.
    WRITER = "move it away or choose another --out; --force replaces it; "

    def test_a_refusal_with_all_over_budget_does_not_say_answers_are_cached(
        self,
    ) -> None:
        # Regression: it told of answers bought, and none had been.
        hint = self.refused_at_the_write(self.pipeline(max_cost=NO_CALL_FITS))
        self.assertEqual(self.model.calls, [])
        self.assertEqual(hint, f"{self.WRITER}5 items over budget and no answer was "
                               "bought, so running again has them all to buy")

    def test_a_refusal_after_a_stop_before_any_answer_says_so(self) -> None:
        self.model = Quota(0)
        hint = self.refused_at_the_write(self.pipeline())
        self.assertEqual(hint, f"{self.WRITER}stopped early (quota) and no answer was "
                               "bought, so running again has them all to buy")

    def test_a_refusal_with_every_item_failed_says_so(self) -> None:
        class Refusing(TaggedModel):
            def generate(self, request: Request) -> Completion:
                raise ProviderError("refused", status=400)

        self.model = Refusing()
        hint = self.refused_at_the_write(self.pipeline())
        self.assertEqual(hint, f"{self.WRITER}5 items failed and no answer was bought, "
                               "so running again has them all to buy")

    def test_a_refusal_with_nothing_to_buy_does_not_say_answers_are_cached(
        self,
    ) -> None:
        # Regression: it told of answers cached, in a run planned to need none.
        for rel in ("shop", "tests"):
            shutil.rmtree(self.tmp / rel)
        pipeline = self.pipeline()
        self.assertEqual(pipeline.prepare().plan.calls(), 0)
        hint = self.refused_at_the_write(pipeline)
        self.assertEqual(self.model.calls, [])
        self.assertEqual(hint, f"{self.WRITER}no answers were needed, so running again "
                               "costs nothing")

    def test_a_refusal_with_the_answers_from_an_earlier_run_costs_nothing_again(
        self,
    ) -> None:
        first = self.pipeline()
        first.run(first.prepare())
        calls = len(self.model.calls)
        shutil.rmtree(self.tmp / "docs")
        hint = self.refused_at_the_write(self.pipeline())
        self.assertEqual(len(self.model.calls), calls)
        self.assertEqual(hint, f"{self.WRITER}the answers are cached, so running again "
                               "costs nothing")

    def embedding(self, failure: DevDocsError) -> EmbeddingModel:
        """The model, once embedding always ends in `failure`."""
        model = EmbeddingModel()
        model.during_embed = failing(failure)
        self.model = model
        return model

    def test_a_refusal_with_vectors_to_buy_says_they_are_not_free(self) -> None:
        # Regression: it said running again cost nothing, and that run bought them.
        for failure in (ProviderError("endpoint down", status=503),
                        QuotaExhausted("embedding quota spent")):
            with self.subTest(failure.message):
                model = self.embedding(failure)
                bought = self.pipeline(context="semantic")
                bought.run(bought.prepare())
                shutil.rmtree(self.tmp / "docs")
                hint = self.refused_at_the_write(self.pipeline(context="semantic"))
                self.assertEqual(hint, f"{self.WRITER}the answers are cached, so "
                                       "running again pays only to embed 7 chunks")

                model.during_embed = None
                (self.tmp / "docs" / "README.md").unlink()
                again = self.pipeline(context="semantic")
                summary = again.run(again.prepare())
                self.assertEqual((sum(map(len, model.batches)), summary.requests),
                                 (7, 1))
                with self.db.transaction() as conn:
                    conn.execute("UPDATE chunk SET embedding = NULL")
                shutil.rmtree(self.tmp / "docs")

    def test_a_refusal_with_only_a_vector_to_buy_says_no_answers_were_needed(
        self,
    ) -> None:
        # One file its docstring documents in full: no prose to buy, but a chunk.
        for rel in ("shop/cart.py", "shop/cli.py", "shop/money.py",
                    "tests/test_cart.py"):
            (self.tmp / rel).unlink()
        (self.tmp / "shop" / "__init__.py").write_text(
            f'"""{"The shop package, which has all the parts of the shop. " * 2}"""\n',
            encoding="utf-8",
        )
        self.embedding(ProviderError("endpoint down", status=503))
        pipeline = self.pipeline(context="semantic")
        prepared = pipeline.prepare()
        self.assertEqual(prepared.plan.calls(), 0)
        embeddings = pipeline._embeddings(prepared)
        assert embeddings is not None
        self.assertEqual(embeddings.quote().chunks, 1)
        hint = self.refused_at_the_write(pipeline)
        self.assertEqual(hint, f"{self.WRITER}no answers were needed, so running again "
                               "pays only to embed 1 chunk")

    def test_a_refusal_with_vectors_the_next_run_would_skip_costs_nothing_again(
        self,
    ) -> None:
        self.embedding(ProviderError("endpoint down", status=503))
        first = self.pipeline(context="semantic")
        first.run(first.prepare())
        calls = len(self.model.calls)
        shutil.rmtree(self.tmp / "docs")
        # Its limits then take embedding off the run, so nothing is left to pay.
        over = self.pipeline(context="semantic", max_requests=1)
        hint = self.refused_at_the_write(over)
        self.assertEqual(len(self.model.calls), calls)
        self.assertTrue(hint.endswith("so running again costs nothing"), hint)

    def test_a_refusal_with_items_and_vectors_to_buy_says_the_rest(self) -> None:
        # "The rest" takes in the vectors, so there is nothing to add.
        self.embedding(ProviderError("endpoint down", status=503))
        hint = self.refused_at_the_write(self.pipeline(context="semantic",
                                                       max_requests=8))
        self.assertTrue(hint.endswith("over budget: the answers bought are cached, so "
                                      "running again pays only for the rest"), hint)

    def test_a_client_that_keeps_nothing_promises_nothing(self) -> None:
        pipeline = self.pipeline()
        self.client.cache = None
        hint = self.refused_at_the_write(pipeline)
        self.assertGreater(len(self.model.calls), 0)
        self.assertEqual(hint, self.WRITER.removesuffix("; "))

    def test_a_stop_while_the_output_is_checked_sends_nothing(self) -> None:
        # A stop request while `run` checks is one like any other: the pages
        # are made from the cache, here empty, and the run is left to resume.
        # (The CLI checks before it lets a Ctrl-C ask for one.)
        pipeline = self.pipeline()
        pipeline.renderers = [FirstRender(self.client.stop.set)]
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual((summary.stopped, self.model.calls), ("interrupted", []))
        self.assertNotIn(PROSE, self.docs()["shop/cart.py.md"])
        self.assertEqual(self.last_run(), "interrupted")

    def test_pythons_own_ctrl_c_while_the_output_is_checked_ends_the_run(self) -> None:
        def ctrl_c() -> None:
            raise KeyboardInterrupt

        pipeline = self.pipeline(renderers=[FirstRender(ctrl_c)])
        with self.assertRaises(KeyboardInterrupt):
            pipeline.run(pipeline.prepare())
        self.assertEqual(self.model.calls, [])
        self.assertFalse((self.tmp / "docs").exists())
        self.assertIsNone(self.db.query_one("SELECT id FROM run"))

    def test_an_external_pdf_engine_is_not_run_to_check(self) -> None:
        # The builtin engine names the book alike, in a fraction of the time.
        printed: list[str] = []

        def chrome(engine: str, docset: DocSet, *, page_size: str) -> bytes:
            printed.append(engine)
            return b"%PDF-1.7\n"

        self.plant("shop.pdf", "a PDF of our own\n")
        with mock.patch.object(external, "find", return_value="/bin/chrome"), \
                mock.patch.object(external, "render", side_effect=chrome):
            pipeline = self.pipeline(formats=("pdf",), pdf_engine="chrome")
            error = self.refused(pipeline)
        self.assertTrue(error.message.endswith(": shop.pdf"))
        self.assertEqual(printed, [])


class Noisy:
    """A renderer whose every render warns `count` times."""

    format = "pdf"

    def __init__(self, count: int) -> None:
        self.count = count
        self.warnings: list[str] = []

    def render(self, docset: DocSet) -> dict[str, bytes]:
        self.warnings = [f"U+{0x4E00 + n:04X} CJK in shop/cart.py"
                         for n in range(self.count)]
        return {"shop.pdf": b"%PDF-1.4\n"}


class Failing:
    """A PDF renderer whose external engine always fails."""

    format = "pdf"

    def render(self, docset: DocSet) -> dict[str, bytes]:
        raise DevDocsError("chrome could not print the book",
                           hint="use the builtin PDF engine")


class TestFormats(PipelineCase):
    def test_every_configured_format_is_written(self) -> None:
        pipeline = self.pipeline(formats=("markdown", "html", "pdf"))
        summary = pipeline.run(pipeline.prepare())
        docs = self.tmp / "docs"
        for page in ("README.md", "shop/cart.py.md", "index.html", "shop/cart.py.html",
                     "assets/style.css", "shop.pdf"):
            self.assertTrue((docs / page).is_file(), page)
        self.assertEqual(check((docs / "shop.pdf").read_bytes()), [])
        listed = json.loads((docs / "index.json").read_text("utf-8"))["files"]
        self.assertEqual(listed, sorted(
            p.relative_to(docs).as_posix() for p in docs.rglob("*")
            if p.is_file() and p.name != "index.json"
        ))
        self.assertEqual(summary.warnings, [])

    def test_markdown_alone_by_default_and_each_format_once(self) -> None:
        self.assertEqual([r.format for r in self.pipeline().renderers], ["markdown"])
        pipeline = self.pipeline(formats=("pdf", "markdown", "pdf"))
        self.assertEqual([r.format for r in pipeline.renderers], ["pdf", "markdown"])

    def test_pdf_options_come_from_the_config(self) -> None:
        pipeline = self.pipeline(formats=("pdf",), pdf_split=True, page_size="Letter")
        pipeline.run(pipeline.prepare())
        split = self.tmp / "docs" / "pdf"
        cart = (split / "shop" / "cart.py.pdf").read_bytes()
        self.assertEqual(check(cart), [])
        self.assertEqual(PdfFile(cart).page(0)["MediaBox"], [0, 0, 612, 792])
        self.assertTrue((split / "index.pdf").is_file())
        self.assertFalse((self.tmp / "docs" / "README.md").exists())

    def test_explicit_renderers_win_over_the_formats(self) -> None:
        with mock.patch.object(external, "find", return_value=None):
            pipeline = self.pipeline(formats=("pdf",), pdf_engine="chrome",
                                     renderers=[MarkdownRenderer()])
        self.assertEqual([r.format for r in pipeline.renderers], ["markdown"])

    def test_a_missing_pdf_engine_fails_before_anything_is_bought(self) -> None:
        with mock.patch.object(external, "find", return_value=None), \
                self.assertRaises(ConfigError):
            self.pipeline(formats=("markdown", "pdf"), pdf_engine="weasyprint")
        self.assertEqual(self.model.calls, [])
        self.assertFalse((self.tmp / "docs").exists())

    def test_a_site_inside_the_project_is_not_taken_for_source(self) -> None:
        first = self.pipeline(formats=("markdown", "html"))
        first.run(first.prepare())
        self.assertTrue((self.tmp / "docs" / "assets" / "filter.js").is_file())
        calls = len(self.model.calls)

        again = self.pipeline(formats=("markdown", "html"))
        prepared = again.prepare()
        self.assertEqual(sorted(prepared.files), ["shop/__init__.py", "shop/cart.py",
                                                  "shop/cli.py", "shop/money.py"])
        self.assertEqual(prepared.skipped["docs/assets/filter.js"],
                         "written by devdocs")
        again.run(prepared)
        self.assertEqual(len(self.model.calls), calls)
        self.assertFalse((self.tmp / "docs" / "docs").exists())

    def test_a_site_another_run_wrote_elsewhere_is_not_taken_for_source(self) -> None:
        # Regression: only the out dir's manifest was read, so the scripts of
        # a site written with another --out were documented, and paid for.
        first = self.pipeline(formats=("html",), out=Path("site"))
        first.run(first.prepare())
        calls = len(self.model.calls)
        again = self.pipeline(formats=("html",))
        prepared = again.prepare()
        self.assertEqual(sorted(prepared.files), ["shop/__init__.py", "shop/cart.py",
                                                  "shop/cli.py", "shop/money.py"])
        made = ("site/assets/filter.js", "site/assets/pages.js", "site/index.json")
        for path in made:
            self.assertEqual(prepared.skipped[path], "written by devdocs")
        again.run(prepared)
        self.assertEqual(len(self.model.calls), calls)
        self.assertFalse((self.tmp / "docs" / "site").exists())

    def test_a_format_that_fails_costs_only_itself(self) -> None:
        first = self.pipeline(formats=("markdown", "pdf"))
        first.run(first.prepare())
        docs = self.tmp / "docs"
        book = (docs / "shop.pdf").read_bytes()
        (self.tmp / "shop" / "cli.py").unlink()

        failing = self.pipeline(renderers=[MarkdownRenderer(), Failing()])
        summary = failing.run(failing.prepare())
        ((kind, key, error),) = summary.failed
        self.assertEqual((kind, key), ("format", "pdf"))
        self.assertEqual(error, "chrome could not print the book\n  hint: use the "
                                "builtin PDF engine; `devdocs export --format pdf` "
                                "makes it from the manifest, with no key")
        self.assertEqual([fmt for fmt, _ in failing.failures], ["pdf"])
        self.assertNotIn("cli.py", (docs / "README.md").read_text("utf-8"))
        # Nothing is pruned meanwhile: the old book stays, and stays listed.
        self.assertEqual((docs / "shop.pdf").read_bytes(), book)
        listed = json.loads((docs / "index.json").read_bytes())["files"]
        self.assertIn("shop.pdf", listed)
        self.assertIn("shop/cli.py.md", listed)

        again = self.pipeline(formats=("markdown", "pdf"))
        self.assertEqual(again.run(again.prepare()).failed, [])
        self.assertFalse((docs / "shop" / "cli.py.md").exists())

    def test_the_way_to_make_a_failed_format_names_an_out_given_as_a_flag(self) -> None:
        # Regression: without it, the command looked in docs/, and found nothing.
        failing = self.pipeline(renderers=[MarkdownRenderer(), Failing()],
                                out=Path("my site"), sources={"out": "flag"})
        summary = failing.run(failing.prepare())
        self.assertIn("`devdocs export --format pdf --out 'my site'` makes it",
                      summary.failed[0][2])

    def test_the_way_to_make_a_failed_format_names_a_project_given_on_the_line(
        self,
    ) -> None:
        # Regression: from elsewhere, the command looked for the project here.
        failing = self.pipeline(renderers=[MarkdownRenderer(), Failing()],
                                sources={"path": "flag"})
        summary = failing.run(failing.prepare())
        self.assertIn(f"`devdocs export {shlex.quote(str(self.tmp))} --format pdf` "
                      "makes it", summary.failed[0][2])

    def test_a_project_and_an_out_dir_with_spaces_are_named_so_a_shell_reads_them(
        self,
    ) -> None:
        config = Config(path=Path("my shop"), out=Path("my site"),
                        sources={"path": "flag", "out": "flag"})
        self.assertIn("`devdocs export 'my shop' --format pdf --out 'my site'` makes",
                      _retry("pdf", DevDocsError("failed"), config))

    def test_the_way_to_make_a_failed_format_leaves_out_what_the_config_says(
        self,
    ) -> None:
        for origin in ("file", "env"):
            with self.subTest(origin):
                failing = self.pipeline(renderers=[MarkdownRenderer(), Failing()],
                                        out=Path("site"),
                                        sources={"path": origin, "out": origin})
                summary = failing.run(failing.prepare())
                self.assertIn("`devdocs export --format pdf` makes it",
                              summary.failed[0][2])

    def test_the_way_to_make_a_failed_pdf_names_the_pdf_options_given_as_flags(
        self,
    ) -> None:
        # Regression: it named none, so export printed with the configured engine.
        typed = {"pdf_engine": "flag", "page_size": "flag"}
        config = Config(pdf_engine="weasyprint", page_size="Letter", sources=typed)
        failed = DevDocsError("failed")
        self.assertIn("`devdocs export --format pdf --pdf-engine weasyprint "
                      "--page-size Letter` makes", _retry("pdf", failed, config))
        self.assertIn("`devdocs export --format html` makes",
                      _retry("html", failed, config))
        split = Config(pdf_split=True, sources={"pdf_split": "flag"})
        self.assertIn("`devdocs export --format pdf --split` makes",
                      _retry("pdf", failed, split))

    def test_hand_written_code_beside_the_pages_is_still_documented(self) -> None:
        first = self.pipeline()
        first.run(first.prepare())
        example = self.tmp / "docs" / "examples" / "demo.py"
        example.parent.mkdir()
        example.write_text('"""A worked example."""\n\n\ndef demo() -> None:\n'
                           "    pass\n", encoding="utf-8")
        self.assertIn("docs/examples/demo.py", self.pipeline().prepare().files)

    def test_writing_needs_no_model(self) -> None:
        pipeline = Pipeline(Config(path=self.tmp, formats=("markdown", "pdf")))
        docset = pipeline._compose(pipeline.prepare(), {})
        report = pipeline.write(docset)
        self.assertIn("shop.pdf", report.written)
        self.assertIn("shop/cart.py.md", report.written)

    def test_render_warnings_are_logged_with_a_remedy_and_kept(self) -> None:
        messages: list[str] = []
        pipeline = self.pipeline(log=messages.append,
                                 renderers=[MarkdownRenderer(), Noisy(12)])
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual(len(summary.warnings), 12)
        (message,) = [m for m in messages if m.startswith("pdf:")]
        lines = message.splitlines()
        self.assertEqual(lines[0], "pdf: characters outside the built-in fonts "
                                   "were printed as '?' (12):")
        self.assertEqual(lines[1:11], [f"  {w}" for w in summary.warnings[:10]])
        self.assertEqual(lines[11:], [
            "  and 2 more",
            "  for all of Unicode, use --pdf-engine chrome or --pdf-engine weasyprint",
        ])

    def test_warnings_describe_the_last_write_only(self) -> None:
        noisy = Noisy(1)
        pipeline = Pipeline(Config(path=self.tmp), renderers=[noisy])
        docset = pipeline._compose(pipeline.prepare(), {})
        pipeline.write(docset)
        noisy.count = 0
        pipeline.write(docset)
        self.assertEqual(pipeline.warnings, [])

    def test_characters_the_pdf_fonts_lack_are_reported_by_document(self) -> None:
        (self.tmp / "shop" / "greet.py").write_text(
            '"""Greetings."""\n\n\ndef hello() -> str:\n'
            '    """Say 你好."""\n    return "hi"\n',
            encoding="utf-8",
        )
        pipeline = self.pipeline(formats=("pdf",))
        summary = pipeline.run(pipeline.prepare())
        self.assertIn("U+4F60 CJK UNIFIED IDEOGRAPH-4F60 in shop/greet.py",
                      summary.warnings)
        self.assertIsInstance(pipeline.renderers[0], PdfRenderer)


class TestEstimate(PipelineCase):
    def test_estimate_prices_without_calling(self) -> None:
        pipeline = self.pipeline()
        estimate = pipeline.estimate(pipeline.prepare())
        self.assertEqual(self.model.calls, [])
        self.assertGreater(estimate.requests, 0)
        self.assertGreater(estimate.cost, 0.0)

    def test_estimate_after_a_run_is_free(self) -> None:
        pipeline = self.pipeline()
        pipeline.run(pipeline.prepare())
        again = self.pipeline()
        estimate = again.estimate(again.prepare())
        self.assertEqual(estimate.requests, 0)
        self.assertEqual(estimate.cost, 0.0)


# Two more files: six are enough for an overview.
MORE = {
    "shop/orders.py": (
        "from shop.cart import Cart\nfrom shop.money import Money\n\n\n"
        "class Order:\n"
        "    def __init__(self, cart: Cart) -> None:\n"
        "        self.cart = cart\n\n"
        "    def due(self) -> Money:\n"
        "        return self.cart.total()\n"
    ),
    "shop/tax.py": (
        "from shop.money import Money\n\n\n"
        "def vat(amount: Money, rate: float = 0.2) -> Money:\n"
        "    return Money(round(amount * rate))\n"
    ),
}
CACHED = {("file", f"shop/{name}.py")
          for name in ("money", "orders", "tax", "__init__")}


class TestCachedAnswersAreFree(PipelineCase):
    """A limit counts only the calls a run makes, never an answer the cache
    holds. Regression: each such answer took a request, so the calls walked
    before it spent its share, its page was cut to a stub, and the run,
    planned more calls than its limit allows, stopped on the budget after the
    first."""

    def setUp(self) -> None:
        super().setUp()
        self.model = EmbeddingModel()
        for relpath, text in MORE.items():
            (self.tmp / relpath).write_text(text, encoding="utf-8")
        first = self.pipeline()
        first.run(first.prepare())
        # The first two files the walk reaches change; the other four stay cached.
        for relpath in ("shop/cart.py", "shop/cli.py"):
            path = self.tmp / relpath
            path.write_text(path.read_text("utf-8") + "\n# changed\n", "utf-8")
        self.model.calls.clear()

    def priced(self, **overrides: object) -> Estimate:
        pipeline = self.pipeline(**overrides)
        return pipeline.estimate(pipeline.prepare())

    def test_a_request_limit_cuts_calls_alone(self) -> None:
        calls = [("module", "shop"), ("file", "shop/cart.py"), ("file", "shop/cli.py")]
        for requests, cut in ((1, calls), (2, calls[1:]), (4, [])):
            with self.subTest(max_requests=requests):
                estimate = self.priced(max_requests=requests)
                self.assertEqual(estimate.demoted, dict.fromkeys(cut, "max_requests"))
                self.assertEqual(estimate.requests, requests)
                self.assertEqual({(i.kind, i.key) for i in estimate.items if i.cached},
                                 CACHED)

    def test_a_run_on_the_limit_makes_its_call_and_writes_every_cached_page(
        self,
    ) -> None:
        pipeline = self.pipeline(max_requests=1)
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual([request.tag for request in self.model.calls],
                         ["architecture"])
        self.assertEqual((summary.stopped, summary.failed), ("", []))
        self.assertEqual((summary.requests, summary.cache_hits), (1, 4))
        docs = self.docs()
        for _, relpath in sorted(CACHED):
            self.assertIn(PROSE, docs[f"{relpath}.md"])
        for relpath in ("shop/cart.py", "shop/cli.py"):
            self.assertNotIn(PROSE, docs[f"{relpath}.md"])
        self.assertEqual(self.last_run(), "finished")

    def test_a_cost_limit_never_cuts_a_cached_page(self) -> None:
        estimate = self.priced(max_cost=NO_CALL_FITS)
        self.assertEqual(estimate.demoted, dict.fromkeys(
            [("architecture", ""), ("module", "shop"), ("file", "shop/cart.py"),
             ("file", "shop/cli.py")], "max_cost"))
        self.assertEqual((estimate.requests, estimate.cached), (0, 4))

    def test_embeddings_share_is_held_back_as_before(self) -> None:
        # Of 3 requests the batch takes 1; the overview and the module the rest.
        estimate = self.priced(context="semantic", max_requests=3)
        self.assertEqual((estimate.items[0].kind, estimate.items[0].requests),
                         ("embed", 1))
        self.assertEqual(list(estimate.demoted),
                         [("file", "shop/cart.py"), ("file", "shop/cli.py")])
        self.assertEqual((estimate.requests, estimate.cached), (3, 4))

        pipeline = self.pipeline(context="semantic", max_requests=3)
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual(len(self.model.batches), 1)
        # The module's new answer is the one the cached overview read, so the
        # overview is a cache hit again: the run spends less than its quote.
        self.assertEqual([request.tag for request in self.model.calls], ["shop"])
        self.assertEqual((summary.stopped, summary.requests), ("", 2))
        self.assertIn("A small shop", self.docs()["architecture.md"])

    def test_embeddings_cost_is_held_back_as_before(self) -> None:
        # Room for the overview, the module and cart.py, less half the vectors'
        # quote: the run, pricing again once they are paid for, holds back
        # what they cost, as the estimate held their quote, and cuts cart.py.
        quotes = {(i.kind, i.key): i.cost
                  for i in self.priced(context="semantic", max_cost=0).items}
        limit = (quotes[("architecture", "")] + quotes[("module", "shop")]
                 + quotes[("file", "shop/cart.py")] + quotes[("embed", "")] / 2)
        estimate = self.priced(context="semantic", max_cost=limit)
        self.assertEqual(list(estimate.demoted),
                         [("file", "shop/cart.py"), ("file", "shop/cli.py")])

        pipeline = self.pipeline(context="semantic", max_cost=limit)
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual(len(self.model.batches), 1)
        self.assertEqual([request.tag for request in self.model.calls], ["shop"])
        self.assertEqual(summary.stopped, "")

    def test_cached_overviews_walked_first_take_no_request(self) -> None:
        # Two packages of one file each, which no overview sums up: once both
        # change, the overview and the module stay cached and the one request
        # buys a file. Regression: each cached answer took a request.
        for relpath, name in (("extra/tool.py", "tool"), ("spare/part.py", "part")):
            path = self.tmp / relpath
            path.parent.mkdir()
            path.write_text(f"def {name}(x: int) -> int:\n    return x * 2\n", "utf-8")
        full = self.pipeline()
        full.run(full.prepare())
        for relpath in ("extra/tool.py", "spare/part.py"):
            path = self.tmp / relpath
            path.write_text(path.read_text("utf-8") + "\n# changed\n", "utf-8")
        self.model.calls.clear()

        estimate = self.priced(max_requests=1)
        self.assertEqual((estimate.requests, estimate.cached), (1, 8))
        pipeline = self.pipeline(max_requests=1)
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual([request.tag for request in self.model.calls],
                         [item.key for item in estimate.items if not item.cached])
        self.assertEqual((summary.stopped, summary.cache_hits), ("", 8))
        docs = self.docs()
        self.assertIn("A small shop", docs["architecture.md"])
        self.assertIn("Holds the cart", docs["shop/index.md"])


class TestOverviewsOfWhatIsBought(PipelineCase):
    """A cached overview is free only while the run sends its prompt as the
    cache holds it. A module's prompt carries its files' purposes and the
    architecture's its modules', so a plan that buys one rebuilds the
    overview: it is a call, and the run makes only the calls its estimate
    counted. Regression: it was counted free, the run asked it again past the
    limit, stopped on the budget (exit 7), and its page lost its prose."""

    def made(self) -> list[str]:
        """The requests the model has had since the last look, by tag."""
        tags = [request.tag for request in self.model.calls]
        self.model.calls.clear()
        return tags

    def planned(self, **limits: object) -> Estimate:
        pipeline = self.pipeline(**limits)
        return pipeline.estimate(pipeline.prepare())

    def as_planned(self, **limits: object) -> list[str]:
        """The calls of a run with `limits`, checked against its estimate: it
        makes none the estimate did not count, and ends as a run that fits."""
        planned = Counter(item.key or item.kind for item in self.planned(**limits).items
                          if not item.cached)
        pipeline = self.pipeline(**limits)
        summary = pipeline.run(pipeline.prepare())
        made = self.made()
        self.assertLessEqual(Counter(made), planned)
        self.assertEqual((summary.stopped, summary.failed), ("", []))
        self.assertEqual(self.last_run(), "finished")
        return made

    def module_first(self) -> None:
        """A first run under one request: it buys the module, from docstrings."""
        first = self.pipeline(max_requests=1)
        first.run(first.prepare())
        self.assertEqual(self.made(), ["shop"])

    def test_a_module_the_limit_cannot_rebuild_keeps_its_prose(self) -> None:
        self.module_first()
        estimate = self.planned(max_requests=1)
        # Each file would take the module along: two requests, over the one.
        self.assertEqual((estimate.requests, estimate.cached), (0, 1))
        self.assertEqual(len(estimate.demoted), 4)
        self.assertEqual(estimate.demoted_by, ["max_requests"])
        self.assertEqual(self.as_planned(max_requests=1), [])
        self.assertIn("Holds the cart", self.docs()["shop/index.md"])

    def test_a_file_bought_takes_its_cached_module_along(self) -> None:
        self.module_first()
        estimate = self.planned(max_requests=2)
        self.assertEqual([(i.key, i.cached) for i in estimate.items],
                         [("shop/cart.py", False), ("shop", False)])
        self.assertEqual(self.as_planned(max_requests=2), ["shop/cart.py", "shop"])
        docs = self.docs()
        self.assertIn(PROSE, docs["shop/cart.py.md"])
        self.assertIn("Holds the cart", docs["shop/index.md"])

    def test_a_rebuilt_module_is_priced_against_the_cost_limit(self) -> None:
        quotes = {(i.kind, i.key): i.cost for i in self.planned(max_cost=0).items}
        self.module_first()
        # cart.py fits alone, but not with the module it rebuilds; nor does any
        # other file.
        limit = quotes[("file", "shop/cart.py")] + quotes[("module", "shop")] / 2
        self.assertGreater(min(cost for (kind, _), cost in quotes.items()
                               if kind == "file") + quotes[("module", "shop")], limit)
        estimate = self.planned(max_cost=limit)
        self.assertEqual((estimate.requests, estimate.demoted_by), (0, ["max_cost"]))
        self.assertEqual(self.as_planned(max_cost=limit), [])
        self.assertIn("Holds the cart", self.docs()["shop/index.md"])

    def test_resuming_under_one_request_loses_no_prose(self) -> None:
        # With an overview: the first run buys it, and each next run a file,
        # as the module would take the overview along.
        for relpath, text in MORE.items():
            (self.tmp / relpath).write_text(text, encoding="utf-8")
        had: set[str] = set()
        for number, bought in enumerate(("architecture", "shop/cart.py",
                                         "shop/cli.py")):
            with self.subTest(run=number + 1):
                self.assertEqual(self.as_planned(max_requests=1), [bought])
                prose = {page for page, text in self.docs().items()
                         if 'mode: "prose"' in text}
                self.assertLessEqual(had, prose)
                had = prose
        self.assertIn("A small shop", self.docs()["architecture.md"])

    def overview_and_module_first(self) -> None:
        """The larger shop, after a first run under two requests: it buys the
        overview and the module, both from docstrings."""
        for relpath, text in MORE.items():
            (self.tmp / relpath).write_text(text, encoding="utf-8")
        first = self.pipeline(max_requests=2)
        first.run(first.prepare())
        self.assertEqual(sorted(self.made()), ["architecture", "shop"])

    def test_a_rebuilt_architecture_is_priced_against_the_cost_limit(self) -> None:
        self.overview_and_module_first()
        quotes = {(i.kind, i.key): i for i in self.planned(max_cost=0).items}
        cheapest = min(i.cost for (kind, _), i in quotes.items() if kind == "file")
        # The cheapest file fits with its module, but not with the architecture.
        limit = (cheapest + quotes[("module", "shop")].cost
                 + quotes[("architecture", "")].cost / 2)
        estimate = self.planned(max_cost=limit)
        self.assertEqual((estimate.requests, estimate.demoted_by), (0, ["max_cost"]))
        self.assertEqual(self.as_planned(max_cost=limit), [])

    def test_a_rebuilt_overview_is_quoted_as_the_call_the_run_makes(self) -> None:
        self.overview_and_module_first()
        for quality in ("cheap", "high"):
            with self.subTest(quality=quality):
                pipeline = self.pipeline(max_requests=3, context="standard")
                self.client.router.quality = quality
                prepared = pipeline.prepare()
                estimate = pipeline.estimate(prepared)
                self.assertEqual(estimate.requests, 3)
                rebuilt = sorted((i for i in estimate.items if i.kind != "file"),
                                 key=lambda i: i.kind)
                self.assertEqual([(i.kind, i.cached, i.requests) for i in rebuilt],
                                 [("architecture", False, 1), ("module", False, 1)])
                fresh = LLMClient(self.model, router=self.client.router)
                for item in rebuilt:
                    prompt = prepared.prompts[(item.kind, item.key)]
                    quote = fresh.quote(prompt.tier, prompt.system, prompt.user,
                                        max_output_tokens=prompt.max_output_tokens)
                    self.assertEqual(
                        (item.model, item.input_tokens, item.output_tokens, item.cost),
                        (quote.model, quote.input_tokens, quote.output_tokens,
                         quote.cost))

    def test_an_overview_no_model_can_ask_again_is_refused_before_paying(self) -> None:
        # The key reaches no architecture model now, and every file to buy would
        # have the run ask the overview again. Before, the run paid for the
        # files and the module, and only then, once their answers changed the
        # overview's prompt, found no model to ask it.
        self.overview_and_module_first()
        for step in ("estimate", "run"):
            with self.subTest(step=step):
                pipeline = self.pipeline()
                self.client.router.available = {"gemini-2.5-flash-lite",
                                                "gemini-2.5-flash"}
                with self.assertRaisesRegex(ConfigError, "architecture"):
                    getattr(pipeline, step)(pipeline.prepare())
                self.assertEqual(self.made(), [])

    def test_an_overview_no_model_can_ask_again_is_still_read_free(self) -> None:
        # The key reaches no architecture model now, which only a rebuilt
        # overview would need: nothing changed, so the run needs none.
        for relpath, text in MORE.items():
            (self.tmp / relpath).write_text(text, encoding="utf-8")
        full = self.pipeline()
        full.run(full.prepare())
        self.made()
        pipeline = self.pipeline()
        self.client.router.available = {"gemini-2.5-flash-lite", "gemini-2.5-flash"}
        estimate = pipeline.estimate(pipeline.prepare())
        self.assertEqual((estimate.requests, estimate.cached), (0, 8))
        pipeline = self.pipeline()
        self.client.router.available = {"gemini-2.5-flash-lite", "gemini-2.5-flash"}
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual((self.made(), summary.stopped, summary.cache_hits),
                         ([], "", 8))


class EmbeddingModel(TaggedModel):
    """The tagged model plus an embeddings endpoint that counts its batches."""

    dimensions = 8

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[str]] = []
        self.attempts = 0
        self.during_embed: Callable[[], None] | None = None

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        self.attempts += 1
        if self.during_embed is not None:
            self.during_embed()
        self.batches.append(list(texts))
        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            raw = [byte - 127.5 for byte in digest[: self.dimensions]]
            norm = sum(v * v for v in raw) ** 0.5
            vectors.append([v / norm for v in raw])
        return vectors


class TestContext(PipelineCase):
    def file_prompt(self, relpath: str) -> str:
        return next(r.prompt for r in self.model.calls if r.tag == relpath)

    def test_prompts_carry_how_a_file_is_used(self) -> None:
        pipeline = self.pipeline()
        pipeline.run(pipeline.prepare())
        prompt = self.file_prompt("shop/cart.py")
        self.assertIn("- shop/cli.py:6 uses Cart: Cart()", prompt)
        self.assertIn("- shop/money.py:3 defines Money: class Money(int) — "
                      "An amount in cents.", prompt)
        self.assertLess(prompt.index("How other files use"), prompt.index("Source:"))

    def test_context_can_be_turned_off(self) -> None:
        pipeline = self.pipeline(context="off")
        pipeline.run(pipeline.prepare())
        self.assertNotIn("How other files use", self.file_prompt("shop/cart.py"))

    def test_an_edit_elsewhere_does_not_buy_a_file_again(self) -> None:
        first = self.pipeline()
        first.run(first.prepare())
        cli = self.tmp / "shop" / "cli.py"
        cli.write_text(cli.read_text("utf-8").replace(
            "    Cart()\n", "    # build the basket\n    Cart().total()\n"
        ), encoding="utf-8")
        calls = len(self.model.calls)
        second = self.pipeline()
        second.run(second.prepare())
        # cart.py's context changed with cli.py, but its answer is still valid.
        self.assertEqual([r.tag for r in self.model.calls[calls:]], ["shop/cli.py"])


class TestSemanticContext(PipelineCase):
    def setUp(self) -> None:
        super().setUp()
        self.embedder = EmbeddingModel()
        self.model = self.embedder

    def test_chunks_are_embedded_once(self) -> None:
        first = self.pipeline(context="semantic")
        summary = first.run(first.prepare())
        self.assertEqual(len(self.embedder.batches), 1)
        self.assertEqual(summary.requests, len(self.model.calls) + 1)

        again = self.pipeline(context="semantic")
        summary = again.run(again.prepare())
        self.assertEqual(len(self.embedder.batches), 1)
        self.assertEqual(summary.requests, 0)

    def test_estimate_quotes_the_embedding_without_buying_it(self) -> None:
        pipeline = self.pipeline(context="semantic")
        estimate = pipeline.estimate(pipeline.prepare())
        embed = estimate.items[0]
        self.assertEqual((embed.kind, embed.model, embed.requests),
                         ("embed", "gemini-embedding-001", 1))
        self.assertGreater(embed.cost, 0.0)
        self.assertEqual(self.embedder.batches, [])

    def test_a_stop_while_embedding_asks_for_nothing_more(self) -> None:
        # A chunk a batch, so that the stop has batches left to refuse.
        self.enterContext(mock.patch("devdocs.generate.Embeddings",
                                     functools.partial(Embeddings, batch=1)))
        pipeline = self.pipeline(context="semantic")
        prepared = pipeline.prepare()

        def unembedded() -> int:
            embeddings = pipeline._embeddings(prepared)
            assert embeddings is not None
            return embeddings.quote().chunks

        chunks = unembedded()
        self.assertGreater(chunks, 1)
        self.embedder.during_embed = self.client.stop.set
        summary = pipeline.run(prepared)
        self.assertEqual(summary.stopped, "interrupted")
        self.assertEqual((len(self.embedder.batches), self.model.calls), (1, []))
        self.assertEqual(unembedded(), chunks - 1)
        self.assertEqual(self.last_run(), "interrupted")

        self.embedder.during_embed = None
        again = self.pipeline(context="semantic")
        again.run(again.prepare())
        # The batch embedded before the stop was kept: each chunk is sent once.
        self.assertEqual([len(b) for b in self.embedder.batches], [1] * chunks)

    def test_a_run_that_would_be_refused_embeds_nothing(self) -> None:
        # Regression: the chunks were embedded, and paid for, before anything.
        readme = self.tmp / "docs" / "README.md"
        readme.parent.mkdir()
        readme.write_text("# Ours\n", encoding="utf-8")
        pipeline = self.pipeline(context="semantic")
        with self.assertRaises(ConfigError):
            pipeline.run(pipeline.prepare())
        self.assertEqual((self.embedder.batches, self.model.calls), ([], []))

    def test_embedding_never_takes_most_of_the_budget(self) -> None:
        messages: list[str] = []
        pipeline = self.pipeline(context="semantic", max_cost=0.00001,
                                 log=messages.append)
        pipeline.run(pipeline.prepare())
        self.assertEqual(self.embedder.batches, [])
        self.assertTrue(any("semantic context skipped" in m for m in messages))

    def test_a_skipped_embedding_names_the_limit_to_raise(self) -> None:
        # Regression: it said to raise --max-cost, whichever limit was in the way.
        for limits, flags in (({"max_cost": 0.00001, "max_requests": 0}, "--max-cost"),
                              ({"max_cost": 0.0, "max_requests": 1}, "--max-requests"),
                              ({"max_cost": 0.00001, "max_requests": 1},
                               "--max-cost and --max-requests")):
            with self.subTest(flags):
                messages: list[str] = []
                pipeline = self.pipeline(context="semantic", log=messages.append,
                                         **limits)
                pipeline.run(pipeline.prepare())
                (skipped,) = [m for m in messages if "skipped" in m]
                self.assertTrue(skipped.endswith(
                    f"over half of {flags}; raise that or use --context standard"),
                    skipped)

    def test_embedding_counts_against_the_day(self) -> None:
        # Regression: only completions were recorded in usage_day.
        pipeline = self.pipeline(context="semantic")
        pipeline.run(pipeline.prepare())
        model = "gemini-embedding-001"
        self.assertEqual(DailyUsage(self.db).requests(model), 1)
        info = find(model)
        assert info is not None
        later = self.pipeline(context="semantic")
        self.client.free_tier = True
        self.assertEqual(self.client.limiter(info).used_today, 1)
        self.assertEqual(later.run(later.prepare()).requests, 0)
        self.assertEqual(DailyUsage(self.db).requests(model), 1)   # nothing was sent

    def test_a_day_spent_by_earlier_runs_sends_no_embedding(self) -> None:
        info = find("gemini-embedding-001")
        assert info is not None and info.free_rpd is not None
        usage = DailyUsage(self.db)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO usage_day (model, day, requests) VALUES (?, ?, ?)",
                (info.id, quota_day(), info.free_rpd),
            )
        self.assertEqual(usage.requests(info.id), info.free_rpd)
        messages: list[str] = []
        pipeline = self.pipeline(context="semantic", log=messages.append)
        self.client.free_tier = True
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual((self.embedder.attempts, summary.stopped), (0, ""))
        reached = [m for m in messages if m.startswith("semantic context: reached")]
        self.assertEqual(len(reached), 1)
        self.assertGreater(summary.prose, 0)


class TestCountsAtOne(PipelineCase):
    """Progress lines say one of a thing in the singular."""

    def setUp(self) -> None:
        super().setUp()
        for relpath in PROJECT:
            (self.tmp / relpath).unlink()
        (self.tmp / "one.py").write_text("def f() -> None:\n    return None\n",
                                         "utf-8")   # one chunk
        self.model = EmbeddingModel()

    def test_one_of_each(self) -> None:
        # Regression: 'scanned 1 files', '1 model calls', '1 chunks', '1 requests'
        # and '1 documents'.
        messages: list[str] = []
        pipeline = self.pipeline(context="semantic", max_requests=1,
                                 log=messages.append)
        pipeline.run(pipeline.prepare())
        self.assertTrue(messages[0].startswith(
            "scanned 1 file, 1 documentable, 1 model call planned ("), messages)
        self.assertIn("semantic context skipped: embedding 1 chunk could take 1 "
                      "request and ", "\n".join(messages))
        messages.clear()
        pipeline = self.pipeline(context="semantic", log=messages.append)
        pipeline.run(pipeline.prepare())
        self.assertIn("embedded 1 chunk ($", "\n".join(messages))

        whole = read_manifest(pipeline.out)
        one = DocSet(project=whole.project)
        one.add(whole.documents[0])
        pipeline.write(one)
        messages.clear()
        pipeline.export()
        self.assertEqual(messages, [f"read 1 document from {pipeline.out / MANIFEST}"])


class TestCountsInThousands(PipelineCase):
    """Progress lines group a count by thousands, as the summaries do."""

    def test_the_render_warnings(self) -> None:
        messages: list[str] = []
        pipeline = self.pipeline(log=messages.append, renderers=[Noisy(1204)])
        pipeline.run(pipeline.prepare())
        (message,) = [m for m in messages if m.startswith("pdf:")]
        self.assertIn(" (1,204):\n", message)
        self.assertIn("\n  and 1,194 more\n", message)


def failing(error: BaseException) -> Callable[[], None]:
    def fail() -> None:
        raise error

    return fail


class TestEmbeddingFailure(PipelineCase):
    """An embedding endpoint that stays down costs semantic hits, not the run."""

    def setUp(self) -> None:
        super().setUp()
        self.embedder = EmbeddingModel()
        self.model = self.embedder
        self.messages: list[str] = []

    def semantic(self, **overrides: object) -> Pipeline:
        return self.pipeline(context="semantic", log=self.messages.append, **overrides)

    def warnings(self) -> list[str]:
        return [m for m in self.messages if m.startswith("warning:")]

    def chunk_a_batch(self) -> None:
        self.enterContext(mock.patch("devdocs.generate.Embeddings",
                                     functools.partial(Embeddings, batch=1)))

    def test_no_failure_ends_the_run_or_is_warned_of_twice(self) -> None:
        # Regression: any of these aborted the run before any prose was bought.
        lasting = RetryPolicy().attempts
        for error, attempts in ((RateLimited("slow", retry_after=0, status=429),
                                 lasting),
                                (ProviderError("bad gateway", status=502), lasting),
                                (ProviderError("network error", status=None), lasting),
                                (ModelNotFound("no such model"), 1),
                                (ProviderError("bad request", status=400), 1),
                                (AuthError("this key cannot embed"), 1)):
            with self.subTest(error.message):
                self.embedder.during_embed = failing(error)
                self.embedder.attempts = 0
                self.messages.clear()
                pipeline = self.semantic()
                summary = pipeline.run(pipeline.prepare())
                self.assertEqual(self.embedder.attempts, attempts)
                self.assertEqual(summary.semantic_fallback, error.message)
                self.assertEqual((summary.stopped, summary.failed), ("", []))
                self.assertEqual(summary.prose, 5)
                self.assertIn(PROSE, self.docs()["shop/cart.py.md"])
                (warning,) = self.warnings()
                self.assertIn(error.message, warning)
                self.assertIn("standard context", warning)

    def test_a_key_that_is_really_bad_still_ends_the_run_at_its_first_call(
        self,
    ) -> None:
        class BadKey(EmbeddingModel):
            def generate(self, request: Request) -> Completion:
                raise AuthError("bad key")

        self.model = self.embedder = BadKey()
        self.embedder.during_embed = failing(AuthError("bad key"))
        pipeline = self.semantic()
        with self.assertRaises(AuthError):
            pipeline.run(pipeline.prepare())
        self.assertEqual(len(self.warnings()), 1)
        self.assertFalse((self.tmp / "docs").exists())

    def test_it_uses_the_clients_policy(self) -> None:
        self.embedder.during_embed = failing(ProviderError("down", status=503))
        pipeline = self.semantic()
        self.client.retry = RetryPolicy(attempts=2, base_delay=0)
        pipeline.run(pipeline.prepare())
        self.assertEqual(self.embedder.attempts, 2)

    def test_attempts_count_as_requests_but_cost_nothing(self) -> None:
        self.embedder.during_embed = failing(ProviderError("down", status=503))
        pipeline = self.semantic()
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual(summary.requests,
                         len(self.model.calls) + RetryPolicy().attempts)
        answers = Usage(input_tokens=500, output_tokens=60)
        prices = [find(request.model) for request in self.model.calls]
        self.assertAlmostEqual(
            summary.cost,
            sum(info.cost(answers) for info in prices if info is not None),
            places=5,
        )
        self.assertEqual(DailyUsage(self.db).requests("gemini-embedding-001"), 0)

    def test_retries_are_reported_like_a_completions(self) -> None:
        self.embedder.during_embed = failing(ProviderError("down", status=503))
        events: list[CallEvent] = []
        pipeline = self.semantic()
        self.client.on_event = events.append
        pipeline.run(pipeline.prepare())
        retries = [e for e in events if e.kind == "retry"]
        self.assertEqual([(e.model, e.tag) for e in retries],
                         [("gemini-embedding-001", "embeddings")]
                         * (RetryPolicy().attempts - 1))
        self.assertIn("#1 in", retries[0].detail)

    def test_it_recovers_when_a_retry_succeeds(self) -> None:
        errors = [ProviderError("down", status=503)]

        def once() -> None:
            if errors:
                raise errors.pop()

        self.embedder.during_embed = once
        pipeline = self.semantic()
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual((self.embedder.attempts, len(self.embedder.batches)), (2, 1))
        self.assertEqual(summary.semantic_fallback, "")
        self.assertEqual(self.warnings(), [])

    def test_files_without_vectors_get_the_standard_context(self) -> None:
        self.embedder.during_embed = failing(ProviderError("down", status=503))
        pipeline = self.semantic()
        pipeline.run(pipeline.prepare())
        sent = {request.tag: request.prompt for request in self.model.calls}

        standard = self.pipeline()
        prepared = standard.prepare()
        standard.price(prepared)
        self.assertEqual(sent, {key or kind: prompt.user
                                for (kind, key), prompt in prepared.prompts.items()})

    def test_the_vectors_bought_before_it_failed_are_used_and_kept(self) -> None:
        self.chunk_a_batch()
        sent = []

        def after_the_first() -> None:
            sent.append(1)
            if len(sent) > 1:
                raise ProviderError("bad request", status=400)

        self.embedder.during_embed = after_the_first
        pipeline = self.semantic()
        prepared = pipeline.prepare()
        pipeline.run(prepared)
        assert prepared.retriever is not None and prepared.retriever.vectors is not None
        self.assertEqual(len(prepared.retriever.vectors), 1)
        self.assertEqual(len(self.embedder.batches), 1)

        # The one vector is shop/__init__.py's: the other files' prompts are as
        # standard context makes them.
        sent = {request.tag: request.prompt for request in self.model.calls}
        standard = self.pipeline()
        plain = standard.prepare()
        standard.price(plain)
        for rel in ("shop/cart.py", "shop/cli.py", "shop/money.py"):
            self.assertEqual(sent[rel], plain.prompts[("file", rel)].user, rel)

    def test_the_next_run_buys_only_what_is_missing(self) -> None:
        self.chunk_a_batch()
        sent = []

        def after_the_first() -> None:
            sent.append(1)
            if len(sent) > 1:
                raise ProviderError("bad request", status=400)

        self.embedder.during_embed = after_the_first
        first = self.semantic()
        first.run(first.prepare())
        calls, texts = len(self.model.calls), list(self.embedder.batches)

        self.embedder.during_embed = None
        again = self.semantic()
        summary = again.run(again.prepare())
        self.assertEqual(summary.semantic_fallback, "")
        self.assertEqual(len(self.model.calls), calls)     # the prose stays bought
        bought = [text for batch in self.embedder.batches for text in batch]
        self.assertEqual(len(bought), len(set(bought)))    # each chunk once
        self.assertEqual(self.embedder.batches[0], texts[0])

    def test_a_stop_while_it_fails_is_a_stop_and_no_failure(self) -> None:
        pipeline = self.semantic()

        def stopped() -> None:
            self.client.stop.set()
            raise ProviderError("down", status=503)

        self.embedder.during_embed = stopped
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual(self.embedder.attempts, 1)
        self.assertEqual(summary.stopped, "interrupted")
        self.assertEqual(summary.semantic_fallback, "")
        self.assertEqual((self.warnings(), self.model.calls), ([], []))

    def test_quota_is_no_failure_and_is_not_retried(self) -> None:
        self.embedder.during_embed = failing(QuotaExhausted("daily quota spent"))
        pipeline = self.semantic()
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual(self.embedder.attempts, 1)
        self.assertEqual((summary.stopped, summary.semantic_fallback), ("", ""))
        self.assertEqual(self.warnings(), [])
        self.assertIn("semantic context: daily quota spent; using the vectors stored",
                      self.messages)
        self.assertEqual(summary.prose, 5)

    def test_retries_stay_within_embeddings_share_of_the_requests(self) -> None:
        # Regression guard: a failure a batch would take twice the requests
        # quoted, and leave none of the limit for the prose.
        self.chunk_a_batch()
        probe = self.pipeline(context="semantic")
        prepared = probe.prepare()
        embeddings = probe._embeddings(prepared)
        assert embeddings is not None
        batches = embeddings.quote().requests
        self.assertGreater(batches, 1)
        tries = []

        def every_other() -> None:
            tries.append(1)
            if len(tries) % 2:
                raise ProviderError("down", status=503)

        self.embedder.during_embed = every_other
        pipeline = self.semantic(max_requests=2 * batches)
        summary = pipeline.run(prepared)
        self.assertEqual(self.embedder.attempts, batches)
        self.assertEqual(summary.prose, 5)
        self.assertEqual(summary.stopped, "")

    def test_a_limit_a_cured_failure_used_up_is_no_failure(self) -> None:
        # Nothing is down, so there is nothing to warn of: the retry only left
        # the last batch no request.
        self.chunk_a_batch()
        probe = self.pipeline(context="semantic")
        prepared = probe.prepare()
        embeddings = probe._embeddings(prepared)
        assert embeddings is not None
        batches = embeddings.quote().requests
        errors = [ProviderError("down", status=503)]

        def once() -> None:
            if errors:
                raise errors.pop()

        self.embedder.during_embed = once
        pipeline = self.semantic(max_requests=2 * batches)
        summary = pipeline.run(prepared)
        self.assertEqual(self.embedder.attempts, batches)
        self.assertEqual((summary.semantic_fallback, self.warnings()), ("", []))
        self.assertIn(f"semantic context: embedding reached its request limit "
                      f"({batches}); using the vectors stored", self.messages)

    def test_the_warning_goes_where_warnings_go(self) -> None:
        # Regression: it went to the log alone, which --quiet silences, and the
        # run exited 0 with nothing to say it had no semantic context.
        warned: list[str] = []
        self.embedder.during_embed = failing(ProviderError("down", status=503))
        pipeline = self.semantic(warn=warned.append)
        pipeline.run(pipeline.prepare())
        (warning,) = warned
        self.assertTrue(warning.startswith("semantic context: embedding failed (down)"),
                        warning)
        self.assertFalse([m for m in self.messages if "embedding failed" in m])

    def test_a_tight_request_limit_still_warns_of_what_failed(self) -> None:
        # Regression: below --max-requests 8, half the limit is under the
        # policy's attempts, and an outage read as a limit reached: no warning,
        # and no `semantic_fallback` for --json.
        self.embedder.during_embed = failing(ProviderError("down", status=503))
        pipeline = self.semantic(max_requests=6)
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual(self.embedder.attempts, 3)
        self.assertEqual(summary.semantic_fallback, "down")
        (warning,) = self.warnings()
        self.assertIn("embedding failed (down)", warning)
        self.assertFalse([m for m in self.messages if "request limit" in m])

    def test_a_run_with_no_failure_says_nothing_of_one(self) -> None:
        pipeline = self.semantic()
        summary = pipeline.run(pipeline.prepare())
        self.assertEqual((summary.semantic_fallback, self.warnings()), ("", []))


class GeminiWire:
    """A transport that answers as Gemini's API does, generating as TaggedModel
    does; `embedding` scripts what each embedding request gets, in turn: a
    status and body, or an error to raise. Once out of script it answers."""

    def __init__(self, *embedding: tuple[int, Any] | ProviderError) -> None:
        self.embedding = list(embedding)
        self.sent = 0
        self.model = TaggedModel()

    def request(self, method: str, url: str, *, headers: Any = None,
                body: bytes | None = None) -> HttpResponse:
        payload = json.loads(body or b"{}")
        if url.endswith(":batchEmbedContents"):
            self.sent += 1
            if self.embedding:
                outcome = self.embedding.pop(0)
                if isinstance(outcome, ProviderError):
                    raise outcome
                return self.reply(*outcome)
            return self.reply(200, {"embeddings": [
                {"values": [1.0] + [0.0] * 7} for _ in payload["requests"]]})
        text = self.model.generate(Request(
            model="gemini-2.5-flash-lite",
            system=payload["systemInstruction"]["parts"][0]["text"],
            prompt=payload["contents"][0]["parts"][0]["text"],
        )).text
        return self.reply(200, {"candidates": [{
            "content": {"parts": [{"text": text}]}, "finishReason": "STOP",
        }], "usageMetadata": {"promptTokenCount": 500, "candidatesTokenCount": 60}})

    @staticmethod
    def reply(status: int, body: Any) -> HttpResponse:
        return HttpResponse(status, {}, json.dumps(body).encode())


class TestEmbeddingFailureOverTheWire(PipelineCase):
    """The failures named in the reports, as the real provider maps them."""

    LIMITED = (429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "0s"},
    ]}})
    DOWN = (500, {"error": {"code": 500, "status": "INTERNAL"}})
    MISSING = (404, {"error": {"code": 404, "status": "NOT_FOUND",
                               "message": "gemini-embedding-001 is not found"}})
    NETWORK = ProviderError("network error talking to generativelanguage")

    def run_on(self, *script: Any) -> tuple[GeminiWire, Any, list[str]]:
        wire = GeminiWire(*script)
        self.model = GeminiProvider("k", wire, dimensions=8)  # type: ignore[assignment]
        messages: list[str] = []
        pipeline = self.pipeline(context="semantic", log=messages.append)
        return wire, pipeline.run(pipeline.prepare()), messages

    def warnings(self, messages: list[str]) -> list[str]:
        return [m for m in messages if m.startswith("warning:")]

    def test_a_per_minute_429_is_only_a_delay(self) -> None:
        # Regression: this one response ended the run, with no page written.
        wire, summary, messages = self.run_on(self.LIMITED)
        self.assertEqual(wire.sent, 2)
        self.assertEqual((summary.semantic_fallback, self.warnings(messages)), ("", []))
        self.assertEqual(summary.prose, 5)

    def test_what_stays_down_leaves_the_prose_to_be_bought(self) -> None:
        lasting = RetryPolicy().attempts
        for script, sent in (([self.LIMITED] * lasting, lasting),
                             ([self.DOWN] * lasting, lasting),
                             ([self.NETWORK] * lasting, lasting),
                             ([self.MISSING], 1)):
            with self.subTest(script[0]):
                wire, summary, messages = self.run_on(*script)
                self.assertEqual(wire.sent, sent)
                self.assertEqual(summary.prose, 5)
                self.assertEqual(summary.stopped, "")
                self.assertEqual(len(self.warnings(messages)), 1)
                shutil.rmtree(self.tmp / "docs")

    def test_a_days_quota_is_a_stop_as_ever(self) -> None:
        spent = (429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                 "message": "limit per day reached"}})
        wire, summary, messages = self.run_on(spent)
        self.assertEqual(wire.sent, 1)
        self.assertEqual((summary.semantic_fallback, self.warnings(messages)), ("", []))
        self.assertEqual(summary.prose, 5)


class TestALoneSurrogate(PipelineCase):
    """A module docstring may spell one as an escape, which parsing decodes to
    text no strict encoder takes. Regression: one such file, on any OS, ended
    `estimate` and `generate` with an internal error and no pages."""

    def setUp(self) -> None:
        super().setUp()
        (self.tmp / "shop" / "emoji.py").write_text(
            '"""Smiles: \\ud83d\\ude00."""\n\n\ndef smile() -> str:\n    return "x"\n',
            encoding="utf-8",
        )

    def test_standard_context_documents_it(self) -> None:
        pipeline = self.pipeline()
        prepared = pipeline.prepare()
        self.assertGreater(pipeline.estimate(prepared).requests, 0)
        self.assertEqual(pipeline.run(prepared).failed, [])
        self.assertIn(PROSE, self.docs()["shop/emoji.py.md"])

    def test_semantic_context_buys_its_vectors_once(self) -> None:
        # Over the wire, as what is sent must encode too.
        wire = GeminiWire()
        self.model = GeminiProvider("k", wire, dimensions=8)  # type: ignore[assignment]
        first = self.pipeline(context="semantic")
        prepared = first.prepare()
        self.assertEqual(first.estimate(prepared).items[0].kind, "embed")
        summary = first.run(prepared)
        self.assertEqual((summary.semantic_fallback, summary.failed), ("", []))
        self.assertIn(PROSE, self.docs()["shop/emoji.py.md"])
        self.assertEqual(wire.sent, 1)

        again = self.pipeline(context="semantic")
        self.assertEqual(again.run(again.prepare()).requests, 0)
        self.assertEqual(wire.sent, 1)


def dump(db: Database) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table."""
    tables = db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {table["name"]: [tuple(row) for row in db.query(
        f"SELECT * FROM {table['name']} ORDER BY 1, 2")] for table in tables}


class VectorCase(PipelineCase):
    def setUp(self) -> None:
        super().setUp()
        self.embedder = EmbeddingModel()
        self.model = self.embedder

    def semantic(self, **overrides: object) -> Pipeline:
        return self.pipeline(context="semantic", **overrides)

    def bought(self) -> None:
        """A run that embeds every chunk of the project."""
        first = self.semantic()
        first.run(first.prepare())
        self.embedder.batches.clear()

    def vectors(self, source: str | None = None) -> int:
        """Vectors the store holds, of one file or of all."""
        rows = self.db.query("SELECT source FROM chunk WHERE embedding IS NOT NULL")
        return sum(1 for row in rows if source is None or row["source"] == source)

    def a_subpackage(self) -> Path:
        """A directory of the project holding one file with vectors to buy."""
        sub = self.tmp / "shop" / "sub"
        sub.mkdir()
        (sub / "__init__.py").write_text('"""Sub."""\n\n\ndef sub() -> int:\n'
                                         "    return 1\n", encoding="utf-8")
        return sub

    def edit_the_cart(self) -> None:
        cart = self.tmp / "shop" / "cart.py"
        cart.write_text(cart.read_text("utf-8").replace("sum(", "total("), "utf-8")


class TestEstimateChangesNothing(VectorCase):
    """Regression: every estimate synced and pruned the chunks, so a narrower
    --exclude deleted vectors already paid for (measured: 1,314 cut to 827)."""

    def test_a_narrower_selection_deletes_nothing(self) -> None:
        self.bought()
        before = dump(self.db)
        pipeline = self.semantic(exclude=("shop/cli.py",))
        pipeline.estimate(pipeline.prepare())
        self.assertGreater(self.vectors("shop/cli.py"), 0)
        self.assertEqual(dump(self.db), before)

    def test_edits_and_deletions_rewrite_nothing(self) -> None:
        self.bought()
        before = dump(self.db)
        self.edit_the_cart()
        (self.tmp / "shop" / "cli.py").unlink()
        (self.tmp / "shop" / "new.py").write_text("def new() -> int:\n    return 1\n",
                                                  encoding="utf-8")
        pipeline = self.semantic()
        pipeline.estimate(pipeline.prepare())
        self.assertEqual(dump(self.db), before)

    def test_a_first_estimate_writes_nothing(self) -> None:
        before = dump(self.db)
        pipeline = self.semantic()
        estimate = pipeline.estimate(pipeline.prepare())
        self.assertEqual(estimate.items[0].kind, "embed")
        self.assertEqual(dump(self.db), before)

    def test_it_quotes_the_files_selected_and_nothing_else(self) -> None:
        def quoted(**overrides: object) -> tuple[int, int]:
            pipeline = self.semantic(**overrides)
            prepared = pipeline.prepare()
            embed = [i for i in pipeline.estimate(prepared).items if i.kind == "embed"]
            chunks = sum(len(group) for group in prepared.chunks.values())
            return (embed[0].input_tokens if embed else 0), chunks

        whole, chunks = quoted()
        narrower, fewer = quoted(exclude=("shop/cli.py",))
        self.assertLess(fewer, chunks)
        self.assertLess(narrower, whole)
        self.bought()
        self.assertEqual(quoted()[0], 0)
        self.assertEqual(quoted(exclude=("shop/cli.py",))[0], 0)

    def test_an_estimate_of_an_edit_quotes_what_the_run_then_buys(self) -> None:
        self.bought()
        self.edit_the_cart()
        pipeline = self.semantic()
        prepared = pipeline.prepare()
        estimate = pipeline.estimate(prepared)
        self.assertEqual(self.embedder.batches, [])
        embed = estimate.items[0]
        self.assertEqual((embed.kind, embed.requests), ("embed", 1))

        again = self.semantic()
        again.run(again.prepare())
        (batch,) = self.embedder.batches
        self.assertEqual(embed.input_tokens, token_ceiling(*batch))
        self.assertTrue(all(text.startswith("shop/cart.py") for text in batch))

    def test_it_reads_the_vectors_of_the_selection_alone(self) -> None:
        self.bought()
        pipeline = self.semantic(exclude=("shop/cli.py",))
        prepared = pipeline.prepare()
        assert prepared.retriever is not None and prepared.retriever.vectors is not None
        selected = {chunk.id for group in prepared.chunks.values() for chunk in group}
        self.assertEqual(set(prepared.retriever.vectors.ids), selected)
        self.assertEqual(self.vectors(), self.vectors("shop/cli.py") + len(selected))

    def test_the_command_deletes_no_vector(self) -> None:
        from devdocs.cli import main

        self.bought()
        before = dump(self.db)
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = main(["estimate", str(self.tmp), "--context", "semantic",
                         "--exclude", "shop/cli.py", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(dump(self.db), before)


class TestVectorsPaidFor(VectorCase):
    """What a run keeps of the vectors in the store, and what it drops."""

    def test_a_narrower_run_keeps_what_it_leaves_out(self) -> None:
        self.bought()
        kept = self.vectors("shop/cli.py")
        self.assertGreater(kept, 0)
        narrower = self.semantic(exclude=("shop/cli.py",))
        narrower.run(narrower.prepare())
        self.assertEqual(self.vectors("shop/cli.py"), kept)

    def test_widening_the_selection_again_pays_nothing(self) -> None:
        # Regression: pruned by the narrower run, they were all bought again.
        self.bought()
        narrower = self.semantic(exclude=("shop/cli.py",))
        narrower.run(narrower.prepare())
        wider = self.semantic()
        summary = wider.run(wider.prepare())
        self.assertEqual((self.embedder.batches, summary.requests), ([], 0))

    def test_a_chunk_left_out_is_not_bought(self) -> None:
        # Its row may be there with no vector, from a run whose embedding failed.
        def down() -> None:
            raise ProviderError("bad request", status=400)

        self.embedder.during_embed = down
        failed = self.semantic()
        failed.run(failed.prepare())
        self.assertEqual(self.vectors(), 0)
        self.embedder.during_embed = None
        narrower = self.semantic(exclude=("shop/cli.py",))
        narrower.run(narrower.prepare())
        (batch,) = self.embedder.batches
        self.assertFalse([text for text in batch if text.startswith("shop/cli.py")])
        self.assertEqual(self.vectors("shop/cli.py"), 0)

    def test_retrieval_does_not_see_what_is_left_out(self) -> None:
        self.bought()
        narrower = self.semantic(exclude=("shop/cli.py",))
        prepared = narrower.prepare()
        narrower.run(prepared)
        assert prepared.retriever is not None and prepared.retriever.vectors is not None
        self.assertFalse(set(prepared.retriever.vectors.ids)
                         - {chunk.id for chunks in prepared.chunks.values()
                            for chunk in chunks})

    def test_a_file_that_is_gone_loses_its_vectors(self) -> None:
        self.bought()
        self.assertGreater(self.vectors("shop/cli.py"), 0)
        (self.tmp / "shop" / "cli.py").unlink()
        again = self.semantic()
        again.run(again.prepare())
        self.assertEqual(self.vectors("shop/cli.py"), 0)
        self.assertGreater(self.vectors("shop/cart.py"), 0)

    def test_a_file_where_a_directory_was_loses_its_vectors(self) -> None:
        sub = self.a_subpackage()
        self.bought()
        (sub / "__init__.py").unlink()
        sub.rmdir()
        sub.write_text("not a directory now\n", encoding="utf-8")
        again = self.semantic()
        again.run(again.prepare())
        self.assertEqual(self.vectors("shop/sub/__init__.py"), 0)

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0,
                     "POSIX permission bits, which root ignores")
    def test_a_directory_the_run_cannot_read_keeps_its_files_vectors(self) -> None:
        # Regression: that it could not see them was taken for their being gone,
        # and they were bought again once it could.
        sub = self.a_subpackage()
        self.bought()
        kept = self.vectors("shop/sub/__init__.py")
        self.assertGreater(kept, 0)
        sub.chmod(0)
        self.addCleanup(sub.chmod, 0o755)
        locked = self.semantic()
        locked.run(locked.prepare())
        self.assertEqual(self.vectors("shop/sub/__init__.py"), kept)

        sub.chmod(0o755)
        again = self.semantic()
        self.assertEqual(again.run(again.prepare()).requests, 0)
        self.assertEqual(self.embedder.batches, [])

    @unittest.skipIf(os.name == "nt", "symbolic links need a privilege on Windows")
    def test_a_file_replaced_by_a_broken_link_keeps_its_vectors(self) -> None:
        # The name is still there, left out as links are, and the file may return.
        self.bought()
        kept = self.vectors("shop/cli.py")
        self.assertGreater(kept, 0)
        cli = self.tmp / "shop" / "cli.py"
        cli.unlink()
        cli.symlink_to(self.tmp / "nowhere.py")
        again = self.semantic()
        again.run(again.prepare())
        self.assertEqual(self.vectors("shop/cli.py"), kept)

    def test_a_file_that_changed_loses_only_its_stale_vectors(self) -> None:
        self.bought()
        ids = [row["id"] for row in self.db.query(
            "SELECT id FROM chunk WHERE source = 'shop/cart.py' ORDER BY start_line")]
        self.edit_the_cart()
        again = self.semantic()
        again.run(again.prepare())
        now = {row["id"] for row in self.db.query(
            "SELECT id FROM chunk "
            "WHERE source = 'shop/cart.py' AND embedding IS NOT NULL")}
        self.assertLess(len(now & set(ids)), len(ids))   # the edit's are replaced
        self.assertTrue(now & set(ids))                  # the rest are not bought again
        self.assertEqual(len(self.embedder.batches), 1)
        self.assertLess(len(self.embedder.batches[0]), len(ids))

    def test_a_run_that_is_refused_changes_nothing(self) -> None:
        self.bought()
        before = dump(self.db)
        self.edit_the_cart()
        docs = self.tmp / "docs"
        shutil.rmtree(docs)
        docs.mkdir()
        (docs / "README.md").write_text("# Ours\n", encoding="utf-8")
        pipeline = self.semantic(exclude=("shop/cli.py",))
        with self.assertRaises(ConfigError):
            pipeline.run(pipeline.prepare())
        self.assertEqual(dump(self.db), before)


if __name__ == "__main__":
    unittest.main()
