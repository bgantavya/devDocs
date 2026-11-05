"""Python facts via `ast`, degrading to a token scan when `ast` cannot parse.

The exact path reports top-level classes, functions and PEP 695 `type`
aliases, including those under `if`/`try` blocks, where version shims and
`TYPE_CHECKING` stubs define real API (the first definition of a name wins
there); class members, nested classes included; and UPPER_CASE constants.
Definitions inside function bodies, loops and `with` blocks are not API.

Signatures are source text, never rebuilt from the tree. Headers are read with
`tokenize`, so comments drop out, string literals stay byte-for-byte and a
bracket inside a string cannot end the header; only whitespace between tokens
is collapsed. Lines are counted as the compiler counts them (`\\n`, `\\r\\n`,
`\\r`) and a leading BOM is dropped, so every line number indexes the text
`ast` saw. Work stays linear in the size of the file, so a generated table or
a minified line costs its length and no more.

`ast` only parses syntax its own interpreter understands, so a devdocs running
on 3.11 hits SyntaxError on PEP 695 generics from a 3.12 codebase, and the
parser stack overflows on very long generated chains. The fallback then scans
tokens, never raw lines, so nothing inside a string becomes a symbol. It
survives `match`, `except*` and PEP 695 and finds the same shapes in the same
order, but has no docstrings, decorators, imports or `__all__`, so its facts
are DEGRADED.

It does not survive PEP 701 f-strings: before 3.12 the tokenizer has no
FSTRING_START, so `f"{"a"}"` yields three wrong tokens with no error raised.
Silent corruption is worse than a gap, so the fallback detects that case and
marks the file FAILED rather than emitting a symbol table that looks complete.
"""

from __future__ import annotations

import ast
import io
import keyword
import re
import sys
import tokenize
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import chain
from pathlib import Path

from devdocs.parse import (
    Confidence,
    FileFacts,
    Symbol,
    SymbolKind,
    count_lines,
    register,
)

__all__ = ["PythonParser"]

if sys.version_info >= (3, 12):
    _ALIASES: tuple[type[ast.stmt], ...] = (ast.TypeAlias,)
else:
    _ALIASES = ()
_DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, *_ALIASES)

_BOM = "\ufeff"
_OPEN = frozenset("([{")
_CLOSE = frozenset(")]}")
_UNSAFE = re.compile(r"[#'\"\\]")   # needs tokens: a string, comment or continuation
_FSTRING = re.compile(r'f"(?=([^"\n]*)")')   # an f-string up to its first quote
_SKIPPED = frozenset({tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT})
_ENDS = frozenset({tokenize.NEWLINE, tokenize.ENDMARKER})
# PEP 701 f-strings (3.12) and PEP 750 t-strings (3.14) arrive as many tokens.
_OPENERS: frozenset[int] = frozenset(
    getattr(tokenize, name, -1) for name in ("FSTRING_START", "TSTRING_START")
) - {-1}
_CLOSERS: frozenset[int] = frozenset(
    getattr(tokenize, name, -1) for name in ("FSTRING_END", "TSTRING_END")
) - {-1}
_BLOCKS = ("body", "orelse", "finalbody", "handlers", "cases")   # statement lists
_BRANCHES = frozenset({"if", "elif", "else", "try", "except", "finally"})
_COMPOUND = _BRANCHES | {"for", "while", "with", "def", "class", "async", "match"}


class PythonParser:
    """Parses .py and .pyi files."""

    languages: tuple[str, ...] = ("python",)

    def parse(self, path: Path, source: str) -> FileFacts:
        loc = 0
        try:
            text = source.removeprefix(_BOM)
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            loc = count_lines(text)
            return self._parse(path, text, loc)
        except Exception as err:   # never let one file end a run
            return _failed(path, loc, f"parser error: {type(err).__name__}: {err}")

    def _parse(self, path: Path, text: str, loc: int) -> FileFacts:
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as err:
            return self._fallback(
                path, text, loc, f"ast failed at line {err.lineno} ({err.msg})"
            )
        except ValueError as err:   # null bytes before 3.12, lone surrogates
            return _failed(path, loc, f"unparseable: {type(err).__name__}")
        except (RecursionError, MemoryError) as err:
            # Parser limits on long generated chains; tokenize copes with them.
            return self._fallback(
                path, text, loc, f"ast failed ({type(err).__name__}: {err})"
            )
        return self._from_ast(path, tree, text, loc)

    # -- exact path ------------------------------------------------------

    def _from_ast(
        self, path: Path, tree: ast.Module, text: str, loc: int
    ) -> FileFacts:
        lines = _Lines(text)
        symbols = self._definitions(tree.body, lines, member=False)
        constants = self._constants(tree, lines)

        return FileFacts(
            path=path,
            language="python",
            symbols=tuple(symbols + constants),
            imports=tuple(self._imports(tree)),
            exports=tuple(self._dunder_all(tree)),
            module_docstring=ast.get_docstring(tree) or "",
            loc=loc,
            confidence=Confidence.EXACT,
        )

    def _definitions(
        self, body: list[ast.stmt], lines: _Lines, *, member: bool
    ) -> list[Symbol]:
        """Classes, functions and type aliases of one body, in source order.

        `if`/`try` blocks are entered; inside them a name already defined at
        this level is skipped, so the first definition wins. A stack rather
        than recursion, because ast accepts elif chains thousands long.
        """
        found: list[Symbol] = []
        seen: set[str] = set()
        stack: list[tuple[Iterator[ast.stmt], bool]] = [(iter(body), False)]
        while stack:
            statements, conditional = stack[-1]
            node = next(statements, None)
            if node is None:
                stack.pop()
            elif isinstance(node, _DEFINITIONS):
                name = _name(node)
                if not (conditional and name in seen):
                    seen.add(name)
                    found.append(self._symbol(node, lines, member=member))
            elif isinstance(node, ast.If):
                stack.append((chain(node.body, node.orelse), True))
            elif isinstance(node, ast.Try | ast.TryStar):
                blocks = [node.body, *(h.body for h in node.handlers),
                          node.orelse, node.finalbody]
                stack.append((chain.from_iterable(blocks), True))
        return found

    def _symbol(self, node: ast.stmt, lines: _Lines, *, member: bool) -> Symbol:
        name = _name(node)
        line = node.lineno
        end_line = max(node.end_lineno or line, line)
        if not isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            return Symbol(
                name=name,
                kind=SymbolKind.TYPE,
                line=line,
                end_line=end_line,
                signature=_statement(node, lines, _Stop.STATEMENT),
                is_public=_is_public(name),
            )

        children: tuple[Symbol, ...] = ()
        bases: tuple[str, ...] = ()
        if isinstance(node, ast.ClassDef):
            kind = SymbolKind.CLASS
            children = tuple(self._definitions(node.body, lines, member=True))
            bases = tuple(self._render(base) for base in node.bases)
        else:
            kind = SymbolKind.METHOD if member else SymbolKind.FUNCTION

        return Symbol(
            name=name,
            kind=kind,
            line=line,
            end_line=end_line,
            signature=_header(node, lines),
            docstring=ast.get_docstring(node) or "",
            decorators=tuple(self._render(d) for d in node.decorator_list),
            bases=bases,
            children=children,
            is_public=_is_public(name),
            is_async=isinstance(node, ast.AsyncFunctionDef),
        )

    @staticmethod
    def _render(node: ast.AST) -> str:
        try:
            return ast.unparse(node)
        except Exception:
            return getattr(node, "id", "") or ""

    @staticmethod
    def _imports(tree: ast.Module) -> list[str]:
        """Dotted names this file imports, sorted and deduplicated.

        `import a.b` records `a.b`, not `a`: crediting the parent package would
        give every sibling module the parent's fan-in and wreck the ranking.
        `from a.b import c, d` records `a.b.c` and `a.b.d`, since `c` may be a
        submodule; the resolver takes the longest prefix that names a module.
        `from a import *` records `a`. Relative forms keep their dots: `from .m
        import x` is `.m.x`, `from . import x` is `.x`, `from .. import y` is
        `..y`. Only statement blocks are walked, as an import is a statement.
        """
        found: set[str] = set()
        pending: list[ast.AST] = [tree]
        while pending:
            node = pending.pop()
            for block in _BLOCKS:
                pending.extend(getattr(node, block, ()))
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = "." * node.level + (node.module or "")
                for alias in node.names:
                    if alias.name == "*":
                        found.add(base)
                    elif node.module:
                        found.add(f"{base}.{alias.name}")
                    else:
                        found.add(base + alias.name)
        return sorted(found)

    @staticmethod
    def _dunder_all(tree: ast.Module) -> list[str]:
        """The string literals `__all__` holds by the end of the module body.

        Assignment (plain or annotated) replaces the list; `+=`, `.extend()`
        and `.append()` add to it. Values that are not literals are skipped,
        and statements nested in blocks are not read.
        """
        names: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(map(_is_all, node.targets)):
                literal = _strings(node.value)
                names = names if literal is None else literal
            elif isinstance(node, ast.AnnAssign) and _is_all(node.target):
                literal = _strings(node.value) if node.value else None
                names = names if literal is None else literal
            elif (
                isinstance(node, ast.AugAssign)
                and _is_all(node.target)
                and isinstance(node.op, ast.Add)
            ):
                names.extend(_strings(node.value) or ())
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                names.extend(_appended(node.value))
        return list(dict.fromkeys(names))

    @staticmethod
    def _constants(tree: ast.Module, lines: _Lines) -> list[Symbol]:
        """Top-level UPPER_CASE names; the signature is the statement's first
        line, since a value can be a thousand-line table."""
        found: list[Symbol] = []
        for node in tree.body:
            target: ast.expr | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
            elif isinstance(node, ast.AnnAssign):
                target = node.target
            if not isinstance(target, ast.Name) or not _is_constant(target.id):
                continue
            line = node.lineno
            found.append(
                Symbol(
                    name=target.id,
                    kind=SymbolKind.CONSTANT,
                    line=line,
                    end_line=max(node.end_lineno or line, line),
                    signature=_statement(node, lines, _Stop.LINE),
                )
            )
        return found

    # -- degraded path ---------------------------------------------------

    def _fallback(self, path: Path, text: str, loc: int, reason: str) -> FileFacts:
        """Recover symbols from tokens when `ast` refuses the file."""
        if self._has_nested_quote_fstring(text):
            return _failed(
                path,
                loc,
                "PEP 701 f-strings need Python 3.12+ to parse; "
                "this interpreter would mis-tokenize them",
            )

        tokens, error = _tokenize(text)
        if error:
            return _failed(path, loc, f"{reason}; tokenize failed too: {error}")
        if any(token.type == tokenize.ERRORTOKEN for token in tokens):
            return _failed(
                path, loc, "tokenizer produced error tokens; symbols unreliable"
            )

        symbols = _TokenScan(tokens, text.split("\n")).scan()
        return FileFacts(
            path=path,
            language="python",
            symbols=tuple(symbols),
            loc=loc,
            confidence=Confidence.DEGRADED,
            notes=[
                f"{reason}; symbols recovered by token scan, without "
                "docstrings, decorators, imports or __all__"
            ],
        )

    @staticmethod
    def _has_nested_quote_fstring(source: str) -> bool:
        """Detect an f-string reusing its own quote character inside a brace.

        Legal from 3.12. On an older tokenizer it silently produces wrong
        tokens instead of an error, so it must be caught before scanning.
        """
        if sys.version_info >= (3, 12):
            return False
        return _reuses_quote(source)


# -- shared helpers ------------------------------------------------------


def _failed(path: Path, loc: int, note: str) -> FileFacts:
    return FileFacts(
        path=path,
        language="python",
        loc=loc,
        confidence=Confidence.FAILED,
        notes=[note],
    )


def _name(node: ast.stmt) -> str:
    name = getattr(node, "name", "")
    return name.id if isinstance(name, ast.Name) else str(name)


def _is_public(name: str) -> bool:
    """Public unless underscored. Dunders are public; `__x` is name-mangled."""
    if not name.startswith("_"):
        return True
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _is_constant(name: str) -> bool:
    return name.isupper() and not name.startswith("_")


def _is_all(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "__all__"


def _strings(node: ast.expr) -> list[str] | None:
    """String literals of a list or tuple display, or of `+` joining them;
    None when nothing in it is a display. A loop, as the `+` chain can run
    deeper than the recursion limit."""
    found: list[str] = []
    display = False
    pending = [node]
    while pending:
        part = pending.pop()
        if isinstance(part, ast.BinOp) and isinstance(part.op, ast.Add):
            pending += (part.right, part.left)
        elif isinstance(part, ast.List | ast.Tuple):
            display = True
            found += (
                element.value
                for element in part.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
    return found if display else None


def _appended(call: ast.Call) -> list[str]:
    """Names a `__all__.extend([...])` or `__all__.append("x")` call adds."""
    func = call.func
    if (
        not isinstance(func, ast.Attribute)
        or not _is_all(func.value)
        or len(call.args) != 1
        or call.keywords
    ):
        return []
    arg = call.args[0]
    if func.attr == "extend":
        return _strings(arg) or []
    if (
        func.attr == "append"
        and isinstance(arg, ast.Constant)
        and isinstance(arg.value, str)
    ):
        return [arg.value]
    return []


def _reuses_quote(source: str) -> bool:
    """True when an `f"` has a `{` before its first quote that no `}` closes,
    so that quote sits inside the replacement field.

    One pass per f-string; a backtracking pattern is quadratic on a long run
    of braces.
    """
    return any(
        head.rfind("{") > head.rfind("}")
        for head in (match.group(1) for match in _FSTRING.finditer(source))
    )


class _Lines:
    """The text `ast` parsed, split as the compiler splits it.

    ast columns are UTF-8 byte offsets. Mapping one to a str index resumes
    from the last mapping on that row, so a minified line holding thousands
    of statements is decoded once rather than once per statement.
    """

    def __init__(self, text: str) -> None:
        self.rows = text.split("\n")
        self._decoded: dict[int, tuple[bytes, int, int]] = {}

    def index(self, row: int, offset: int) -> int:
        """Byte column `offset` of `row` (1-based) as an index into the row."""
        line = self.rows[row - 1]
        if line.isascii():
            return offset
        encoded, done, chars = self._decoded.get(row) or (line.encode(), 0, 0)
        if offset < done:
            done = chars = 0
        chars += len(encoded[done:offset].decode("utf-8", "ignore"))
        self._decoded[row] = (encoded, offset, chars)
        return chars

    def region(
        self, row: int, offset: int, end_row: int, end_offset: int | None
    ) -> list[str]:
        """Rows `row`..`end_row`, from byte column `offset` on the first to
        `end_offset` (None for its end) on the last."""
        col = self.index(row, offset)
        last = self.rows[end_row - 1]
        end_col = len(last) if end_offset is None else self.index(end_row, end_offset)
        if row == end_row:
            return [last[col:end_col]]
        region = self.rows[row - 1 : end_row]
        region[0] = region[0][col:]
        region[-1] = last[:end_col]
        return region


def _tokenize(text: str, rows: int = 0) -> tuple[list[tokenize.TokenInfo], str]:
    """Tokens of `text`, and why tokenizing stopped early ("" if it did not).

    With `rows`, stops at the first token that ends below that row: a
    statement's first line needs nothing from the rest of a long value.
    """
    found: list[tokenize.TokenInfo] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            found.append(token)
            if rows and token.end[0] > rows:
                break
    except (tokenize.TokenError, SyntaxError) as err:
        return found, f"{type(err).__name__}: {err}"
    return found, ""


def _header(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, lines: _Lines
) -> str:
    """A def or class header, from its keyword to the colon that ends it.

    A one-line header with no string, comment or lambda is cut directly;
    anything else is tokenized up to the first body statement, which bounds
    the scan however long the header runs.
    """
    row = node.lineno
    if not 0 < row <= len(lines.rows):
        return ""
    col = lines.index(row, node.col_offset)
    plain = _plain_header(lines.rows[row - 1][col:])
    if plain is not None:
        return plain

    first = node.body[0]
    starts = [(first.lineno, first.col_offset)]
    starts += [(d.lineno, d.col_offset) for d in getattr(first, "decorator_list", ())]
    end_row, end_offset = min(starts)
    region = lines.region(row, node.col_offset, end_row, end_offset)
    text, _ = _source(_tokenize("\n".join(region) + "\n")[0], 0, region, _Stop.COLON)
    return text


def _plain_header(text: str) -> str | None:
    """`text` up to its first top-level colon with whitespace collapsed, or
    None when tokens are needed to find that colon safely."""
    unsafe = _UNSAFE.search(text)
    limit = unsafe.start() if unsafe else len(text)
    depth = counted = 0
    colon = text.find(":", 0, limit)
    while colon != -1:
        part = text[counted:colon]
        depth += sum(map(part.count, _OPEN)) - sum(map(part.count, _CLOSE))
        if depth == 0:
            head = text[:colon]
            return None if "lambda" in head else " ".join(head.split())
        counted = colon
        colon = text.find(":", colon + 1, limit)
    return None


def _statement(node: ast.stmt, lines: _Lines, stop: _Stop) -> str:
    """A simple statement's source text: all of it, or its first line."""
    row = node.lineno
    end_row = node.end_lineno or row
    if not 0 < row <= end_row <= len(lines.rows):
        return ""
    region = lines.region(row, node.col_offset, end_row, node.end_col_offset)
    if len(region) == 1 and not _UNSAFE.search(region[0]):
        return " ".join(region[0].split())
    rows = 1 if stop is _Stop.LINE else 0
    text, _ = _source(_tokenize("\n".join(region) + "\n", rows)[0], 0, region, stop)
    return text


class _Stop(StrEnum):
    """Where `_source` stops reading."""

    COLON = "colon"            # the colon that ends a def or class header
    STATEMENT = "statement"    # the end of the statement
    LINE = "line"              # the end of the statement's first line


def _source(
    tokens: Sequence[tokenize.TokenInfo],
    start: int,
    lines: Sequence[str],
    stop: _Stop,
) -> tuple[str, int]:
    """Source text of the tokens from `start`, and the index it stopped at.

    Comments are dropped and the gap between two tokens becomes one space;
    each string literal, f-strings included, is copied verbatim. Brackets
    and lambdas are tracked, so only the colon that closes a header ends it.
    """
    parts: list[str] = []
    previous: tuple[int, int] | None = None
    first_row = tokens[start].start[0] if start < len(tokens) else 0
    depth = lambdas = 0
    index = start
    while index < len(tokens):
        token = tokens[index]
        kind, text = token.type, token.string
        if kind in _ENDS:
            break
        if kind in _SKIPPED:
            index += 1
            continue
        last = index
        if kind in _OPENERS:
            last = _closing(tokens, index)
            text = _slice(lines, token.start, tokens[last].end)
        elif kind == tokenize.OP:
            if text in _OPEN:
                depth += 1
            elif text in _CLOSE:
                depth -= 1
            elif depth == 0 and text == ";":
                break
            elif depth == 0 and text == ":" and stop is _Stop.COLON:
                if not lambdas:
                    break
                lambdas -= 1
        elif kind == tokenize.NAME and text == "lambda" and depth == 0:
            lambdas += 1

        begin, end = token.start, tokens[last].end
        if stop is _Stop.LINE and begin[0] > first_row:
            break
        if previous is not None and begin != previous:
            parts.append(" ")
        index = last + 1
        if stop is _Stop.LINE and end[0] > first_row:
            parts.append(text.split("\n", 1)[0])
            break
        parts.append(text)
        previous = end
    return "".join(parts), index


def _closing(tokens: Sequence[tokenize.TokenInfo], index: int) -> int:
    """Index of the token closing the f-string or t-string opened at `index`."""
    depth = 0
    for position in range(index, len(tokens)):
        kind = tokens[position].type
        if kind in _OPENERS:
            depth += 1
        elif kind in _CLOSERS:
            depth -= 1
            if depth == 0:
                return position
    return len(tokens) - 1


def _slice(lines: Sequence[str], begin: tuple[int, int], end: tuple[int, int]) -> str:
    (row, col), (end_row, end_col) = begin, end
    if row == end_row:
        return lines[row - 1][col:end_col]
    middle = lines[row : end_row - 1]
    return "\n".join([lines[row - 1][col:], *middle, lines[end_row - 1][:end_col]])


# -- token scan (degraded path) ------------------------------------------


class _BlockKind(StrEnum):
    CLASS = "class"     # definitions inside are the class's members
    BRANCH = "branch"   # if/try family: definitions count for the level outside
    OPAQUE = "opaque"   # function bodies, loops, `with`: nothing inside is API


@dataclass
class _Draft:
    """A symbol under construction; frozen into `Symbol` at the end."""

    name: str
    kind: SymbolKind
    line: int
    end_line: int
    signature: str
    is_async: bool = False
    children: list[_Draft] = field(default_factory=list)
    names: set[str] = field(default_factory=set)

    def freeze(self) -> Symbol:
        return Symbol(
            name=self.name,
            kind=self.kind,
            line=self.line,
            end_line=self.end_line,
            signature=self.signature,
            children=tuple(child.freeze() for child in self.children),
            is_public=_is_public(self.name),
            is_async=self.is_async,
        )


@dataclass
class _Block:
    kind: _BlockKind
    draft: _Draft | None = None


class _TokenScan:
    """The exact path's shapes, read from a token stream.

    INDENT and DEDENT give the block structure, so a definition is placed by
    the blocks around it, and a block's DEDENT gives its owner's end line.
    """

    def __init__(self, tokens: list[tokenize.TokenInfo], lines: list[str]) -> None:
        self.tokens = tokens
        self.lines = lines
        self.top: list[_Draft] = []
        self.names: set[str] = set()
        self.stack: list[_Block] = []
        self.heads: list[str] = [""]   # first word of the last statement, per depth
        self.opener = _Block(_BlockKind.OPAQUE)
        self.pending: _Draft | None = None
        self.last_row = 0

    def scan(self) -> list[Symbol]:
        tokens = self.tokens
        index = 0
        at_start = True   # the next token begins a statement
        simple = True     # the logical line began with a simple statement
        while index < len(tokens):
            token = tokens[index]
            kind = token.type
            if kind == tokenize.INDENT:
                self.stack.append(self.opener)
                self.heads.append("")
            elif kind == tokenize.DEDENT:
                self._dedent()
            elif kind == tokenize.NEWLINE:
                self._newline(token.start[0])
                at_start = True
            elif kind in _SKIPPED or kind == tokenize.ENDMARKER:
                pass
            elif at_start:
                at_start = False
                simple = token.string not in _COMPOUND
                index = self._statement(index)
                continue
            elif simple and kind == tokenize.OP and token.string == ";":
                at_start = True
            index += 1
        # The exact path's order: definitions, then constants.
        top = sorted(self.top, key=lambda draft: draft.kind is SymbolKind.CONSTANT)
        return [draft.freeze() for draft in top]

    def _dedent(self) -> None:
        if not self.stack:
            return
        block = self.stack.pop()
        self.heads.pop()
        if block.draft is not None:
            block.draft.end_line = max(block.draft.end_line, self.last_row)

    def _newline(self, row: int) -> None:
        self.last_row = row
        if self.pending is not None:
            self.pending.end_line = max(self.pending.end_line, row)
            self.pending = None

    def _word(self, index: int) -> str:
        """The name at `index`, or "" when it is not an identifier."""
        if index >= len(self.tokens) or self.tokens[index].type != tokenize.NAME:
            return ""
        word = self.tokens[index].string
        return "" if keyword.iskeyword(word) else word

    def _op(self, index: int) -> str:
        if index >= len(self.tokens) or self.tokens[index].type != tokenize.OP:
            return ""
        return self.tokens[index].string

    def _statement(self, index: int) -> int:
        """Handle the statement starting at `index`; return where to go on."""
        token = self.tokens[index]
        word = token.string if token.type == tokenize.NAME else ""
        previous, self.heads[-1] = self.heads[-1], word
        self.opener = _Block(_BlockKind.OPAQUE)

        after = self.tokens[index + 1].string if index + 1 < len(self.tokens) else ""
        if word in ("def", "class") or (word == "async" and after == "def"):
            return self._definition(index)
        if word == "type" and self._is_alias(index):
            return self._alias(index)
        if word in _BRANCHES and (word != "else" or previous in _BRANCHES):
            self.opener = _Block(_BlockKind.BRANCH)
        elif (
            not self.stack
            and _is_constant(word)
            and self._op(index + 1) in ("=", ":")
            and self._single_target(index)
        ):
            return self._constant(index)
        return index + 1

    def _definition(self, index: int) -> int:
        at = index + 1 if self.tokens[index].string == "async" else index
        name = self._word(at + 1)
        if not name:
            return index + 1
        is_class = self.tokens[at].string == "class"
        signature, stop = _source(self.tokens, index, self.lines, _Stop.COLON)
        draft = self._place(
            name,
            SymbolKind.CLASS if is_class else SymbolKind.FUNCTION,
            self.tokens[index].start[0],
            signature,
            is_async=at != index,
        )
        if draft is not None:
            draft.end_line = self.tokens[min(stop, len(self.tokens) - 1)].start[0]
            kind = _BlockKind.CLASS if is_class else _BlockKind.OPAQUE
            self.opener = _Block(kind, draft)
            self.pending = draft
        return max(stop, index + 1)

    def _is_alias(self, index: int) -> bool:
        """`type X = ...` or `type X[T] = ...`, not a name called `type`."""
        if not self._word(index + 1):
            return False
        at = index + 2
        if self._op(at) == "[":
            depth = 0
            while at < len(self.tokens) and self.tokens[at].type not in _ENDS:
                text = self._op(at)
                depth += (text in _OPEN) - (text in _CLOSE)
                at += 1
                if depth == 0:
                    break
        return self._op(at) == "="

    def _alias(self, index: int) -> int:
        signature, stop = _source(self.tokens, index, self.lines, _Stop.STATEMENT)
        name, row = self._word(index + 1), self.tokens[index].start[0]
        self.pending = self._place(name, SymbolKind.TYPE, row, signature)
        return max(stop, index + 1)

    def _single_target(self, index: int) -> bool:
        """No second top-level `=`: the exact path skips `A = B = 1`, and a
        lambda's default values are not assignments."""
        depth = assignments = 0
        for at in range(index, len(self.tokens)):
            token = self.tokens[at]
            if token.type in _ENDS:
                break
            if token.type == tokenize.NAME and token.string == "lambda" and not depth:
                break
            if token.type != tokenize.OP:
                continue
            depth += (token.string in _OPEN) - (token.string in _CLOSE)
            if not depth and token.string == ";":
                break
            assignments += not depth and token.string == "="
        return assignments <= 1

    def _constant(self, index: int) -> int:
        token = self.tokens[index]
        signature, stop = _source(self.tokens, index, self.lines, _Stop.LINE)
        row = token.start[0]
        draft = _Draft(token.string, SymbolKind.CONSTANT, row, row, signature)
        self.top.append(draft)
        self.pending = draft
        return max(stop, index + 1)

    def _place(
        self,
        name: str,
        kind: SymbolKind,
        row: int,
        signature: str,
        *,
        is_async: bool = False,
    ) -> _Draft | None:
        """Attach a definition where the exact path would, or drop it.

        Inside a function, loop or `with` it is not API; inside `if`/`try` a
        name already defined at that level is skipped, as on the exact path.
        """
        owner: _Draft | None = None
        conditional = False
        for block in reversed(self.stack):
            if block.kind is _BlockKind.BRANCH:
                conditional = True
                continue
            if block.kind is not _BlockKind.CLASS or block.draft is None:
                return None
            owner = block.draft
            break

        names = self.names if owner is None else owner.names
        if conditional and name in names:
            return None
        names.add(name)
        if owner is not None and kind is SymbolKind.FUNCTION:
            kind = SymbolKind.METHOD
        draft = _Draft(name, kind, row, row, signature, is_async)
        (self.top if owner is None else owner.children).append(draft)
        return draft


register(PythonParser(), replace=False)
