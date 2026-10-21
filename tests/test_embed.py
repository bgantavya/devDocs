"""Chunk embeddings: quoted before they are bought, bought once, on budget."""

from __future__ import annotations

import threading
import unittest
from typing import Any

from devdocs.errors import BudgetExceeded, ConfigError, ProviderError, QuotaExhausted
from devdocs.index.chunk import ChunkStore, chunk_file
from devdocs.index.db import Database
from devdocs.index.embed import Embeddings
from devdocs.llm.budget import Budget, RateLimiter, token_ceiling
from devdocs.llm.catalog import ModelInfo, find
from devdocs.llm.stop import Interrupted
from tests.test_http import Watched, stopped_while_waiting
from tests.test_retrieval import CART, MONEY, facts

_FOUND = find("gemini-embedding-001")
assert _FOUND is not None
MODEL: ModelInfo = _FOUND


class Counting:
    """An embedder that records each batch and returns unit vectors."""

    provider = "gemini"

    def __init__(self, dimensions: int = 4, error: BaseException | None = None) -> None:
        self.dimensions = dimensions
        self.error = error
        self.batches: list[list[str]] = []

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        if self.error is not None:
            raise self.error
        self.batches.append(list(texts))
        return [[1.0] + [0.0] * (self.dimensions - 1) for _ in texts]


class StopsMidBatch(Counting):
    """Sets `stop` while its first batch is out, as a Ctrl-C would."""

    def __init__(self, stop: threading.Event) -> None:
        super().__init__()
        self.stop = stop

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        self.stop.set()
        return super().embed(texts, query=query)


class Refusing:
    def acquire(self) -> None:
        raise QuotaExhausted("daily quota spent", status=429, provider="gemini")


class EmbedCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.store = ChunkStore(self.db)
        for rel, text in (("shop/money.py", MONEY), ("shop/cart.py", CART)):
            self.store.replace(rel, chunk_file(rel, text, facts(rel, text)))
        self.texts = [chunk.text for chunk in self.store.chunks()]

    def embeddings(
        self,
        embedder: Counting | None = None,
        *,
        budget: Budget | None = None,
        limiter: Any = None,
        batch: int = 100,
        stop: threading.Event | None = None,
    ) -> Embeddings:
        embedder = embedder or Counting()
        return Embeddings(self.store, model=MODEL, dims=embedder.dimensions,
                          embedder=embedder, budget=budget, limiter=limiter,
                          batch=batch, stop=stop)


class TestQuote(EmbedCase):
    def test_quote_is_the_ceiling_of_what_is_missing(self) -> None:
        quote = self.embeddings(batch=2).quote()
        texts = self.texts
        tokens = sum(token_ceiling(*texts[i : i + 2]) for i in range(0, len(texts), 2))
        self.assertEqual((quote.chunks, quote.requests, quote.tokens),
                         (len(texts), 3, tokens))
        self.assertAlmostEqual(quote.cost, tokens * 0.15 / 1e6)

    def test_quoting_needs_no_embedder(self) -> None:
        quote = Embeddings(self.store, model=MODEL, dims=4).quote()
        self.assertEqual(quote.chunks, len(self.texts))
        with self.assertRaises(ConfigError):
            Embeddings(self.store, model=MODEL, dims=4).update()


class TestUpdate(EmbedCase):
    def test_each_chunk_is_bought_once(self) -> None:
        embedder = Counting()
        embeddings = self.embeddings(embedder)
        done, spent = embeddings.update()
        self.assertEqual(done, len(self.texts))
        self.assertGreater(spent, 0)
        self.assertEqual(embeddings.update(), (0, 0.0))
        self.assertEqual(len(embedder.batches), 1)
        ids, vectors = embeddings.vectors()
        self.assertEqual(len(ids), len(self.texts))
        self.assertEqual(list(vectors[0]), [1.0, 0.0, 0.0, 0.0])
        self.assertEqual(embeddings.quote().chunks, 0)

    def test_a_new_size_is_a_new_index(self) -> None:
        self.embeddings(Counting(4)).update()
        wider = Counting(8)
        done, _ = self.embeddings(wider).update()
        self.assertEqual(done, len(self.texts))

    def test_spending_matches_the_quote_and_counts_requests(self) -> None:
        budget = Budget(max_cost=1.0)
        embeddings = self.embeddings(budget=budget, batch=2)
        quote = embeddings.quote()
        embeddings.update()
        # Each batch is booked in whole micro-dollars.
        self.assertLessEqual(abs(budget.spent - quote.cost), quote.requests * 1e-6)
        self.assertEqual(budget.requests, quote.requests)

    def test_a_budget_stop_keeps_the_batches_already_bought(self) -> None:
        first = token_ceiling(self.texts[0]) * 0.15 / 1e6
        budget = Budget(max_cost=first * 1.5)
        embeddings = self.embeddings(budget=budget, batch=1)
        with self.assertRaises(BudgetExceeded):
            embeddings.update()
        self.assertEqual(embeddings.quote().chunks, len(self.texts) - 1)

    def test_a_refused_slot_is_not_a_request(self) -> None:
        budget = Budget()
        embeddings = self.embeddings(budget=budget, limiter=Refusing())
        with self.assertRaises(QuotaExhausted):
            embeddings.update()
        self.assertEqual((budget.requests, budget.spent), (0, 0.0))

    def test_a_failed_request_still_counts(self) -> None:
        budget = Budget()
        failing = Counting(error=ProviderError("bad gateway", status=502))
        with self.assertRaises(ProviderError):
            self.embeddings(failing, budget=budget).update()
        self.assertEqual((budget.requests, budget.spent), (1, 0.0))
        self.assertEqual(self.embeddings().quote().chunks, len(self.texts))


class TestStop(EmbedCase):
    def test_the_batch_on_the_wire_is_kept_and_no_other_is_sent(self) -> None:
        stop = threading.Event()
        embedder = StopsMidBatch(stop)
        budget = Budget()
        embeddings = self.embeddings(embedder, budget=budget, batch=1, stop=stop)
        with self.assertRaises(Interrupted):
            embeddings.update()
        self.assertEqual(len(embedder.batches), 1)
        self.assertEqual(budget.requests, 1)
        self.assertEqual(embeddings.quote().chunks, len(self.texts) - 1)

    def test_a_stop_ends_a_wait_for_a_request_slot(self) -> None:
        stop = Watched()
        limiter = RateLimiter(1, stop=stop)
        limiter.acquire()   # this minute's one slot is used
        embedder = Counting()
        budget = Budget()
        embeddings = self.embeddings(embedder, budget=budget, limiter=limiter,
                                     stop=stop)
        raised, elapsed = stopped_while_waiting(embeddings.update, stop)
        self.assertIsInstance(raised, Interrupted)
        self.assertLess(elapsed, 5)   # not the minute to the next slot
        self.assertEqual((embedder.batches, budget.requests), ([], 0))


if __name__ == "__main__":
    unittest.main()
