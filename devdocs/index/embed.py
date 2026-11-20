"""Chunk embeddings, kept current for the cost of what changed.

Embedding a whole repository on every run would cost more than documenting
it, so vectors are stored with their chunk and only chunks without a vector
from the current model and size are sent. Each batch is reserved against the
same `Budget` and paced by the same `RateLimiter` as generation, so turning
semantic retrieval on can never push a run past its ceiling.

The embeddings endpoint reports no token usage, so a batch is reserved and
booked at a ceiling (one token per UTF-8 byte), which can only overstate it.
"""

from __future__ import annotations

from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from devdocs.errors import ConfigError
from devdocs.index.chunk import Chunk, ChunkStore
from devdocs.llm import Embedder
from devdocs.llm.budget import Budget, RateLimiter, token_ceiling
from devdocs.llm.catalog import ModelInfo

__all__ = ["EmbeddingQuote", "Embeddings"]


@dataclass(frozen=True)
class EmbeddingQuote:
    """What bringing the vectors up to date would take, at most."""

    chunks: int
    requests: int
    tokens: int
    cost: float


class Embeddings:
    """Quotes and performs the vector updates for one embedding model.

    `embedder` may be None for quoting alone, as `devdocs estimate` does
    without a key.
    """

    def __init__(
        self,
        store: ChunkStore,
        *,
        model: ModelInfo,
        dims: int,
        embedder: Embedder | None = None,
        budget: Budget | None = None,
        limiter: RateLimiter | None = None,
        batch: int = 100,
    ) -> None:
        self.store = store
        self.model = model
        self.dims = dims
        self.embedder = embedder
        self.budget = budget or Budget()
        self.limiter = limiter
        self.batch = batch

    def quote(self) -> EmbeddingQuote:
        """Priced batch by batch, as `update` books them."""
        batches = self._batches()
        return EmbeddingQuote(
            chunks=sum(len(group) for group in batches),
            requests=len(batches),
            tokens=sum(self._tokens(group) for group in batches),
            cost=sum(self._price(group) for group in batches),
        )

    def update(
        self, *, progress: Callable[[int, int], None] | None = None
    ) -> tuple[int, float]:
        """Embed every chunk that lacks a current vector.

        Returns (chunks embedded, dollars booked). Batches already stored are
        kept when a later one stops on the budget or the daily quota.
        """
        if self.embedder is None:
            raise ConfigError("no embedding model configured")
        batches = self._batches()
        total = sum(len(group) for group in batches)
        done = 0
        spent = 0.0
        for group in batches:
            cost = self._price(group)
            self.budget.reserve(cost)
            if self.limiter is not None:
                try:
                    self.limiter.acquire()
                except BaseException:
                    self.budget.release(cost)
                    raise
            try:
                vectors = self.embedder.embed([c.text for c in group])
            except BaseException:
                self.budget.fail(cost)    # it reached the provider: still a request
                raise
            self.budget.settle(cost, cost)
            self.store.store(
                [(c.id, v) for c, v in zip(group, vectors, strict=True)], self.model.id
            )
            done += len(group)
            spent += cost
            if progress is not None:
                progress(done, total)
        return done, spent

    def vectors(self) -> tuple[list[str], list[array[float]]]:
        """Every stored vector of this model and size, with its chunk id."""
        return self.store.vectors(self.model.id, self.dims)

    def _batches(self) -> list[list[Chunk]]:
        pending = self.store.missing(self.model.id, self.dims)
        return [pending[i : i + self.batch] for i in range(0, len(pending), self.batch)]

    @staticmethod
    def _tokens(chunks: Sequence[Chunk]) -> int:
        return token_ceiling(*(c.text for c in chunks))

    def _price(self, chunks: Sequence[Chunk]) -> float:
        return self.model.estimate(self._tokens(chunks), 0)
