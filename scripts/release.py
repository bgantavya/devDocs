#!/usr/bin/env python3
"""Release tooling: one version, conventional commits, and tags as the record.

The release model:

- The version lives in one place, `__version__` in devdocs/__init__.py, in
  PEP 440 normal form: X.Y.Z, optionally with .devN, aN, bN or rcN. The npm
  wrapper carries the same version in npm/package.json, spelled as semver
  (4.0.0.dev0 is 4.0.0-dev.0). `bump` writes both files or neither; `check`
  proves they agree.
- Every commit subject is a conventional commit, `type(scope)!: subject`, and
  `lint-commits` enforces it. `feat` asks for a minor release, `fix` and
  `perf` for a patch, and a `!` after the type or a `BREAKING CHANGE:` footer
  for a major one. Other types release nothing.
- Releases are the tags vX.Y.Z merged into HEAD; other tags are ignored.
  `next` decides what is due:
  - a pre-release `__version__` (4.0.0.dev0, 4.0.0rc1) holds every release:
    work toward 4.0.0 can merge without shipping, and ships once
    `bump 4.0.0` makes the version final;
  - a final `__version__` without its tag ships as it is;
  - otherwise the newest tag is bumped by the highest kind among the commits
    made since it, or nothing ships. No version at or below the newest tag is
    ever proposed, so a tag never repeats.
- The release job runs `bump`, commits `chore(release): vX.Y.Z`, tags that
  commit and publishes. Release commits never trigger another release.

Commands, run from the repository root:

    python scripts/release.py current               print __version__
    python scripts/release.py check                 versions agree, or exit 1
    python scripts/release.py next [--json]         the release due now, if any
    python scripts/release.py bump VERSION          write VERSION to both files
    python scripts/release.py lint-commits RANGE    flag non-conventional subjects
    python scripts/release.py notes RANGE           Markdown release notes

Exit status: 0 success; 1 a failed check, or a git or file error; 2 bad usage,
including an invalid version for `bump`. Standard library only, Python 3.11+.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parent.parent
INIT = Path("devdocs/__init__.py")
PACKAGE = Path("npm/package.json")

TYPES = (
    "feat", "fix", "perf", "refactor", "docs", "test",
    "build", "ci", "chore", "style", "revert",
)
SECTIONS = ("Breaking changes", "Features", "Fixes", "Performance")

Kind = Literal["major", "minor", "patch"]
Bump = Literal["major", "minor", "patch", "pending", "held", "none"]

_N = r"(?:0|[1-9][0-9]*)"                          # no leading zeros, ASCII only
_VERSION = re.compile(rf"({_N})\.({_N})\.({_N})(?:(\.dev|a|b|rc)({_N}))?")
_TAG = re.compile(rf"v({_N})\.({_N})\.({_N})")
_SUBJECT = re.compile(
    r"(?P<type>[a-z]+)(?:\((?P<scope>[^()\s]+)\))?(?P<bang>!)?: (?P<description>\S.*)"
)
_BREAKING = re.compile(r"BREAKING[ -]CHANGE:(?P<text>.*)")
_FOOTER = re.compile(r"[A-Za-z][\w-]*(?:: | #)")   # any trailer ends a note
_INIT_VERSION = re.compile(
    r"^__version__[ \t]*=[ \t]*([\"'])(?P<value>[^\"'\n]*)\1", re.MULTILINE
)
_NPM_PRE = {".dev": "dev", "a": "alpha", "b": "beta", "rc": "rc"}
_KIND_OF_TYPE: dict[str, Kind] = {"feat": "minor", "fix": "patch", "perf": "patch"}
_SECTION_OF_TYPE = {"feat": "Features", "fix": "Fixes", "perf": "Performance"}
_RANK: dict[Kind, int] = {"patch": 1, "minor": 2, "major": 3}
_MARKUP = re.compile(r"[\\`*_\[\]<>~&]")
_ENTITY = {"<": "&lt;", ">": "&gt;"}
_URL = (                                            # a bare URL that GitHub links
    r"(?:(?<![A-Za-z])(?i:https?|ftp)://|(?<![^\s(*_~])www\.)"
    r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*(?![\w.-])[^\s<>]*"
)
_VERBATIM = re.compile(rf"`[^`]+`|(?<![\w@])@[A-Za-z0-9][\w-]*(?:/[\w.-]+)?|{_URL}")
_BLOCK_START = re.compile(r"(?:#{1,6}|[-+]|[0-9]{1,9}[.)])(?=\s|$)|-(?=[-\s]*$)")


class ReleaseError(Exception):
    """A problem the user can fix: a missing file, a bad version line, git failing."""


# -- versions ----------------------------------------------------------------


@dataclass(frozen=True)
class Version:
    """A PEP 440 version in normal form; `pre` is "", ".dev", "a", "b" or "rc"."""

    major: int
    minor: int
    patch: int
    pre: str = ""
    number: int = 0

    @property
    def core(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    @property
    def final(self) -> Version:
        """The release this version leads to: 4.0.0.dev0 -> 4.0.0."""
        return Version(self.major, self.minor, self.patch)

    def bumped(self, kind: Kind) -> Version:
        """The next final version after this one for a release of `kind`."""
        if kind == "major":
            return Version(self.major + 1, 0, 0)
        if kind == "minor":
            return Version(self.major, self.minor + 1, 0)
        return Version(self.major, self.minor, self.patch + 1)

    def __str__(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        return f"{core}{self.pre}{self.number}" if self.pre else core


def parse_version(text: str) -> Version:
    """Parse X.Y.Z with an optional .devN, aN, bN or rcN; ValueError otherwise.

    Only PEP 440 normal forms pass, so every accepted string maps to exactly
    one npm version and back.
    """
    match = _VERSION.fullmatch(text)
    if match is None:
        raise ValueError(
            f"{text!r} is not a release version: want X.Y.Z, "
            "optionally followed by .devN, aN, bN or rcN"
        )
    major, minor, patch, pre, number = match.groups()
    return Version(int(major), int(minor), int(patch), pre or "", int(number or 0))


def tag_version(name: str) -> Version | None:
    """The version a release tag names; None for anything but vX.Y.Z."""
    match = _TAG.fullmatch(name)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return Version(major, minor, patch)


def npm_version(version: str) -> str:
    """The semver spelling npm needs, e.g. 4.0.0.dev0 -> 4.0.0-dev.0.

    .devN, aN, bN and rcN become -dev.N, -alpha.N, -beta.N and -rc.N; anything
    `parse_version` refuses raises ValueError.
    """
    parsed = parse_version(version)
    core = str(parsed.final)
    return f"{core}-{_NPM_PRE[parsed.pre]}.{parsed.number}" if parsed.pre else core


# -- commits -----------------------------------------------------------------


@dataclass(frozen=True)
class Commit:
    """A conventional commit message: `type(scope)!: description`, plus footers.

    `notes` holds the text of each BREAKING CHANGE footer that has any.
    """

    type: str
    scope: str | None
    description: str
    breaking: bool = False
    notes: tuple[str, ...] = ()

    @property
    def is_release(self) -> bool:
        """A `chore(release)` commit: it records a release and never asks for one."""
        return self.type == "chore" and self.scope == "release"

    @property
    def kind(self) -> Kind | None:
        """The release this commit asks for, if any."""
        if self.is_release:
            return None
        return "major" if self.breaking else _KIND_OF_TYPE.get(self.type)


def parse_commit(message: str) -> Commit | None:
    """Parse a full commit message; None unless its subject is conventional.

    The subject is the first non-blank line: a lowercase type from `TYPES`, an
    optional scope without spaces, an optional `!`, then ": " and the
    description. A `BREAKING CHANGE:` (or `BREAKING-CHANGE:`) footer at the
    start of any later line marks the commit as breaking.
    """
    lines = message.splitlines()
    while lines and not lines[0].strip():
        del lines[0]
    match = _SUBJECT.fullmatch(lines[0].rstrip()) if lines else None
    if match is None or match["type"] not in TYPES:
        return None
    notes = _breaking_notes(lines[1:])
    return Commit(
        type=match["type"],
        scope=match["scope"],
        description=match["description"],
        breaking=match["bang"] is not None or bool(notes),
        notes=tuple(note for note in notes if note),
    )


def _breaking_notes(lines: Sequence[str]) -> list[str]:
    """The text of each BREAKING CHANGE footer; "" for one with no text.

    A footer's text runs on over the following lines until a blank line or
    the next `Token: ` / `Token #` trailer, as Conventional Commits defines.
    """
    notes: list[str] = []
    current: list[str] | None = None
    for line in lines:
        found = _BREAKING.match(line)
        if found is not None:
            if current is not None:
                notes.append(" ".join(current))
            text = found["text"].strip()
            current = [text] if text else []
        elif current is not None:
            if line.strip() and not _FOOTER.match(line):
                current.append(line.strip())
            else:
                notes.append(" ".join(current))
                current = None
    if current is not None:
        notes.append(" ".join(current))
    return notes


def classify(message: str) -> Kind | None:
    """The release one commit message asks for: major, minor, patch or None."""
    commit = parse_commit(message)
    return commit.kind if commit is not None else None


def subject_of(message: str) -> str:
    """The first non-blank line of a commit message."""
    return next((line for line in message.splitlines() if line.strip()), "")


# -- the next release --------------------------------------------------------


@dataclass(frozen=True)
class Release:
    """What `next` decided. `as_dict` is the JSON contract; `warning` is extra.

    `previous` is the newest vX.Y.Z tag merged into HEAD, as a tag name, so it
    can start a git range; `version` and `tag` are "" when nothing is due.
    """

    release: bool
    version: str
    tag: str
    previous: str | None
    bump: Bump
    warning: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "release": self.release,
            "version": self.version,
            "tag": self.tag,
            "previous": self.previous,
            "bump": self.bump,
        }

    def as_json(self) -> str:
        """One line, so a workflow can write it to $GITHUB_OUTPUT as it is."""
        return json.dumps(self.as_dict())


def latest_release(tags: Iterable[str]) -> Version | None:
    """The highest vX.Y.Z among `tags`, compared as versions, not as text."""
    versions = [version for version in map(tag_version, tags) if version is not None]
    return max(versions, key=lambda version: version.core, default=None)


def next_release(current: str, tags: Iterable[str], messages: Iterable[str]) -> Release:
    """Decide the release due now.

    `current` is `__version__`, `tags` the tags merged into HEAD, and
    `messages` the full messages of the commits made since the newest vX.Y.Z
    among them (all commits when there is none). A pre-release holds every
    release, so unfinished work can merge; a final version ships as it is.
    When the version is not above the newest tag, because its tag exists or
    it fell behind, the newest tag is bumped by the highest kind asked for.
    """
    version = parse_version(current)
    latest = latest_release(tags)
    previous = f"v{latest}" if latest is not None else None
    kinds = [kind for kind in map(classify, messages) if kind is not None]
    kind = max(kinds, key=_RANK.__getitem__, default=None)
    nothing = Release(False, "", "", previous, "none")
    target = version.final
    if version.pre:
        return Release(False, "", "", previous, "held",
                       f"{current} is a pre-release, so nothing ships until "
                       f"`bump {target}` makes it final")
    if latest is None or target.core > latest.core:
        warning = ""
        if latest is not None and kind is not None:
            implied = latest.bumped(kind)
            if implied.core > target.core:
                warning = (
                    f"commits since {previous} call for a {kind} release "
                    f"({implied}), but __version__ is {current}"
                )
        return Release(True, str(target), f"v{target}", previous, "pending", warning)
    if kind is None:
        return nothing
    new = latest.bumped(kind)
    return Release(True, str(new), f"v{new}", previous, kind)


# -- release notes -----------------------------------------------------------


def escape_markdown(text: str) -> str:
    """`text` made literal for one line of Markdown, as GitHub renders it.

    Markup characters, `&` among them, are escaped outside code spans and
    URLs, which stay as written so that GitHub still links the URLs. `<` and
    `>` are written as entities instead: GitHub extends a bare URL's link to
    the next space or `<`, over any backslash, which would free a `<` to open
    an HTML tag, but it leaves a final entity out. A leading heading, list or
    thematic break marker is defused, and @handles become code spans, so
    publishing the notes on GitHub notifies nobody.
    """
    parts: list[str] = []
    end = 0
    for match in _VERBATIM.finditer(text):
        parts.append(_MARKUP.sub(_escape, text[end:match.start()]))
        token = match.group()
        parts.append(f"`{token}`" if token.startswith("@") else token)
        end = match.end()
    parts.append(_MARKUP.sub(_escape, text[end:]))
    escaped = "".join(parts)
    block = _BLOCK_START.match(escaped)
    if block is not None:
        marker = block.group()
        cut = len(marker) - 1 if marker[-1] in ".)" else 0
        escaped = f"{escaped[:cut]}\\{escaped[cut:]}"
    return escaped


def _escape(match: re.Match[str]) -> str:
    char = match.group()
    return _ENTITY.get(char, f"\\{char}")


def render_notes(commits: Iterable[tuple[str, str]]) -> str:
    """Markdown release notes from (short hash, message) pairs, in commit order.

    Breaking changes come first, each with its footer text; then features,
    fixes and performance work. Other types and release commits are left out,
    and each commit appears once.
    """
    sections: dict[str, list[str]] = {title: [] for title in SECTIONS}
    for short, message in commits:
        commit = parse_commit(message)
        if commit is None or commit.is_release:
            continue
        title = _SECTION_OF_TYPE.get(commit.type)
        if commit.breaking:
            title = "Breaking changes"
        if title is not None:
            sections[title].append(_bullet(commit, short))
    blocks = [
        f"### {title}\n\n" + "\n".join(items)
        for title, items in sections.items()
        if items
    ]
    return "\n\n".join(blocks) + "\n" if blocks else "No notable changes.\n"


def _bullet(commit: Commit, short: str) -> str:
    scope = f"**{escape_markdown(commit.scope)}:** " if commit.scope else ""
    lines = [f"- {scope}{escape_markdown(commit.description)} ({short})"]
    lines += [f"  - {escape_markdown(note)}" for note in commit.notes]
    return "\n".join(lines)


# -- version files -----------------------------------------------------------


def read_version(text: str) -> str:
    """The `__version__` value in the text of devdocs/__init__.py."""
    return _version_line(text)["value"]


def rewrite_init(text: str, version: str) -> str:
    """devdocs/__init__.py text with only the `__version__` value replaced."""
    line = _version_line(text)
    return text[:line.start("value")] + version + text[line.end("value"):]


def _version_line(text: str) -> re.Match[str]:
    lines = list(_INIT_VERSION.finditer(text))
    if len(lines) != 1:
        raise ReleaseError(f"{INIT}: expected one __version__ line, found {len(lines)}")
    return lines[0]


def package_version(text: str) -> str:
    """The top-level "version" in the text of npm/package.json."""
    return _package(text)[1]


def rewrite_package(text: str, version: str) -> str:
    """npm/package.json text with only the top-level "version" value replaced.

    The new value is spliced into the original text rather than re-serialised,
    so key order, indentation, escapes and line endings all survive; the
    result must parse to the old data with the new version, or nothing is
    returned.
    """
    data, _ = _package(text)
    start, end = _top_level_span(text, "version")
    new = text[:start] + json.dumps(version) + text[end:]
    if json.loads(new) != {**data, "version": version}:
        raise ReleaseError(f"{PACKAGE}: the rewrite changed more than the version")
    return new


def _package(text: str) -> tuple[dict[str, Any], str]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ReleaseError(f"{PACKAGE}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("version"), str):
        raise ReleaseError(f'{PACKAGE}: no top-level "version" string')
    return data, data["version"]


def _top_level_span(text: str, key: str) -> tuple[int, int]:
    """Where `key`'s value sits in the top-level object of valid JSON `text`.

    Walks the top-level members with the json decoder itself, so an object
    nested deeper that has a key of the same name is never touched.
    """
    decoder = json.JSONDecoder()
    spans: list[tuple[int, int]] = []
    at = _skip_space(text, _skip_space(text, 0) + 1)          # past "{"
    while text[at] == '"':
        name, at = decoder.raw_decode(text, at)
        start = _skip_space(text, _skip_space(text, at) + 1)  # past ":"
        _, end = decoder.raw_decode(text, start)
        if name == key:
            spans.append((start, end))
        at = _skip_space(text, end)
        if text[at] == ",":
            at = _skip_space(text, at + 1)
    if len(spans) != 1:
        raise ReleaseError(
            f'{PACKAGE}: expected one top-level "{key}", found {len(spans)}'
        )
    return spans[0]


def _skip_space(text: str, at: int) -> int:
    while at < len(text) and text[at] in " \t\n\r":
        at += 1
    return at


def _read(path: Path) -> str:
    """A file's exact text; bytes are decoded as-is so line endings survive."""
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError as exc:
        raise ReleaseError(f"{path}: no such file") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"{path}: {exc}") from exc


def _write_all(changes: Sequence[tuple[Path, str, str]]) -> None:
    """Give each (path, old text, new text) its new text: every file or none.

    A file that is not writable is refused before anything changes. Each file
    is replaced in one rename, so it is never seen half-written, and when one
    fails, the files already replaced get their old text back the same way.
    """
    for path, _, _ in changes:
        if not os.access(path, os.W_OK):
            raise ReleaseError(f"{path}: not writable")
    replaced: list[tuple[Path, str]] = []
    try:
        for path, old, new in changes:
            _replace_text(path, new)
            replaced.append((path, old))
    except BaseException as exc:
        for done, old in replaced:
            _replace_text(done, old)
        if not isinstance(exc, OSError):
            raise
        reason = exc.strerror or str(exc)
        raise ReleaseError(f"{path}: {reason}; no file was changed") from exc


def _replace_text(path: Path, text: str) -> None:
    """Rename over `path` a temp file beside it that has `text` and its mode."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(text.encode("utf-8"))
        os.chmod(temp, stat.S_IMODE(path.stat().st_mode))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)                # gone after a successful rename


# -- git ---------------------------------------------------------------------


def git(root: Path, *args: str) -> str:
    """Run git in `root` and return its standard output; ReleaseError on failure.

    Every git call goes through here, so tests can replace this one function.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ReleaseError("git is not installed") from exc
    except subprocess.CalledProcessError as exc:
        reason = (exc.stderr or "").strip() or f"exit status {exc.returncode}"
        raise ReleaseError(f"git {args[0]}: {reason}") from exc
    return done.stdout


def log(root: Path, revisions: str) -> list[tuple[str, str]]:
    """(short hash, full message) of each non-merge commit in `revisions`.

    Oldest first, in topological order, so the result does not depend on
    commit dates. `--end-of-options` makes a range that starts with "-" a
    revision, never an option.
    """
    out = git(
        root, "log", "--no-merges", "--topo-order", "--reverse",
        "--no-show-signature", "-z", "--format=%h%x1f%B",
        "--end-of-options", revisions, "--",
    )
    commits: list[tuple[str, str]] = []
    for record in out.split("\0"):
        short, found, message = record.partition("\x1f")
        if found:
            commits.append((short.strip(), message))
    return commits


# -- commands ----------------------------------------------------------------


def cmd_current(args: argparse.Namespace) -> int:
    print(read_version(_read(args.root / INIT)))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    version = read_version(_read(args.root / INIT))
    package = package_version(_read(args.root / PACKAGE))
    try:
        expected: str | None = npm_version(version)
    except ValueError:
        expected = None                             # equal to no package version
    if package == expected:
        print(f"versions agree: {version} (npm {package})")
        return 0
    wanted = f"npm {expected}" if expected else "not a release version"
    print(f"{INIT.as_posix()}: {version} ({wanted})", file=sys.stderr)
    print(f"{PACKAGE.as_posix()}: {package}", file=sys.stderr)
    if expected:
        print(f"fix with: python scripts/release.py bump {version}", file=sys.stderr)
    return 1


def cmd_next(args: argparse.Namespace) -> int:
    current = read_version(_read(args.root / INIT))
    if git(args.root, "rev-parse", "--is-shallow-repository").strip() == "true":
        raise ReleaseError(
            "shallow clone: fetch all history and tags first "
            "(actions/checkout with fetch-depth: 0)"
        )
    tags = git(args.root, "tag", "--merged", "HEAD", "--list", "v*").split()
    latest = latest_release(tags)
    since = f"v{latest}..HEAD" if latest is not None else "HEAD"
    messages = [message for _, message in log(args.root, since)]
    release = next_release(current, tags, messages)
    if release.warning:
        print(f"warning: {release.warning}", file=sys.stderr)
    if args.json:
        print(release.as_json())
    elif release.release:
        print(release.version)
    return 0


def cmd_bump(args: argparse.Namespace) -> int:
    try:
        npm = npm_version(args.version)
    except ValueError as exc:
        print(f"release.py bump: {exc}", file=sys.stderr)
        return 2
    init_path, package_path = args.root / INIT, args.root / PACKAGE
    init, package = _read(init_path), _read(package_path)
    _write_all([                                    # both rewritten before any write
        (init_path, init, rewrite_init(init, args.version)),
        (package_path, package, rewrite_package(package, npm)),
    ])
    print(f"{INIT.as_posix()}: {read_version(init)} -> {args.version}")
    print(f"{PACKAGE.as_posix()}: {package_version(package)} -> {npm}")
    return 0


def cmd_lint_commits(args: argparse.Namespace) -> int:
    commits = log(args.root, args.range)
    offenders = [
        (short, subject_of(message) or "(empty message)")
        for short, message in commits
        if parse_commit(message) is None
    ]
    for short, subject in offenders:
        print(f"{short} {subject}")
    checked = f"{len(commits)} commit{'' if len(commits) == 1 else 's'}"
    if offenders:
        print(
            f"not conventional: {len(offenders)} of {checked}. Write subjects as "
            f"type(scope)!: subject, where type is one of {', '.join(TYPES)}.",
            file=sys.stderr,
        )
        return 1
    print(f"{checked} checked: all conventional", file=sys.stderr)
    return 0


def cmd_notes(args: argparse.Namespace) -> int:
    print(render_notes(log(args.root, args.range)), end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release.py",
        description="Versions, commit rules and release notes for devdocs.",
        epilog="Exit status: 0 success, 1 failed check or error, 2 bad usage.",
    )
    parser.add_argument(
        "--root", type=Path, default=ROOT, metavar="PATH",
        help="repository to work on (default: the one holding this script)",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    current = commands.add_parser("current", help="print __version__")
    current.set_defaults(handler=cmd_current)

    check = commands.add_parser(
        "check", help="exit 1 unless npm/package.json matches __version__"
    )
    check.set_defaults(handler=cmd_check)

    upcoming = commands.add_parser(
        "next", help="print the release due now, or nothing when none is"
    )
    upcoming.add_argument(
        "--json", action="store_true",
        help="print {release, version, tag, previous, bump} as one JSON object",
    )
    upcoming.set_defaults(handler=cmd_next)

    bump = commands.add_parser(
        "bump", help="write VERSION to devdocs/__init__.py and npm/package.json"
    )
    bump.add_argument(
        "version", metavar="VERSION", help="X.Y.Z, optionally with .devN, aN, bN or rcN"
    )
    bump.set_defaults(handler=cmd_bump)

    lint = commands.add_parser(
        "lint-commits", help="list commits in RANGE whose subject is not conventional"
    )
    lint.add_argument("range", metavar="RANGE", help="git range, e.g. origin/v4..HEAD")
    lint.set_defaults(handler=cmd_lint_commits)

    notes = commands.add_parser("notes", help="print Markdown release notes for RANGE")
    notes.add_argument("range", metavar="RANGE", help="git range, e.g. v4.0.0..HEAD")
    notes.set_defaults(handler=cmd_notes)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and return its exit status.

    Standard output is UTF-8 whatever the locale, as it carries commit
    subjects: on Windows, a redirected stream would otherwise use the ANSI
    code page and fail on the first character outside it.
    """
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        return handler(args)
    except (ReleaseError, OSError, ValueError) as exc:
        print(f"release.py {args.command}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
