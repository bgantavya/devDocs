"""A gitignore matcher, because respecting .gitignore is not optional.

Every project already states what is noise — build output, vendored code,
caches — in its .gitignore. Reading that is far more accurate than the
hardcoded name list v3 used, and it costs nothing at generation time.

This implements the gitignore spec directly rather than depending on
`pathspec`, which keeps the runtime dependency count at zero. Supported:
comments, blank lines, `!` negation, `/` anchoring, trailing `/` for
directories, `*`, `?`, `[...]` classes, and `**`. Patterns are compiled to
regex once; matching is then a loop over compiled patterns.

Precedence follows git: the last pattern to match decides, so a later `!rule`
un-ignores something an earlier rule caught.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

__all__ = ["Rule", "GitignoreMatcher"]


def _translate(pattern: str) -> str:
    """Compile one gitignore glob into a regex body.

    Handled separately from `fnmatch` because gitignore's `*` must not cross a
    `/`, and `**` must. `fnmatch` makes no such distinction.
    """
    out: list[str] = []
    index = 0
    length = len(pattern)

    while index < length:
        char = pattern[index]

        if char == "*":
            # `**` spans directories; a single `*` stays inside one segment.
            if pattern.startswith("**", index):
                index += 2
                if pattern.startswith("/", index):
                    out.append("(?:.*/)?")
                    index += 1
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                index += 1

        elif char == "?":
            out.append("[^/]")
            index += 1

        elif char == "[":
            end = index + 1
            if end < length and pattern[end] in "!^":
                end += 1
            if end < length and pattern[end] == "]":
                end += 1
            while end < length and pattern[end] != "]":
                end += 1
            if end >= length:
                out.append(re.escape(char))   # unterminated class is literal
                index += 1
            else:
                body = pattern[index + 1 : end]
                if body.startswith(("!", "^")):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                index = end + 1

        else:
            out.append(re.escape(char))
            index += 1

    return "".join(out)


@dataclass(frozen=True)
class Rule:
    """One compiled gitignore line.

    `base` is the directory the rule came from; a nested .gitignore only
    governs paths beneath it.
    """

    regex: re.Pattern[str]
    negated: bool
    dir_only: bool
    source: str
    base: str = ""
    under: re.Pattern[str] | None = None

    def matches(self, relpath: str, is_dir: bool) -> bool:
        if self.base and not relpath.startswith(f"{self.base}/"):
            return False
        candidate = relpath[len(self.base) + 1 :] if self.base else relpath

        # `build/` names a directory, so a *file* only matches when the pattern
        # is one of its ancestors: build/out.js is ignored, build itself is not.
        if self.dir_only and not is_dir:
            return self.under is not None and self.under.match(candidate) is not None
        return self.regex.match(candidate) is not None

    @classmethod
    def parse(cls, line: str, *, base: str = "") -> Rule | None:
        """Compile one line, or None when it is blank or a comment."""
        raw = line
        if not raw.strip() or raw.lstrip().startswith("#"):
            return None

        # Trailing whitespace is insignificant unless backslash-escaped.
        stripped = re.sub(r"(?<!\\)\s+$", "", raw)
        if not stripped:
            return None
        stripped = stripped.replace("\\ ", " ")

        negated = stripped.startswith("!")
        if negated:
            stripped = stripped[1:]
        elif stripped.startswith("\\!"):
            stripped = stripped[1:]

        dir_only = stripped.endswith("/")
        if dir_only:
            stripped = stripped[:-1]
        if not stripped:
            return None

        # A pattern containing a slash is anchored to its .gitignore's
        # directory; one without matches at any depth.
        anchored = "/" in stripped.rstrip("/")
        stripped = stripped.removeprefix("/")

        body = _translate(stripped)
        prefix = "" if anchored else "(?:.*/)?"
        # Matching a directory also matches everything inside it.
        regex = re.compile(f"^{prefix}{body}(?:/.*)?$")
        under = re.compile(f"^{prefix}{body}/.+$") if dir_only else None
        return cls(regex, negated, dir_only, raw.rstrip("\n"), base, under)


class GitignoreMatcher:
    """Decides whether a path is ignored, given a set of rules.

    Built by `from_root`, which collects the repository's .gitignore files, or
    by `from_lines` for explicit rules and tests.
    """

    def __init__(self, rules: list[Rule] | None = None) -> None:
        self.rules: list[Rule] = rules or []

    @classmethod
    def from_lines(cls, lines: list[str], *, base: str = "") -> GitignoreMatcher:
        parsed = (Rule.parse(line, base=base) for line in lines)
        return cls([rule for rule in parsed if rule is not None])

    @classmethod
    def from_root(cls, root: Path, *, extra: list[str] | None = None) -> GitignoreMatcher:
        """Collect every .gitignore under `root`, plus `.git/info/exclude`.

        Nested files are recorded with their directory as `base` so their rules
        do not leak upward, which is what git does.
        """
        rules: list[Rule] = []

        for line in extra or []:
            rule = Rule.parse(line)
            if rule is not None:
                rules.append(rule)

        exclude = root / ".git" / "info" / "exclude"
        if exclude.is_file():
            rules.extend(cls._read(exclude, base=""))

        for path in sorted(root.rglob(".gitignore")):
            if any(part == ".git" for part in path.parts):
                continue
            relative = path.parent.relative_to(root)
            base = "" if relative == Path(".") else relative.as_posix()
            rules.extend(cls._read(path, base=base))

        return cls(rules)

    @staticmethod
    def _read(path: Path, *, base: str) -> list[Rule]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []   # unreadable ignore file must not end the scan
        parsed = (Rule.parse(line, base=base) for line in text.splitlines())
        return [rule for rule in parsed if rule is not None]

    def match(self, relpath: str | Path, *, is_dir: bool = False) -> Rule | None:
        """Return the rule that decides this path, or None if nothing matches.

        The last matching rule wins, and a negation returns None because the
        path ends up *not* ignored. Callers wanting a boolean use `ignored`.
        """
        text = PurePosixPath(str(relpath).replace("\\", "/")).as_posix().lstrip("./")
        if not text:
            return None

        decided: Rule | None = None
        for rule in self.rules:
            if rule.matches(text, is_dir):
                decided = rule

        if decided is None or decided.negated:
            return None
        return decided

    def ignored(self, relpath: str | Path, *, is_dir: bool = False) -> bool:
        return self.match(relpath, is_dir=is_dir) is not None

    def __len__(self) -> int:
        return len(self.rules)
