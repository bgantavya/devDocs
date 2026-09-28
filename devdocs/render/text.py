"""Text helpers the renderers share, each defined once.

Inline code is read by CommonMark's rule for code spans wherever prose is
rendered or cleaned, so a span shows the same text in Markdown and HTML and
the style guard never edits one. Links between pages are relative paths,
percent-encoded so neither a browser nor a Markdown renderer reads syntax in a
file name. Output file names are compared the way the file systems macOS and
Windows use by default compare them, so no two outputs land on one file.
"""

from __future__ import annotations

import bisect
import posixpath
import re
import string
import unicodedata
from collections.abc import Iterator
from urllib.parse import quote

__all__ = ["code_spans", "code_text", "folded", "relpath", "url_path"]

_TICKS = re.compile(r"`+")
_URL_SAFE = frozenset(string.ascii_letters + string.digits + "-._~/")


def code_spans(text: str) -> Iterator[tuple[bool, str]]:
    """Split `text` into plain runs and code spans, by CommonMark's rule.

    A run of n backticks opens a span that the next run of exactly n closes;
    an opener with no closer is literal text. A span keeps its backticks, so
    the pieces join back into `text`; `code_text` gives what one shows.
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


def code_text(span: str) -> str:
    """What code span `span`, backticks included, shows by CommonMark's rule:
    each line break a space, then one space off each end when both ends have
    one and something other than spaces is left."""
    span = " ".join(span.splitlines())
    ticks = len(span) - len(span.lstrip("`"))
    inner = span[ticks:len(span) - ticks]
    if inner[:1] == inner[-1:] == " " and inner.strip(" "):
        return inner[1:-1]
    return inner


def relpath(path: str, start: str) -> str:
    """File `path` relative to directory `start`, both POSIX paths from one
    root, from the strings alone where `posixpath.relpath` would read the
    cwd. The file's own name always stays, even where a directory on the way
    to `start` has that name."""
    target = [p for p in posixpath.normpath(path).split("/") if p not in ("", ".")]
    origin = [p for p in posixpath.normpath(start).split("/") if p not in ("", ".")]
    shared = 0
    for mine, theirs in zip(target[:-1], origin, strict=False):
        if mine != theirs:
            break
        shared += 1
    return "/".join([".."] * (len(origin) - shared) + target[shared:]) or "."


def url_path(path: str) -> str:
    """`path` as a relative URL: all but unreserved ASCII, `/` and printable
    non-ASCII percent-encoded as UTF-8.

    That leaves nothing a browser or a Markdown renderer reads as syntax: no
    `:` to pass for a scheme, no `#` or `?` to cut the path short, no `%` to
    read as an escape, no `&` to open a character reference, no space or
    bracket to end a link. A byte a file name could not decode, held as a
    `surrogateescape` surrogate, is encoded as itself; any other lone
    surrogate, as U+FFFD.
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


def folded(name: str) -> str:
    """`name` by Unicode's canonical caseless match, so names that differ
    only in case or normalization fold alike: macOS stores such names as one
    file by default, and Windows those that differ in case. Folding more than
    a file system does costs at most a needless suffix; folding less loses a
    file."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", name).casefold())
