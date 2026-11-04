"""External PDF engines, found and run with the tools mocked out."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from devdocs.errors import ConfigError, DevDocsError
from devdocs.render.pdf import external

EXTERNAL = "devdocs.render.pdf.external"


@contextlib.contextmanager
def printing(
    case: unittest.TestCase, engine: str, result: Any = None
) -> Iterator[list[list[str]]]:
    """`engine` installed, and a subprocess.run that prints a PDF where the
    command asks, `case` asserting on each call; yields every command run."""
    commands: list[list[str]] = []

    def fake(command: list[str], **options: Any) -> Any:
        commands.append(command)
        case.assertNotIn("shell", options)
        case.assertIn("timeout", options)
        target = Path(next(
            (part.split("=", 1)[1] for part in command
             if part.startswith("--print-to-pdf=")),
            command[-1],
        ))
        html = (target.parent / "book.html").read_bytes()
        case.assertIn(b"<style>@page{size:A4}</style></head>", html)
        target.write_bytes(b"%PDF-1.4 printed")
        return result or subprocess.CompletedProcess(command, 0, b"", b"")

    with (
        mock.patch.object(external, "find", return_value=f"/bin/{engine}"),
        mock.patch(f"{EXTERNAL}.subprocess.run", side_effect=fake),
    ):
        yield commands


class TestExternal(unittest.TestCase):
    def which(self, found: dict[str, str]) -> Any:
        return mock.patch(f"{EXTERNAL}.shutil.which", side_effect=found.get)

    def test_browsers_are_found_on_path_first(self) -> None:
        with self.which({"chromium": "/usr/bin/chromium",
                         "msedge": "/usr/bin/msedge"}):
            self.assertEqual(external.find("chrome"), "/usr/bin/chromium")

    def test_browsers_are_found_where_installers_put_them(self) -> None:
        app = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        with (
            self.which({}),
            mock.patch(f"{EXTERNAL}.sys.platform", "darwin"),
            mock.patch(f"{EXTERNAL}.os.path.isfile", lambda path: Path(path) == app),
            mock.patch(f"{EXTERNAL}.os.access", return_value=True),
        ):
            self.assertEqual(Path(external.find("chrome") or ""), app)
        exe = Path("C:/Users/me/Google/Chrome/Application/chrome.exe")
        with (
            self.which({}),
            mock.patch(f"{EXTERNAL}.sys.platform", "win32"),
            mock.patch.dict("os.environ", {"LOCALAPPDATA": "C:/Users/me"}),
            mock.patch(f"{EXTERNAL}.os.path.isfile", lambda path: Path(path) == exe),
            mock.patch(f"{EXTERNAL}.os.access", return_value=True),
        ):
            self.assertEqual(Path(external.find("chrome") or ""), exe)

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0, "POSIX modes bind no root")
    def test_a_browser_in_a_folder_it_cannot_search_is_absent(self) -> None:
        # Regression: before 3.14 Path.is_file raised, and doctor and a chrome
        # run ended in an internal error.
        locked = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, locked)
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o700)
        with self.which({}), mock.patch.object(external, "_browser_locations",
                                                return_value=[locked / "chrome"]):
            self.assertIsNone(external.find("chrome"))
            self.assertEqual(external.available_engines(), {"builtin": sys.executable})

    def test_available_engines(self) -> None:
        with (
            self.which({"weasyprint": "/usr/local/bin/weasyprint"}),
            mock.patch.object(external, "_browser_locations", return_value=[]),
        ):
            self.assertEqual(external.available_engines(), {
                "builtin": sys.executable, "weasyprint": "/usr/local/bin/weasyprint",
            })
        with self.which({}), mock.patch.object(external, "_browser_locations",
                                                return_value=[]):
            self.assertEqual(external.available_engines(), {"builtin": sys.executable})

    def test_chrome_prints_headless_without_a_shell(self) -> None:
        with printing(self, "chrome") as commands:
            data = external.print_html("chrome", b"<html><head></head></html>",
                                       page_size="A4")
        self.assertEqual(data, b"%PDF-1.4 printed")
        (command,) = commands
        self.assertEqual(command[0], "/bin/chrome")
        self.assertIn("--headless", command)
        self.assertIn("--generate-pdf-document-outline", command)     # bookmarks
        self.assertTrue(any(p.startswith("--user-data-dir=") for p in command))
        self.assertTrue(command[-1].startswith("file:"))
        target = next(p for p in command if p.startswith("--print-to-pdf="))
        self.assertFalse(Path(target.split("=", 1)[1]).parent.exists())   # cleaned up

    def test_weasyprint_prints_source_to_target(self) -> None:
        with printing(self, "weasyprint") as commands:
            external.print_html("weasyprint", b"<head></head>", page_size="A4")
        (command,) = commands
        self.assertEqual(command[0], "/bin/weasyprint")
        self.assertEqual([Path(part).name for part in command[1:]],
                         ["book.html", "book.pdf"])

    def test_a_missing_engine_is_a_configuration_error_with_a_hint(self) -> None:
        with (
            mock.patch.object(external, "find", return_value=None),
            self.assertRaises(ConfigError) as caught,
        ):
            external.print_html("weasyprint", b"", page_size="A4")
        self.assertIn("pip install weasyprint", caught.exception.hint or "")

    def test_failures_and_timeouts_are_reported(self) -> None:
        failed = subprocess.CompletedProcess([], 3, b"", b"warming up\nno printer\n")
        with (
            printing(self, "chrome", failed),
            self.assertRaises(DevDocsError) as caught,
        ):
            external.print_html("chrome", b"<head></head>", page_size="A4")
        self.assertIn("(exit status 3): no printer", str(caught.exception))
        self.assertIsNotNone(caught.exception.hint)
        timeout = subprocess.TimeoutExpired(["chrome"], 5)
        with (
            mock.patch.object(external, "find", return_value="/bin/chrome"),
            mock.patch(f"{EXTERNAL}.subprocess.run", side_effect=timeout),
            self.assertRaisesRegex(DevDocsError, "within 5 seconds"),
        ):
            external.print_html("chrome", b"", page_size="A4", timeout=5)


if __name__ == "__main__":
    unittest.main()
