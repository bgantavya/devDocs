"""The command line end to end: commands, output, exit codes. No network: the
provider factory is replaced by the scripted model the pipeline tests use."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from devdocs import __version__, capabilities, cli
from devdocs.capabilities import Capability, Report
from devdocs.config import load_config
from devdocs.errors import AuthError, DevDocsError
from devdocs.render.pdf import external
from devdocs.render.pdf.check import PdfFile, check
from tests.test_generate import PROJECT, TaggedModel

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


if __name__ == "__main__":
    unittest.main()
