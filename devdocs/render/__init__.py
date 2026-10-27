"""Turning a `DocSet` into files on disk.

`Renderer` is the third extension point, beside `Parser` and `ChatModel`: a
format is one class that maps a DocSet to `{relative path: bytes}`.
`renderer_for` is the registry of the formats that ship: `markdown`, the
default; `html`, a static site; and `pdf`, one bound book or one file per
document. Each is imported only when asked for, so a run that writes
Markdown never loads the PDF engine. Their files never collide, as each
format names its own with its own extensions.

Writing is shared by every format and is careful in four ways. A file whose
bytes are unchanged is not rewritten, so timestamps and diffs stay quiet. Each
write goes to a temporary file beside its target and is renamed into place, so
a reader or a crash never sees half a page. Only files devdocs itself wrote
are ever replaced or deleted: the manifest lists them, so a stale doc for a
removed source goes away while a hand-written page in the same directory is
never touched. And the manifest is written before the pages as well as after
them, listing all the write may create, so one that dies midway still leaves
a record of every file it made.

A file devdocs did not write, where an output goes, is therefore refused, and
nothing is written unless `force` lets the write replace it. A file there
that already holds exactly the output is left alone instead, and unlisted, so
a hand-written `.nojekyll` the HTML site also needs is never pruned later.
Only without any manifest, on a first run or after it was lost, is such an
identical file taken for devdocs's own. A manifest is recognised by its
fields, and a corrupt one, or another tool's `index.json`, is itself a file
devdocs did not write.

A write the file system fails is a ConfigError naming the file, or the out
dir, with what to do about it. Most such failures come before anything has
changed, as each file the write makes or replaces is probed first, the
manifest first of all: its directory is made, its target looked up, and a
temp file holding its whole name, where that fits, created and removed. So
a name the file system cannot hold (too long; or, on macOS, with bytes that
are not UTF-8, as a tree from Linux may have), a directory devdocs may not
write or a read-only disk fails there, any directory made goes again, and
the last write's files and manifest stay exactly as they were.
`OutputWriter.check` makes these probes, and every refusal above, without
writing anything, so a run can refuse before it buys the prose.

Each file is then written to a temp file and renamed into place in turn,
so the disk needs room for only one more file at a time, and a process
killed outright leaves at most one temp file behind. What fails only
then, as a full disk, or a file another program holds open or one flagged
immutable, fails midway. The files renamed by then are new and the rest as
they were, and the manifest, renamed first, holds the new record and lists
every file the write may make, so the next write replaces or prunes them
all and `export` renders that record.

The manifest is also the run's record: it holds the whole DocSet, so
`read_manifest` gives it back and another format can be rendered later
without scanning the project or asking a model anything. `OutputWriter.add`
then lists the new files and keeps that record exactly as it was, so fields
a newer devdocs added to the schema survive.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from devdocs.errors import ConfigError
from devdocs.render.model import SCHEMA_VERSION, DocSet

__all__ = [
    "FORMATS",
    "MANIFEST",
    "PAGE_SIZES",
    "PDF_ENGINES",
    "OutputWriter",
    "Renderer",
    "WriteReport",
    "generated_files",
    "read_manifest",
    "renderer_for",
]

MANIFEST = "index.json"
FORMATS = ("markdown", "html", "pdf")
PDF_ENGINES = ("builtin", "chrome", "weasyprint")
PAGE_SIZES = ("A4", "Letter")

_DRIVE = re.compile(r"[A-Za-z]:")
_SURROGATE = re.compile("[\ud800-\udfff]")   # an undecodable byte, as os.walk has it
_REGENERATE = "run `devdocs generate` to write it again"
_NAMED = 5               # paths an error names before "and N more"
_NAME_MAX = 255          # bytes in a file name, the usual cap
_FULL = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
_INVALID_NAME = 123      # Windows' ERROR_INVALID_NAME

# What is where an output goes; see `_inspect`.
_ABSENT, _SAME, _OTHER, _BLOCKED = "absent", "same", "other", "blocked"


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
                hint="install it or use --pdf-engine builtin; "
                     "`devdocs doctor` lists the engines it finds",
            )
        return renderer
    raise ConfigError(f"unknown format {fmt!r}",
                      hint=f"choose from: {', '.join(FORMATS)}")


def read_manifest(out: Path) -> DocSet:
    """The DocSet the last write left in `out`, read back from its manifest.

    Raises ConfigError when there is none, when it is unreadable, not
    devdocs's or names a page outside `out`, and when a newer devdocs wrote
    it in a schema this one cannot read.
    """
    path = Path(out) / MANIFEST
    try:
        data = json.loads(path.read_bytes())
    except FileNotFoundError:
        raise ConfigError(
            f"no generated documentation in {path.parent}: {MANIFEST} is missing",
            hint="run `devdocs generate` first, or point --out at the "
                 "directory it wrote",
        ) from None
    except (OSError, ValueError, RecursionError) as err:
        raise ConfigError(f"cannot read {path}: {err}", hint=_REGENERATE) from err
    if not isinstance(data, dict) or "schema_version" not in data:
        raise ConfigError(f"{path} is not a devdocs manifest", hint=_REGENERATE)

    version = str(data["schema_version"])
    major, ours = version.partition(".")[0], SCHEMA_VERSION.partition(".")[0]
    if major.isascii() and major.isdigit() and int(major) > int(ours):
        raise ConfigError(
            f"{path} was written by a newer devdocs, in document schema "
            f"{version}; this one reads schema {ours}.x",
            hint=f"upgrade devdocs, or {_REGENERATE}",
        )
    try:
        docset = DocSet.from_dict(data)
    except (ValueError, TypeError) as err:
        raise ConfigError(f"cannot read {path}: {err}", hint=_REGENERATE) from err

    seen: set[str] = set()
    for document in docset.documents:
        if document.id in seen:
            raise ConfigError(f"{path}: document {document.id!r} appears twice",
                              hint=_REGENERATE)
        if not _safe(document.path):
            raise ConfigError(
                f"{path}: document {document.id!r} has the unsafe path "
                f"{document.path!r}",
                hint=_REGENERATE,
            )
        seen.add(document.id)
    return docset


@dataclass
class WriteReport:
    """What one write did, as sorted POSIX paths relative to the out dir."""

    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Found:
    """What a write found where one output goes: `_inspect`'s verdict, the
    (device, inode) of the file there, if any, and the error that kept it
    from looking, if one did."""

    state: str
    identity: tuple[int, int] | None = None
    error: OSError | None = None


class OutputWriter:
    """Writes rendered files atomically into `out`, prunes stale ones, and
    never replaces a file devdocs did not write unless forced."""

    def __init__(self, out: Path) -> None:
        self.out = out

    def write(
        self,
        files: dict[str, bytes],
        docset: DocSet,
        *,
        prune: bool = True,
        force: bool = False,
    ) -> WriteReport:
        """Write `files` plus a manifest holding `docset`; remove what the last
        write made and this one did not, or with `prune` False keep it, still
        listed. `force` replaces files devdocs did not write.

        Raises ValueError for a path that is not plain relative POSIX, and
        ConfigError for an output another file is in the way of; either
        before anything is written. A write the file system fails raises
        ConfigError too, naming the file: as a rule before anything has
        changed, else midway, with a manifest the next write understands;
        see the module docstring.
        """
        return self._commit(files, docset.to_dict(include_run=True),
                            _load(Path(self.out)), prune=prune, force=force)

    def check(
        self, files: dict[str, bytes], docset: DocSet, *, force: bool = False
    ) -> None:
        """Raise what `write` would raise for `files` and `docset` before it
        changed anything, and change nothing: the same code runs, up to the
        probes, and directories a probe made go again."""
        self._commit(files, docset.to_dict(include_run=True), _load(Path(self.out)),
                     prune=True, force=force, dry=True)

    def add(self, files: dict[str, bytes], *, force: bool = False) -> WriteReport:
        """Write `files` beside the last write's, which all stay listed. The
        manifest keeps its record exactly as it was; only its list of files
        grows.

        Raises ConfigError when `out` holds no devdocs manifest, and as
        `write` does.
        """
        last = _load(Path(self.out))
        if last is None:
            raise ConfigError(f"no devdocs manifest in {self.out} to add files to",
                              hint="run `devdocs generate` first")
        return self._commit(files, last, last, prune=False, force=force)

    def _commit(
        self,
        files: Mapping[str, bytes],
        record: Mapping[str, Any],
        last: Mapping[str, Any] | None,
        *,
        prune: bool,
        force: bool,
        dry: bool = False,
    ) -> WriteReport:
        """Write `files`, and `record` as the manifest, over what `last`, the
        manifest found in `out`, lists. The manifest's own path in `files` is
        ignored. With `dry`, stop once every file the write would make or
        replace is probed."""
        _validate(files)
        out = Path(self.out)
        outputs = {path: data for path, data in files.items() if path != MANIFEST}
        previous = _listed(last)
        found = _inspect_all(out, outputs, previous, last, force=force)
        owned = sorted(path for path in outputs if path not in found.left)

        report = WriteReport()
        kept: set[tuple[int, int]] = set()   # (device, inode) of every output
        for path in sorted(outputs):
            here = found.outputs[path]
            if here.state == _SAME and here.identity is not None:
                report.unchanged.append(path)
                kept.add(here.identity)
            else:
                report.written.append(path)
        manifest = out / MANIFEST
        ahead = _encode(record, sorted(previous.union(owned)))
        wrote = not _holds(manifest, ahead)
        batch = _Batch(out)
        try:
            pages = [(path, outputs[path]) for path in report.written]
            planned = [(MANIFEST, ahead), *pages] if wrote else pages
            if dry:
                batch.check(planned)
                return report
            kept.update(batch.write(planned))
            stale = _leftovers(out, previous.difference(outputs), kept)
            if prune:
                report.removed = _prune(out, stale)
            else:
                owned = sorted([*owned, *stale])
            final = _encode(record, owned)
            if not _holds(manifest, final):
                batch.write([(MANIFEST, final)])
                wrote = True
        except OSError as err:
            raise _unwritable(out, batch.at, err) from err
        finally:
            batch.discard()
        (report.written if wrote else report.unchanged).append(MANIFEST)
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


def _encode(record: Mapping[str, Any], files: list[str]) -> bytes:
    """The manifest: `record` with `files` as its list of generated files."""
    data = {**record, "files": files}
    try:
        return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    except UnicodeEncodeError:   # undecodable file names; escapes keep them exact
        return (json.dumps(data, indent=2) + "\n").encode("ascii")


def _load(out: Path) -> dict[str, Any] | None:
    """The manifest in `out`, when devdocs wrote it: a JSON object with a
    schema version, its documents and its list of files. None when it is
    missing, unreadable or another tool's."""
    try:
        data = json.loads((Path(out) / MANIFEST).read_bytes())
    except (OSError, ValueError, RecursionError):
        return None
    if (isinstance(data, dict) and "schema_version" in data
            and isinstance(data.get("documents"), list)
            and isinstance(data.get("files"), list)):
        return data
    return None


def _listed(manifest: Mapping[str, Any] | None) -> set[str]:
    """The safe paths `manifest` lists as generated, itself excluded."""
    if manifest is None:
        return set()
    return {path for path in manifest["files"] if _safe(path) and path != MANIFEST}


def generated_files(out: Path) -> set[str]:
    """What the last write generated in `out`, per its manifest. Empty when
    the manifest is missing, corrupt or not devdocs's, so nothing is pruned
    on a guess."""
    return _listed(_load(out))


@dataclass(frozen=True)
class _Inspection:
    """Where a write's outputs would go: what is at each, and which already
    hold exactly their output but are not devdocs's, so stay unlisted."""

    outputs: dict[str, _Found]
    left: frozenset[str]


def _inspect_all(
    out: Path,
    outputs: Mapping[str, bytes],
    previous: set[str],
    last: Mapping[str, Any] | None,
    *,
    force: bool,
) -> _Inspection:
    """Look where every output goes before anything is written.

    A file there is devdocs's when `previous` lists its path, or the same
    file under another name, as a case-insensitive disk spells one file two
    ways. Raises ConfigError where a directory is in the way or a path,
    `out` itself first, cannot be looked at, as when a name in it is too
    long, which `force` cannot help; and unless `force`, where an output
    would replace any other file, the manifest included when it is not one
    devdocs can read.
    """
    error = _occupant(out).error
    if error is not None:
        raise _unwritable(out, MANIFEST, error)
    found = {path: _inspect(out / path, data) for path, data in outputs.items()}
    blocked = {path: found[path] for path in sorted(found)
               if found[path].state == _BLOCKED}
    unlisted = sorted(
        path for path, here in found.items()
        if here.state in (_SAME, _OTHER) and path not in previous
    )
    if unlisted:
        ours = _identities(out, previous)
        unlisted = [path for path in unlisted if found[path].identity not in ours]
    left = [path for path in unlisted
            if found[path].state == _SAME and last is not None]
    foreign = [path for path in unlisted if found[path].state == _OTHER]
    if last is None:
        manifest = _occupant(out / MANIFEST)
        if manifest.state == _BLOCKED:
            blocked = {MANIFEST: manifest, **blocked}
        elif manifest.state == _OTHER:
            foreign.insert(0, MANIFEST)

    for path, here in blocked.items():
        if here.error is not None:
            raise _unwritable(out, path, here.error)
    if blocked:
        directories = list(blocked)
        raise ConfigError(
            f"cannot write {_count(len(directories))} in {out}: a directory is "
            f"in the way of {_names(directories)}",
            hint=f"move {_them(directories)} away or choose another --out",
        )
    if foreign and not force:
        raise ConfigError(
            f"refusing to replace {_count(len(foreign))} in {out} that devdocs "
            f"has no record of writing: {_names(foreign)}",
            hint=f"move {_them(foreign)} away or choose another --out; "
                 f"--force replaces {_them(foreign)}",
        )
    return _Inspection(found, frozenset(left))


def _inspect(target: Path, data: bytes) -> _Found:
    """What is at `target`, as `_occupant` finds it, but "same" for a file
    that holds exactly `data`, perhaps through a link."""
    here = _occupant(target)
    if here.state == _OTHER and _holds(target, data):
        return _Found(_SAME, here.identity)
    return here


def _occupant(target: Path) -> _Found:
    """What is at `target`: nothing; a file or a link, which a write
    replaces; or a directory, or a path that cannot be looked at, which it
    cannot."""
    try:
        info = target.lstat()
    except FileNotFoundError:
        return _Found(_ABSENT)
    except OSError as err:   # e.g. a file where a parent directory should be
        return _Found(_BLOCKED, error=err)
    if stat.S_ISDIR(info.st_mode):
        return _Found(_BLOCKED)
    return _Found(_OTHER, (info.st_dev, info.st_ino))


def _identities(out: Path, paths: Iterable[str]) -> set[tuple[int, int]]:
    """The (device, inode) of each regular file `paths` name inside `out`."""
    found: set[tuple[int, int]] = set()
    for path in paths:
        with contextlib.suppress(OSError):
            info = (out / path).lstat()
            if stat.S_ISREG(info.st_mode):
                found.add((info.st_dev, info.st_ino))
    return found


def _count(number: int) -> str:
    return f"{number} file{'' if number == 1 else 's'}"


def _them(paths: list[str]) -> str:
    return "it" if len(paths) == 1 else "them"


def _names(paths: list[str]) -> str:
    """The first few `paths`, and how many more there are."""
    shown = ", ".join(paths[:_NAMED])
    more = len(paths) - _NAMED
    return f"{shown} and {more} more" if more > 0 else shown


def _holds(target: Path, data: bytes) -> bool:
    """Whether `target` is, or links to, a regular file of exactly `data`."""
    try:
        info = target.stat()
        return (stat.S_ISREG(info.st_mode) and info.st_size == len(data)
                and target.read_bytes() == data)
    except OSError:
        return False


class _Batch:
    """Files one write puts into `out`. `write` probes them all, then writes
    each in turn; `discard` removes what a failure left: the temp file in
    flight, and directories made that are empty again."""

    def __init__(self, out: Path) -> None:
        self.out = out
        self.made: list[Path] = []      # directories made, outermost first
        self.temp: Path | None = None   # the temp file in flight
        self.at = MANIFEST              # the path last worked on

    def check(self, files: list[tuple[str, bytes]]) -> None:
        """Probe every file; `discard` then removes the directories made."""
        for path, _ in files:
            self.probe(path)

    def write(self, files: list[tuple[str, bytes]]) -> list[tuple[int, int]]:
        """Probe every file, then write each; their (device, inode)."""
        self.check(files)
        done = [self.put(path, data) for path, data in files]
        self.made.clear()   # each holds a file now
        return done

    def probe(self, path: str) -> None:
        """Make `path`'s missing directories, look it up, and create and
        remove a temp file beside it: the lookup finds a name too long,
        even where its directory is new, and the temp file one the file
        system cannot name; see `_temp`."""
        self.at = path
        target = self.out / path
        _makedirs(target.parent, self.made)
        with contextlib.suppress(FileNotFoundError):
            target.lstat()
        handle, self.temp = _temp(target)
        os.close(handle)
        os.unlink(self.temp)
        self.temp = None

    def put(self, path: str, data: bytes) -> tuple[int, int]:
        """Write `data` to a synced temp file beside `path`, then rename it
        over `path`. Returns its (device, inode), which the rename keeps."""
        self.at = path
        target = self.out / path
        handle, self.temp = _temp(target)
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            info = os.fstat(stream.fileno())
        os.replace(self.temp, target)
        self.temp = None
        return info.st_dev, info.st_ino

    def discard(self) -> None:
        """Remove the temp file in flight, then the directories made,
        innermost first, where that leaves them empty."""
        if self.temp is not None:
            with contextlib.suppress(OSError):
                os.unlink(self.temp)
            self.temp = None
        for directory in reversed(self.made):
            with contextlib.suppress(OSError):
                directory.rmdir()
        self.made.clear()


def _makedirs(directory: Path, made: list[Path]) -> None:
    """Make `directory` and its missing parents, adding each one made to
    `made`, outermost first."""
    missing: list[Path] = []
    while not os.path.isdir(directory) and directory != directory.parent:
        missing.append(directory)
        directory = directory.parent
    for path in reversed(missing):
        try:
            path.mkdir()
        except FileExistsError:
            if not os.path.isdir(path):   # a file in the way
                raise
            continue                      # made meanwhile, not by this write
        made.append(path)


def _temp(target: Path) -> tuple[int, Path]:
    """Create a new temp file beside `target`, with the permissions a plain
    write would get; `tempfile.mkstemp` would make the page owner-only.

    The temp name holds the target's whole name, so the probe that makes
    one first finds a name the file system cannot hold before anything is
    replaced. Where that would not fit 255 bytes, or the file system finds it
    too long though the target's fits (eCryptfs caps names at 143 bytes,
    Windows whole paths), it keeps the first 40 characters: at 4 bytes a
    character that is 160, within a 255-byte limit.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    whole, attempt = True, 0
    while True:
        tail = f".{os.getpid()}.{attempt}.tmp"
        name = target.name
        if not whole or len(os.fsencode(f".{name}{tail}")) > _NAME_MAX:
            name = name[:40]
        temp = target.with_name(f".{name}{tail}")
        try:
            return os.open(temp, flags, 0o666), temp
        except FileExistsError:
            attempt += 1
        except OSError as err:   # ENOENT: Windows, for a path too long
            if not whole or err.errno not in (errno.ENAMETOOLONG, errno.ENOENT):
                raise
            whole = False


def _unwritable(out: Path, path: str, err: OSError) -> ConfigError:
    """`err`, met writing `path` into `out`, as an error that names the file
    and says what to do about it. The file is the one the failing call
    named: a rename's target, which may be held open or flagged immutable;
    or `out` itself, or a directory above it, whose path is then to blame."""
    reason = err.strerror or str(err)
    named = err.filename if err.filename2 is None else err.filename2
    failed = out / path
    if isinstance(named, (str, bytes, os.PathLike)):
        failed = Path(os.fsdecode(named))
    above = failed == out or failed in out.parents   # not a name devdocs chose
    if err.errno == errno.EILSEQ or getattr(err, "winerror", None) == _INVALID_NAME:
        reason = f"the file system cannot name it ({reason})"
        if above:
            hint = ("choose an --out whose path is UTF-8"
                    if _SURROGATE.search(os.fspath(out)) else
                    "choose an --out the file system can name")
        else:
            utf8 = " so its name is UTF-8" if _SURROGATE.search(path) else ""
            hint = (f"rename the source file it documents{utf8}, then run `devdocs "
                    "generate` again; or choose an --out on a file system that can "
                    "name it")
    elif err.errno == errno.ENAMETOOLONG:
        shorter = "choose an --out with a shorter path"
        hint = shorter if above else f"shorten the source file's name, or {shorter}"
    elif err.errno in (errno.EACCES, errno.EPERM):
        if err.filename2 is not None:   # a rename, over a file that refuses it
            hint = (f"{failed} is locked or read-only: close any program that has "
                    "it open, or clear its read-only or immutable flag, then run again")
        else:
            hint = f"devdocs needs write access to {failed.parent}"
    elif err.errno == errno.EROFS:
        hint = "choose an --out on a file system devdocs can write to"
    elif err.errno in _FULL:
        hint = "free some space on the disk, then run again"
    elif err.errno in (errno.EISDIR, errno.ENOTDIR, errno.EEXIST):
        hint = "a file or directory is in the way: move it away or choose another --out"
    else:
        hint = f"check that devdocs can write to {out}, or choose another --out"
    where = f"to {out}" if above else f"{path} in {out}"
    return ConfigError(f"cannot write {where}: {reason}", hint=hint)


def _leftovers(
    out: Path, stale: Iterable[str], kept: set[tuple[int, int]]
) -> list[str]:
    """The stale paths, sorted, that still name a regular file inside `out`.

    A path naming the same file as an output (a case-insensitive alias on
    macOS or Windows) is left out, or a renamed source would lose its new page.
    """
    root = out.resolve()
    found: list[str] = []
    for path in sorted(stale):
        target = out / path
        try:
            info = target.lstat()
            inside = target.parent.resolve().is_relative_to(root)
        except (OSError, RuntimeError):   # RuntimeError: symlink loop before 3.13
            continue
        if stat.S_ISREG(info.st_mode) and inside and (
            (info.st_dev, info.st_ino) not in kept
        ):
            found.append(path)
    return found


def _prune(out: Path, stale: Iterable[str]) -> list[str]:
    """Delete each stale file, then the directories that leaves empty. Best
    effort: a failure skips the file."""
    removed: list[str] = []
    for path in stale:
        target = out / path
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
