"""Python facts via `ast`, degrading to `tokenize` when the syntax is too new.

`ast` only parses syntax its own interpreter understands, so a devdocs running
on 3.11 hits SyntaxError on PEP 695 generics from a 3.12 codebase. The fallback
scans tokens instead, which survives `match`, `except*` and PEP 695.

It does not survive PEP 701 f-strings: before 3.12 the tokenizer has no
FSTRING_START, so `f"{"a"}"` yields three wrong tokens with no error raised.
Silent corruption is worse than a gap, so the fallback detects that case and
marks the file FAILED rather than emitting a symbol table that looks complete.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

from devdocs.parse import (
    Confidence,
    FileFacts,
    Symbol,
    SymbolKind,
    register,
)

__all__ = ["PythonParser"]

_DEF = re.compile(r"^(?P<indent>\s*)(?:(?P<async>async)\s+)?def\s+(?P<name>\w+)\s*\(")
_CLASS = re.compile(r"^(?P<indent>\s*)class\s+(?P<name>\w+)\s*[(:]")
_CONST = re.compile(r"^(?P<name>[A-Z_][A-Z0-9_]*)\s*(?::[^=]+)?=")


class PythonParser:
    """Parses .py and .pyi files."""

    languages: tuple[str, ...] = ("python",)

    def parse(self, path: Path, source: str) -> FileFacts:
        loc = source.count("\n") + 1
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as err:
            return self._fallback(path, source, loc, err)
        except (ValueError, RecursionError) as err:
            # Null bytes or pathological nesting: ast raises, tokenize will too.
            return FileFacts(
                path=path,
                language="python",
                loc=loc,
                confidence=Confidence.FAILED,
                notes=[f"unparseable: {type(err).__name__}"],
            )
        return self._from_ast(path, tree, source, loc)

    # -- exact path ------------------------------------------------------

    def _from_ast(
        self, path: Path, tree: ast.Module, source: str, loc: int
    ) -> FileFacts:
        lines = source.splitlines()
        symbols = tuple(
            self._symbol(node, lines, top_level=True)
            for node in tree.body
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        )
        constants = tuple(self._constants(tree, lines))

        return FileFacts(
            path=path,
            language="python",
            symbols=symbols + constants,
            imports=tuple(self._imports(tree)),
            exports=tuple(self._dunder_all(tree)),
            module_docstring=ast.get_docstring(tree) or "",
            loc=loc,
            confidence=Confidence.EXACT,
        )

    def _symbol(
        self, node: ast.AST, lines: list[str], *, top_level: bool = False
    ) -> Symbol:
        name = getattr(node, "name", "")
        is_class = isinstance(node, ast.ClassDef)
        is_async = isinstance(node, ast.AsyncFunctionDef)

        if is_class:
            kind = SymbolKind.CLASS
        else:
            kind = SymbolKind.FUNCTION if top_level else SymbolKind.METHOD

        children: tuple[Symbol, ...] = ()
        if is_class and isinstance(node, ast.ClassDef):
            children = tuple(
                self._symbol(child, lines)
                for child in node.body
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            )

        bases: tuple[str, ...] = ()
        if isinstance(node, ast.ClassDef):
            bases = tuple(self._render(base) for base in node.bases)

        decorators = tuple(
            self._render(d) for d in getattr(node, "decorator_list", [])
        )

        return Symbol(
            name=name,
            kind=kind,
            line=getattr(node, "lineno", 0),
            end_line=getattr(node, "end_lineno", 0) or 0,
            signature=self._signature(node, lines),
            docstring=ast.get_docstring(node) or "",  # type: ignore[arg-type]
            decorators=decorators,
            bases=bases,
            children=children,
            is_public=not name.startswith("_") or name.startswith("__"),
            is_async=is_async,
        )

    @staticmethod
    def _signature(node: ast.AST, lines: list[str]) -> str:
        """Take the signature verbatim from the source.

        Reconstructing it from the AST risks describing an API that differs
        from the real one, which is the failure mode docs must never have.
        """
        start = getattr(node, "lineno", 0)
        if not start or start > len(lines):
            return ""

        # Scan to the colon that closes the header, tracking bracket depth so a
        # colon inside an annotation, default or lambda does not end it early.
        text = "\n".join(lines[start - 1 : start + 30])
        depth = 0
        for index, char in enumerate(text):
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            elif char == ":" and depth == 0:
                return " ".join(text[:index].split())
        return " ".join(text.splitlines()[0].split())

    @staticmethod
    def _render(node: ast.AST) -> str:
        try:
            return ast.unparse(node)
        except Exception:
            return getattr(node, "id", "") or ""

    @staticmethod
    def _imports(tree: ast.Module) -> list[str]:
        """Module paths this file imports, resolved to the specific module.

        `import a.b` records `a.b`, not `a`: crediting the parent package would
        give every sibling module the parent's fan-in and wreck the ranking.
        """
        found: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                prefix = "." * node.level
                found.append(f"{prefix}{node.module}")
        return sorted(set(found))

    @staticmethod
    def _dunder_all(tree: ast.Module) -> list[str]:
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "__all__" not in targets:
                continue
            if isinstance(node.value, ast.List | ast.Tuple):
                return [
                    element.value
                    for element in node.value.elts
                    if isinstance(element, ast.Constant)
                    and isinstance(element.value, str)
                ]
        return []

    @staticmethod
    def _constants(tree: ast.Module, lines: list[str]) -> list[Symbol]:
        found: list[Symbol] = []
        for node in tree.body:
            target: ast.expr | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
            elif isinstance(node, ast.AnnAssign):
                target = node.target
            if not isinstance(target, ast.Name) or not target.id.isupper():
                continue
            if target.id.startswith("_"):
                continue
            line = node.lineno
            found.append(
                Symbol(
                    name=target.id,
                    kind=SymbolKind.CONSTANT,
                    line=line,
                    end_line=node.end_lineno or line,
                    signature=lines[line - 1].strip() if line <= len(lines) else "",
                )
            )
        return found

    # -- degraded path ---------------------------------------------------

    def _fallback(
        self, path: Path, source: str, loc: int, err: SyntaxError
    ) -> FileFacts:
        """Recover signatures with `tokenize` when `ast` refuses the syntax."""
        if self._has_nested_quote_fstring(source):
            return FileFacts(
                path=path,
                language="python",
                loc=loc,
                confidence=Confidence.FAILED,
                notes=[
                    "PEP 701 f-strings need Python 3.12+ to parse; "
                    "this interpreter would mis-tokenize them"
                ],
            )

        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return FileFacts(
                path=path,
                language="python",
                loc=loc,
                confidence=Confidence.FAILED,
                notes=[f"syntax error at line {err.lineno}: {err.msg}"],
            )

        if any(token.type == tokenize.ERRORTOKEN for token in tokens):
            return FileFacts(
                path=path,
                language="python",
                loc=loc,
                confidence=Confidence.FAILED,
                notes=["tokenizer produced error tokens; symbols unreliable"],
            )

        symbols = self._scan_lines(source)
        return FileFacts(
            path=path,
            language="python",
            symbols=tuple(symbols),
            loc=loc,
            confidence=Confidence.DEGRADED,
            notes=[
                f"ast failed at line {err.lineno} ({err.msg}); "
                "signatures recovered by token scan, docstrings unavailable"
            ],
        )

    @staticmethod
    def _has_nested_quote_fstring(source: str) -> bool:
        """Detect an f-string reusing its own quote character inside a brace.

        Legal from 3.12. On an older tokenizer it silently produces wrong
        tokens instead of an error, so it must be caught before scanning.
        """
        import sys

        if sys.version_info >= (3, 12):
            return False
        return re.search(r'f"[^"\n]*\{[^}\n]*"', source) is not None

    @staticmethod
    def _scan_lines(source: str) -> list[Symbol]:
        """Recover top-level defs, classes and constants by line shape."""
        found: list[Symbol] = []
        for number, line in enumerate(source.splitlines(), start=1):
            match = _CLASS.match(line) or _DEF.match(line)
            if match and not match.group("indent"):
                name = match.group("name")
                is_class = line.lstrip().startswith("class")
                found.append(
                    Symbol(
                        name=name,
                        kind=SymbolKind.CLASS if is_class else SymbolKind.FUNCTION,
                        line=number,
                        signature=line.strip().rstrip(":"),
                        is_public=not name.startswith("_"),
                        is_async="async" in (match.groupdict().get("async") or ""),
                    )
                )
                continue
            constant = _CONST.match(line)
            if constant:
                found.append(
                    Symbol(
                        name=constant.group("name"),
                        kind=SymbolKind.CONSTANT,
                        line=number,
                        signature=line.strip(),
                    )
                )
        return found


register(PythonParser())
