"""The command line end to end: commands, output, exit codes. No network: the
provider factory is replaced by the scripted model the pipeline tests use."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FrameType
from typing import Any, TextIO
from unittest import mock

from devdocs import __version__, capabilities, cli
from devdocs import generate as generate_module
from devdocs.capabilities import Capability, Report
from devdocs.config import load_config
from devdocs.errors import (
    AuthError,
    DevDocsError,
    RateLimited,
)
from devdocs.index.db import Database
from devdocs.llm import Completion, Request
from devdocs.llm.client import LLMClient
from devdocs.render.markdown import MarkdownRenderer
from devdocs.render.model import DocSet
from devdocs.render.pdf import external
from devdocs.render.pdf.check import PdfFile, check
from tests.test_generate import PROJECT, PROSE, TaggedModel

_KEYS = ("DEVDOCS_", "GEMINI_", "GOOGLE_", "ANTHROPIC_")


@contextlib.contextmanager
def clean_env(**extra: str) -> Iterator[None]:
    """The process environment without any key or DEVDOCS_* setting."""
    kept = {k: v for k, v in os.environ.items() if not k.startswith(_KEYS)}
    with mock.patch.dict(os.environ, {**kept, **extra}, clear=True):
        yield


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        for relpath, text in PROJECT.items():
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.model = TaggedModel()

    def run_cli(self, *argv: str, model: Any = None,
                env: dict[str, str] | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        fake = model if model is not None else self.model
        with clean_env(**(env or {})), \
                mock.patch.object(cli, "make_provider", return_value=fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class TestBasics(CliCase):
    def test_no_command_prints_help(self) -> None:
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("generate", out)

    def test_version(self) -> None:
        with self.assertRaises(SystemExit) as caught, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn(__version__, out.getvalue())


class TestGenerate(CliCase):
    def test_generate_writes_docs_and_reports_json(self) -> None:
        code, out, _ = self.run_cli("generate", str(self.root), "--api-key", "k",
                                    "--json", "--quiet")
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertGreater(summary["prose"], 0)
        self.assertEqual(summary["failed"], [])
        self.assertTrue((self.root / "docs" / "README.md").is_file())
        self.assertTrue((self.root / ".devdocs" / "devdocs.db").is_file())

    def test_no_key_is_a_config_error(self) -> None:
        code, _, err = self.run_cli("generate", str(self.root))
        self.assertEqual(code, 2)
        self.assertIn("no API key", err)

    def test_a_rejected_key_exits_3(self) -> None:
        class Rejecting(TaggedModel):
            def list_models(self) -> list[str]:
                raise AuthError("Gemini rejected the API key")

        code, _, err = self.run_cli("generate", str(self.root), "--api-key", "bad",
                                    model=Rejecting())
        self.assertEqual(code, 3)
        self.assertIn("rejected", err)

    def test_v3_flags_are_translated(self) -> None:
        # v3 wrote under --output in the current directory
        with contextlib.chdir(self.root):
            code, _, err = self.run_cli("-p", str(self.root), "--output", "manual",
                                        "--include", "tests", "--api-key", "k")
        self.assertEqual(code, 0)
        self.assertIn("deprecated", err)
        self.assertTrue((self.root / "manual" / "shop" / "cart.md").is_file())

    def test_the_v3_notice_masks_the_key(self) -> None:
        for spelling in (["--api-key", "AIza-SECRET"], ["--api-key=AIza-SECRET"],
                         ["--api", "AIza-SECRET"]):
            with self.subTest(spelling=spelling), contextlib.chdir(self.root):
                code, _, err = self.run_cli("-p", ".", *spelling)
                self.assertEqual(code, 0)
                self.assertIn("deprecated; running `devdocs generate . ", err)
                self.assertNotIn("AIza-SECRET", err)

    def test_a_v3_line_v3_rejects_masks_the_key_too(self) -> None:
        # --a abbreviates v3's --authors and --api-key alike
        err = io.StringIO()
        with contextlib.chdir(self.root), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as caught:
            cli.main(["-p", ".", "--a=AIza-SECRET"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("ambiguous option: --a=*** could match", err.getvalue())
        self.assertNotIn("AIza-SECRET", err.getvalue())
        self.assertEqual(self.model.calls, [])

    def test_a_v4_command_line_is_never_translated(self) -> None:
        # `--output` is a v3 flag; read as v3, this would run a paid generate.
        with self.assertRaises(SystemExit) as caught, \
                contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("export", str(self.root), "--output", "manual")
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.model.calls, [])
        self.assertFalse((self.root / "manual").exists())

    def test_formats_reach_the_pipeline(self) -> None:
        code, out, _ = self.run_cli("generate", str(self.root), "--api-key", "k",
                                    "--format", "html", "--format", "markdown,pdf",
                                    "--page-size", "letter", "--json", "--quiet")
        self.assertEqual(code, 0)
        docs = self.root / "docs"
        for page in ("README.md", "index.html", "assets/style.css", "shop.pdf"):
            self.assertTrue((docs / page).is_file(), page)
        book = (docs / "shop.pdf").read_bytes()
        self.assertEqual(check(book), [])
        self.assertEqual(PdfFile(book).page(0)["MediaBox"], [0, 0, 612, 792])
        self.assertEqual(json.loads(out)["warnings"], [])

    def test_only_the_formats_asked_for_are_written(self) -> None:
        code, _, _ = self.run_cli("generate", str(self.root), "--api-key", "k",
                                  "--format", "pdf", "--split", "--quiet")
        self.assertEqual(code, 0)
        docs = self.root / "docs"
        self.assertTrue((docs / "pdf" / "index.pdf").is_file())
        self.assertTrue((docs / "pdf" / "shop" / "cart.py.pdf").is_file())
        self.assertFalse((docs / "README.md").exists())

    def test_impossible_pdf_options_are_config_errors(self) -> None:
        code, _, err = self.run_cli("generate", str(self.root), "--api-key", "k",
                                    "--format", "pdf", "--pdf-engine", "chrome",
                                    "--split")
        self.assertEqual(code, 2)
        self.assertIn("cannot split", err)
        with mock.patch.object(external, "find", return_value=None):
            code, _, err = self.run_cli("generate", str(self.root), "--api-key", "k",
                                        "--format", "pdf", "--pdf-engine", "chrome")
        self.assertEqual(code, 2)
        self.assertIn("chrome PDF engine is not installed", err)
        self.assertEqual(self.model.calls, [])

    def test_no_split_overrides_the_file(self) -> None:
        # Regression: --split could only say yes, so the file always won.
        (self.root / "devdocs.toml").write_text("pdf_split = true\n", "utf-8")
        code, _, err = self.run_cli("generate", str(self.root), "--api-key", "k",
                                    "--format", "pdf", "--no-split", "--quiet")
        self.assertEqual(code, 0, err)
        docs = self.root / "docs"
        self.assertTrue((docs / "shop.pdf").is_file())
        self.assertFalse((docs / "pdf").exists())

    def test_a_split_file_does_not_break_commands_without_pdf(self) -> None:
        (self.root / "devdocs.toml").write_text("pdf_split = true\n", "utf-8")
        chrome = {"DEVDOCS_PDF_ENGINE": "chrome"}
        code, _, err = self.run_cli("cache", "stats", str(self.root), env=chrome)
        self.assertEqual(code, 0, err)
        code, _, err = self.run_cli("generate", str(self.root), "--api-key", "k",
                                    "--quiet", env=chrome)
        self.assertEqual(code, 0, err)

    def test_pdf_options_without_pdf_output_are_config_errors(self) -> None:
        # Regression: they were ignored, and the run wrote Markdown alone.
        for flags in (["--pdf-engine", "chrome"], ["--split"], ["--no-split"],
                      ["--page-size", "letter", "--format", "html"]):
            with self.subTest(flags):
                code, _, err = self.run_cli("generate", str(self.root), "--api-key",
                                            "k", *flags)
                self.assertEqual(code, 2)
                self.assertIn(f"{flags[0]} applies only to PDF output", err)
                self.assertIn("add --format pdf", err)
        code, _, err = self.run_cli("generate", str(self.root), "--api-key", "k",
                                    "--page-size", "letter", "--split")
        self.assertEqual(code, 2)
        self.assertIn("--page-size, --split apply only to PDF output", err)
        self.assertEqual(self.model.calls, [])

    def test_an_empty_format_list_is_refused_before_anything_is_bought(self) -> None:
        self.run_cli("generate", str(self.root), "--api-key", "k", "--quiet")
        docs = sorted(p.name for p in (self.root / "docs").iterdir())
        calls = len(self.model.calls)
        for argv, env in ((["--format", ""], None), ([], {"DEVDOCS_FORMATS": ","})):
            with self.subTest(argv=argv, env=env):
                code, _, err = self.run_cli("generate", str(self.root), "--api-key",
                                            "k", *argv, env=env)
                self.assertEqual(code, 2)
                self.assertIn("no output format configured", err)
        self.assertEqual(len(self.model.calls), calls)
        self.assertEqual(sorted(p.name for p in (self.root / "docs").iterdir()), docs)

    def test_a_hand_written_page_in_the_way_is_kept(self) -> None:
        readme = self.root / "docs" / "README.md"
        readme.parent.mkdir()
        readme.write_text("# Our documentation\n", "utf-8")
        code, _, err = self.run_cli("generate", str(self.root), "--api-key", "k",
                                    "--quiet")
        self.assertEqual(code, 2)
        self.assertIn("has no record of writing: README.md", err)
        self.assertIn("--force replaces it; the answers are cached", err)
        self.assertEqual(sorted(p.name for p in readme.parent.iterdir()),
                         ["README.md"])
        self.assertEqual(readme.read_text("utf-8"), "# Our documentation\n")
        calls = len(self.model.calls)
        code, _, err = self.run_cli("generate", str(self.root), "--api-key", "k",
                                    "--quiet", "--force")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.model.calls), calls)
        self.assertIn("Generated by devdocs", readme.read_text("utf-8"))

    def test_a_failing_pdf_engine_costs_only_the_pdf(self) -> None:
        # Regression: nothing was written, not even the manifest export needs.
        broken = DevDocsError("chrome could not print the book (exit status 1)",
                              hint="run chrome by hand to see why")
        with mock.patch.object(external, "find", return_value="/bin/chrome"), \
                mock.patch.object(external, "render", side_effect=broken):
            code, out, _ = self.run_cli("generate", str(self.root), "--api-key", "k",
                                        "--format", "markdown,pdf", "--pdf-engine",
                                        "chrome", "--json", "--quiet")
        self.assertEqual(code, 4)
        summary = json.loads(out)
        self.assertEqual(summary["failed"], [[
            "format", "pdf", "chrome could not print the book (exit status 1)\n"
            "  hint: run chrome by hand to see why; `devdocs export --format pdf` "
            "makes it from the manifest, with no key",
        ]])
        docs = self.root / "docs"
        listed = json.loads((docs / "index.json").read_bytes())["files"]
        self.assertIn("README.md", listed)
        self.assertGreater(summary["requests"], 0)
        code, _, err = self.run_cli("export", str(self.root), "--format", "pdf")
        self.assertEqual(code, 0, err)
        self.assertEqual(check((docs / "shop.pdf").read_bytes()), [])


class TestEstimate(CliCase):
    def test_estimate_needs_no_key_and_sends_nothing(self) -> None:
        code, out, _ = self.run_cli("estimate", str(self.root), "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertGreater(data["requests"], 0)
        self.assertGreater(data["max_cost_usd"], 0)
        self.assertEqual(self.model.calls, [])

    def test_semantic_estimate_quotes_the_embeddings_first(self) -> None:
        code, out, _ = self.run_cli("estimate", str(self.root), "--context",
                                    "semantic", "--json")
        self.assertEqual(code, 0)
        embed = json.loads(out)["items"][0]
        self.assertEqual((embed["kind"], embed["requests"]), ("embed", 1))

    def test_semantic_context_with_claude_is_a_config_error(self) -> None:
        code, _, err = self.run_cli("estimate", str(self.root), "--context",
                                    "semantic", "--provider", "anthropic")
        self.assertEqual(code, 2)
        self.assertIn("Gemini embeddings", err)

    def test_explain_lists_every_file(self) -> None:
        code, out, _ = self.run_cli("estimate", str(self.root), "--explain")
        self.assertEqual(code, 0)
        self.assertIn("shop/cart.py", out)

    def test_excludes_from_flags_apply(self) -> None:
        # Regression: list flags were refused by the config, exiting 2.
        code, out, _ = self.run_cli("estimate", str(self.root), "--explain",
                                    "--exclude", "shop/cli.py,shop/money.py")
        self.assertEqual(code, 0)
        self.assertIn("shop/cart.py", out)
        self.assertNotIn("shop/cli.py", out)
        self.assertNotIn("shop/money.py", out)


class TestInitAndCache(CliCase):
    def test_init_writes_config_and_ignores_state(self) -> None:
        (self.root / ".gitignore").write_text("node_modules/", encoding="utf-8")
        code, _, _ = self.run_cli("init", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn('provider = "gemini"',
                      (self.root / "devdocs.toml").read_text("utf-8"))
        self.assertEqual((self.root / ".gitignore").read_text("utf-8"),
                         "node_modules/\n.devdocs/\n")
        code, _, err = self.run_cli("init", str(self.root))
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)

    def test_every_option_the_template_mentions_is_valid(self) -> None:
        self.run_cli("init", str(self.root))
        text = (self.root / "devdocs.toml").read_text("utf-8")
        for option in ("formats", "pdf_engine", "pdf_split", "page_size"):
            self.assertIn(f"# {option} = ", text)
        uncommented = "\n".join(
            line.removeprefix("# ") if " = " in line else line
            for line in text.splitlines()
        )
        path = self.root / "uncommented.toml"
        path.write_text(uncommented, encoding="utf-8")
        config = load_config(config_file=path, env={})
        self.assertEqual((config.formats, config.page_size), (("markdown",), "A4"))

    def test_cache_stats_and_clear(self) -> None:
        self.run_cli("generate", str(self.root), "--api-key", "k", "--quiet")
        code, out, _ = self.run_cli("cache", "stats", str(self.root), "--json")
        self.assertEqual(code, 0)
        self.assertGreater(json.loads(out)["entries"], 0)
        code, out, _ = self.run_cli("cache", "clear", str(self.root), "--json")
        self.assertGreater(json.loads(out)["removed"], 0)


class TestDoctorAndModels(CliCase):
    def test_doctor_offline_with_a_key(self) -> None:
        code, out, _ = self.run_cli("doctor", str(self.root), "--offline",
                                    "--api-key", "k")
        self.assertIn("api key", out)
        self.assertIn(code, (0, 1))   # 1 only if this machine lacks a capability

    def test_doctor_without_a_key_is_blocking(self) -> None:
        code, out, _ = self.run_cli("doctor", str(self.root), "--offline")
        self.assertEqual(code, 1)
        self.assertIn("!! api key", out)

    def test_doctor_lists_the_pdf_engines_it_finds(self) -> None:
        found = {"builtin": "/usr/bin/python3", "weasyprint": "/opt/bin/weasyprint"}
        with mock.patch.object(external, "available_engines", return_value=found):
            _, out, _ = self.run_cli("doctor", str(self.root), "--offline",
                                     "--api-key", "k")
            _, data, _ = self.run_cli("doctor", str(self.root), "--offline",
                                      "--api-key", "k", "--json")
        self.assertIn("ok pdf-engines", out)
        self.assertIn("builtin, weasyprint (/opt/bin/weasyprint)", out)
        self.assertNotIn("!! pdf engine", out)
        self.assertEqual(json.loads(data)["pdf_engines"], found)

    def doctor(self, *argv: str, env: dict[str, str] | None = None) -> tuple[int, str]:
        """`doctor` on a healthy machine that has only the builtin engine."""
        healthy = Report((Capability("python", True, "3.11"),))
        with mock.patch.object(external, "available_engines",
                               return_value={"builtin": "/usr/bin/python3"}), \
                mock.patch.object(capabilities, "probe_all", return_value=healthy):
            code, out, _ = self.run_cli("doctor", str(self.root), "--offline",
                                        "--api-key", "k", *argv, env=env)
        return code, out

    def test_a_missing_engine_blocks_when_pdf_is_configured(self) -> None:
        # Regression: doctor exited 0 where generate exits 2, and --json
        # carried neither the setting nor the problem.
        (self.root / "devdocs.toml").write_text(
            'formats = ["markdown", "pdf"]\npdf_engine = "chrome"\n', "utf-8")
        code, out = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("!! pdf engine           chrome is configured but not "
                      "installed", out)
        code, out = self.doctor("--json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual(data["formats"], ["markdown", "pdf"])
        self.assertEqual(data["pdf_engine"], {"configured": "chrome", "path": None})

    def test_a_missing_engine_is_no_problem_without_pdf(self) -> None:
        code, out = self.doctor(env={"DEVDOCS_PDF_ENGINE": "weasyprint"})
        self.assertEqual(code, 0)
        self.assertNotIn("pdf engine", out)

    def test_an_empty_format_list_is_a_config_error(self) -> None:
        (self.root / "devdocs.toml").write_text("formats = []\n", "utf-8")
        code, out = self.doctor()
        self.assertEqual(code, 2)
        self.assertIn("!! config               no output format configured", out)

    def test_models_marks_what_the_key_can_reach(self) -> None:
        code, out, _ = self.run_cli("models", "--api-key", "k", "--json")
        self.assertEqual(code, 0)
        rows = {row["id"]: row["available"] for row in json.loads(out)["catalogued"]}
        self.assertTrue(rows["gemini-2.5-flash-lite"])
        self.assertFalse(rows["gemini-3.1-flash-lite"])


def as_from_a_terminal(case: unittest.TestCase) -> None:
    """Give SIGINT and SIGTERM the handling a process started from a terminal
    has, until `case` ends: a test run in the background inherits SIGINT
    ignored, which devdocs, as Python does, leaves ignored."""
    for signum, handler in ((signal.SIGINT, signal.default_int_handler),
                            (signal.SIGTERM, signal.SIG_DFL)):
        case.addCleanup(signal.signal, signum, signal.signal(signum, handler))


class TestInterrupts(unittest.TestCase):
    """The handler itself, called by hand: no signal is sent."""

    def setUp(self) -> None:
        as_from_a_terminal(self)
        self.now = 100.0
        self.halted: list[int] = []
        self.err = io.StringIO()
        self.interrupts = self.make(self.err)

    def make(self, stderr: TextIO) -> cli._Interrupts:
        """Interrupts on a clock the test turns, whose halt is recorded."""
        out = cli._Output(stdout=io.StringIO(), stderr=stderr)
        return cli._Interrupts(out, clock=lambda: self.now, halt=self.halted.append)

    def test_outside_a_paid_run_the_first_signal_stops_at_once(self) -> None:
        for signum, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signum.name):
                interrupts = self.make(io.StringIO())
                with self.assertRaises(KeyboardInterrupt):
                    interrupts.handle(signum, None)
                self.assertFalse(interrupts.event.is_set())
                self.assertEqual((interrupts.code, self.halted), (code, []))

    def test_in_a_paid_run_the_first_signal_asks_the_run_to_stop(self) -> None:
        with self.interrupts.paying():
            self.interrupts.handle(signal.SIGTERM, None)
        self.assertTrue(self.interrupts.event.is_set())
        self.assertEqual((self.interrupts.code, self.halted), (143, []))
        self.assertIn("interrupt again to stop at once", self.err.getvalue())

    def test_a_second_delivery_within_a_second_is_the_same_request(self) -> None:
        with self.interrupts.paying():
            self.interrupts.handle(signal.SIGINT, None)
            self.now += 0.9
            self.interrupts.handle(signal.SIGINT, None)   # the wrapper's copy
            self.assertEqual(self.halted, [])
            self.now += 0.2
            self.interrupts.handle(signal.SIGINT, None)
        self.assertEqual(self.halted, [130])
        self.assertEqual(self.err.getvalue().count("stopped at once"), 1)

    def test_a_later_signal_outside_a_run_claims_no_answers(self) -> None:
        # Regression: it spoke of answers cached, in a command that bought none.
        with self.assertRaises(KeyboardInterrupt):
            self.interrupts.handle(signal.SIGINT, None)
        self.now += 2
        self.interrupts.handle(signal.SIGINT, None)
        self.assertEqual(self.halted, [130])
        self.assertEqual(self.err.getvalue(), "warning: stopped at once\n")

    def test_a_later_signal_once_a_run_has_paid_says_what_it_kept(self) -> None:
        with self.interrupts.paying():
            pass
        with self.assertRaises(KeyboardInterrupt):   # as the summary is printed
            self.interrupts.handle(signal.SIGTERM, None)
        self.now += 2
        self.interrupts.handle(signal.SIGTERM, None)
        self.assertEqual(self.halted, [143])
        self.assertIn("warning: stopped at once; the answers received are cached",
                      self.err.getvalue())

    def test_a_doubled_ctrl_c_outside_a_run_raises_once(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self.interrupts.handle(signal.SIGINT, None)
        self.now += 0.01
        self.interrupts.handle(signal.SIGINT, None)   # not again, mid-cleanup
        self.assertEqual(self.halted, [])

    def test_handlers_are_installed_for_the_command_then_restored(self) -> None:
        both = (signal.SIGINT, signal.SIGTERM)
        before = [signal.getsignal(signum) for signum in both]
        with self.interrupts.handling():
            for signum in both:
                self.assertEqual(signal.getsignal(signum), self.interrupts.handle)
        self.assertEqual([signal.getsignal(signum) for signum in both], before)
        folder = self.enterContext(tempfile.TemporaryDirectory())
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["init", folder]), 0)
        self.assertEqual([signal.getsignal(signum) for signum in both], before)

    def test_a_line_the_stream_refuses_goes_to_its_descriptor(self) -> None:
        # The signal came while the main thread was printing to stderr, and
        # the stream refuses a second writer then.
        read, write = os.pipe()
        with open(read, encoding="utf-8") as pipe, \
                open(write, "w", encoding="utf-8") as stream:
            interrupts = self.make(stream)
            busy = RuntimeError("reentrant call inside <_io.BufferedWriter>")
            with mock.patch.object(cli._Output, "warn", side_effect=busy), \
                    interrupts.paying():
                interrupts.handle(signal.SIGINT, None)
                self.now += 2
                interrupts.handle(signal.SIGINT, None)
            stream.close()
            text = pipe.read()
        self.assertEqual(self.halted, [130])
        self.assertIn("warning: stopped at once;", text)

    def test_a_line_that_cannot_be_written_never_keeps_a_stop_from_stopping(
        self,
    ) -> None:
        # Regression: only a reentrant print was caught, so any other failure
        # raised from the handler, before a second request could stop at once.
        closed = ValueError("I/O operation on closed file.")
        with mock.patch.object(cli._Output, "info", side_effect=closed), \
                mock.patch.object(cli._Output, "warn", side_effect=closed), \
                self.interrupts.paying():
            self.interrupts.handle(signal.SIGINT, None)
            self.now += 2
            self.interrupts.handle(signal.SIGINT, None)
        self.assertTrue(self.interrupts.event.is_set())
        self.assertEqual(self.halted, [130])

    def test_a_signal_amid_a_wait_on_the_stop_still_sets_it(self) -> None:
        # Regression: the handler set the stop itself, taking the lock that a
        # wait on it holds as it starts and ends. A signal there, on a main
        # thread waiting on the stop as the embedding limiter does, deadlocked.
        def interrupted() -> None:
            with self.interrupts.event._cond:   # as inside Event.wait
                self.interrupts.handle(signal.SIGINT, None)

        with self.interrupts.paying():
            thread = threading.Thread(target=interrupted, daemon=True)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(self.interrupts.event.wait(5))

    @unittest.skipIf(sys.platform == "win32", "POSIX signal masks")
    def test_the_thread_that_sets_the_stop_leaves_signals_to_the_main_one(
        self,
    ) -> None:
        # As the pool's workers do: a signal that thread took instead would
        # leave the main thread's wait, and so the stop, asleep.
        masks: list[set[signal.Signals]] = []

        class Seen(threading.Event):
            def set(self) -> None:
                masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, []))
                super().set()

        self.interrupts.event = Seen()
        with self.interrupts.paying():
            self.interrupts.handle(signal.SIGINT, None)
        (mask,) = masks
        self.assertLessEqual({signal.SIGINT, signal.SIGTERM}, mask)

    def test_an_ignored_signal_stays_ignored(self) -> None:
        previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        with self.interrupts.handling():
            self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
            self.assertEqual(signal.getsignal(signal.SIGINT), self.interrupts.handle)

    def test_off_the_main_thread_nothing_is_installed(self) -> None:
        seen: list[object] = []

        def command() -> None:
            with self.interrupts.handling():
                seen.append(signal.getsignal(signal.SIGINT))

        thread = threading.Thread(target=command)
        thread.start()
        thread.join(5)
        self.assertEqual(seen, [signal.getsignal(signal.SIGINT)])


class SignalsMidRun(TaggedModel):
    """Sends `signum` to this process while shop/cli.py's call is on the wire
    and shop/cart.py's waits out a 30 s Retry-After; answers cli.py once the
    run's stop is set, as a reply that arrives after the signal."""

    def __init__(self, signum: int) -> None:
        super().__init__()
        self.signum = signum
        self.limited = threading.Event()
        self.stop = threading.Event()   # the run's, once the CLI made it
        self.asked: list[str] = []

    def generate(self, request: Request) -> Completion:
        self.asked.append(request.tag)
        if request.tag == "shop/cart.py" and not self.limited.is_set():
            self.limited.set()
            raise RateLimited("slow down", retry_after=30, status=429)
        if request.tag == "shop/cli.py":
            self.limited.wait(5)
            os.kill(os.getpid(), self.signum)
            self.stop.wait(10)
        return super().generate(request)


@unittest.skipIf(sys.platform == "win32", "sends POSIX signals")
class TestStopSignals(CliCase):
    """Real signals, end to end."""

    def setUp(self) -> None:
        super().setUp()
        as_from_a_terminal(self)
        # Without devdocs' handler, SIGTERM would end the test run itself.
        signal.signal(signal.SIGTERM, self.unhandled)

    @staticmethod
    def unhandled(signum: int, frame: FrameType | None) -> None:
        raise AssertionError("SIGTERM reached the tests, not devdocs")

    def stopped_by(self, signum: int) -> tuple[int, dict[str, Any], str]:
        model = SignalsMidRun(signum)
        real = cli._client

        def client(*args: Any, **kwargs: Any) -> LLMClient:
            made = real(*args, **kwargs)
            model.stop = made.stop
            return made

        started = time.monotonic()
        with mock.patch.object(cli, "_client", client):
            code, out, err = self.run_cli("generate", str(self.root), "--api-key",
                                          "k", "--concurrency", "2", "--json",
                                          model=model)
        # Well under the 30 s Retry-After; a stop seen late would show as the
        # 10 s the model waits for it.
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(sorted(model.asked), ["shop/cart.py", "shop/cli.py"])
        return code, json.loads(out), err

    def check_written_and_recorded(self, summary: dict[str, Any], err: str) -> None:
        self.assertEqual(summary["stopped"], "interrupted")
        self.assertIn("stopping: requests already sent finish", err)
        docs = self.root / "docs"
        self.assertIn(PROSE, (docs / "shop" / "cli.py.md").read_text("utf-8"))
        self.assertNotIn(PROSE, (docs / "shop" / "cart.py.md").read_text("utf-8"))
        self.assertTrue((docs / "README.md").is_file())
        with Database(self.root / ".devdocs" / "devdocs.db") as db:
            (run,) = db.query("SELECT status FROM run")
        self.assertEqual(run["status"], "interrupted")

        # cli.py's answer, which came after the signal, was cached; cart.py's
        # call, in its backoff when the stop came, was never sent again.
        self.assertEqual(self.run_cli("generate", str(self.root), "--api-key", "k",
                                      "--quiet")[0], 0)
        self.assertEqual(sorted(r.tag for r in self.model.calls),
                         ["shop", "shop/__init__.py", "shop/cart.py", "shop/money.py"])

    def test_ctrl_c_mid_run_stops_it_gracefully(self) -> None:
        code, summary, err = self.stopped_by(signal.SIGINT)
        self.assertEqual(code, 130)
        self.check_written_and_recorded(summary, err)

    def test_sigterm_mid_run_stops_it_gracefully(self) -> None:
        code, summary, err = self.stopped_by(signal.SIGTERM)
        self.assertEqual(code, 143)
        self.check_written_and_recorded(summary, err)

    def test_a_signal_while_the_pages_are_written_lets_them_be(self) -> None:
        # Regression: the write was abandoned, every page with it.
        class Signalled(MarkdownRenderer):
            def render(self, docset: DocSet) -> dict[str, bytes]:
                os.kill(os.getpid(), signal.SIGINT)
                return super().render(docset)

        with mock.patch.object(generate_module, "renderer_for",
                               lambda fmt, **options: Signalled()):
            code, out, err = self.run_cli("generate", str(self.root), "--api-key",
                                          "k", "--json")
        docs = self.root / "docs"
        self.assertTrue((docs / "index.json").is_file())
        self.assertIn(PROSE, (docs / "shop" / "cart.py.md").read_text("utf-8"))
        self.assertEqual(code, 130)
        self.assertIn("stopping:", err)
        self.assertEqual(json.loads(out)["stopped"], "")   # nothing left to cut
        with Database(self.root / ".devdocs" / "devdocs.db") as db:
            (run,) = db.query("SELECT status FROM run")
        self.assertEqual(run["status"], "finished")

    def test_a_second_ctrl_c_a_second_later_stops_at_once(self) -> None:
        script = textwrap.dedent("""\
            import signal
            import sys
            import threading
            from unittest import mock

            from devdocs import cli
            from tests.test_generate import TaggedModel

            # as a terminal starts it
            signal.signal(signal.SIGINT, signal.default_int_handler)

            class Hanging(TaggedModel):
                def generate(self, request):
                    if request.tag == "shop/cli.py":   # never answers in time
                        print("on the wire", file=sys.stderr, flush=True)
                        threading.Event().wait(60)
                    return super().generate(request)

            with mock.patch.object(cli, "make_provider", return_value=Hanging()):
                sys.exit(cli.main(["generate", sys.argv[1], "--api-key", "k",
                                   "--concurrency", "1"]))
        """)
        kept = {k: v for k, v in os.environ.items() if not k.startswith(_KEYS)}
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(self.root)],
            cwd=Path(__file__).resolve().parents[1], env=kept, text=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.addCleanup(child.kill)
        watchdog = threading.Timer(60, child.kill)
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        stderr = child.stderr
        assert stderr is not None
        self.addCleanup(stderr.close)

        def read_until(text: str) -> None:
            for line in stderr:
                if text in line:
                    return
            self.fail(f"devdocs ended before printing {text!r}")

        read_until("on the wire")
        child.send_signal(signal.SIGINT)
        read_until("stopping:")
        time.sleep(1.2)   # past one second: a new request, not the same delivered twice
        started = time.monotonic()
        child.send_signal(signal.SIGINT)
        rest = stderr.read()
        self.assertEqual(child.wait(10), 130)
        self.assertLess(time.monotonic() - started, 10)   # not the call's 60 s
        self.assertEqual(rest.count("stopped at once"), 1)
        self.assertFalse((self.root / "docs").exists())

        # cart.py was answered before cli.py's call went out: the next run
        # has it for free, and pays for what the stop kept from being asked.
        code, out, _ = self.run_cli("generate", str(self.root), "--api-key", "k",
                                    "--json", "--quiet")
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual((summary["resumed"], summary["cache_hits"]), (True, 1))
        self.assertEqual(sorted(r.tag for r in self.model.calls), [
            "shop", "shop/__init__.py", "shop/cli.py", "shop/money.py"])


def signals(signum: int = signal.SIGINT) -> Callable[..., Any]:
    """A stand-in for a step of a command, during which `signum` arrives."""
    def step(*args: object, **kwargs: object) -> None:
        os.kill(os.getpid(), signum)
        raise AssertionError("the signal did not end the command")

    return step


class SignalsAsked(TaggedModel):
    """Takes the signal while the provider is asked for its models."""

    def list_models(self) -> list[str]:
        signals()()
        return []


@unittest.skipIf(sys.platform == "win32", "sends POSIX signals")
class TestWhatAStopSays(CliCase):
    """What a stop that ends a command says of it, by real signals. Regression:
    every command said its documents were kept and a run would resume."""

    PIPELINE = generate_module.Pipeline

    def setUp(self) -> None:
        super().setUp()
        as_from_a_terminal(self)
        signal.signal(signal.SIGTERM, TestStopSignals.unhandled)

    def stopped(self, *argv: str, code: int = 130, model: Any = None) -> str:
        """What a stop made of `argv`, after its exit code is checked."""
        exit_code, _, err = self.run_cli(*argv, model=model)
        self.assertEqual(exit_code, code, err)
        (line,) = [line for line in err.splitlines() if "interrupted" in line]
        return line

    def test_estimate(self) -> None:
        with mock.patch.object(self.PIPELINE, "prepare", signals()):
            line = self.stopped("estimate", str(self.root))
        self.assertEqual(
            line, "warning: interrupted; nothing was sent, so nothing was spent")

    def test_doctor(self) -> None:
        with mock.patch.object(capabilities, "probe_all", signals()):
            line = self.stopped("doctor", str(self.root), "--offline")
        self.assertEqual(line, "warning: interrupted; the checks did not finish; "
                               "nothing was changed")

    def test_models(self) -> None:
        line = self.stopped("models", "--api-key", "k", model=SignalsAsked())
        self.assertEqual(line, "warning: interrupted; nothing was changed")

    def test_cache(self) -> None:
        with mock.patch.object(cli.ResponseCache, "stats", signals()):
            line = self.stopped("cache", "stats", str(self.root))
        self.assertEqual(line, "warning: interrupted; the cache is as it was")
        self.assertIn("0 cached responses", self.run_cli("cache", "stats",
                                                         str(self.root))[1])
        with mock.patch.object(cli.ResponseCache, "clear", signals()):
            line = self.stopped("cache", "clear", str(self.root))
        self.assertEqual(line, "warning: interrupted; the cache is empty or as it was; "
                               "`devdocs cache stats` tells which")

    def test_init_once_it_writes(self) -> None:
        config, ignore = self.root / "devdocs.toml", self.root / ".gitignore"
        ignore.write_bytes(b"build/\n")
        for owner, step in ((Path, "write_text"),):
            with self.subTest(step), mock.patch.object(owner, step, signals()):
                line = self.stopped("init", str(self.root), "--force")
                self.assertEqual(line, "warning: interrupted; its files may be "
                                       "unfinished; run it again with --force to "
                                       "finish them")
        self.assertEqual(ignore.read_bytes(), b"build/\n")
        code, _, err = self.run_cli("init", str(self.root), "--force")   # as it says
        self.assertEqual(code, 0, err)
        self.assertIn("[devdocs]", config.read_text("utf-8"))
        self.assertEqual(ignore.read_bytes(), b"build/\n.devdocs/\n")

    def test_export(self) -> None:
        with mock.patch.object(self.PIPELINE, "export", signals()):
            line = self.stopped("export", str(self.root))
        self.assertEqual(line, "warning: interrupted; each file written so far is "
                               "whole; export again for the rest, for free")

    def test_generate_before_anything_is_bought(self) -> None:
        for step in ("prepare",):
            with self.subTest(step), mock.patch.object(self.PIPELINE, step, signals()):
                line = self.stopped("generate", str(self.root), "--api-key", "k")
                self.assertEqual(line, "warning: interrupted; nothing was bought and "
                                       "no documents were written")
        line = self.stopped("generate", str(self.root), "--api-key", "k",
                            model=SignalsAsked())
        self.assertIn("nothing was bought", line)
        self.assertEqual(self.model.calls, [])

    def test_generate_once_it_has_paid(self) -> None:
        # The pages are written; the summary was being printed.
        with mock.patch.object(cli, "_warnings", signals()):
            line = self.stopped("generate", str(self.root), "--api-key", "k")
        self.assertEqual(line, "warning: interrupted; documents written so far are "
                               "kept, run again to resume")
        self.assertTrue((self.root / "docs" / "index.json").is_file())
        self.assertGreater(len(self.model.calls), 0)

    def test_generate_stopped_by_an_interrupt_no_handler_took(self) -> None:
        self.model.interrupt_after = 1
        line = self.stopped("generate", str(self.root), "--api-key", "k",
                            "--concurrency", "1")
        self.assertIn("documents written so far are kept", line)

    def test_sigterm_keeps_its_own_status(self) -> None:
        with mock.patch.object(self.PIPELINE, "prepare", signals(signal.SIGTERM)):
            line = self.stopped("estimate", str(self.root), code=143)
        self.assertIn("nothing was sent", line)
        with mock.patch.object(self.PIPELINE, "prepare", signals(signal.SIGTERM)):
            line = self.stopped("generate", str(self.root), "--api-key", "k", code=143)
        self.assertIn("nothing was bought", line)


if __name__ == "__main__":
    unittest.main()
