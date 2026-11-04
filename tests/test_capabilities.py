"""Capability probes must always answer, including on a broken environment."""

from __future__ import annotations

import ast
import unittest
from typing import Any
from unittest import mock

from devdocs.capabilities import Capability, Report, probe_all


class TestProbes(unittest.TestCase):
    def setUp(self) -> None:
        self.report = probe_all()

    def test_every_probe_reports(self) -> None:
        names = {capability.name for capability in self.report.capabilities}
        self.assertLessEqual(
            {"python", "syntax", "sqlite", "sqlite-threads", "tls", "certificates"},
            names,
        )

    def test_no_probe_raises(self) -> None:
        for capability in self.report.capabilities:
            self.assertIsInstance(capability.ok, bool)
            self.assertNotIn("probe failed", capability.value)

    def test_sqlite_version_is_reported(self) -> None:
        sqlite = self.report.get("sqlite")
        assert sqlite is not None
        self.assertRegex(sqlite.value, r"^\d+\.\d+")

    def test_nothing_is_probed_that_nothing_uses(self) -> None:
        # Regression: doctor reported an FTS5 "lexical prefilter", when no
        # query uses FTS5 and BM25 is pure Python.
        self.assertIsNone(self.report.get("sqlite-fts5"))
        self.assertNotIn("prefilter", str(self.report))

    def test_report_renders_one_line_per_capability(self) -> None:
        rendered = str(self.report)
        for capability in self.report.capabilities:
            self.assertIn(capability.name, rendered)

    def test_failures_are_a_subset_of_capabilities(self) -> None:
        self.assertTrue(set(self.report.failures) <= set(self.report.capabilities))
        self.assertEqual(self.report.ok, not self.report.failures)

    def test_unknown_capability_lookup_is_none(self) -> None:
        self.assertIsNone(self.report.get("no-such-probe"))


class TestSyntax(unittest.TestCase):
    def test_syntax_newer_than_the_interpreter_is_noted_not_failed(self) -> None:
        # Regression: on 3.11 the probe failed for 3.12 syntax, so doctor
        # exited 1 on every install, when that costs only the files using it.
        real = ast.parse

        def parse_as_311(source: str, *args: Any, **kwargs: Any) -> ast.Module:
            if "def f[T]" in source or source == 'f"{"a"}"':
                raise SyntaxError("invalid syntax")
            return real(source, *args, **kwargs)

        with mock.patch("ast.parse", parse_as_311):
            report = probe_all()
        syntax = report.get("syntax")
        assert syntax is not None
        self.assertEqual((syntax.ok, syntax.value), (True, "2/4 constructs"))
        self.assertIn("cannot parse: pep695-generics (3.12+), pep701-fstrings (3.12+)",
                      syntax.detail)
        self.assertTrue(report.ok)


class TestRendering(unittest.TestCase):
    def test_failure_is_marked(self) -> None:
        line = str(Capability("thing", False, "1.0", "why it matters"))
        self.assertTrue(line.startswith("!!"))
        self.assertIn("why it matters", line)

    def test_success_without_detail_is_one_line(self) -> None:
        line = str(Capability("thing", True, "1.0"))
        self.assertEqual(len(line.splitlines()), 1)

    def test_empty_report_is_ok(self) -> None:
        self.assertTrue(Report(()).ok)


if __name__ == "__main__":
    unittest.main()
