"""Known issues, read from the markers developers leave in comments.

A TODO or FIXME is the author's own record of what is unfinished or wrong, so
it belongs in the docs as written — and extracting it costs nothing. Only
comments are searched: a marker inside a string is data, not a note. Python
comments come from `tokenize`; other languages use the comment spans the
masker records, so the same rules that keep the scanner out of strings keep
this out of them too.
"""

from __future__ import annotations

import bisect
import io
import re
import tokenize
from dataclasses import dataclass

from devdocs.parse.masking import Syntax, mask

__all__ = ["MARKERS", "Issue", "comment_syntax", "find_issues"]

MARKERS = ("TODO", "FIXME", "XXX", "HACK", "BUG")

_MARKER = re.compile(
    r"\b(?P<tag>TODO|FIXME|XXX|HACK|BUG)\b(?:\((?P<owner>[^)]*)\))?"
    r"[\s:\-\u2013\u2014]*(?P<text>.*)"
)
_MAX_ISSUES = 25
_MAX_TEXT = 160

_HASH = Syntax(line_comments=("#",), block_comment=None)
_DASH = Syntax(line_comments=("--",), block_comment=("/*", "*/"))


def comment_syntax(language: str) -> Syntax:
    """How comments and strings are spelled in `language`, for masking."""
    from devdocs.parse.languages import GO, JAVA, JAVASCRIPT, RUST

    known = {
        "javascript": JAVASCRIPT.syntax,
        "typescript": JAVASCRIPT.syntax,
        "go": GO.syntax,
        "rust": RUST.syntax,
        "java": JAVA.syntax,
        "shell": _HASH,
        "ruby": _HASH,
        "elixir": _HASH,
        "lua": Syntax(line_comments=("--",), block_comment=("--[[", "]]")),
        "sql": _DASH,
    }
    return known.get(language, Syntax())


@dataclass(frozen=True)
class Issue:
    line: int
    tag: str
    text: str


def find_issues(source: str, language: str) -> list[Issue]:
    """Every marker in a comment, in line order, capped at 25 per file."""
    if not any(marker in source for marker in MARKERS):
        return []
    if language == "python":
        comments = _python_comments(source)
    else:
        comments = _masked_comments(source, comment_syntax(language))

    found: list[Issue] = []
    for line, text in comments:
        for raw in text.splitlines():
            issue = _issue(line, raw)
            if issue is not None:
                found.append(issue)
            line += 1
        if len(found) >= _MAX_ISSUES:
            break
    return found[:_MAX_ISSUES]


def _issue(line: int, raw: str) -> Issue | None:
    match = _MARKER.search(raw)
    if match is None:
        return None
    text = match.group("text").strip()
    text = re.sub(r"\s*\*+/\s*$", "", text).strip()   # block-comment closer
    if not text:
        return None
    if len(text) > _MAX_TEXT:
        text = text[: _MAX_TEXT - 3].rsplit(" ", 1)[0] + "..."
    return Issue(line, match.group("tag"), text)


def _python_comments(source: str) -> list[tuple[int, str]]:
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return _masked_comments(source, _HASH)
    return [(t.start[0], t.string) for t in tokens if t.type == tokenize.COMMENT]


def _masked_comments(source: str, syntax: Syntax) -> list[tuple[int, str]]:
    """(first line, text) of every comment, from the masker's spans."""
    masked = mask(source, syntax)
    newlines = [index for index, char in enumerate(source) if char == "\n"]
    return [
        (bisect.bisect_left(newlines, start) + 1, source[start:end])
        for start, end in masked.comments
    ]
