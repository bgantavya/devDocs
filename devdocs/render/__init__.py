"""Turning a `DocSet` into files on disk.

`Renderer` is the third extension point, beside `Parser` and `ChatModel`: a
format is one class that maps a DocSet to `{relative path: bytes}`.
`renderer_for` is the registry of the formats that ship: `markdown`, the
default; `html`, a static site; and `pdf`, one bound book or one file per
document. Each is imported only when asked for, so a run that writes
Markdown never loads the PDF engine. Their files never collide, as each
format names its own with its own extensions.

Writing is shared by every format and is careful in three ways. A file whose
bytes are unchanged is not rewritten, so timestamps and diffs stay quiet. Each
write goes to a temporary file beside its target and is renamed into place, so
a reader or a crash never sees half a page. And only files devdocs itself
generated are ever deleted: the previous run's manifest lists them, so a stale
doc for a removed source goes away while a hand-written page in the same
directory is never touched.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from devdocs.errors import ConfigError
from devdocs.render.model import DocSet

__all__ = [
    "FORMATS",
    "MANIFEST",
    "PAGE_SIZES",
    "PDF_ENGINES",
    "OutputWriter",
    "Renderer",
    "WriteReport",
    "generated_files",
    "renderer_for",
]

MANIFEST = "index.json"
FORMATS = ("markdown", "html", "pdf")
PDF_ENGINES = ("builtin", "chrome", "weasyprint")
PAGE_SIZES = ("A4", "Letter")

_DRIVE = re.compile(r"[A-Za-z]:")


@runtime_checkable
class Renderer(Protocol):
    """One output format. A renderer may also keep `warnings: list[str]`,
    refilled by each `render`, for what its format could not show; the
    pipeline reports them."""

    format: str

    def render(self, docset: DocSet) -> dict[str, bytes]:
        """Every output file for `docset`, keyed by POSIX path under the out dir."""
        ...


def renderer_for(
    fmt: str, *, split: bool = False, page_size: str = "A4", pdf_engine: str = "builtin"
) -> Renderer:
    """The renderer for format `fmt`; the other options apply to PDF only.

    Raises ConfigError for an unknown format or PDF option, and for an
    external PDF engine this machine lacks: a run then fails before it buys
    anything, not after.
    """
    if fmt == "markdown":
        from devdocs.render.markdown import MarkdownRenderer

        return MarkdownRenderer()
    if fmt == "html":
        from devdocs.render.html import HtmlRenderer

        return HtmlRenderer()
    if fmt == "pdf":
        from devdocs.render.pdf import PdfRenderer, external

        renderer = PdfRenderer(split=split, page_size=page_size, engine=pdf_engine)
        if pdf_engine != "builtin" and external.find(pdf_engine) is None:
            raise ConfigError(
                f"the {pdf_engine} PDF engine is not installed",
                hint="install it or use --pdf-engine builtin",
            )
        return renderer
    raise ConfigError(f"unknown format {fmt!r}",
                      hint=f"choose from: {', '.join(FORMATS)}")


@dataclass
class WriteReport:
    """What one write did, as sorted POSIX paths relative to the out dir."""

    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


class OutputWriter:
    """Writes rendered files atomically into `out` and prunes stale ones."""

    def __init__(self, out: Path) -> None:
        self.out = out

    def write(self, files: dict[str, bytes], docset: DocSet) -> WriteReport:
        """Write `files` plus the manifest; remove what the last run made and
        this one did not.

        Raises ValueError, before anything is written, for a path that is not
        plain relative POSIX. The manifest is written last, so a run that dies
        midway leaves the previous list for the next run to prune by.
        """
        _validate(files)
        listed = sorted(path for path in files if path != MANIFEST)
        manifest = _manifest(docset, listed)
        out = Path(self.out)
        out.mkdir(parents=True, exist_ok=True)
        previous = generated_files(out)

        report = WriteReport()
        kept: set[tuple[int, int]] = set()   # (device, inode) of every output
        for path in listed:
            written, identity = _store(out / path, files[path])
            (report.written if written else report.unchanged).append(path)
            kept.add(identity)
        report.removed = _prune(out, previous.difference(files, (MANIFEST,)), kept)
        written, _ = _store(out / MANIFEST, manifest)
        (report.written if written else report.unchanged).append(MANIFEST)
        report.written.sort()
        report.unchanged.sort()
        return report


def _safe(path: object) -> bool:
    """True for a plain relative POSIX path: no root, drive, backslash or dot
    segment, so joining it to the out dir cannot leave the out dir; and
    nameable by the OS, so no file call on it raises UnicodeEncodeError."""
    if not isinstance(path, str) or "\\" in path or "\x00" in path:
        return False
    if _DRIVE.match(path):
        return False
    try:
        os.fsencode(path)
    except UnicodeError:   # lone surrogate, e.g. from a "\ud800" JSON escape
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _validate(paths: Iterable[str]) -> None:
    """Raise ValueError for an unsafe path, or one that another output needs
    as a directory; either would fail midway through a write."""
    taken = {MANIFEST}
    for path in paths:
        if not _safe(path):
            raise ValueError(
                f"refusing to write {path!r}: output paths must be relative "
                "POSIX paths without '..', backslashes, a drive or characters "
                "the file system cannot name"
            )
        taken.add(path)
    for path in sorted(taken):
        parts = path.split("/")
        for depth in range(1, len(parts)):
            parent = "/".join(parts[:depth])
            if parent in taken:
                raise ValueError(f"output {path!r} needs {parent!r} to be a directory")


def _manifest(docset: DocSet, files: list[str]) -> bytes:
    data = docset.to_dict(include_run=True)
    data["files"] = files
    try:
        return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    except UnicodeEncodeError:   # undecodable file names; escapes keep them exact
        return (json.dumps(data, indent=2) + "\n").encode("ascii")


def generated_files(out: Path) -> set[str]:
    """What the last write generated in `out`, per its manifest. Empty when
    the manifest is missing, corrupt or not devdocs's, so nothing is pruned
    on a guess."""
    try:
        data = json.loads((Path(out) / MANIFEST).read_bytes())
    except (OSError, ValueError, RecursionError):
        return set()
    if not isinstance(data, dict) or "schema_version" not in data:
        return set()
    files = data.get("files")
    if not isinstance(files, list):
        return set()
    return {path for path in files if _safe(path)}


def _store(target: Path, data: bytes) -> tuple[bool, tuple[int, int]]:
    """Write `data` unless `target` already holds exactly it.

    Returns whether it wrote, and the file's (device, inode).
    """
    try:
        info = target.stat()
    except OSError:
        info = None
    if info is not None and stat.S_ISREG(info.st_mode) and info.st_size == len(data):
        with contextlib.suppress(OSError):
            if target.read_bytes() == data:
                return False, (info.st_dev, info.st_ino)
    return True, _replace(target, data)


def _replace(target: Path, data: bytes) -> tuple[int, int]:
    """Write a synced temp file beside `target`, then rename it over `target`."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = _temp(target)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            info = os.fstat(stream.fileno())
        os.replace(temp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    return info.st_dev, info.st_ino


def _temp(target: Path) -> tuple[int, Path]:
    """Create a new temp file beside `target`, with the permissions a plain
    write would get; `tempfile.mkstemp` would make the page owner-only.

    The temp name keeps only the first 40 characters of the target's: at 4
    bytes a character that is 160, so it fits a 255-byte name limit even when
    the target's own name only just does.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    attempt = 0
    while True:
        temp = target.with_name(f".{target.name[:40]}.{os.getpid()}.{attempt}.tmp")
        try:
            return os.open(temp, flags, 0o666), temp
        except FileExistsError:
            attempt += 1


def _prune(out: Path, stale: Iterable[str], kept: set[tuple[int, int]]) -> list[str]:
    """Delete each stale path that is still a regular file inside `out`, then
    the directories that leaves empty. Best effort: a failure skips the file.

    A path naming the same file as an output (a case-insensitive alias on
    macOS or Windows) is kept, or a renamed source would lose its new page.
    """
    root = out.resolve()
    removed: list[str] = []
    for path in sorted(stale):
        target = out / path
        try:
            info = target.lstat()
            inside = target.parent.resolve().is_relative_to(root)
        except (OSError, RuntimeError):   # RuntimeError: symlink loop before 3.13
            continue
        if not stat.S_ISREG(info.st_mode) or not inside:
            continue
        if (info.st_dev, info.st_ino) in kept:
            continue
        try:
            target.unlink()
        except OSError:
            continue
        removed.append(path)
        _remove_empty(target.parent, out)
    return removed


def _remove_empty(directory: Path, out: Path) -> None:
    """Remove `directory` and its ancestors while they are empty, up to `out`."""
    while directory != out and out in directory.parents:
        try:
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent
