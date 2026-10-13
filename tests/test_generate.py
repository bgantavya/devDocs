"""The whole pipeline over a small real project, with a scripted model.

No network: `TaggedModel` answers each prompt in the reply format the style
guard parses, so these tests exercise scan, parse, graph, plan, prompts, the
client, composition, rendering and writing end to end.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from devdocs.config import Config
from devdocs.errors import ConfigError
from devdocs.generate import Pipeline
from devdocs.index.cache import ResponseCache
from devdocs.index.db import Database
from devdocs.index.ledger import DailyUsage
from devdocs.llm import Completion, Finish, Request, Usage
from devdocs.llm.budget import Budget, Router
from devdocs.llm.client import LLMClient
from devdocs.llm.http import RetryPolicy
from devdocs.render import Renderer
from devdocs.render.markdown import MarkdownRenderer
from devdocs.render.model import DocSet
from devdocs.render.pdf import PdfRenderer, external
from devdocs.render.pdf.check import PdfFile, check

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


class PipelineCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        for relpath, text in PROJECT.items():
            path = self.tmp / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.db = Database(self.tmp / ".devdocs" / "devdocs.db")
        self.addCleanup(self.db.close)
        self.model = TaggedModel()

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
        return Pipeline(config, client=client, db=self.db, log=log,
                        renderers=renderers)

    def docs(self) -> dict[str, str]:
        out = self.tmp / "docs"
        return {
            p.relative_to(out).as_posix(): p.read_text("utf-8")
            for p in sorted(out.rglob("*")) if p.is_file()
        }


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

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
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

    def test_embedding_never_takes_most_of_the_budget(self) -> None:
        messages: list[str] = []
        pipeline = self.pipeline(context="semantic", max_cost=0.00001,
                                 log=messages.append)
        pipeline.run(pipeline.prepare())
        self.assertEqual(self.embedder.batches, [])
        self.assertTrue(any("semantic context skipped" in m for m in messages))


if __name__ == "__main__":
    unittest.main()
