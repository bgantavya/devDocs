"""Blank out comments and string contents without moving anything.

A pattern scanner must not find `class Foo` inside a comment or a string, but
it also needs exact line numbers and offsets. So instead of deleting comments
and strings, `mask` overwrites comments and the bodies of string, template and
regex literals with spaces, keeping line breaks and delimiters. Everything else
is left exactly as it was. The result is the same length as the source and
aligned position for position: a regex runs over the mask, and its offsets
still index the original text. `Masked.comments` says where each comment was,
so a caller can search comments alone (for TODO markers) under the same rules.

Language quirks that break naive masking, all handled here: JS template
literals with nested `${}` interpolation, JS regex literals, JSX, a JS `#!`
first line, Rust lifetimes that look like unterminated char literals, Rust raw
strings and nested block comments, Go raw strings, and Java text blocks.

A JS `/` opens a regex only where an operand can start, judged by the token
before it: an operator or punctuator such as `(`, `=`, `,`, `!` or `=>`, or a
keyword such as `return`. After a name, a literal, `)`, `]`, a postfix `x!`,
`i++` or a property named like a keyword (`obj.return`) it divides, as does
`/>` after `}`, the end of a JSX tag. Two cases are read wrongly, both rare
outside minified code: a regex straight after the `)` of an `if`, `while` or
`for` head is taken for division, and a division after a variable named `of`,
`await` or `yield` for a regex.

JSX is recognised whenever regex or template literals are on. Element content,
the text after a tag's `>` up to the next `<` or `{`, is neither code, comment
nor string: it is left as it is, so `Don't`, `https://` or `**/*.ts` in it opens
nothing, while `{...}` inside JSX is code again. When unsure, `<` is code: it
opens JSX only where an operand can start (after `(`, `=`, `:`, `,`, `?`, `{`,
`&&`, `=>`, `return` and the like), only before a tag name or `>`, and never
before a TS type-parameter list (`<T,>`, `<T = U>`, `<T extends U>`, the test
TS itself applies); the content is read as text only if a closing tag for that
name appears later in the file. So a TS generic or type assertion
(`<T>(x: T) => x`, `<any>w`) and a less-than stay code. JSX the masker does
not recognise is masked as code.

`mask` never raises and runs in time linear in the source. Malformed input
degrades instead of failing: each construct still open at the end of the
source counts in `Masked.unterminated`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

__all__ = ["Masked", "Syntax", "mask"]

# `/` after one of these opens a JS regex literal; `<` after one may open JSX.
_REGEX_PRECEDERS = frozenset("(,=:[!&|?{};+-*%~^")
_JSX_PRECEDERS = frozenset("(,=:[&|?{")
_REGEX_KEYWORDS = frozenset({
    "return", "typeof", "case", "in", "of", "delete", "void", "throw", "new",
    "yield", "await", "else", "do", "default", "instanceof",
})
_JSX_KEYWORDS = frozenset({"return", "yield", "default"})
_SPACE = frozenset(" \t\r\n\f\v\u00a0\ufeff")
_MAX_REGEX_MISSES = 8   # per line; past that, `/` on the line is division

_NOT_BREAK = re.compile(r"[^\r\n]")
_EOL = r"\r\n\u2028\u2029"
_REGEX_LITERAL = re.compile(
    rf"/(?:[^\\/\[{_EOL}]|\\[^{_EOL}]|\[(?:[^\]\\{_EOL}]|\\[^{_EOL}])*+\])*+"
    r"/(?P<flags>[A-Za-z]*)"
)
_TEMPLATE_STOP = re.compile(r"[\\`]|\$\{")
_JSX_TEXT_STOP = re.compile(r"[{<]")
_JSX_NAME = re.compile(r"(?:[^\W\d]|\$)[\w$.:-]*+")
_JSX_CLOSE = re.compile(r"</\s*+(?:((?:[^\W\d]|\$)[\w$.:-]*+)\s*+)?>")
_JSX_ATTRS = re.compile(r"[\s\w$:=-]+")
# TS's own test for a generic arrow, not JSX: `<T,`, `<T =`, `<T extends U`.
_TYPE_PARAMS = re.compile(r"\s*+(?:[,=]|extends\b\s*+(?![=>/]))")
_CONST_PARAM = re.compile(r"const\s++(?:[^\W\d]|\$)")
_TYPE_ARG = r"(?:=>|[^<>`\r\n])"
_JSX_TYPE_ARGS = re.compile(rf"<(?:{_TYPE_ARG}|<(?:{_TYPE_ARG}|<{_TYPE_ARG}*+>)*+>)*+>")


def _is_word(char: str) -> bool:
    return char.isalnum() or char in "_$"


@dataclass(frozen=True)
class Syntax:
    """The lexical features that decide what counts as comment or string.

    JS regex or template literals also turn on JSX and `#!` recognition.
    """

    line_comments: tuple[str, ...] = ("//",)
    block_comment: tuple[str, str] | None = ("/*", "*/")
    nested_block_comments: bool = False     # Rust
    quotes: str = "\"'"
    multiline_strings: bool = False         # Rust: "..." may span lines
    template_literals: bool = False         # JS/TS backticks with ${}
    raw_backticks: bool = False             # Go raw strings
    text_blocks: bool = False               # Java """..."""
    rust_literals: bool = False             # lifetimes, r#"..."#, b"...", cr"..."
    regex_literals: bool = False            # JS /.../flags


@dataclass(frozen=True)
class Masked:
    """Masked text, where its comments were, and a count of what never closed.

    `comments` holds the [start, end) offsets of every comment blanked, in
    source order: a line comment ends before its line break, an unterminated
    block comment at the end of the source. A nonzero `unterminated` means the
    source is malformed or uses syntax the masker does not know, so anything
    scanned from it deserves less trust.
    """

    text: str
    unterminated: int = 0
    comments: tuple[tuple[int, int], ...] = ()


class _Mode(StrEnum):
    CODE = "code"
    TEMPLATE = "template"   # the text of a template literal
    TAG = "tag"             # a JSX opening tag, among its attributes
    TEXT = "text"           # JSX element content


@dataclass(slots=True)
class _Frame:
    """A construct still open: code, template text, a JSX tag or its content."""

    mode: _Mode
    start: int = 0      # template: its opening backtick
    depth: int = 0      # code: `{` seen and not yet closed
    name: str = ""      # tag: the element name its closing tag repeats


@lru_cache(maxsize=32)
def _stops(syntax: Syntax) -> re.Pattern[str]:
    """Where a construct can start, so plain code is skipped at C speed."""
    starts = {marker[0] for marker in syntax.line_comments if marker}
    if syntax.block_comment and syntax.block_comment[0]:
        starts.add(syntax.block_comment[0][0])
    starts.update(syntax.quotes)
    if syntax.text_blocks:
        starts.add('"')
    if syntax.rust_literals:
        starts.add("'")
    if syntax.template_literals or syntax.raw_backticks:
        starts.add("`")
    if syntax.regex_literals:
        starts.add("/")
    if syntax.regex_literals or syntax.template_literals:
        starts.update("<{}")
    parts = []
    if starts:
        parts.append("[" + "".join(re.escape(char) for char in sorted(starts)) + "]")
    if syntax.rust_literals:
        parts.append(r'(?<!\w)[bc]?r#*+"')
    return re.compile("|".join(parts) or "(?!)")


@lru_cache(maxsize=32)
def _string_stops(quote: str, multiline: bool) -> re.Pattern[str]:
    return re.compile((r"\\|" if multiline else r"[\\\n]|") + re.escape(quote))


@lru_cache(maxsize=8)
def _either(opener: str, closer: str) -> re.Pattern[str]:
    return re.compile(f"{re.escape(opener)}|{re.escape(closer)}")


class _Masker:
    """One pass over one source. Not reusable; construct per call.

    An explicit stack of frames replaces recursion, so nesting depth (templates
    in templates, JSX in JSX) is bounded by memory, not the call stack.
    """

    def __init__(self, source: str, syntax: Syntax) -> None:
        self.src = source
        self.n = len(source)
        self.syntax = syntax
        self.out = list(source)
        self.unterminated = 0
        self.comments: list[tuple[int, int]] = []
        self.line_markers = tuple(marker for marker in syntax.line_comments if marker)
        block = syntax.block_comment
        self.block = block if block and block[0] and block[1] else None
        self.jsx = syntax.regex_literals or syntax.template_literals
        self.stops = _stops(syntax)
        self.stack = [_Frame(_Mode.CODE)]
        self.templates = 0                  # TEMPLATE frames on the stack
        self.dead: dict[str, int] = {}      # quote -> one-line strings before fail
        self.absent: dict[str, int] = {}    # char -> no occurrence at or after
        self.closers: dict[str, int] | None = None
        self.miss_eol = -1
        self.misses = 0

    def blank(self, start: int, end: int) -> None:
        """Overwrite [start, end) with spaces, keeping line breaks."""
        start, end = max(start, 0), min(end, self.n)
        if start < end:
            self.out[start:end] = _NOT_BREAK.sub(" ", self.src[start:end])

    # -- entry --------------------------------------------------------------

    def run(self) -> Masked:
        i = self.line_comment(0) if self.jsx and self.src.startswith("#!") else 0
        while i < self.n:
            frame = self.stack[-1]
            if frame.mode is _Mode.CODE:
                i = self.code(frame, i)
            elif frame.mode is _Mode.TEMPLATE:
                i = self.template(frame, i)
            elif frame.mode is _Mode.TAG:
                i = self.tag(frame, i)
            else:
                i = self.text(i)
        self.unterminated += len(self.stack) - 1
        outermost = next((f for f in self.stack if f.mode is _Mode.TEMPLATE), None)
        if outermost is not None:
            self.blank(outermost.start + 1, self.n)
        return Masked("".join(self.out), self.unterminated, tuple(self.comments))

    # -- frames -------------------------------------------------------------

    def code(self, frame: _Frame, i: int) -> int:
        """Code up to the end of the source or, when nested, its closing `}`."""
        src, stops, stack = self.src, self.stops, self.stack
        while (found := stops.search(src, i)) is not None:
            j = found.start()
            char = src[j]
            if char == "{":
                frame.depth += 1
                i = j + 1
            elif char == "}":
                if frame.depth:
                    frame.depth -= 1
                elif len(stack) > 1:
                    stack.pop()
                    return j + 1
                i = j + 1
            else:
                end = self.construct(j)
                if end is None:
                    i = j + 1
                elif stack[-1] is not frame:
                    return end
                else:
                    i = end
        return self.n

    def template(self, frame: _Frame, i: int) -> int:
        """The text of a JS template literal; `${` opens nested code.

        The outermost template blanks its whole body, interpolations and inner
        templates included, once its closing backtick is found: no brace or
        backtick inside can leak out, and nothing is blanked twice.
        """
        src = self.src
        while (found := _TEMPLATE_STOP.search(src, i)) is not None:
            j = found.start()
            if src[j] == "\\":
                i = self.escape_end(j)
            elif src[j] == "`":
                self.stack.pop()
                self.templates -= 1
                if not self.templates:
                    self.blank(frame.start + 1, j)
                return j + 1
            else:
                self.stack.append(_Frame(_Mode.CODE))
                return j + 2
        return self.n

    def tag(self, frame: _Frame, i: int) -> int:
        """Attributes of a JSX opening tag, up to its `>` or `/>`.

        Anything a tag cannot hold means the `<` was not JSX after all: the
        frame is dropped and its parent resumes at that character.
        """
        src, stack = self.src, self.stack
        while i < self.n:
            after_equals = False
            attrs = _JSX_ATTRS.match(src, i)
            if attrs is not None:
                i = attrs.end()
                after_equals = attrs.group().rstrip().endswith("=")
                if i >= self.n:
                    break
            char = src[i]
            if char == ">":
                stack.pop()
                if self.closes_later(frame.name, i + 1):
                    stack.append(_Frame(_Mode.TEXT))
                return i + 1
            if src.startswith("/>", i):
                stack.pop()
                return i + 2
            if char == "{":
                stack.append(_Frame(_Mode.CODE))
                return i + 1
            if char in "\"'":
                close = self.find(char, i + 1)
                if close >= 0:   # JSX strings have no escapes and may span lines
                    self.blank(i + 1, close)
                    i = close + 1
                    continue
                self.unterminated += 1
            else:
                skipped = self.comment(i)
                if skipped is not None:
                    i = skipped
                    continue
                if char == "<" and after_equals:
                    nested = self.element(i)
                    if nested is not None:
                        return nested
            stack.pop()
            return i
        return self.n

    def text(self, i: int) -> int:
        """JSX element content: text stays as it is, `{` opens code, `<` a tag."""
        src, stack = self.src, self.stack
        while (found := _JSX_TEXT_STOP.search(src, i)) is not None:
            j = found.start()
            if src[j] == "{":
                stack.append(_Frame(_Mode.CODE))
                return j + 1
            closing = _JSX_CLOSE.match(src, j)
            if closing is not None:
                stack.pop()
                return closing.end()
            nested = self.element(j)
            if nested is not None:
                return nested
            i = j + 1
        return self.n

    # -- constructs ---------------------------------------------------------

    def construct(self, i: int) -> int | None:
        """Mask what starts at i, or push a frame for what nests there.

        Returns where scanning resumes, or None if nothing starts at i.
        """
        end = self.comment(i)
        if end is not None:
            return end
        src, syntax = self.src, self.syntax
        char = src[i]

        if syntax.text_blocks and src.startswith('"""', i):
            return self.delimited(i, '"""', multiline=True)

        if syntax.rust_literals:
            raw = self.rust_raw_string(i)
            if raw is not None:
                return raw
            if char == "'":
                return self.rust_quote(i)

        if char == "`" and syntax.template_literals:
            self.stack.append(_Frame(_Mode.TEMPLATE, start=i))
            self.templates += 1
            return i + 1

        if char == "`" and syntax.raw_backticks:
            end = self.src.find("`", i + 1)
            if end < 0:
                self.unterminated += 1
                self.blank(i + 1, self.n)
                return self.n
            self.blank(i + 1, end)
            return end + 1

        if char in syntax.quotes:
            return self.delimited(i, char, multiline=syntax.multiline_strings)

        if char == "/" and syntax.regex_literals and self.starts_regex(i):
            return self.regex(i)

        if char == "<" and self.jsx and self.operand_after(
            self.back(i - 1), _JSX_PRECEDERS, _JSX_KEYWORDS, at_start=False
        ):
            return self.element(i)

        return None

    def comment(self, i: int) -> int | None:
        """Blank the comment starting at i, if any, and return its end.

        When a block opener extends a line marker (Lua's `--[[` and `--`), the
        longer one wins.
        """
        src = self.src
        marker = next((m for m in self.line_markers if src.startswith(m, i)), "")
        block = self.block
        if block and len(block[0]) > len(marker) and src.startswith(block[0], i):
            return self.block_comment(i, *block)
        return self.line_comment(i) if marker else None

    def line_comment(self, i: int) -> int:
        """Blank from i to the line break (before a CRLF's `\\r`); return it."""
        src = self.src
        end = src.find("\n", i)
        if end < 0:
            end = self.n
        elif end > i and src[end - 1] == "\r":
            end -= 1
        self.blank(i, end)
        self.comments.append((i, end))
        return end

    def block_comment(self, i: int, opener: str, closer: str) -> int:
        src = self.src
        end = i + len(opener)
        if self.syntax.nested_block_comments:
            pattern = _either(opener, closer)
            depth = 1
            while depth:
                found = pattern.search(src, end)
                if found is None:
                    end = -1
                    break
                depth += 1 if found.group() == opener else -1
                end = found.end()
        else:
            end = src.find(closer, end)
            if end >= 0:
                end += len(closer)
        if end < 0:
            self.unterminated += 1
            end = self.n
        self.blank(i, end)
        self.comments.append((i, end))
        return end

    def delimited(self, i: int, quote: str, *, multiline: bool) -> int:
        """A quoted string. Keeps the delimiters, blanks the contents.

        A single-line quote with no closer on its line is not treated as a
        string at all. In JSX the masker did not recognise, that is an
        apostrophe in text (`<p>Don't</p>`), and blanking to end of line could
        hide a brace and skew every depth calculation after it.
        """
        src = self.src
        start = i + len(quote)
        if not multiline and i < self.dead.get(quote, -1):
            return start   # a scan from an earlier quote already failed here
        pattern = _string_stops(quote, multiline)
        j = start
        while (found := pattern.search(src, j)) is not None:
            k = found.start()
            if src[k] == "\\":
                j = self.escape_end(k)
            elif src.startswith(quote, k):
                self.blank(start, k)
                return k + len(quote)
            else:
                self.dead[quote] = k
                return start
        if not multiline:
            self.dead[quote] = self.n
            return start
        self.unterminated += 1
        self.blank(start, self.n)
        return self.n

    def escape_end(self, k: int) -> int:
        """Index after the escape at k: a `\\` before CRLF continues the line."""
        return k + 3 if self.src.startswith("\r\n", k + 1) else k + 2

    def rust_raw_string(self, i: int) -> int | None:
        """r"..", r#".."#, br#".."#, cr#".."#. None if none starts here."""
        src = self.src
        if i > 0 and (src[i - 1].isalnum() or src[i - 1] == "_"):
            return None   # `r` inside an identifier like `bar"`

        if src.startswith(("br", "cr"), i):
            j = i + 2
        elif src.startswith("r", i):
            j = i + 1
        else:
            return None

        hashes = 0
        while j < self.n and src[j] == "#":
            hashes += 1
            j += 1
        if j >= self.n or src[j] != '"':
            return None

        closer = '"' + "#" * hashes
        end = src.find(closer, j + 1)
        if end == -1:
            self.unterminated += 1
            self.blank(j + 1, self.n)
            return self.n
        self.blank(j + 1, end)
        return end + len(closer)

    def rust_quote(self, i: int) -> int:
        """Char literal or lifetime. `'a'` is a char; `'a` alone is a lifetime.

        Treating a lifetime as an unterminated char literal would blank the
        rest of the line and hide the declaration being scanned. An escaped
        char closes after its escape, so `'\\''` ends at its last quote.
        """
        src = self.src
        if src.startswith("\\", i + 1):
            end = src.find("'", i + 3, i + 14)
            if end != -1:
                self.blank(i + 1, end)
                return end + 1
            return i + 1
        if src.startswith("'", i + 2):
            self.blank(i + 1, i + 2)
            return i + 3
        return i + 1   # lifetime: leave as code

    def starts_regex(self, i: int) -> bool:
        """Decide whether `/` at i opens a JS regex rather than dividing."""
        src = self.src
        if src.startswith(("//", "/*"), i):
            return False
        if self.misses >= _MAX_REGEX_MISSES and i < self.miss_eol:
            return False
        k = self.back(i - 1)
        if k >= 0 and self.out[k] == "}" and src.startswith("/>", i):
            return False   # `<X a={b} />` the masker did not take for JSX
        return self.operand_after(k, _REGEX_PRECEDERS, _REGEX_KEYWORDS, at_start=True)

    def regex(self, i: int) -> int:
        """A JS regex literal: blank its body, return the index after its flags.

        With no closing `/` on the line, the `/` was division after all.
        Misses are capped per line so a line full of them cannot be rescanned
        once per `/`.
        """
        src = self.src
        found = _REGEX_LITERAL.match(src, i)
        if found is not None:
            self.blank(i + 1, found.start("flags") - 1)
            return found.end()
        eol = src.find("\n", i)
        eol = self.n if eol < 0 else eol
        if eol == self.miss_eol:
            self.misses += 1
        else:
            self.miss_eol, self.misses = eol, 1
        return i + 1

    def element(self, i: int) -> int | None:
        """Open the JSX element whose `<` is at i; None if none starts there."""
        src = self.src
        if src.startswith("<>", i):
            if not self.closes_later("", i + 2):
                return None
            self.stack.append(_Frame(_Mode.TEXT))
            return i + 2
        name = _JSX_NAME.match(src, i + 1)
        if name is None or _CONST_PARAM.match(src, i + 1):
            return None
        end = name.end()
        if _TYPE_PARAMS.match(src, end):
            return None
        if src.startswith("<", end):   # TSX type arguments: `<Table<Row> ...`
            args = _JSX_TYPE_ARGS.match(src, end)
            if args is None:
                return None
            end = args.end()
        self.stack.append(_Frame(_Mode.TAG, name=name.group()))
        return end

    def closes_later(self, name: str, start: int) -> bool:
        """Whether a closing tag for `name` appears at or after `start`."""
        if self.closers is None:
            self.closers = {
                found.group(1) or "": found.start()
                for found in _JSX_CLOSE.finditer(self.src)
            }
        return self.closers.get(name, -1) >= start

    def find(self, char: str, start: int) -> int:
        """`src.find(char, start)`, remembering where `char` stops occurring."""
        if start >= self.absent.get(char, self.n + 1):
            return -1
        found = self.src.find(char, start)
        if found < 0:
            self.absent[char] = start
        return found

    # -- what came before ---------------------------------------------------

    def operand_after(
        self,
        k: int,
        preceders: frozenset[str],
        keywords: frozenset[str],
        *,
        at_start: bool,
    ) -> bool:
        """Whether an operand, not an operator, can follow the mask's char at k.

        Judged by the token ending at k: one of `preceders`, an arrow, or one
        of `keywords` (not a property named like one, as in `obj.return`). A
        postfix `x!`, `i++` or `i--` ends an operand, so division follows.
        """
        if k < 0:
            return at_start
        out = self.out
        prev = out[k]
        if prev in preceders:
            if prev in "+-":
                return k == 0 or out[k - 1] != prev
            if prev == "!":
                return not self.postfix_bang(k)
            return True
        if prev == ">":
            return k > 0 and out[k - 1] == "="
        if _is_word(prev):
            start = self.word_start(k)
            word = "".join(out[start : k + 1])
            return word in keywords and not self.is_property(start)
        return False

    def postfix_bang(self, k: int) -> bool:
        """Whether the `!` at k is TS's postfix non-null assertion (`x!`)."""
        out = self.out
        j = k - 1
        while j >= 0 and out[j] in " \t":
            j -= 1
        if j < 0:
            return False
        if out[j] in ")]":
            return True
        if not _is_word(out[j]):
            return False
        start = self.word_start(j)
        word = "".join(out[start : j + 1])
        return word not in _REGEX_KEYWORDS or self.is_property(start)

    def back(self, k: int) -> int:
        """Index of the last non-space character of the mask at or before k."""
        out = self.out
        while k >= 0 and out[k] in _SPACE:
            k -= 1
        return k

    def word_start(self, k: int) -> int:
        out = self.out
        while k > 0 and _is_word(out[k - 1]):
            k -= 1
        return k

    def is_property(self, start: int) -> bool:
        """Whether the word at `start` follows a `.`, as in `obj.return`."""
        k = self.back(start - 1)
        return k >= 0 and self.out[k] == "."


def mask(source: str, syntax: Syntax) -> Masked:
    """Blank comments and string contents in `source`, preserving offsets."""
    return _Masker(source, syntax).run()
