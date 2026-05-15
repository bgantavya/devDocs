"""A gitignore matcher, because respecting .gitignore is not optional.

Every project already states what is noise — build output, vendored code,
caches — in its .gitignore. Reading that is far more accurate than the
hardcoded name list v3 used, and it costs nothing at generation time.

This implements git's rules directly rather than depending on `pathspec`,
which keeps the runtime dependency count at zero; real git is the reference it
is tested against.

- Lines: a UTF-8 BOM is skipped, `#` starts a comment only as the first
  character, only trailing spaces are trimmed (unless escaped), and `\\`
  escapes the next character, so `\\#`, `\\!` and `\\ ` are literals.
- Patterns: `!` negates, a trailing `/` matches directories only, any other
  `/` anchors the pattern to its file's directory, and a pattern without one
  matches a name at any depth. Globs follow git's wildmatch: `*` and `?` stop
  at `/`; `**` crosses directories only as a whole segment (`**/`, `/**/`,
  `/**`) and is a plain `*` elsewhere; `[...]` takes `!`/`^`, ranges, POSIX
  classes and escapes, and never matches `/`. Each pattern becomes an escaped
  regex once; a line that still cannot compile is skipped, never raised.
- Precedence: within a file the last matching line decides, a deeper file
  beats a shallower one, and `info/exclude` ranks below them all. A path inside
  an excluded directory stays excluded whatever later lines say, because git
  cannot re-include a file whose parent directory is excluded.
- Repository: `from_root` finds the enclosing work tree, applies its
  .gitignore files from the top down to the root plus `info/exclude`, and
  honours the repository's `core.ignorecase`. The global excludes file is never
  read, so results do not depend on the machine. Nested files are read lazily,
  when a path beneath them is first matched, and never inside an excluded
  directory, as git does: a walk that prunes ignored directories never reads
  into them.

Known differences from git: an unclosed `[` is a literal (git's wildmatch
makes the whole pattern match nothing); matching works on characters, not
bytes, so `?` and `[...]` consume a whole non-ASCII character; and the root
given to `from_root` is never itself ignored, since scanning it was asked for.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath

__all__ = ["GitignoreMatcher", "Rule"]

# Glob token kinds: literal regex, `*`, a whole-segment `**/`, and `**` at the end.
_FIXED, _STAR, _DIRS, _ANY = range(4)
_STARS = {_STAR: "[^/]*", _DIRS: "(?:[^/]*/)*", _ANY: ".*"}
_NEVER = "(?!)"

# Bracket classes as regex set bodies; wildmatch only knows ASCII.
_POSIX_CLASSES: dict[str, str] = {
    "alnum": "0-9A-Za-z",
    "alpha": "A-Za-z",
    "blank": " \\t",
    "cntrl": "\\x00-\\x1f\\x7f",
    "digit": "0-9",
    "graph": "!-~",
    "lower": "a-z",
    "print": " -~",
    "punct": "!-/:-@\\[-`{-~",
    "space": " \\t\\n\\r\\x0b\\x0c",
    "upper": "A-Z",
    "xdigit": "0-9A-Fa-f",
}

_SECTION = re.compile(r'\[\s*([A-Za-z0-9.-]+)\s*("(?:[^"\\]|\\.)*")?\s*\]')
_TRUE = {"true", "yes", "on"}


def _literal(char: str, ignore_case: bool) -> str:
    """Escape one literal; under ignorecase git never matches an uppercase one.

    Git folds the path but not these pattern characters, so `[A]` and `\\A`
    match nothing when core.ignorecase is set.
    """
    if ignore_case and "A" <= char <= "Z":
        return ""
    return re.escape(char)


def _bracket(pattern: str, start: int, ignore_case: bool) -> tuple[str, int] | None:
    """Translate the `[...]` at `start` as wildmatch reads it.

    Returns the regex and the index after the closing `]`, or None when the
    bracket never closes.
    """
    index = start + 1
    length = len(pattern)
    negated = index < length and pattern[index] in "!^"
    if negated:
        index += 1
    members: list[str] = []
    valid = True
    previous = ""   # wildmatch's prev_ch: a literal that can open a range
    first = True
    while True:
        if index >= length:
            return None
        char = pattern[index]
        if char == "]" and not first:
            break
        first = False
        if char == "\\":
            index += 1
            if index >= length:
                return None
            previous = pattern[index]
            members.append(_literal(previous, ignore_case))
        elif (
            char == "-" and previous and index + 1 < length
            and pattern[index + 1] != "]"
        ):
            index += 1
            end = pattern[index]
            if end == "\\":
                index += 1
                if index >= length:
                    return None
                end = pattern[index]
            if previous <= end:
                members.append(f"{re.escape(previous)}-{re.escape(end)}")
            previous = ""
        elif char == "[" and pattern.startswith(":", index + 1):
            close = pattern.find("]", index + 2)
            if close < 0:
                return None
            if close - index < 4 or pattern[close - 1] != ":":
                members.append(re.escape(char))   # no `:]`: a plain `[`
                previous = char
            else:
                posix = _POSIX_CLASSES.get(pattern[index + 2 : close - 1])
                if posix is None:
                    valid = False   # git rejects an unknown class name
                else:
                    members.append(posix)
                previous = ""
                index = close
        else:
            members.append(_literal(char, ignore_case))
            previous = char
        index += 1

    body = "".join(members)
    if not valid:
        return _NEVER, index + 1
    if negated:
        return f"[^/{body}]", index + 1
    if not body:
        return _NEVER, index + 1
    return f"(?!/)[{body}]", index + 1


def _translate(pattern: str, *, ignore_case: bool = False) -> str:
    """Compile one gitignore glob into a regex body, as git's wildmatch reads it.

    Handled separately from `fnmatch` because gitignore's `*` must not cross a
    `/`, and a whole-segment `**` must.
    """
    tokens: list[tuple[int, str]] = []
    index = 0
    length = len(pattern)

    while index < length:
        char = pattern[index]

        if char == "*":
            end = index
            while end < length and pattern[end] == "*":
                end += 1
            whole = (
                end - index > 1
                and (index == 0 or pattern[index - 1] == "/")
                and (end == length or pattern.startswith(("/", "\\/"), end))
            )
            if not whole:
                tokens.append((_STAR, ""))
            elif end < length and pattern[end] == "/":
                tokens.append((_DIRS, ""))
                end += 1
            else:
                tokens.append((_ANY, ""))
            index = end

        elif char == "?":
            tokens.append((_FIXED, "[^/]"))
            index += 1

        elif char == "[":
            parsed = _bracket(pattern, index, ignore_case)
            if parsed is None:
                tokens.append((_FIXED, re.escape(char)))   # unclosed: literal
                index += 1
            else:
                tokens.append((_FIXED, parsed[0]))
                index = parsed[1]

        elif char == "\\":
            escaped = pattern[index + 1 : index + 2]
            # A trailing `\` never matches in git.
            tokens.append((_FIXED, _literal(escaped, ignore_case) or _NEVER))
            index += 2

        else:
            tokens.append((_FIXED, re.escape(char)))
            index += 1

    return _assemble(tokens)


def _assemble(tokens: list[tuple[int, str]]) -> str:
    """Join glob tokens into a regex that cannot backtrack catastrophically.

    A `*` reaches to the next star of any kind, a `**` to the next `**`. When
    another follows, what lies between is matched once, leftmost, in an atomic
    group: the following star can absorb any slack (between two `**` the text
    spans whole segments), so no later choice could succeed where it fails.
    """
    out: list[str] = []
    index = 0
    count = len(tokens)
    while index < count:
        kind, text = tokens[index]
        index += 1
        if kind == _FIXED:
            out.append(text)
            continue
        stops: tuple[int, ...] = (_DIRS, _ANY)
        if kind == _STAR:
            stops = (_STAR, *stops)
        stop = index
        while stop < count and tokens[stop][0] not in stops:
            stop += 1
        star = _STARS[kind]
        body = _assemble(tokens[index:stop])
        atomic = stop < count and (bool(body) or kind != _STAR)
        out.append(f"(?>{star}?{body})" if atomic else f"{star}{body}")
        index = stop
    return "".join(out)


def _trim_trailing_spaces(line: str) -> str:
    """Drop trailing spaces, not tabs, unless escaped: git's trim_trailing_spaces."""
    cut = -1
    index = 0
    while index < len(line):
        char = line[index]
        if char == " ":
            if cut < 0:
                cut = index
        else:
            cut = -1
            if char == "\\":
                index += 1
                if index >= len(line):
                    return line
        index += 1
    return line if cut < 0 else line[:cut]


@dataclass(frozen=True)
class Rule:
    """One compiled gitignore line.

    `regex` fullmatches a path relative to `base`, the directory the rule came
    from, so a nested .gitignore only governs paths beneath it. `under` is no
    longer used and stays only so existing constructors keep working.
    """

    regex: re.Pattern[str]
    negated: bool
    dir_only: bool
    source: str
    base: str = ""
    under: re.Pattern[str] | None = None

    def matches(self, relpath: str, is_dir: bool) -> bool:
        """True when this line's pattern matches exactly this path.

        Parent directories are the matcher's business: `build/` matches the
        directory build, not build/out.js.
        """
        if self.dir_only and not is_dir:
            return False
        if self.base:
            if not relpath.startswith(f"{self.base}/"):
                return False
            relpath = relpath[len(self.base) + 1 :]
        return self.regex.fullmatch(relpath) is not None

    @classmethod
    def parse(
        cls, line: str, *, base: str = "", ignore_case: bool = False
    ) -> Rule | None:
        """Compile one line, or None when it is blank, a comment, or unusable."""
        raw = line.removesuffix("\n").removesuffix("\r")
        if not raw or raw.startswith("#"):
            return None

        text = _trim_trailing_spaces(raw)
        negated = text.startswith("!")
        if negated:
            text = text[1:]
        dir_only = text.endswith("/")
        if dir_only:
            text = text[:-1]

        # A slash anchors the pattern to its file's directory; without one it
        # matches the last path segment at any depth.
        anchored = "/" in text
        text = text.removeprefix("/")
        if not text:
            return None

        body = _translate(text, ignore_case=ignore_case)
        if not anchored:
            body = f"(?:[^/]*/)*+{body}"
        flags = re.DOTALL | (re.IGNORECASE | re.ASCII if ignore_case else 0)
        try:
            regex = re.compile(body, flags)
        except (re.error, OverflowError, RecursionError):
            return None
        return cls(regex, negated, dir_only, raw, base.strip("/"))


def _last(rules: list[Rule] | None, path: str, is_dir: bool) -> Rule | None:
    """The last of `rules` matching `path`, which is relative to their base."""
    for rule in reversed(rules or ()):
        if (is_dir or not rule.dir_only) and rule.regex.fullmatch(path):
            return rule
    return None


class GitignoreMatcher:
    """Decides whether a path is ignored, given a set of rules.

    Built by `from_root`, which reads the repository's ignore files as paths
    are matched, or by `from_lines` for explicit rules and tests. `rules` holds
    every rule loaded so far, in load order; appending to it is supported.
    """

    def __init__(
        self, rules: list[Rule] | None = None, *, ignore_case: bool = False
    ) -> None:
        self.rules: list[Rule] = rules if rules is not None else []
        self.ignore_case = ignore_case
        self._root: Path | None = None    # where nested .gitignore files live
        self._offset = ""                 # root, relative to the work-tree top
        self._fallback: list[Rule] = []   # `extra`, relative to root, ranked last
        self._by_base: dict[str, list[Rule]] = {}
        self._depth = 0                   # segments in the deepest base
        self._indexed = -1
        self._dirs: dict[str, Rule | None] = {}
        self._loaded: set[str] = set()

    @classmethod
    def from_lines(
        cls, lines: list[str], *, base: str = "", ignore_case: bool = False
    ) -> GitignoreMatcher:
        return cls(_parse_all(lines, base, ignore_case), ignore_case=ignore_case)

    @classmethod
    def from_root(
        cls, root: Path, *, extra: list[str] | None = None
    ) -> GitignoreMatcher:
        """Match paths beneath `root`, inside whatever git work tree holds it.

        Reads core.ignorecase, `info/exclude` and the .gitignore files from the
        work-tree top down to `root` now; nested ones as paths reach them.
        `extra` lines are relative to `root` and rank below every file.
        """
        try:
            real = Path(root).resolve()
        except (OSError, RuntimeError):
            real = Path(os.path.abspath(root))
        top, git_dir = _work_tree(real)
        ignore_case = git_dir is not None and _ignorecase(git_dir)

        matcher = cls(ignore_case=ignore_case)
        matcher._root = real
        matcher._offset = "" if real == top else real.relative_to(top).as_posix()
        matcher._fallback = _parse_all(extra or [], "", ignore_case)
        matcher.rules.extend(matcher._fallback)
        matcher._sync()

        if git_dir is not None:
            exclude = git_dir / "info" / "exclude"
            matcher._append(_read_rules(exclude, "", ignore_case, in_tree=False))
        parts = matcher._offset.split("/") if matcher._offset else []
        for depth in range(len(parts) + 1):
            path = top.joinpath(*parts[:depth], ".gitignore")
            base = "/".join(parts[:depth])
            matcher._append(_read_rules(path, base, ignore_case, in_tree=True))
        return matcher

    def match(self, relpath: str | PurePath, *, is_dir: bool = False) -> Rule | None:
        """Return the rule that decides this path, or None if nothing matches.

        A path inside an excluded directory is decided by the rule excluding
        that directory. A negation returns None because the path ends up *not*
        ignored. Callers wanting a boolean use `ignored`.

        A str may use either separator, and a trailing slash marks a
        directory; a path object keeps its own flavour, so a backslash inside
        a POSIX name stays part of that name.
        """
        if isinstance(relpath, PurePath):
            text = relpath.as_posix()
        else:
            text = relpath.replace("\\", "/")
            is_dir = is_dir or text.endswith("/")
            text = PurePosixPath(text).as_posix()
        text = text.lstrip("/")
        if text in ("", "."):
            return None

        self._sync()
        parent = text.rpartition("/")[0]
        if parent:
            excluded = self._excluded(parent)
            if excluded is not None:
                return excluded
        return self._decide(text, is_dir)

    def ignored(self, relpath: str | PurePath, *, is_dir: bool = False) -> bool:
        return self.match(relpath, is_dir=is_dir) is not None

    def __len__(self) -> int:
        return len(self.rules)

    def _decide(self, relpath: str, is_dir: bool) -> Rule | None:
        """The path's own verdict, parents aside: git's list precedence.

        Rules are tried by base, deepest first, and from the last line up; the
        first that matches decides. `extra` comes after every file.
        """
        path = f"{self._offset}/{relpath}" if self._offset else relpath
        cuts: list[int] = []
        cut = -1
        while len(cuts) < self._depth:
            cut = path.find("/", cut + 1)
            if cut < 0:
                break
            cuts.append(cut)
        found: Rule | None = None
        for cut in reversed(cuts):
            found = _last(self._by_base.get(path[:cut]), path[cut + 1 :], is_dir)
            if found is not None:
                break
        else:
            found = _last(self._by_base.get(""), path, is_dir)
            if found is None:
                found = _last(self._fallback, relpath, is_dir)
        return None if found is None or found.negated else found

    def _excluded(self, directory: str) -> Rule | None:
        """The rule excluding `directory` or one of its ancestors, if any.

        Walks down from the nearest decided ancestor, reading each directory's
        .gitignore once it is known not to be excluded, which is git's order.
        """
        if directory in self._dirs:
            return self._dirs[directory]
        pending = [directory]
        parent = directory.rpartition("/")[0]
        while parent and parent not in self._dirs:
            pending.append(parent)
            parent = parent.rpartition("/")[0]
        decided = self._dirs.get(parent) if parent else None
        for current in reversed(pending):
            if decided is None:
                decided = self._decide(current, True)
                if decided is None:
                    self._load(current)
            self._dirs[current] = decided
        return decided

    def _load(self, directory: str) -> None:
        if self._root is None or directory in self._loaded:
            return
        self._loaded.add(directory)
        if ".." in directory.split("/"):
            return   # never read above the root
        base = f"{self._offset}/{directory}" if self._offset else directory
        path = self._root / directory / ".gitignore"
        self._append(_read_rules(path, base, self.ignore_case, in_tree=True))

    def _append(self, rules: list[Rule]) -> None:
        self._sync()
        for rule in rules:
            self.rules.append(rule)
            self._index(rule)
        self._indexed = len(self.rules)

    def _index(self, rule: Rule) -> None:
        self._by_base.setdefault(rule.base, []).append(rule)
        if rule.base:
            self._depth = max(self._depth, rule.base.count("/") + 1)

    def _sync(self) -> None:
        """Re-index when `rules` was changed from outside."""
        if len(self.rules) == self._indexed:
            return
        fallback = {id(rule) for rule in self._fallback}
        self._by_base = {}
        self._depth = 0
        for rule in self.rules:
            if id(rule) not in fallback:
                self._index(rule)
        self._indexed = len(self.rules)
        self._dirs.clear()


def _read_text(path: Path, *, follow: bool = True) -> str:
    """A regular file's text, or "" when it is missing, unreadable or not a file.

    Anything but a regular file is never opened: reading a FIFO would block.
    """
    try:
        if not stat.S_ISREG(os.stat(path, follow_symlinks=follow).st_mode):
            return ""
        data = path.read_bytes()
    except (OSError, ValueError):
        return ""   # unreadable ignore file must not end the scan
    return data.decode("utf-8", errors="replace")


def _parse_all(lines: list[str], base: str, ignore_case: bool) -> list[Rule]:
    parsed = (Rule.parse(line, base=base, ignore_case=ignore_case) for line in lines)
    return [rule for rule in parsed if rule is not None]


def _read_rules(
    path: Path, base: str, ignore_case: bool, *, in_tree: bool
) -> list[Rule]:
    """Parse one ignore file; a missing or unreadable one gives no rules.

    Like git, a .gitignore inside the work tree is never read through a symlink.
    """
    text = _read_text(path, follow=not in_tree).removeprefix("\ufeff")
    return _parse_all(text.split("\n"), base, ignore_case)


def _work_tree(start: Path) -> tuple[Path, Path | None]:
    """The work-tree top holding `start`, and its (common) git directory.

    Stops at the first `.git`, directory or file, as git does; returns
    `(start, None)` outside any repository.
    """
    for directory in (start, *start.parents):
        dotgit = directory / ".git"
        try:
            is_dir = stat.S_ISDIR(os.stat(dotgit).st_mode)
        except (OSError, ValueError):
            continue
        if is_dir:
            return directory, _common_dir(dotgit)
        text = _read_text(dotgit)
        if not text.startswith("gitdir:"):
            return directory, None
        return directory, _common_dir(directory / text[7:].strip())
    return start, None


def _common_dir(git_dir: Path) -> Path:
    """Where info/exclude and config live; a linked worktree points elsewhere."""
    common = _read_text(git_dir / "commondir").strip()
    return git_dir / common if common else git_dir


def _ignorecase(git_dir: Path) -> bool:
    """core.ignorecase from the repository's own config; git's default is false."""
    section = ""
    value = False
    for raw in _read_text(git_dir / "config").splitlines():
        line = raw.strip()
        header = _SECTION.match(line)
        if header:
            section = header.group(1).lower() if header.group(2) is None else ""
            line = line[header.end() :].strip()
        if section != "core" or not line or line[0] in "#;":
            continue
        key, sep, rest = line.partition("=")
        if key.strip().lower() == "ignorecase":
            word = re.split(r"\s[#;]|^[#;]", rest.strip(), maxsplit=1)[0]
            word = word.strip().strip('"').lower()
            value = not sep or word in _TRUE or (word.isdigit() and int(word) != 0)
    return value
