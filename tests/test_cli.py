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

from devdocs import __version__, cli
from devdocs.errors import AuthError
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

    def run_cli(self, *argv: str, model: Any = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        fake = model if model is not None else self.model
        with clean_env(), \
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
        code, _, err = self.run_cli("-p", str(self.root), "--output", "manual",
                                    "--api-key", "k")
        self.assertEqual(code, 0)
        self.assertIn("deprecated", err)
        self.assertTrue((self.root / "manual" / "shop" / "cart.md").is_file())


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

    def test_models_marks_what_the_key_can_reach(self) -> None:
        code, out, _ = self.run_cli("models", "--api-key", "k", "--json")
        self.assertEqual(code, 0)
        rows = {row["id"]: row["available"] for row in json.loads(out)["catalogued"]}
        self.assertTrue(rows["gemini-2.5-flash-lite"])
        self.assertFalse(rows["gemini-3.1-flash-lite"])


if __name__ == "__main__":
    unittest.main()
