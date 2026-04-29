"""Capability probes must always answer, including on a broken environment."""

from __future__ import annotations

import unittest

from devdocs.capabilities import Capability, Report, probe_all


class TestProbes(unittest.TestCase):
    def setUp(self) -> None:
        self.report = probe_all()

    def test_every_probe_reports(self) -> None:
        names = {capability.name for capability in self.report.capabilities}
        self.assertLessEqual(
            {"python", "syntax", "sqlite", "sqlite-fts5", "tls", "certificates"},
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

    def test_missing_fts5_is_not_fatal(self) -> None:
        # Degraded search must not fail the run, so this probe never reports !!.
        fts5 = self.report.get("sqlite-fts5")
        assert fts5 is not None
        self.assertTrue(fts5.ok)

    def test_report_renders_one_line_per_capability(self) -> None:
        rendered = str(self.report)
        for capability in self.report.capabilities:
            self.assertIn(capability.name, rendered)

    def test_failures_are_a_subset_of_capabilities(self) -> None:
        self.assertTrue(set(self.report.failures) <= set(self.report.capabilities))
        self.assertEqual(self.report.ok, not self.report.failures)

    def test_unknown_capability_lookup_is_none(self) -> None:
        self.assertIsNone(self.report.get("no-such-probe"))


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
