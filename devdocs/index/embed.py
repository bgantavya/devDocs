"""Chunk embeddings, kept current for the cost of what changed.

Embedding a whole repository on every run would cost more than documenting
it, so vectors are stored with their chunk and only chunks without a vector
from the current model and size are sent. Each batch is reserved against the
same `Budget`, paced by the same `RateLimiter` and retried by the same
`RetryPolicy` as generation, so turning semantic retrieval on can never push a
run past its ceiling, and each one answered is counted in the same `DailyUsage`.

`Embeddings` works on the chunks it is given, those of the files a run selected,
whose rows `ChunkStore.replace` must have written before `update`. Quoting and
reading vectors write nothing, so an estimate leaves the store as it found it.

The embeddings endpoint reports no token usage, so a batch is reserved and
booked at a ceiling (one token per UTF-8 byte), which can only overstate it.

A stop request (`stop`, the run's signal, which the limiter shares) sends no
further batch, and a wait for a request slot or a retry ends at once. The batch
already sent is stored.
"""

from __future__ import annotations

import threading
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from devdocs.errors import BudgetExceeded, ConfigError, Interrupted
from devdocs.index.chunk import Chunk, ChunkStore
from devdocs.index.ledger import DailyUsage
from devdocs.llm import Embedder
from devdocs.llm.budget import Budget, RateLimiter, token_ceiling
from devdocs.llm.catalog import ModelInfo
from devdocs.llm.http import RetryPolicy

__all__ = ["EmbeddingQuote", "Embeddings"]


@dataclass(frozen=True)
class EmbeddingQuote:
    """What bringing the vectors up to date would take: at most these tokens and
    dollars, and these requests unless a batch has to be retried."""

    chunks: int
    requests: int
    tokens: int
    cost: float


class Embeddings:
    """Quotes and performs the vector updates for one embedding model.

    `embedder` may be None for quoting alone, as `devdocs estimate` does
    without a key. `on_retry` is told of each retry, as `RetryPolicy.run` tells
    it. `max_requests`, unless 0, is the most one `update` may send, retries
    included, even where the budget would allow more.
    """

    def __init__(
        self,
        store: ChunkStore,
        chunks: Sequence[Chunk],
        *,
        model: ModelInfo,
        dims: int,
        embedder: Embedder | None = None,
        budget: Budget | None = None,
        limiter: RateLimiter | None = None,
        batch: int = 100,
        stop: threading.Event | None = None,
        retry: RetryPolicy | None = None,
        on_retry: Callable[[int, BaseException, float], None] | None = None,
        usage: DailyUsage | None = None,
        max_requests: int = 0,
    ) -> None:
        self.store = store
        self.chunks = chunks
        self.model = model
        self.dims = dims
        self.embedder = embedder
        self.budget = budget or Budget()
        self.limiter = limiter
        self.batch = batch
        self.stop = stop
        self.retry = retry or RetryPolicy()
        self.on_retry = on_retry
        self.usage = usage
        self.max_requests = max_requests

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
        kept when a later one stops on the budget, the daily quota or a stop
        request, which raises Interrupted; or fails even after its retries,
        which raises that failure. No later batch is then tried: it would most
        likely fail alike, after the same waits. BudgetExceeded for `max_requests`
        means a batch had none left to start with; one that runs out of them
        while failing raises its failure, which is what went wrong.
        """
        if self.embedder is None:
            raise ConfigError("no embedding model configured")
        batches = self._batches()
        total = sum(len(group) for group in batches)
        limit = self.budget.requests + self.max_requests
        done = 0
        spent = 0.0
        for group in batches:
            if self.stop is not None and self.stop.is_set():
                raise Interrupted("stopped before every chunk was embedded")
            cost = self._price(group)
            vectors = self._send(self.embedder, group, cost, limit)
            self.budget.settle(cost, cost)
            self.store.store(
                [(c.id, v) for c, v in zip(group, vectors, strict=True)], self.model.id
            )
            if self.usage is not None:
                self.usage.record(self.model.id, cost=cost)
            done += len(group)
            spent += cost
            if progress is not None:
                progress(done, total)
        return done, spent

    def _send(
        self, embedder: Embedder, group: Sequence[Chunk], cost: float, limit: int
    ) -> list[list[float]]:
        """One batch's vectors, as `LLMClient._call` gets a completion: every
        attempt is a request, reserved, paced and counted, and one that fails
        keeps its request count but not its money. `limit` is what `max_requests`
        comes to in the budget's count, and the batch gets only the attempts
        left of it: no wait goes before a retry that would be refused."""
        policy = self.retry
        if self.max_requests:
            left = limit - self.budget.requests
            if left < 1:
                raise BudgetExceeded(
                    f"embedding reached its request limit ({self.max_requests})",
                    spent=self.budget.spent, limit=self.budget.max_cost,
                )
            policy = replace(policy, attempts=min(policy.attempts, left))
        texts = [c.text for c in group]

        def attempt() -> list[list[float]]:
            self.budget.reserve(cost)
            if self.limiter is not None:
                try:
                    self.limiter.acquire()
                except BaseException:
                    self.budget.release(cost)
                    raise
            try:
                return embedder.embed(texts)
            except BaseException:
                self.budget.fail(cost)    # it reached the provider: still a request
                raise

        return policy.run(attempt, on_retry=self.on_retry, stop=self.stop)

    def vectors(self) -> tuple[list[str], list[array[float]]]:
        """Every stored vector of this model and size for these chunks, with
        its chunk id."""
        wanted = {chunk.id for chunk in self.chunks}
        return self.store.vectors(self.model.id, self.dims, wanted)

    def _batches(self) -> list[list[Chunk]]:
        pending = self.store.missing(self.chunks, self.model.id, self.dims)
        return [pending[i : i + self.batch] for i in range(0, len(pending), self.batch)]

    @staticmethod
    def _tokens(chunks: Sequence[Chunk]) -> int:
        return token_ceiling(*(c.text for c in chunks))

    def _price(self, chunks: Sequence[Chunk]) -> float:
        return self.model.estimate(self._tokens(chunks), 0)
