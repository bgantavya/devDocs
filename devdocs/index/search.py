"""Retrieval over chunks: lexical BM25, vector similarity, and their fusion.

Code is unusually friendly to lexical search: identifiers are precise, and
splitting `parseRetryAfter` or `parse_retry_after` into its words lets BM25
match either spelling. That index is free, so it always runs.

Vectors are optional and kept compact. Each is an `array('f')` — measured at
3 KB per 768-dimension vector against 31 KB as a list of floats — and scored
with a dot product, since stored vectors are unit length. Beyond about 13,000
vectors a brute-force pass passes 200 ms, so a binary prefilter takes over:
each vector's sign bits packed into one integer, Hamming distance by XOR and
`int.bit_count`, and an exact rerank of the closest few percent.

`fuse` merges rankings by reciprocal rank, which needs no score calibration
between the two very different scales.
"""

from __future__ import annotations

import math
import operator
import re
from array import array
from collections import Counter
from collections.abc import Collection, Sequence

from devdocs.index.chunk import Chunk

__all__ = ["LexicalIndex", "VectorIndex", "fuse", "terms"]

_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_STOP = frozenset((
    "an", "and", "are", "as", "at", "be", "by", "class", "const", "def", "do",
    "else", "end", "export", "false", "fn", "for", "from", "func", "function",
    "if", "impl", "import", "in", "int", "is", "it", "let", "new", "none",
    "null", "of", "on", "or", "private", "protected", "pub", "public",
    "return", "self", "static", "str", "the", "this", "to", "true", "use",
    "var", "void", "with",
))

PREFILTER_ABOVE = 13_000


def terms(text: str) -> list[str]:
    """Lowercased words of `text`, splitting camelCase and snake_case."""
    return [
        word for word in (m.group(0).lower() for m in _WORD.finditer(text))
        if len(word) > 1 and word not in _STOP
    ]


class LexicalIndex:
    """BM25 over chunk text (k1=1.2, b=0.75), built once in memory."""

    def __init__(self, chunks: Sequence[Chunk], *, k1: float = 1.2, b: float = 0.75):
        self.chunks = list(chunks)
        self.k1 = k1
        self.b = b
        self._postings: dict[str, list[tuple[int, int]]] = {}
        self._lengths: list[int] = []
        for index, chunk in enumerate(self.chunks):
            counts = Counter(terms(chunk.text))
            self._lengths.append(sum(counts.values()))
            for term, count in counts.items():
                self._postings.setdefault(term, []).append((index, count))
        total = len(self._lengths)
        self._average = (sum(self._lengths) / total) if total else 0.0
        self._idf = {
            term: math.log(1 + (total - len(p) + 0.5) / (len(p) + 0.5))
            for term, p in self._postings.items()
        }

    def search(
        self, query: str, k: int = 5, *, exclude: Collection[str] = ()
    ) -> list[tuple[Chunk, float]]:
        """The `k` best chunks for `query`, skipping chunks from `exclude` files."""
        scores: dict[int, float] = {}
        for term in set(terms(query)):
            idf = self._idf.get(term)
            if idf is None:
                continue
            for index, tf in self._postings[term]:
                length = self._lengths[index]
                norm = 1 - self.b + self.b * length / (self._average or 1.0)
                scores[index] = scores.get(index, 0.0) + idf * tf * (self.k1 + 1) / (
                    tf + self.k1 * norm
                )
        ranked = sorted(
            (i for i in scores if self.chunks[i].source not in exclude),
            key=lambda i: (-scores[i], self.chunks[i].id),
        )
        return [(self.chunks[i], round(scores[i], 6)) for i in ranked[:k]]


def _signs(vector: Sequence[float]) -> int:
    bits = 0
    for position, value in enumerate(vector):
        if value > 0:
            bits |= 1 << position
    return bits


class VectorIndex:
    """Nearest neighbours by dot product over unit vectors."""

    def __init__(
        self,
        ids: Sequence[str],
        vectors: Sequence[array[float]],
        *,
        prefilter_above: int = PREFILTER_ABOVE,
    ) -> None:
        if len(ids) != len(vectors):
            raise ValueError("ids and vectors differ in length")
        self.ids = list(ids)
        self.vectors = list(vectors)
        self._signs = (
            [_signs(v) for v in self.vectors]
            if len(self.vectors) > prefilter_above else None
        )

    def __len__(self) -> int:
        return len(self.ids)

    def search(
        self, query: Sequence[float], k: int = 5, *, exclude: Collection[str] = ()
    ) -> list[tuple[str, float]]:
        """The `k` most similar ids, skipping ids in `exclude`."""
        candidates: Sequence[int] = range(len(self.vectors))
        if self._signs is not None:
            q = _signs(query)
            keep = max(200, len(self.vectors) // 20, k)
            signs = self._signs
            candidates = sorted(
                range(len(signs)), key=lambda i: ((q ^ signs[i]).bit_count(), i)
            )[:keep]
        probe = array("f", query)
        scored = [
            (sum(map(operator.mul, self.vectors[i], probe)), i)
            for i in candidates if self.ids[i] not in exclude
        ]
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [(self.ids[i], round(score, 6)) for score, i in scored[:k]]


def fuse(*rankings: Sequence[str], k: int = 60) -> list[str]:
    """Reciprocal rank fusion of several rankings of ids, best first."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + position + 1)
    return sorted(scores, key=lambda item: (-scores[item], item))
