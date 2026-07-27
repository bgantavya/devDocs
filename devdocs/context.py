"""Cross-file context for a prompt, retrieved from the code's own structure.

A file is easier to describe accurately when the writer can see how it is used
and what it builds on, and both are known without a model or an embedding.
The import graph names the files on either side; a whole-word search of their
source — with comments and strings masked out — finds the exact lines that use
this file's public names, and the parser already holds the signatures of what
it calls. The architecture overview gets the body of the program's entry
point, which is where the real control flow is written down.

This is the retrieval half of the RAG design that costs nothing: exact, free
to compute, and capped by a token budget so it never crowds out the source.
Lexical and semantic retrieval (`devdocs.index.search`) add related code the
graph cannot see.
"""

from __future__ import annotations

import io
import re
import tokenize
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from devdocs.graph import ProjectGraph
from devdocs.llm.budget import estimate_tokens
from devdocs.parse import FileFacts, Symbol, SymbolKind

__all__ = ["Snippet", "StructuralContext", "fit", "masked_source"]

_IMPORT_LINE = re.compile(
    r"^\s*(?:import\b|from\s+\S+\s+import\b|use\s|export\s+\*|export\s*\{[^}]*\}\s*from\b"
    r"|(?:const|let|var)\s+[\w{},\s]+=\s*require\(|#include\b|package\s)"
)
_MAX_LINE = 160


@dataclass(frozen=True)
class Snippet:
    """One piece of context: a line or signature from another file."""

    source: str        # relpath it comes from
    line: int
    text: str
    kind: str          # "uses" | "defines" | "entry" | "related"
    symbol: str = ""   # the name it concerns


def masked_source(source: str, language: str) -> str:
    """`source` with comment and string contents blanked, positions kept, so
    a name inside a docstring or a log message is not mistaken for a use."""
    if language == "python":
        return _mask_python(source)
    from devdocs.issues import comment_syntax
    from devdocs.parse.masking import mask

    return mask(source, comment_syntax(language)).text


def _mask_python(source: str) -> str:
    lines = source.splitlines(keepends=True)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    chars = [list(line) for line in lines]
    for token in tokens:
        if token.type not in (tokenize.STRING, tokenize.COMMENT):
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        for row in range(start_row, end_row + 1):
            if row - 1 >= len(chars):
                break
            line = chars[row - 1]
            begin = start_col if row == start_row else 0
            end = end_col if row == end_row else len(line)
            for col in range(begin, min(end, len(line))):
                if line[col] != "\n":
                    line[col] = " "
    return "".join("".join(line) for line in chars)


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_LINE else text[: _MAX_LINE - 3] + "..."


class StructuralContext:
    """Exact context from the import graph, parsed facts and masked source.

    Masked sources are computed once per file on first use, so asking for the
    context of every file in a project stays linear in its size.
    """

    def __init__(
        self,
        graph: ProjectGraph,
        facts: Mapping[str, FileFacts],
        sources: Mapping[str, str],
    ) -> None:
        self.graph = graph
        self.facts = facts
        self.sources = sources
        self._masked: dict[str, list[str]] = {}

    def _masked_lines(self, relpath: str) -> list[str]:
        cached = self._masked.get(relpath)
        if cached is None:
            node = self.graph.file(relpath)
            language = node.language if node is not None else ""
            cached = masked_source(self.sources.get(relpath, ""), language).splitlines()
            self._masked[relpath] = cached
        return cached

    def _public_names(self, relpath: str) -> list[str]:
        facts = self.facts.get(relpath)
        if facts is None:
            return []
        return sorted({s.name for s in facts.public_symbols if len(s.name) > 2})

    # -- files -------------------------------------------------------------

    def uses(
        self, relpath: str, *, limit: int = 8, per_name: int = 2, per_file: int = 2
    ) -> list[Snippet]:
        """Lines in importing files that use this file's public names.

        Non-test importers come first, then tests; import lines are skipped
        because they say only that a name is imported, not how it is used.
        A cap per importer spreads the lines over as many callers as fit.
        """
        node = self.graph.file(relpath)
        names = self._public_names(relpath)
        if node is None or not names:
            return []
        pattern = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b")
        found: list[Snippet] = []
        seen: dict[str, int] = {}
        for importer in (*node.dependents, *node.tested_by):
            raw = self.sources.get(importer, "").splitlines()
            taken = 0
            for number, masked in enumerate(self._masked_lines(importer), start=1):
                if taken >= per_file:
                    break
                if _IMPORT_LINE.match(masked):
                    continue
                match = pattern.search(masked)
                if match is None or number > len(raw):
                    continue
                name = match.group(1)
                if seen.get(name, 0) >= per_name:
                    continue
                seen[name] = seen.get(name, 0) + 1
                taken += 1
                line = _clip(raw[number - 1])
                found.append(Snippet(importer, number, line, "uses", name))
                if len(found) >= limit:
                    return found
        return found

    def defines(self, relpath: str, *, limit: int = 12) -> list[Snippet]:
        """Signatures of the project symbols this file actually uses."""
        node = self.graph.file(relpath)
        if node is None:
            return []
        text = "\n".join(self._masked_lines(relpath))
        found: list[Snippet] = []
        for target in node.internal:
            facts = self.facts.get(target)
            if facts is None:
                continue
            for symbol in facts.public_symbols:
                if len(symbol.name) <= 2 or not symbol.signature:
                    continue
                if not re.search(rf"\b{re.escape(symbol.name)}\b", text):
                    continue
                found.append(Snippet(
                    target, symbol.line, _clip(_with_summary(symbol)), "defines",
                    symbol.name,
                ))
                if len(found) >= limit:
                    return found
        return found

    def for_file(self, relpath: str, *, budget_tokens: int = 600) -> list[Snippet]:
        """Usage first (how the file is used), then definitions it builds on,
        trimmed to `budget_tokens`."""
        return fit([*self.uses(relpath), *self.defines(relpath)], budget_tokens)

    # -- architecture ------------------------------------------------------

    def entry_bodies(self, *, limit: int = 3, lines: int = 40) -> list[Snippet]:
        """The opening of each entry point's main routine: the control flow the
        architecture overview should describe, as written."""
        found: list[Snippet] = []
        for node in self.graph.entry_points()[:limit]:
            facts = self.facts.get(node.relpath)
            raw = self.sources.get(node.relpath, "").splitlines()
            if not raw:
                continue
            start, end = 1, min(len(raw), lines)
            main = _main_symbol(facts)
            if main is not None and main.line:
                start = main.line
                stop = main.end_line or start + lines
                end = min(stop, start + lines - 1, len(raw))
            body = "\n".join(raw[start - 1 : end]).rstrip()
            if body:
                found.append(Snippet(node.relpath, start, body, "entry",
                                     main.name if main else ""))
        return found

    def for_architecture(self, *, budget_tokens: int = 1500) -> list[Snippet]:
        return fit(self.entry_bodies(), budget_tokens)


def _with_summary(symbol: Symbol) -> str:
    summary = symbol.docstring.strip().split("\n\n", 1)[0]
    summary = " ".join(summary.split())
    return f"{symbol.signature} — {summary}" if summary else symbol.signature


def _main_symbol(facts: FileFacts | None) -> Symbol | None:
    if facts is None:
        return None
    functions = [s for s in facts.symbols if s.kind is SymbolKind.FUNCTION]
    for name in ("main", "run", "cli", "app", "handler"):
        for symbol in functions:
            if symbol.name == name:
                return symbol
    return None


def fit(snippets: Sequence[Snippet], budget_tokens: int) -> list[Snippet]:
    """Keep snippets in order while they fit; a large one is skipped, not cut,
    so a smaller one after it can still be used."""
    kept: list[Snippet] = []
    used = 0
    for snippet in snippets:
        cost = estimate_tokens(snippet.source, snippet.text) + 4
        if used + cost > budget_tokens:
            continue
        kept.append(snippet)
        used += cost
    return kept
