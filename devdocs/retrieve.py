"""Choosing the context each prompt carries: exact first, related second.

Three sources, cheapest and most certain first:

1. Structural — how the file is used and what it builds on, from the import
   graph (`devdocs.context`). Exact and free.
2. Lexical — related code the graph cannot see, such as a handler and the
   client that calls its route, by BM25 over symbol chunks. Free.
3. Semantic — the same question by embedding similarity, when enabled. The
   query is the mean of the file's own stored chunk vectors, so retrieval adds
   no API call per prompt; only embedding changed chunks costs anything.

Related code is looked for only where the graph says little: a file with
callers and dependencies already has its exact context, and fuzzy hits next
to it would mostly repeat its neighbours at the price of tokens. Everything
is fitted to a token budget, structural context first, so retrieval can
sharpen a prompt but never crowd out the source.
"""

from __future__ import annotations

import math
from array import array
from collections.abc import Collection, Mapping, Sequence

from devdocs.context import Snippet, StructuralContext, fit
from devdocs.index.chunk import Chunk
from devdocs.index.search import LexicalIndex, VectorIndex, fuse
from devdocs.parse import FileFacts

__all__ = ["Retriever"]

_MIN_SCORE = 2.0        # BM25 below this is coincidence, not relation
_RELATIVE = 0.5         # keep hits scoring at least half the best one
_CLOSE = 0.9            # keep vector hits within 10% of the nearest one
_THIN = 3               # fewer exact snippets than this: add related code


class Retriever:
    """Context for file, module and architecture prompts."""

    def __init__(
        self,
        structural: StructuralContext,
        *,
        chunks: Sequence[Chunk] = (),
        lexical: LexicalIndex | None = None,
        vectors: VectorIndex | None = None,
        vector_of: Mapping[str, array[float]] | None = None,
    ) -> None:
        self.structural = structural
        self.lexical = lexical
        self.vectors = vectors
        self.vector_of = vector_of or {}
        self._chunks = {chunk.id: chunk for chunk in chunks}
        self._by_source: dict[str, list[Chunk]] = {}
        for chunk in chunks:
            self._by_source.setdefault(chunk.source, []).append(chunk)

    def for_file(
        self, relpath: str, facts: FileFacts | None, *, budget_tokens: int = 700
    ) -> list[Snippet]:
        exact = self.structural.for_file(relpath, budget_tokens=budget_tokens)
        if len(exact) >= _THIN:
            return exact
        seen = self._linked([relpath]) | {s.source for s in exact}
        related = self._related(_query(facts), [relpath], exclude=seen, k=3)
        return fit([*exact, *related], budget_tokens)

    def for_module(
        self, files: Collection[str], facts: Mapping[str, FileFacts],
        *, budget_tokens: int = 600,
    ) -> list[Snippet]:
        """Code outside the module and unlinked to it by imports: the module
        prompt already names the modules on either side."""
        query = " ".join(_query(facts.get(f)) for f in sorted(files))
        related = self._related(query, sorted(files), exclude=self._linked(files), k=4)
        return fit(related, budget_tokens)

    def for_architecture(self, *, budget_tokens: int = 1500) -> list[Snippet]:
        return self.structural.for_architecture(budget_tokens=budget_tokens)

    def _linked(self, files: Collection[str]) -> set[str]:
        """`files` and every file an import edge joins them to."""
        linked = set(files)
        for relpath in files:
            node = self.structural.graph.file(relpath)
            if node is not None:
                linked.update(node.internal, node.dependents, node.tested_by)
        return linked

    def _related(
        self, query: str, sources: Sequence[str], *, exclude: Collection[str], k: int
    ) -> list[Snippet]:
        rankings: list[list[str]] = []
        if self.lexical is not None and query:
            hits = self.lexical.search(query, k * 3, exclude=exclude)
            if hits and hits[0][1] >= _MIN_SCORE:
                floor = hits[0][1] * _RELATIVE
                rankings.append([c.id for c, score in hits if score >= floor])
        probe = self._mean(sources)
        if self.vectors is not None and probe is not None:
            excluded = {c.id for s in exclude for c in self._by_source.get(s, ())}
            near = self.vectors.search(probe, k * 3, exclude=excluded)
            if near and near[0][1] > 0:
                floor = near[0][1] * _CLOSE
                rankings.append([i for i, score in near if score >= floor])
        snippets: list[Snippet] = []
        for chunk_id in fuse(*rankings)[:k]:
            chunk = self._chunks.get(chunk_id)
            if chunk is not None:
                snippets.append(Snippet(
                    chunk.source, chunk.start_line, _headline(chunk), "related",
                    chunk.symbol,
                ))
        return snippets

    def _mean(self, sources: Sequence[str]) -> array[float] | None:
        vectors = [
            self.vector_of[c.id]
            for source in sources for c in self._by_source.get(source, ())
            if c.id in self.vector_of
        ]
        if not vectors:
            return None
        total = array("f", [0.0]) * len(vectors[0])
        for vector in vectors:
            for index, value in enumerate(vector):
                total[index] += value
        norm = math.sqrt(sum(v * v for v in total)) or 1.0
        return array("f", (v / norm for v in total))


def _query(facts: FileFacts | None) -> str:
    """What a file is about, in words: its public names and docstring."""
    if facts is None:
        return ""
    names = " ".join(s.name for s in facts.public_symbols)
    return f"{names} {facts.module_docstring}".strip()


def _headline(chunk: Chunk) -> str:
    """The first meaningful code line of a chunk, after its label line."""
    for line in chunk.text.splitlines()[1:]:
        text = " ".join(line.split())
        if text and not text.startswith(("@", "#[", "//", "#", "/*", "*")):
            return text[:157] + "..." if len(text) > 160 else text
    return chunk.symbol or chunk.source
