"""Text helpers the renderers share, each defined once.

Inline code is read by CommonMark's rule for code spans wherever prose is
rendered or cleaned, so the style guard never edits a span Markdown shows as
code. Links between pages are relative paths, with what a link destination
cannot carry percent-encoded and the rest left legible.
"""

from __future__ import annotations

import bisect
import posixpath
import re
import string
from collections.abc import Iterator
from urllib.parse import quote

__all__ = ["code_spans", "relpath", "url_path"]

_TICKS = re.compile(r"`+")
_URL_SAFE = frozenset(string.ascii_letters + string.digits + "-._~/!$&'*+,;=@")


def code_spans(text: str) -> Iterator[tuple[bool, str]]:
    """Split `text` into plain runs and code spans, by CommonMark's rule.

    A run of n backticks opens a span that the next run of exactly n closes;
    an opener with no closer is literal text. A span keeps its backticks, so
    the pieces join back into `text`.
    """
    runs = [match.span() for match in _TICKS.finditer(text)]
    by_length: dict[int, list[int]] = {}
    for position, (start, end) in enumerate(runs):
        by_length.setdefault(end - start, []).append(position)

    done = current = 0
    while current < len(runs):
        start, end = runs[current]
        same = by_length[end - start]
        later = bisect.bisect_right(same, current)
        if later == len(same):
            current += 1
            continue
        closer = same[later]
        if start > done:
            yield False, text[done:start]
        done = runs[closer][1]
        yield True, text[start:done]
        current = closer + 1
    if done < len(text):
        yield False, text[done:]


def relpath(path: str, start: str) -> str:
    """File `path` relative to directory `start`, both POSIX paths from one
    root, from the strings alone where `posixpath.relpath` would read the
    cwd."""
    target = [p for p in posixpath.normpath(path).split("/") if p not in ("", ".")]
    origin = [p for p in posixpath.normpath(start).split("/") if p not in ("", ".")]
    shared = 0
    for mine, theirs in zip(target, origin, strict=False):
        if mine != theirs:
            break
        shared += 1
    return "/".join([".."] * (len(origin) - shared) + target[shared:]) or "."


def url_path(path: str) -> str:
    """`path` as a relative URL: all but unreserved ASCII, `/`, `!$&'*+,;=@`
    and printable non-ASCII percent-encoded as UTF-8, so the path stays
    legible.

    A byte a file name could not decode, held as a `surrogateescape`
    surrogate, is encoded as itself; any other lone surrogate, as U+FFFD.
    """
    return "".join(
        char
        if char in _URL_SAFE or (not char.isascii() and char.isprintable())
        else _percent(char)
        for char in path
    )


def _percent(char: str) -> str:
    try:
        return quote(char, safe="", errors="surrogateescape")
    except UnicodeEncodeError:   # a lone surrogate that stands for no byte
        return "%EF%BF%BD"
