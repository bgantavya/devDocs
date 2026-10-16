"""Printing the HTML book with an installed browser or WeasyPrint.

The built-in engine is limited to what the standard PDF fonts can show,
roughly Latin-1. For full Unicode or CSS typography, two external engines
print `HtmlRenderer(book=True)` output instead: "chrome" runs Chrome,
Chromium or Edge headless with --print-to-pdf, and "weasyprint" runs
WeasyPrint's command line. Neither is a dependency; each is found on PATH or,
for browsers, in the usual macOS, Linux and Windows install locations, and
`available_engines` reports what this machine has.

The book is written to a temporary directory together with a one-line
@page rule for the chosen paper size, printed by the tool with a timeout and
no shell, read back and checked to be a PDF; the directory is removed
afterwards. A browser gets a throwaway profile there so it neither reads nor
changes the user's own, and is asked for bookmarks from the book's
headings, which WeasyPrint makes unasked; a browser too old to know the
switch ignores it. External output is not byte-reproducible: both tools
stamp their own dates and identifiers into the file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from devdocs.errors import ConfigError, DevDocsError
from devdocs.render.model import DocSet

__all__ = ["ENGINES", "available_engines", "find", "print_html", "render"]

ENGINES = ("builtin", "chrome", "weasyprint")
_TIMEOUT = 120.0        # seconds a tool may take to print the book

_BROWSERS = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "microsoft-edge", "microsoft-edge-stable", "msedge", "chrome",
)
_MAC_APPS = (
    "Google Chrome.app/Contents/MacOS/Google Chrome",
    "Chromium.app/Contents/MacOS/Chromium",
    "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
_LINUX_PATHS = (
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium",
    "/usr/bin/chromium-browser", "/snap/bin/chromium", "/usr/bin/microsoft-edge",
    "/opt/google/chrome/chrome", "/opt/microsoft/msedge/msedge",
)
_WINDOWS_PATHS = (
    ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
    ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
    ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    ("PROGRAMFILES", "Chromium/Application/chrome.exe"),
    ("PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
    ("PROGRAMFILES", "Microsoft/Edge/Application/msedge.exe"),
)
_INSTALL = {
    "chrome": "install Google Chrome, Chromium or Microsoft Edge, "
              "or use the builtin PDF engine",
    "weasyprint": "install WeasyPrint (pip install weasyprint), "
                  "or use the builtin PDF engine",
}


def find(engine: str) -> str | None:
    """The executable that runs `engine`, or None when it is not installed."""
    if engine == "weasyprint":
        return shutil.which("weasyprint")
    if engine != "chrome":
        return None
    for name in _BROWSERS:
        path = shutil.which(name)
        if path:
            return path
    for candidate in _browser_locations():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _browser_locations() -> list[Path]:
    """Where browser installers put the executable on this platform."""
    if sys.platform == "darwin":
        roots = ("/Applications", os.path.expanduser("~/Applications"))
        return [Path(root, app) for root in roots for app in _MAC_APPS]
    if sys.platform == "win32":
        return [
            Path(os.environ[variable], relative)
            for variable, relative in _WINDOWS_PATHS
            if os.environ.get(variable)
        ]
    return [Path(path) for path in _LINUX_PATHS]


def available_engines() -> dict[str, str]:
    """Each usable engine and the executable that runs it, in `ENGINES`
    order. "builtin" is always present and runs in this interpreter, so it
    maps to `sys.executable`."""
    engines = {"builtin": sys.executable}
    for engine in ENGINES[1:]:
        path = find(engine)
        if path:
            engines[engine] = path
    return engines


def render(
    engine: str, docset: DocSet, *, page_size: str, timeout: float = _TIMEOUT
) -> bytes:
    """`docset` as a PDF printed by `engine` from the HTML book."""
    from devdocs.render.html import HtmlRenderer  # only external engines need it

    html = HtmlRenderer(book=True).render(docset)["book.html"]
    return print_html(engine, html, page_size=page_size, timeout=timeout)


def print_html(
    engine: str, html: bytes, *, page_size: str, timeout: float = _TIMEOUT
) -> bytes:
    """Print a self-contained HTML page to PDF with `engine`.

    Raises ConfigError when the engine is not installed, and DevDocsError
    when it fails, times out or produces no PDF.
    """
    executable = find(engine)
    if executable is None:
        raise ConfigError(f"the {engine} PDF engine is not installed",
                          hint=_INSTALL.get(engine, "use the builtin PDF engine"))
    with tempfile.TemporaryDirectory(prefix="devdocs-pdf-",
                                     ignore_cleanup_errors=True) as temp:
        folder = Path(temp)
        source, target = folder / "book.html", folder / "book.pdf"
        source.write_bytes(_sized(html, page_size))
        hint = f"run {executable} by hand to see why, or use the builtin PDF engine"
        try:
            result = subprocess.run(
                _command(engine, executable, source, target, folder),
                stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as err:
            raise DevDocsError(
                f"{engine} did not finish printing within {timeout:g} seconds",
                hint=hint,
            ) from err
        except OSError as err:
            raise DevDocsError(f"cannot run {executable}: {err}", hint=hint) from err
        data = target.read_bytes() if target.is_file() else b""
        if result.returncode != 0 or not data.startswith(b"%PDF-"):
            detail = _last_line(result.stderr)
            raise DevDocsError(
                f"{engine} could not print the book (exit status {result.returncode})"
                + (f": {detail}" if detail else ""),
                hint=hint,
            )
        return data


def _command(
    engine: str, executable: str, source: Path, target: Path, folder: Path
) -> list[str]:
    if engine == "weasyprint":
        return [executable, str(source), str(target)]
    command = [
        executable, "--headless", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--disable-extensions",
        f"--user-data-dir={folder / 'profile'}", "--no-pdf-header-footer",
        "--generate-pdf-document-outline", f"--print-to-pdf={target}",
        source.as_uri(),
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        command.insert(1, "--no-sandbox")   # browsers refuse root, as in containers
    return command


def _sized(html: bytes, page_size: str) -> bytes:
    """`html` with an @page rule for `page_size` last in its head, so it wins."""
    rule = b"<style>@page{size:%s}</style>" % page_size.encode("ascii")
    head = html.lower().rfind(b"</head>")
    return rule + html if head < 0 else html[:head] + rule + html[head:]


def _last_line(stream: bytes) -> str:
    lines = stream.decode("utf-8", "replace").strip().splitlines()
    return lines[-1].strip()[:200] if lines else ""
