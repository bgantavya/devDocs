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
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from devdocs.config import Config
from devdocs.errors import (
    ConfigError,
    DevDocsError,
    ProviderError,
    RateLimited,
)
from devdocs.generate import Pipeline, Prepared
from devdocs.index.cache import ResponseCache
from devdocs.index.db import Database
from devdocs.index.embed import Embeddings
from devdocs.index.ledger import DailyUsage
from devdocs.llm import Completion, Finish, Request, Usage
from devdocs.llm.budget import Budget, Router, token_ceiling
from devdocs.llm.client import LLMClient
from devdocs.llm.http import RetryPolicy
from devdocs.render import Renderer
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

    def pipeline(self, *, log: Any = None, renderers: Sequence[Renderer] | None = None,
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
        return Pipeline(config, client=client, db=self.db, log=log,
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
                    (prepared.plan.calls(), prepared.prompts),
                    (calls, {}))

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


class EmbeddingModel(TaggedModel):
    """The tagged model plus an embeddings endpoint that counts its batches."""

    dimensions = 8

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[str]] = []
        self.during_embed: Callable[[], None] | None = None

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
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
    """What a run keeps of the vectors in the store."""

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
