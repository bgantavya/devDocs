"""The command line end to end: commands, output, exit codes. No network: the
provider factory is replaced by the scripted model the pipeline tests use."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import select
import shutil
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

from devdocs import __version__, capabilities, cli, demo
from devdocs import generate as generate_module
from devdocs.capabilities import Capability, Report
from devdocs.config import load_config
from devdocs.errors import (
    AuthError,
    ConfigError,
    DevDocsError,
    ModelNotFound,
    ProviderError,
    QuotaExhausted,
    RateLimited,
    StorageError,
)
from devdocs.index.db import Database
from devdocs.llm import Completion, Request
from devdocs.llm.client import LLMClient
from devdocs.render.markdown import MarkdownRenderer
from devdocs.render.model import DocSet
from devdocs.render.pdf import external
from devdocs.render.pdf.check import PdfFile, check
from tests.test_generate import MORE, PROJECT, PROSE, TaggedModel

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


class TestArgumentErrors(unittest.TestCase):
    """No argument error shows an API key, however the line spells it."""

    KEY = "AIza-SECRET-0123"

    def refused(self, *argv: str) -> str:
        """The stderr of a command line argparse refuses, exiting 2."""
        err = io.StringIO()
        with clean_env(), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as caught:
            cli.main(list(argv))
        self.assertEqual(caught.exception.code, 2)
        return err.getvalue()

    def test_the_key_is_masked_however_it_is_spelled(self) -> None:
        # Regression: argparse echoed the arguments it did not take, and a
        # value it refused, as typed.
        key = self.KEY
        for argv, shown in (
            (["generate", ".", "--", "--api-key", key],
             "unrecognized arguments: --api-key ***\n"),
            (["generate", ".", "--", f"--api-key={key}"],
             "unrecognized arguments: --api-key=***\n"),
            (["generate", ".", "--", f"--api={key}"],
             "unrecognized arguments: --api=***\n"),
            (["generate", ".", "--", "--a", key], "unrecognized arguments: --a ***\n"),
            (["export", ".", "--api-key", key],
             "unrecognized arguments: --api-key ***\n"),
            (["estimate", ".", "--api-key", key],      # it sends nothing
             "unrecognized arguments: --api-key ***\n"),
            (["estimate", ".", f"--api-key={key}"],
             "unrecognized arguments: --api-key=***\n"),
            ([f"--api-key={key}", "generate", "."],
             "unrecognized arguments: --api-key=***\n"),
            (["--api-key", key, "generate", "."],
             "argument command: invalid choice: '***'"),
            (["--ap", key, "generate", "."], "argument command: invalid choice: '***'"),
            (["cache", "--api-key", key, "stats"],
             "devdocs cache: error: argument action: invalid choice: '***'"),
            (["-p", ".", "--", "--api-key", key],                # v3, then v4
             "unrecognized arguments: -- --api-key ***\n"),
            (["-p", ".", "--", f"--api-key={key}"],
             "unrecognized arguments: -- --api-key=***\n"),
        ):
            with self.subTest(argv=argv):
                err = self.refused(*argv)
                self.assertNotIn(key, err)
                self.assertIn(shown, err)
        # 3.11 takes `--` itself for the command; later versions what follows
        self.assertNotIn(key, self.refused("--", f"--api-key={key}"))

    def test_the_key_is_masked_behind_a_flag_argparse_does_not_know(self) -> None:
        # Regression: only what argparse takes for --api-key was masked, so a
        # key after a mistyped flag was shown whole, on a v3 line twice.
        key = self.KEY
        for flag in ("-api-key", "--api_key", "-api_key", "--apikey", "--API-KEY",
                     "---api-key", "--api.key", "--api-keys", "--gemini-api-key",
                     "--GEMINI_API_KEY", "--token", "--secret"):
            for line in (["generate", "."], ["-p", "."]):
                with self.subTest(flag=flag, line=line):
                    err = self.refused(*line, flag, key)
                    self.assertNotIn(key, err)
                    self.assertIn(f"unrecognized arguments: {flag} ***\n", err)
                    err = self.refused(*line, f"{flag}={key}")
                    self.assertNotIn(key, err)
                    self.assertIn(f"unrecognized arguments: {flag}=***\n", err)
        err = self.refused("generate", ".", "--key", key)   # in v3, --keywords
        self.assertNotIn(key, err)
        self.assertIn("unrecognized arguments: --key ***\n", err)
        for line in (["generate", "."], ["-p", "."]):
            with self.subTest(line=line):
                err = self.refused(*line, f"GEMINI_API_KEY={key}")   # as in a shell
                self.assertNotIn(key, err)
                self.assertIn("unrecognized arguments: GEMINI_API_KEY=***\n", err)

    def test_a_double_dash_does_not_part_the_key_from_its_flag(self) -> None:
        # Regression: the `--` was masked in the key's place, and the key shown.
        key = self.KEY
        for argv, shown in (
            (["export", ".", "--api-key", "--", key], "--api-key -- ***"),
            (["generate", ".", "--apikey", "--", key], "--apikey -- ***"),
            (["generate", ".", "--", "--api-key", "--", key], "--api-key -- ***"),
            (["-p", ".", "--api_key", "--", key], "--api_key -- ***"),
        ):
            with self.subTest(argv=argv):
                err = self.refused(*argv)
                self.assertNotIn(key, err)
                self.assertIn(f"unrecognized arguments: {shown}\n", err)

    def test_a_key_read_as_the_project_is_refused(self) -> None:
        # Regression: `--` made it the project's path, which the refusal named,
        # `doctor` on stdout.
        key = self.KEY
        for line in (["generate"], ["estimate"], ["export"], ["doctor"], ["init"],
                     ["cache", "stats"]):
            for flag in ("--api-key", "--api_key"):
                with self.subTest(line=line, flag=flag):
                    err = self.refused(*line, "--", f"{flag}={key}")
                    self.assertNotIn(key, err)
                    self.assertIn("argument path: expected a directory, got "
                                  f"{flag}=***\n", err)
        err = self.refused("generate", f"GEMINI_API_KEY={key}")   # as in a shell
        self.assertNotIn(key, err)
        self.assertIn("got GEMINI_API_KEY=***\n", err)

    def test_a_path_with_an_equals_sign_is_still_a_path(self) -> None:
        for path in ("a=b", "api=v2", "./key=1", "--out=docs"):
            with self.subTest(path=path):
                err = io.StringIO()
                with clean_env(), contextlib.redirect_stderr(err):
                    self.assertEqual(cli.main(["generate", "--", path]), 2)
                self.assertIn(f"{Path(path).name} does not exist", err.getvalue())

    def test_the_rest_of_the_message_stays_as_it_was(self) -> None:
        err = self.refused("generate", ".", "--api-key", "k", "--bogus", "keep")
        self.assertIn("devdocs: error: unrecognized arguments: --bogus keep\n", err)


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

    def test_a_v3_key_reaches_the_provider(self) -> None:
        err = io.StringIO()
        with contextlib.chdir(self.root), clean_env(), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err), \
                mock.patch.object(cli, "make_provider",
                                  return_value=self.model) as made:
            code = cli.main(["-p", ".", "--api-key", "AIza-SECRET"])
        self.assertEqual(code, 0, err.getvalue())
        made.assert_called_once_with("gemini", "AIza-SECRET")

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
        # Regression: the run paid for every call first, then refused.
        readme = self.root / "docs" / "README.md"
        readme.parent.mkdir()
        readme.write_text("# Our documentation\n", "utf-8")
        code, _, err = self.run_cli("generate", str(self.root), "--api-key", "k",
                                    "--quiet")
        self.assertEqual(code, 2)
        self.assertIn("has no record of writing: README.md\n"
                      "  hint: move it away or choose another --out; "
                      "--force replaces it\n", err)
        self.assertEqual(self.model.calls, [])
        self.assertEqual(sorted(p.name for p in readme.parent.iterdir()),
                         ["README.md"])
        self.assertEqual(readme.read_text("utf-8"), "# Our documentation\n")
        code, _, err = self.run_cli("generate", str(self.root), "--api-key", "k",
                                    "--quiet", "--force")
        self.assertEqual(code, 0, err)
        self.assertGreater(len(self.model.calls), 0)
        self.assertIn("Generated by devdocs", readme.read_text("utf-8"))

    def test_an_embedding_failure_is_warned_of_even_under_quiet(self) -> None:
        # Regression: the warning went to the progress lines --quiet silences.
        class DownEmbedder(TaggedModel):
            dimensions = 8

            def embed(self, texts: list[str], *,
                      query: bool = False) -> list[list[float]]:
                raise ProviderError("HTTP 500", status=500, retryable=False)

        said = ("warning: semantic context: embedding failed (HTTP 500); files "
                "without vectors get standard context, and the next run tries again\n")
        for quiet in (["--quiet"], []):
            with self.subTest(quiet=quiet):
                code, _, err = self.run_cli("generate", str(self.root), "--api-key",
                                            "k", "--context", "semantic", *quiet,
                                            model=DownEmbedder())
                self.assertEqual(code, 0, err)
                self.assertEqual(err.count(said), 1, err)
                if quiet:
                    self.assertEqual(err, said)

    def test_a_run_checks_its_output_once(self) -> None:
        pipeline = generate_module.Pipeline
        with mock.patch.object(pipeline, "check", autospec=True,
                               side_effect=pipeline.check) as checked:
            code, _, err = self.run_cli("generate", str(self.root), "--api-key", "k",
                                        "--quiet")
        self.assertEqual((code, checked.call_count), (0, 1), err)

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
        ((kind, item, error),) = summary["failed"]
        self.assertEqual((kind, item), ("format", "pdf"))
        self.assertIn("chrome could not print the book (exit status 1)\n"
                      "  hint: run chrome by hand to see why; `devdocs export ", error)
        self.assertIn(" --format pdf --pdf-engine chrome` makes it from the manifest, "
                      "with no key", error)
        docs = self.root / "docs"
        listed = json.loads((docs / "index.json").read_bytes())["files"]
        self.assertIn("README.md", listed)
        self.assertGreater(summary["requests"], 0)
        code, _, err = self.run_cli("export", str(self.root), "--format", "pdf")
        self.assertEqual(code, 0, err)
        self.assertEqual(check((docs / "shop.pdf").read_bytes()), [])


class TestFatalErrors(CliCase):
    """A failure that ends a command says `error:`, in argparse's form."""

    def failing(self, failure: Exception) -> tuple[int, str]:
        """The exit code and stderr of a generate whose model raises `failure`."""
        class Failing(TaggedModel):
            def list_models(self) -> list[str]:
                raise failure

        code, _, err = self.run_cli("generate", str(self.root), "--api-key", "k",
                                    "--quiet", model=Failing())
        return code, err

    def test_every_typed_failure_says_error(self) -> None:
        # Regression: a rejected key said "warning:", and the others neither.
        for failure, code in ((AuthError("rejected"), 3),
                              (ConfigError("refused"), 2),
                              (ProviderError("down"), 4),
                              (RateLimited("slow down"), 5),
                              (ModelNotFound("gone"), 6),
                              (QuotaExhausted("spent"), 10),
                              (StorageError("damaged"), 9),
                              (DevDocsError("failed"), 1)):
            with self.subTest(type(failure).__name__):
                exit_code, err = self.failing(failure)
                self.assertEqual(exit_code, code)
                self.assertIn(f"devdocs: error: {failure.message}", err)
                self.assertNotIn("warning:", err)

    def test_the_hint_follows_the_error(self) -> None:
        code, err = self.failing(AuthError("rejected", hint="check the key"))
        self.assertEqual(code, 3)
        self.assertEqual(err, "devdocs: error: rejected\n  hint: check the key\n")

    def test_a_config_error_from_the_command_itself_says_error(self) -> None:
        code, _, err = self.run_cli("generate", str(self.root))
        self.assertEqual(code, 2)
        self.assertIn("devdocs: error: no API key for provider 'gemini'\n", err)

    def test_a_bug_says_so(self) -> None:
        code, err = self.failing(ValueError("oops"))
        self.assertEqual(code, 1)
        self.assertEqual(err, "devdocs: internal error: ValueError: oops\n")


class TestHelp(unittest.TestCase):
    """What `--help` says, and that it says something for every option."""

    def setUp(self) -> None:
        (commands,) = [action.choices for action in cli.build_parser()._actions
                       if isinstance(action, argparse._SubParsersAction)]
        self.commands: dict[str, argparse.ArgumentParser] = dict(commands)

    def option(self, command: str, flag: str) -> argparse.Action:
        return self.commands[command]._option_string_actions[flag]

    def test_every_argument_of_every_command_has_help(self) -> None:
        # Regression: --max-requests and --concurrency had none, among others.
        for name, parser in self.commands.items():
            for action in parser._actions:
                with self.subTest(command=name, argument=action.dest):
                    self.assertTrue(action.help)
            self.assertIn("devdocs", parser.format_help())   # no stray % in a help

    def test_the_run_limits_say_what_they_limit(self) -> None:
        for command in ("generate", "estimate"):
            with self.subTest(command):
                requests = self.option(command, "--max-requests").help or ""
                self.assertIn("retries included", requests)
                self.assertIn("no limit", requests)
                self.assertIn("default: 4", self.option(command, "--concurrency").help)

    def test_include_is_no_pattern_and_says_so(self) -> None:
        # Regression: PATTERN promised globs; it matches literal text.
        for command in ("generate", "estimate"):
            with self.subTest(command):
                option = self.option(command, "--include")
                self.assertEqual(option.metavar, "PATH")
                said = " ".join((option.help or "").split())
                for words in ("not a glob", "path from the project root",
                              "name at any depth", "end of a path"):
                    self.assertIn(words, said)

    def test_include_says_what_it_cannot_take_in(self) -> None:
        # Regression: "force paths in, despite ignores" promised any path; see
        # TestEstimate.test_include_takes_in_source_alone.
        for command in ("generate", "estimate"):
            with self.subTest(command):
                said = " ".join((self.option(command, "--include").help or "").split())
                self.assertIn("take in source files that ignores, --exclude or the "
                              "size limit leave out", said)
                self.assertIn("tests, vendored and generated code and files that "
                              "are not source stay out", said)
        self.assertIn("# take source in despite ignores", cli._TEMPLATE)

    def test_exclude_says_it_reads_gitignore_lines(self) -> None:
        for command in ("generate", "estimate"):
            with self.subTest(command):
                option = self.option(command, "--exclude")
                self.assertEqual(option.metavar, "PATTERN")
                self.assertIn(".gitignore", option.help or "")
                self.assertIn("globs", option.help or "")

    def test_only_what_can_send_takes_a_key(self) -> None:
        # Regression: estimate took --api-key and ignored it.
        self.assertEqual(
            {name for name, parser in self.commands.items()
             if "--api-key" in parser._option_string_actions},
            {"generate", "doctor", "models"},
        )

    def test_the_path_says_where_it_comes_from_when_left_out(self) -> None:
        # Regression: "default: current directory", which comes last.
        given = {name: action.help for name, parser in self.commands.items()
                 for action in parser._actions if action.dest == "path"}
        self.assertEqual(given.pop("init"),
                         "project root (default: current directory)")
        self.assertEqual(given, dict.fromkeys(
            ("generate", "estimate", "export", "doctor", "cache"),
            "project root; else $DEVDOCS_PATH, the path in devdocs.toml, or the "
            "current directory"))


class TestEstimate(CliCase):
    def explained(self, *argv: str) -> str:
        """The files `estimate --explain` lists, ignoring shop/money.py."""
        (self.root / ".gitignore").write_text("shop/money.py\n", "utf-8")
        code, out, err = self.run_cli("estimate", str(self.root), "--explain", *argv)
        self.assertEqual(code, 0, err)
        return out

    def test_include_matches_as_its_help_says(self) -> None:
        self.assertNotIn("shop/money.py", self.explained())
        for literal in ("shop/money.py", "./shop/money.py", "SHOP/MONEY.PY",   # a path
                        "money.py", "shop",                  # a name, at any depth
                        ".py", "oney.py"):                   # the end of a path
            with self.subTest(literal):
                self.assertIn("shop/money.py", self.explained("--include", literal))
        for glob in ("*.py", "shop/*", "mon*", "?oney.py", "**/money.py"):
            with self.subTest(glob):
                self.assertNotIn("shop/money.py", self.explained("--include", glob))

    def test_include_reaches_inside_an_excluded_directory_by_path_alone(self) -> None:
        out = self.explained("--exclude", "shop/", "--include", "shop/cart.py")
        self.assertIn("shop/cart.py", out)
        self.assertNotIn("shop/cli.py", out)
        out = self.explained("--exclude", "shop/", "--include", "cart.py")
        self.assertNotIn("shop/cart.py", out)

    def test_include_takes_in_source_alone(self) -> None:
        # What --help says. The scanner lets any named file through; the plan
        # keeps only source.
        for relpath in ("data.xyz", "NOTES.md", "settings.toml", "tests/test_x.py",
                        "vendor/w.py", "x_pb2.py", "stamped.py"):
            path = self.root / relpath
            path.parent.mkdir(exist_ok=True)
            path.write_text("# @generated\ndef f():\n    return 1\n", "utf-8")
        for named in ("data.xyz", ".xyz", "NOTES.md", "settings.toml",
                      "tests/test_x.py", "tests", "vendor/w.py", "vendor", "x_pb2.py",
                      "stamped.py"):
            with self.subTest(named):
                out = self.explained("--include", named)
                for relpath in ("data.xyz", "NOTES.md", "settings.toml", "test_x.py",
                                "w.py", "x_pb2.py", "stamped.py"):
                    self.assertNotIn(relpath, out)

        big = self.root / "big.py"                                # over the size limit
        big.write_text(f"# {'x' * 1_000_001}\ndef f():\n    return 1\n", "utf-8")
        self.assertNotIn("big.py", self.explained())
        self.assertIn("big.py", self.explained("--include", "big.py"))
        built = self.root / "build" / "made.py"           # in a default-ignored folder
        built.parent.mkdir()
        built.write_text("def f():\n    return 1\n", "utf-8")
        self.assertNotIn("build/made.py", self.explained())
        self.assertIn("build/made.py", self.explained("--include", "build/made.py"))
        query = self.root / "q.sql"                                 # the help's example
        query.write_text("SELECT 1;\n", "utf-8")
        (self.root / ".gitignore").write_text("*.sql\n", "utf-8")
        code, out, err = self.run_cli("estimate", str(self.root), "--explain")
        self.assertEqual(code, 0, err)
        self.assertNotIn("q.sql", out)
        code, out, err = self.run_cli("estimate", str(self.root), "--explain",
                                      "--include", ".sql")
        self.assertEqual(code, 0, err)
        self.assertIn("q.sql", out)

    def test_exclude_matches_as_its_help_says(self) -> None:
        for pattern, gone, kept in (("shop/c*.py", "cart.py", "__init__.py"),   # glob
                                    ("c?rt.py", "cart.py", "cli.py"),      # any depth
                                    ("/cart.py", "", "cart.py"),      # at the root only
                                    ("cart.py/", "", "cart.py"),   # directories only
                                    ("**/cli.py", "cli.py", "cart.py")):
            with self.subTest(pattern):
                out = self.explained("--exclude", pattern)
                if gone:
                    self.assertNotIn(f"shop/{gone}", out)
                self.assertIn(f"shop/{kept}", out)

    def test_estimate_needs_no_key_and_sends_nothing(self) -> None:
        code, out, _ = self.run_cli("estimate", str(self.root), "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertGreater(data["requests"], 0)
        self.assertGreater(data["worst_case_cost_usd"], 0)
        self.assertEqual(self.model.calls, [])

    def test_a_first_estimate_makes_no_state(self) -> None:
        # Regression: it made .devdocs/devdocs.db, and with --context semantic
        # synced the chunks into it.
        state = self.root / ".devdocs"
        for argv in ([], ["--context", "semantic"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli("estimate", str(self.root), "--quiet",
                                              *argv)
                self.assertEqual(code, 0, err)
                self.assertIn(", 0 already cached\n", out)
                self.assertFalse(state.exists())
        code, _, err = self.run_cli("generate", str(self.root), "--api-key", "k",
                                    "--quiet")
        self.assertEqual(code, 0, err)
        with Database(state / "devdocs.db") as db:
            runs = [row["status"] for row in db.query("SELECT status FROM run")]
        self.assertEqual(runs, ["finished"])
        code, out, err = self.run_cli("estimate", str(self.root), "--quiet")
        self.assertEqual(code, 0, err)
        self.assertIn("\n0 model calls (none), 5 already cached\n", out)

    def priced(self, *argv: str) -> tuple[str, dict[str, Any]]:
        """`estimate`'s result lines, and its JSON for the same command."""
        code, out, err = self.run_cli("estimate", str(self.root), "--quiet", *argv)
        self.assertEqual(code, 0, err)
        code, data, err = self.run_cli("estimate", str(self.root), "--quiet", "--json",
                                       *argv)
        self.assertEqual(code, 0, err)
        return out, json.loads(data)

    def test_the_json_keeps_each_cost_under_its_own_name(self) -> None:
        # Regression: max_cost_usd held the plan's worst case, not the ceiling.
        for argv, cost, requests in (([], 0.5, None),
                                     (["--max-cost", "7"], 7.0, None),
                                     (["--max-cost", "0"], None, None),
                                     (["--max-requests", "2"], 0.5, 2),
                                     (["--max-cost", "0", "--max-requests", "0"],
                                      None, None)):
            with self.subTest(argv=argv):
                _, data = self.priced(*argv)
                self.assertEqual((data["cost_limit_usd"], data["request_limit"]),
                                 (cost, requests))
                self.assertNotIn("max_cost_usd", data)
                self.assertEqual(data["worst_case_cost_usd"], round(
                    sum(i["cost"] for i in data["items"] if not i["cached"]), 6))

    def test_the_json_keeps_every_other_key(self) -> None:
        _, data = self.priced("--max-requests", "2")
        self.assertLessEqual({"files", "prose_files", "requests", "cached",
                              "input_tokens", "output_tokens", "demoted",
                              "demoted_by", "items"},
                             set(data))
        self.assertEqual(data["files"], 4)
        self.assertEqual(len(data["demoted"]), 3)

    def costs(self) -> tuple[float, list[float]]:
        """With no limit: the module's worst case, then its files', as they are
        walked after it. The module costs the most: the tests below need that."""
        _, data = self.priced("--max-cost", "0")
        module = next(i["cost"] for i in data["items"] if i["kind"] == "module")
        files = [i["cost"] for i in data["items"] if i["kind"] == "file"]
        self.assertGreater(module, sum(files))
        return module, files

    def cut(self, *argv: str) -> tuple[str, list[str]]:
        """The line on what `argv` left as stubs, and which limits --json names."""
        out, data = self.priced(*argv)
        (line,) = [line for line in out.splitlines() if "to fit" in line]
        return line, data["demoted_by"]

    def test_the_limit_that_made_the_cut_is_named(self) -> None:
        # Regression: it was always --max-cost, also with the requests to blame.
        module, files = self.costs()
        room = 0.00001
        spare = module + files[0] + room       # holds the first two items, no more
        for argv, line, named in (
            (["--max-requests", "2"],          # beside the default cost limit
             "3 items left as stubs to fit --max-requests 2", ["max_requests"]),
            (["--max-requests", "2", "--max-cost", "0"],
             "3 items left as stubs to fit --max-requests 2", ["max_requests"]),
            (["--max-requests", "1", "--max-cost", f"{spare}"],
             "4 items left as stubs to fit --max-requests 1", ["max_requests"]),
            (["--max-cost", "0.0000001"],
             "5 items left as stubs to fit --max-cost $1e-07", ["max_cost"]),
            (["--max-cost", "0.0000001", "--max-requests", "2"],
             "5 items left as stubs to fit --max-cost $1e-07", ["max_cost"]),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.cut(*argv), (line, named))

    def test_what_cost_cut_before_the_requests_ran_out_is_named_too(self) -> None:
        # The module is over the cost limit, then two files spend the requests.
        _, files = self.costs()
        limit = f"{files[0] + files[1] + 0.00001:.8f}"
        line, named = self.cut("--max-cost", limit, "--max-requests", "2")
        self.assertEqual(line, "3 items left as stubs to fit "
                               f"--max-cost ${float(limit):g} and --max-requests 2")
        self.assertEqual(named, ["max_cost", "max_requests"])

    def test_a_request_limit_nothing_passed_is_not_named(self) -> None:
        _, files = self.costs()
        # All it cut is the module, before the fourth file spent the requests.
        limit = f"{sum(files) + 0.00001:.8f}"
        line, named = self.cut("--max-cost", limit, "--max-requests", "4")
        self.assertEqual(line, "1 items left as stubs to fit "
                               f"--max-cost ${float(limit):g}")
        self.assertEqual(named, ["max_cost"])
        # Past the last file kept, too, the cost made the cut: requests are left.
        limit = f"{files[0] + 0.00001:.8f}"
        line, named = self.cut("--max-cost", limit, "--max-requests", "5")
        self.assertIn(f"to fit --max-cost ${float(limit):g}", line)
        self.assertEqual(named, ["max_cost"])

    def test_nothing_cut_names_nothing(self) -> None:
        out, data = self.priced("--max-requests", "50")
        self.assertNotIn("to fit", out)
        self.assertEqual((data["demoted"], data["demoted_by"]), ([], []))

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


class TestLimitsCountCalls(CliCase):
    """A limit counts the calls a run makes, never an answer the cache holds:
    a shop of six files, run once, then two of its files changed.
    Regression: each cached answer took a request, so `estimate --max-requests
    1` planned 4 calls and cut 3 cached pages, and `generate` stopped on the
    budget (exit 7) after its one call."""

    BASE = ("--context", "off", "--paid")
    CACHED = ("shop/__init__.py", "shop/money.py", "shop/orders.py", "shop/tax.py")

    def setUp(self) -> None:
        super().setUp()
        for relpath, text in MORE.items():
            (self.root / relpath).write_text(text, "utf-8")
        code, _, err = self.run_cli("generate", str(self.root), "--api-key", "k",
                                    "--quiet", *self.BASE)
        self.assertEqual(code, 0, err)
        for relpath in ("shop/cart.py", "shop/cli.py"):
            path = self.root / relpath
            path.write_text(path.read_text("utf-8") + "\n# changed\n", "utf-8")
        self.model.calls.clear()

    def test_estimate_cuts_calls_alone(self) -> None:
        calls = [["module", "shop"], ["file", "shop/cart.py"], ["file", "shop/cli.py"]]
        for limit, cut in (("1", calls), ("2", calls[1:])):
            with self.subTest(max_requests=limit):
                argv = ("estimate", str(self.root), "--quiet", *self.BASE,
                        "--max-requests", limit)
                code, out, err = self.run_cli(*argv)
                self.assertEqual(code, 0, err)
                self.assertIn(f"{len(cut)} items left as stubs to fit --max-requests "
                              f"{limit}\n", out)
                code, data, err = self.run_cli(*argv, "--json")
                self.assertEqual(code, 0, err)
                priced = json.loads(data)
                self.assertEqual((priced["demoted"], priced["demoted_by"]),
                                 (cut, ["max_requests"]))
                self.assertEqual((priced["requests"], priced["cached"]),
                                 (int(limit), 4))
                self.assertEqual(tuple(sorted(item["key"] for item in priced["items"]
                                              if item["cached"])), self.CACHED)

    def test_both_limits_cut_calls_alone(self) -> None:
        # The overview is over the cost; the module spends the one request.
        argv = ("estimate", str(self.root), "--quiet", "--json", *self.BASE)
        code, data, err = self.run_cli(*argv, "--max-cost", "0")
        self.assertEqual(code, 0, err)
        cost = {item["kind"]: item["cost"] for item in json.loads(data)["items"]
                if item["kind"] != "file"}
        self.assertGreater(cost["architecture"], cost["module"])
        limit = f"{(cost['architecture'] + cost['module']) / 2:.8f}"
        code, data, err = self.run_cli(*argv, "--max-cost", limit, "--max-requests",
                                       "1")
        self.assertEqual(code, 0, err)
        priced = json.loads(data)
        self.assertEqual((priced["demoted"], priced["demoted_by"]), (
            [["architecture", ""], ["file", "shop/cart.py"], ["file", "shop/cli.py"]],
            ["max_cost", "max_requests"]))
        self.assertEqual((priced["requests"], priced["cached"]), (1, 4))

    def test_generate_on_the_limit_writes_every_cached_page(self) -> None:
        code, out, err = self.run_cli("generate", str(self.root), "--api-key", "k",
                                      *self.BASE, "--max-requests", "1")
        self.assertEqual(code, 0, err)   # as any run that its plan fits ends
        self.assertEqual([request.tag for request in self.model.calls],
                         ["architecture"])
        self.assertIn("\n1 model call", out)
        self.assertIn(", 4 cache hits, ", out)
        self.assertNotIn("stopped early", out)
        for relpath in self.CACHED:
            page = self.root / "docs" / f"{relpath}.md"
            self.assertIn(PROSE, page.read_text("utf-8"), relpath)


class TestEstimateAndGenerateAgree(CliCase):
    """`generate` makes the calls `estimate` plans, also where a cached
    overview sums up an answer the run buys: the run rebuilds its prompt, so
    it is a call. Regression: it was planned free; `generate` bought the file,
    was refused the module's call and stopped (exit 7), the page a stub."""

    BASE = ("--context", "off", "--paid")

    def planned(self, *limits: str) -> list[str]:
        code, out, err = self.run_cli("estimate", str(self.root), "--quiet", "--json",
                                      *self.BASE, *limits)
        self.assertEqual(code, 0, err)
        return [item["key"] or item["kind"] for item in json.loads(out)["items"]
                if not item["cached"]]

    def bought(self, *limits: str) -> list[str]:
        """The calls of `generate`, which ends as a run its plan fits ends."""
        self.model.calls.clear()
        code, out, err = self.run_cli("generate", str(self.root), "--api-key", "k",
                                      *self.BASE, *limits)
        self.assertEqual(code, 0, err)
        self.assertNotIn("stopped early", out)
        return [request.tag for request in self.model.calls]

    def test_a_cached_module_whose_file_is_bought_is_a_call(self) -> None:
        self.assertEqual(self.bought("--max-requests", "1"), ["shop"])
        code, out, err = self.run_cli("estimate", str(self.root), "--quiet",
                                      *self.BASE, "--max-requests", "1")
        self.assertEqual(code, 0, err)
        # Each file would take the module along, past the one request.
        self.assertIn("0 model calls (none), 1 already cached\n", out)
        self.assertIn("4 items left as stubs to fit --max-requests 1\n", out)
        for limit, calls in (("1", []), ("2", ["shop/cart.py", "shop"])):
            with self.subTest(max_requests=limit):
                self.assertEqual(self.planned("--max-requests", limit), calls)
                self.assertEqual(self.bought("--max-requests", limit), calls)
                page = self.root / "docs" / "shop" / "index.md"
                self.assertIn("Holds the cart", page.read_text("utf-8"))

    def test_runs_resumed_under_one_request_end_as_planned(self) -> None:
        for relpath, text in MORE.items():
            (self.root / relpath).write_text(text, "utf-8")
        for number, call in enumerate(("architecture", "shop/cart.py")):
            with self.subTest(run=number + 1):
                self.assertEqual(self.planned("--max-requests", "1"), [call])
                self.assertEqual(self.bought("--max-requests", "1"), [call])
        overview = self.root / "docs" / "architecture.md"
        self.assertIn("A small shop", overview.read_text("utf-8"))


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

    def test_a_bad_path_is_refused_before_anything_is_written(self) -> None:
        # Regression: an internal error, or the current directory for an empty path.
        file = self.root / "pyproject.toml"
        for path, said in ((str(self.root / "no" / "such"), "does not exist"),
                           (str(file), "is not a directory"),
                           ("", "path is empty"),
                           ("a\0b", "path contains a NUL")):
            with self.subTest(path=path), contextlib.chdir(self.root):
                before = sorted(p.name for p in self.root.iterdir())
                code, _, err = self.run_cli("init", path)
                self.assertEqual(code, 2)
                self.assertIn(said, err)
                self.assertNotIn("internal error", err)
                self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)
        self.assertFalse((self.root / "no").exists())

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0, "POSIX modes bind no root")
    def test_a_directory_it_cannot_open_is_refused(self) -> None:
        self.root.chmod(0o000)
        self.addCleanup(self.root.chmod, 0o755)
        code, _, err = self.run_cli("init", str(self.root))
        self.assertEqual(code, 2)
        self.assertIn("cannot open project root", err)

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

    def test_only_generate_makes_state(self) -> None:
        # Regression: cache stats and cache clear made an empty .devdocs/devdocs.db.
        root, state = self.root.resolve(), self.root / ".devdocs"
        said = {("cache", "stats"): "0 cached responses, bought for $0.0000 in total\n",
                ("cache", "clear"): f"nothing to clear: {root} has no cache yet\n"}
        with contextlib.chdir(self.root):   # models takes no path
            for argv in (("estimate",), ("export",), ("doctor", "--api-key", "k"),
                         ("models", "--api-key", "k"), *said, ("init",)):
                with self.subTest(argv=argv):
                    _, out, err = self.run_cli(*argv)
                    made = state.exists()
                    shutil.rmtree(state, ignore_errors=True)   # so it fails alone
                    self.assertFalse(made, err)
                    self.assertEqual(out, said.get(argv, out))
            code, _, err = self.run_cli("generate", "--api-key", "k", "--quiet")
            self.assertEqual(code, 0, err)
            self.assertTrue((state / "devdocs.db").is_file())
            _, out, _ = self.run_cli("cache", "clear", "--json")
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

    def test_doctor_says_how_to_give_a_missing_key(self) -> None:
        # Regression: it showed the error without its hint.
        _, out, _ = self.run_cli("doctor", str(self.root), "--offline")
        self.assertIn("!! api key              no API key for provider 'gemini'\n"
                      "  hint: pass --api-key or set one of: $DEVDOCS_API_KEY, "
                      "$GEMINI_API_KEY, $GOOGLE_API_KEY\n", out)

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


class TestDemoKey(CliCase):
    """None ships, and `devdocs.demo` is where one would go. The branch that
    warns of it is reached only then."""

    def demo(self, *argv: str) -> tuple[int, str, str]:
        with mock.patch.object(demo, "DEMO_KEY", "DEMO-SECRET"):
            return self.run_cli(*argv)

    def test_none_ships(self) -> None:
        self.assertEqual(demo.DEMO_KEY, "")
        code, _, err = self.run_cli("generate", str(self.root))
        self.assertEqual(code, 2)
        self.assertIn("no API key", err)
        self.assertNotIn("demo", err)

    def test_the_warning_names_the_real_choices(self) -> None:
        # Regression: it named GEMINI_API_KEY alone.
        code, out, err = self.demo("generate", str(self.root), "--quiet")
        self.assertEqual(code, 0, err)
        self.assertIn(
            "warning: using the shared demo key, whose free quota everyone shares. "
            "To use your own, pass --api-key, set DEVDOCS_API_KEY, GEMINI_API_KEY or "
            "GOOGLE_API_KEY, or set api_key in devdocs.toml.\n", err)
        self.assertNotIn("DEMO-SECRET", out + err)

    def assert_own_key_used(self, *argv: str, **env: str) -> None:
        """A run with the demo key there sends `own`, and says nothing of the demo."""
        err = io.StringIO()
        with mock.patch.object(demo, "DEMO_KEY", "DEMO-SECRET"), clean_env(**env), \
                mock.patch.object(cli, "make_provider",
                                  return_value=self.model) as made, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            code = cli.main(["generate", str(self.root), "--quiet", *argv])
        self.assertEqual(code, 0, err.getvalue())
        self.assertNotIn("demo", err.getvalue())
        made.assert_called_once_with("gemini", "own")

    def test_every_choice_it_names_beats_it(self) -> None:
        # Each alone: a key in the file, there throughout, would hide the others.
        self.assert_own_key_used("--api-key", "own")
        for variable in ("DEVDOCS_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            with self.subTest(variable):
                self.assert_own_key_used(**{variable: "own"})
        (self.root / "devdocs.toml").write_text('api_key = "own"\n', "utf-8")
        self.assert_own_key_used()

    def test_it_serves_gemini_alone(self) -> None:
        code, _, err = self.demo("generate", str(self.root), "--provider", "anthropic")
        self.assertEqual(code, 2)
        self.assertIn("no API key", err)


def gone() -> TextIO:
    """A stream whose reader has gone, as `| tee` or `| jq` has once the
    Ctrl-C that stops a run ends it too."""
    read, write = os.pipe()
    os.close(read)
    return open(write, "w", encoding="utf-8")


class TestOutput(unittest.TestCase):
    def test_output_nobody_reads_is_dropped_and_nothing_fails(self) -> None:
        # Regression: the line raised, or the result raised in Python's flush
        # at exit, which made a stop's 130 a 120.
        lines: dict[str, Callable[[cli._Output], None]] = {
            "info": lambda out: out.info("progress"),
            "warn": lambda out: out.warn("careful"),
            "result": lambda out: out.result("done", {"documents": 6}),
        }
        for name, line in lines.items():
            with self.subTest(name), gone() as stream:
                out = cli._Output(as_json=True, stdout=stream, stderr=stream)
                line(out)
                line(out)
                stream.flush()   # as Python does at exit

    def test_what_a_stream_cannot_encode_is_shown_as_an_escape(self) -> None:
        for encoding, line, shown in (
            ("latin-1", "wrote café/日本", b"wrote caf\xe9/\\u65e5\\u672c\n"),
            ("ascii", "café", b"caf\\xe9\n"),
            ("utf-8", "caf\udcff.py", b"caf\\udcff.py\n"),   # a name that is no UTF-8
            ("utf-8", "café", "café\n".encode()),
        ):
            with self.subTest(encoding=encoding, line=line):
                raw = io.BytesIO()
                with io.TextIOWrapper(raw, encoding=encoding) as stream:
                    cli._write(stream, line)
                    self.assertEqual(raw.getvalue(), shown)

    def test_output_nobody_reads_is_dropped_even_if_it_cannot_be_encoded(self) -> None:
        with gone() as stream:
            cli._write(stream, "caf\udcff.py")
            cli._write(stream, "next")
            stream.flush()   # as Python does at exit

    @unittest.skipUnless(sys.getfilesystemencoding() == "utf-8", "a name in UTF-8")
    def test_a_name_stdout_cannot_encode_ends_no_command(self) -> None:
        # Regression: an internal error, for init with its config written.
        folder = Path(self.enterContext(tempfile.TemporaryDirectory())) / "日本"
        folder.mkdir()
        (folder / "大.py").write_text("def f():\n    return 1\n", "utf-8")
        for argv, shown in (
            (["estimate", str(folder), "--explain", "--quiet"], b"  \\u5927.py  ("),
            (["init", str(folder)], b"next: devdocs estimate "),
        ):
            with self.subTest(argv=argv):
                raw = io.BytesIO()
                err = io.StringIO()
                with io.TextIOWrapper(raw, encoding="latin-1") as stdout, clean_env(), \
                        contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(err):
                    code = cli.main(argv)
                    self.assertEqual(code, 0, err.getvalue())
                    self.assertIn(shown, raw.getvalue())
                self.assertNotIn("internal error", err.getvalue())


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

    def test_a_stop_whose_output_is_gone_still_stops(self) -> None:
        # Regression: under `2>&1 | tee log`, whose tee the Ctrl-C ends too,
        # the notices it could not print stayed buffered, to fail again when
        # the stream closed, as at exit.
        with gone() as stream:
            interrupts = self.make(stream)
            with interrupts.paying():
                interrupts.handle(signal.SIGINT, None)
                self.now += 2
                interrupts.handle(signal.SIGINT, None)
        self.assertTrue(interrupts.event.is_set())
        self.assertEqual(self.halted, [130])

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


class TeeEnds(TaggedModel):
    """Sends SIGINT while shop/cli.py's call is on the wire, as a Ctrl-C to
    `devdocs generate 2>&1 | tee log` does, and ends tee, the reader of the
    `tee` pipe, as that Ctrl-C does: at once, or once tee has the stop's
    notice. Answers once the run's stop is set, as a reply after the signal."""

    def __init__(self, tee: int, *, after_notice: bool) -> None:
        super().__init__()
        self.tee = tee
        self.after_notice = after_notice
        self.stop = threading.Event()   # the run's, once the CLI made it

    def end_tee(self) -> None:
        if self.tee >= 0:
            os.close(self.tee)
            self.tee = -1

    def generate(self, request: Request) -> Completion:
        if request.tag == "shop/cli.py":
            if not self.after_notice:
                self.end_tee()
            os.kill(os.getpid(), signal.SIGINT)
            self.stop.wait(10)
            if self.after_notice:
                self.read_until(b"stopping:")
                self.end_tee()
        return super().generate(request)

    def read_until(self, text: bytes) -> None:
        seen = b""
        while text not in seen:
            if not select.select([self.tee], [], [], 10)[0]:
                raise AssertionError(f"{text!r} never came")
            seen += os.read(self.tee, 4096)


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

    def stopped_as_tee_ends(self, *, after_notice: bool) -> None:
        """Stop a run whose output, stdout and stderr, goes through a tee the
        Ctrl-C ends; check that it stops as it does with a reader."""
        read, write = os.pipe()
        model = TeeEnds(read, after_notice=after_notice)
        self.addCleanup(model.end_tee)
        stdout = self.enterContext(open(os.dup(write), "w", encoding="utf-8"))
        stderr = self.enterContext(   # line-buffered, as sys.stderr is
            open(write, "w", buffering=1, encoding="utf-8"))
        real = cli._client

        def client(*args: Any, **kwargs: Any) -> LLMClient:
            made = real(*args, **kwargs)
            model.stop = made.stop
            return made

        with clean_env(), mock.patch.object(cli, "make_provider", return_value=model), \
                mock.patch.object(cli, "_client", client), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(["generate", str(self.root), "--api-key", "k",
                             "--concurrency", "1", "--json"])
        for stream in (stdout, stderr):
            stream.flush()   # as Python does at exit
        self.assertEqual(code, 130)
        docs = self.root / "docs"
        self.assertIn(PROSE, (docs / "shop" / "cli.py.md").read_text("utf-8"))
        with Database(self.root / ".devdocs" / "devdocs.db") as db:
            (run,) = db.query("SELECT status FROM run")
        self.assertEqual(run["status"], "interrupted")

        # cli.py's answer, which came after the Ctrl-C, was cached.
        self.assertEqual(self.run_cli("generate", str(self.root), "--api-key", "k",
                                      "--quiet")[0], 0)
        self.assertEqual(sorted(r.tag for r in self.model.calls),
                         ["shop", "shop/__init__.py", "shop/money.py"])

    def test_a_stop_outlives_its_output(self) -> None:
        # Regression: with stderr gone, a progress line raised into the run,
        # which wrote no page, lost the answer on the wire and stayed "running".
        self.stopped_as_tee_ends(after_notice=False)

    def test_an_answer_that_comes_after_the_output_went_is_kept(self) -> None:
        # Regression: its progress line raised before it was cached.
        self.stopped_as_tee_ends(after_notice=True)

    def test_a_signal_while_the_pages_are_written_lets_them_be(self) -> None:
        # Regression: the write was abandoned, every page with it.
        class Signalled(MarkdownRenderer):
            def render(self, docset: DocSet) -> dict[str, bytes]:
                # the run's pages, which have a date; not those of its check
                if docset.run is not None and docset.run.generated_at:
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

    def test_a_signal_while_the_output_is_checked_stops_at_once(self) -> None:
        # Regression: the check ran in the paid part of the run, where a stop
        # is graceful, so with nothing bought every page was written again
        # from the cache, which a fresh clone lacks: the prose became stubs.
        self.assertEqual(self.run_cli("generate", str(self.root), "--api-key", "k",
                                      "--quiet")[0], 0)
        shutil.rmtree(self.root / ".devdocs")   # gitignored: a clone has none
        docs = self.root / "docs"

        def pages() -> dict[str, bytes]:
            return {path.relative_to(docs).as_posix(): path.read_bytes()
                    for path in sorted(docs.rglob("*")) if path.is_file()}

        before = pages()
        self.assertIn(PROSE.encode(), before["shop/cart.py.md"])
        self.model.calls.clear()

        class Signalled(MarkdownRenderer):
            def render(self, docset: DocSet) -> dict[str, bytes]:
                # the check's pages, which have no date; not the run's
                if docset.run is not None and not docset.run.generated_at:
                    os.kill(os.getpid(), signal.SIGINT)
                return super().render(docset)

        with mock.patch.object(generate_module, "renderer_for",
                               lambda fmt, **options: Signalled()):
            code, _, err = self.run_cli("generate", str(self.root), "--api-key", "k")
        self.assertEqual(code, 130)
        self.assertNotIn("stopping:", err)
        self.assertIn("warning: interrupted;", err)
        self.assertIn("warning: interrupted; nothing was bought and no documents "
                      "were written\n", err)
        self.assertEqual((self.model.calls, pages()), ([], before))
        with Database(self.root / ".devdocs" / "devdocs.db") as db:
            self.assertEqual(db.query("SELECT id FROM run"), [])

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
        self.assertFalse((self.root / ".devdocs").exists())   # stats makes no state
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
        for step in ("prepare", "check"):
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
