"""Chunk embeddings: quoted before they are bought, bought once, on budget."""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any
from unittest import mock

from devdocs.errors import (
    AuthError,
    BudgetExceeded,
    ConfigError,
    ModelNotFound,
    ProviderError,
    QuotaExhausted,
    RateLimited,
)
from devdocs.index.chunk import Chunk, ChunkStore, chunk_file
from devdocs.index.db import Database
from devdocs.index.embed import Embeddings
from devdocs.llm import http as http_module
from devdocs.llm.budget import Budget, RateLimiter, token_ceiling
from devdocs.llm.catalog import ModelInfo, find
from devdocs.llm.http import RetryPolicy
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


class Flaky(Counting):
    """Fails with each of `failures`, an attempt each, then answers; or, from
    batch `down_from` on, never again."""

    def __init__(self, *failures: BaseException, down_from: int | None = None) -> None:
        super().__init__()
        self.failures = list(failures)
        self.down_from = down_from
        self.attempts = 0

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        self.attempts += 1
        if self.down_from is not None and len(self.batches) >= self.down_from:
            raise ProviderError("down", status=503)
        if self.failures:
            raise self.failures.pop(0)
        return super().embed(texts, query=query)


class Pacing:
    """A limiter that counts the slots it gives."""

    def __init__(self) -> None:
        self.slots = 0

    def acquire(self) -> None:
        self.slots += 1


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
        self.chunks: list[Chunk] = []
        for rel, text in (("shop/money.py", MONEY), ("shop/cart.py", CART)):
            group = chunk_file(rel, text, facts(rel, text))
            self.store.replace(rel, group)
            self.chunks += group
        self.texts = [chunk.text for chunk in self.chunks]

    def embeddings(
        self,
        embedder: Counting | None = None,
        *,
        budget: Budget | None = None,
        limiter: Any = None,
        batch: int = 100,
        stop: threading.Event | None = None,
        **others: Any,
    ) -> Embeddings:
        embedder = embedder or Counting()
        others.setdefault("retry", RetryPolicy(base_delay=0))
        return Embeddings(self.store, self.chunks, model=MODEL,
                          dims=embedder.dimensions, embedder=embedder, budget=budget,
                          limiter=limiter, batch=batch, stop=stop, **others)


class TestQuote(EmbedCase):
    def test_quote_is_the_ceiling_of_what_is_missing(self) -> None:
        quote = self.embeddings(batch=2).quote()
        texts = self.texts
        tokens = sum(token_ceiling(*texts[i : i + 2]) for i in range(0, len(texts), 2))
        self.assertEqual((quote.chunks, quote.requests, quote.tokens),
                         (len(texts), 3, tokens))
        self.assertAlmostEqual(quote.cost, tokens * 0.15 / 1e6)

    def test_quoting_needs_no_embedder(self) -> None:
        quote = Embeddings(self.store, self.chunks, model=MODEL, dims=4).quote()
        self.assertEqual(quote.chunks, len(self.texts))
        with self.assertRaises(ConfigError):
            Embeddings(self.store, self.chunks, model=MODEL, dims=4).update()


class TestScope(EmbedCase):
    """It works on the chunks it is given, whatever else the store holds."""

    def test_quoting_and_reading_write_nothing(self) -> None:
        def rows() -> list[tuple[Any, ...]]:
            return [tuple(row) for row in self.db.query("SELECT * FROM chunk")]

        self.embeddings().update()
        before = rows()
        fresh = Chunk("new", "shop/new.py", "", 1, 1, "shop/new.py\nx = 1")
        embeddings = Embeddings(self.store, [fresh], model=MODEL, dims=4)
        self.assertEqual(embeddings.quote().chunks, 1)
        self.assertEqual(embeddings.vectors(), ([], []))
        self.assertEqual(rows(), before)

    def test_only_its_own_chunks_are_quoted_and_bought(self) -> None:
        money = [chunk for chunk in self.chunks if chunk.source == "shop/money.py"]
        embedder = Counting()
        embeddings = Embeddings(self.store, money, model=MODEL, dims=4,
                                embedder=embedder)
        self.assertEqual(embeddings.quote().chunks, len(money))
        embeddings.update()
        self.assertEqual(embedder.batches, [[chunk.text for chunk in money]])

    def test_only_its_own_vectors_are_read(self) -> None:
        self.embeddings().update()
        money = [chunk for chunk in self.chunks if chunk.source == "shop/money.py"]
        embeddings = Embeddings(self.store, money, model=MODEL, dims=4)
        self.assertEqual(embeddings.vectors()[0], [chunk.id for chunk in money])
        self.assertEqual(embeddings.quote().chunks, 0)

    def test_a_chunk_given_twice_is_bought_once(self) -> None:
        embedder = Counting()
        twice = [*self.chunks, self.chunks[0]]
        embeddings = Embeddings(self.store, twice, model=MODEL, dims=4,
                                embedder=embedder)
        self.assertEqual(embeddings.quote().chunks, len(self.chunks))
        embeddings.update()
        self.assertEqual(len(embedder.batches[0]), len(self.chunks))


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
        failing = Counting(error=ProviderError("bad request", status=400))
        with self.assertRaises(ProviderError):
            self.embeddings(failing, budget=budget).update()
        self.assertEqual((budget.requests, budget.spent), (1, 0.0))
        self.assertEqual(self.embeddings().quote().chunks, len(self.texts))


class TestRetry(EmbedCase):
    """A batch is retried as a completion is: by the same policy and rules."""

    def test_what_a_second_try_can_cure_is_retried(self) -> None:
        for failure in (RateLimited("slow", retry_after=0, status=429),
                        ProviderError("bad gateway", status=502),
                        ProviderError("down", status=503),
                        ProviderError("network error talking to the host")):
            with self.subTest(failure.message):
                embedder = Flaky(failure)
                self.assertEqual(self.embeddings(embedder).update()[0], len(self.texts))
                self.assertEqual(embedder.attempts, 2)
                with self.db.transaction() as conn:
                    conn.execute("UPDATE chunk SET embedding = NULL")

    def test_what_a_second_try_cannot_cure_is_sent_once(self) -> None:
        for failure in (ModelNotFound("no such model"),
                        ProviderError("bad request", status=400),
                        AuthError("bad key"),
                        QuotaExhausted("daily quota spent")):
            with self.subTest(failure.message):
                embedder = Flaky(failure)
                with self.assertRaises(type(failure)) as caught:
                    self.embeddings(embedder).update()
                self.assertIs(caught.exception, failure)
                self.assertEqual(embedder.attempts, 1)

    def test_a_failure_that_lasts_comes_back_after_the_policys_attempts(self) -> None:
        embedder = Flaky(down_from=0)
        policy = RetryPolicy(attempts=3, base_delay=0)
        with self.assertRaises(ProviderError):
            self.embeddings(embedder, retry=policy).update()
        self.assertEqual(embedder.attempts, 3)

    def test_every_attempt_is_a_request_and_only_the_answer_costs(self) -> None:
        budget = Budget(max_cost=1.0)
        down = ProviderError("down", status=503)
        embeddings = self.embeddings(Flaky(down, down), budget=budget)
        quote = embeddings.quote()
        embeddings.update()
        self.assertEqual(budget.requests, 3)
        self.assertLessEqual(abs(budget.spent - quote.cost), 1e-6)
        self.assertAlmostEqual(budget.remaining or 0.0, 1.0 - budget.spent)

    def test_every_attempt_takes_a_slot(self) -> None:
        pacing = Pacing()
        down = ProviderError("down", status=503)
        self.embeddings(Flaky(down, down), limiter=pacing).update()
        self.assertEqual(pacing.slots, 3)

    def test_a_retry_the_budget_refuses_ends_it(self) -> None:
        budget = Budget(max_requests=2)
        embedder = Flaky(down_from=0)
        with self.assertRaises(BudgetExceeded):
            self.embeddings(embedder, budget=budget).update()
        self.assertEqual((embedder.attempts, budget.requests), (2, 2))

    def test_retries_are_reported(self) -> None:
        reported: list[tuple[int, str]] = []
        down = ProviderError("down", status=503)
        self.embeddings(
            Flaky(down), on_retry=lambda n, err, wait: reported.append((n, str(err)))
        ).update()
        self.assertEqual(reported, [(1, "down")])

    def test_no_batch_is_tried_once_one_has_failed(self) -> None:
        embedder = Flaky(down_from=1)
        embeddings = self.embeddings(embedder, batch=1)
        with self.assertRaises(ProviderError):
            embeddings.update()
        self.assertEqual(len(embedder.batches), 1)
        self.assertEqual(embedder.attempts, 1 + RetryPolicy().attempts)
        self.assertEqual(len(embeddings.vectors()[0]), 1)   # bought, so kept
        self.assertEqual(embeddings.quote().chunks, len(self.texts) - 1)

    def test_a_request_limit_counts_the_retries(self) -> None:
        # One failure per batch: the limit is reached in half as many batches.
        down = ProviderError("down", status=503)

        class EveryOther(Flaky):
            def embed(self, texts: list[str], *, query: bool = False,
                      ) -> list[list[float]]:
                if self.attempts % 2 == 0:
                    self.failures.append(down)
                return super().embed(texts, query=query)

        embedder = EveryOther()
        budget = Budget(max_requests=1000)
        embeddings = self.embeddings(embedder, budget=budget, batch=1, max_requests=4)
        self.assertGreater(len(self.texts), 2)
        with self.assertRaises(BudgetExceeded) as caught:
            embeddings.update()
        self.assertEqual(caught.exception.message,
                         "embedding reached its request limit (4)")
        self.assertEqual((embedder.attempts, budget.requests), (4, 4))
        self.assertEqual(len(embeddings.vectors()[0]), 2)

    def test_a_failure_that_outlasts_the_limit_comes_back_as_itself(self) -> None:
        # Regression: the limit stopped the retry and its error replaced the
        # failure, so a run warned of nothing and named only the limit.
        for limit in (1, 2, RetryPolicy().attempts - 1):
            with self.subTest(limit=limit):
                embedder = Flaky(down_from=0)
                budget = Budget()
                with self.assertRaises(ProviderError) as caught:
                    self.embeddings(embedder, budget=budget,
                                    max_requests=limit).update()
                self.assertEqual(caught.exception.message, "down")
                self.assertEqual((embedder.attempts, budget.requests), (limit, limit))

    def test_a_limit_above_the_policys_attempts_does_not_add_any(self) -> None:
        embedder = Flaky(down_from=0)
        with self.assertRaises(ProviderError):
            self.embeddings(embedder, max_requests=100).update()
        self.assertEqual(embedder.attempts, RetryPolicy().attempts)

    def test_no_wait_comes_before_a_retry_the_limit_would_refuse(self) -> None:
        # Regression: a Retry-After of up to 120 s was waited out first.
        reported: list[int] = []
        busy = Counting(error=ProviderError("busy", status=503, retry_after=30))
        started = time.monotonic()
        with self.assertRaises(ProviderError):
            self.embeddings(busy, max_requests=1,
                            on_retry=lambda n, err, wait: reported.append(n)).update()
        self.assertEqual(reported, [])
        self.assertLess(time.monotonic() - started, 5)

    def test_a_limit_counts_only_what_this_update_sends(self) -> None:
        budget = Budget()
        for _ in range(3):
            budget.reserve(0.0)
            budget.settle(0.0, 0.0)
        embedder = Flaky(down_from=0)
        with self.assertRaises(ProviderError):
            self.embeddings(embedder, budget=budget, max_requests=2).update()
        self.assertEqual((embedder.attempts, budget.requests), (2, 5))

    def test_the_budgets_own_request_limit_is_still_a_budget_stop(self) -> None:
        budget = Budget(max_requests=1)
        embedder = Flaky(down_from=0)
        with self.assertRaises(BudgetExceeded):
            self.embeddings(embedder, budget=budget, max_requests=5).update()
        self.assertEqual(embedder.attempts, 1)

    def test_no_request_limit_by_default(self) -> None:
        down = ProviderError("down", status=503)
        self.assertEqual(self.embeddings(Flaky(down, down, down)).update()[0],
                         len(self.texts))


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

    def test_a_stop_ends_the_wait_to_retry(self) -> None:
        self.enterContext(mock.patch.object(http_module.random, "uniform",
                                            lambda low, high: high))
        stop = Watched()
        embedder = Flaky(ProviderError("down", status=503))
        policy = RetryPolicy(base_delay=60, max_delay=60)
        embeddings = self.embeddings(embedder, stop=stop, retry=policy)
        raised, elapsed = stopped_while_waiting(embeddings.update, stop)
        self.assertIsInstance(raised, Interrupted)
        self.assertLess(elapsed, 5)   # not the 60 s backoff
        self.assertEqual(embedder.attempts, 1)

    def test_a_stop_while_a_batch_fails_is_no_failure_and_sends_no_retry(self) -> None:
        stop = threading.Event()

        class Stopped(Flaky):
            def embed(self, texts: list[str], *, query: bool = False,
                      ) -> list[list[float]]:
                stop.set()
                return super().embed(texts, query=query)

        embedder = Stopped(ProviderError("down", status=503))
        budget = Budget()
        with self.assertRaises(Interrupted):
            self.embeddings(embedder, budget=budget, stop=stop).update()
        self.assertEqual((embedder.attempts, budget.requests), (1, 1))


if __name__ == "__main__":
    unittest.main()
