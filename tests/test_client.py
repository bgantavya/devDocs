"""The client's composition of cache, budget, pacing, retry and escalation,
plus the cache and ledger repositories it relies on."""

from __future__ import annotations

import contextlib
import unittest
from datetime import UTC, datetime
from typing import Any
from unittest import mock

from devdocs.errors import (
    AuthError,
    BudgetExceeded,
    ConfigError,
    ModelNotFound,
    ProviderError,
    QuotaExhausted,
)
from devdocs.index import Database
from devdocs.index.cache import ResponseCache
from devdocs.index.ledger import DailyUsage, RunLedger, quota_day
from devdocs.llm import Completion, Finish, Request, Usage
from devdocs.llm.budget import Budget, Router, token_ceiling
from devdocs.llm.catalog import Tier, find
from devdocs.llm.client import CallEvent, LLMClient
from devdocs.llm.http import RetryPolicy
from devdocs.llm.stop import Interrupted
from tests.test_http import Watched, stopped_while_waiting

LITE = "gemini-2.5-flash-lite"
ALL_GEMINI = [LITE, "gemini-2.5-flash", "gemini-2.5-pro"]
# A key that sees only the previous Claude generation, as the Claude fake lists.
PREVIOUS_CLAUDE = {"claude-haiku-4-5-20251001", "claude-sonnet-5", "claude-opus-5"}


def ok(model: str, text: str = "doc") -> Completion:
    return Completion(text, model, "gemini", Finish.STOP,
                      Usage(input_tokens=1000, output_tokens=200))


def nothing(model: str, finish: Finish = Finish.OTHER) -> Completion:
    return Completion(None, model, "gemini", finish, Usage(input_tokens=1000))


class Scripted:
    """A ChatModel whose answer per model is scripted: a Completion, an
    exception, or a callable producing either."""

    provider = "gemini"

    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.calls: list[str] = []

    def list_models(self) -> list[str]:
        return list(ALL_GEMINI)

    def generate(self, request: Request) -> Completion:
        self.calls.append(request.model)
        outcome = self.script[request.model]
        if callable(outcome) and not isinstance(outcome, Completion):
            outcome = outcome()
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, Completion)
        return outcome


class ClientCase(unittest.TestCase):
    def make(self, script: dict[str, Any], **kwargs: Any) -> tuple[LLMClient, Scripted]:
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.events: list[CallEvent] = []
        model = Scripted(script)
        client = LLMClient(
            model,
            router=Router("gemini", available=set(ALL_GEMINI)),
            cache=ResponseCache(self.db),
            usage=DailyUsage(self.db),
            retry=RetryPolicy(base_delay=0),
            on_event=self.events.append,
            **kwargs,
        )
        return client, model

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


class TestHappyPath(ClientCase):
    def test_cheapest_model_answers_and_is_priced(self) -> None:
        client, model = self.make({LITE: ok(LITE)})
        result = client.complete(Tier.FILE, "SYS", "file A")
        self.assertEqual(model.calls, ["gemini-2.5-flash-lite"])
        self.assertAlmostEqual(result.cost_usd, 0.00018)
        self.assertAlmostEqual(client.spent, 0.00018)

    def test_repeat_request_is_free(self) -> None:
        client, model = self.make({LITE: ok(LITE)})
        client.complete(Tier.FILE, "SYS", "file A")
        again = client.complete(Tier.FILE, "SYS", "file A")
        self.assertTrue(again.cached)
        self.assertEqual(again.cost_usd, 0.0)
        self.assertEqual(len(model.calls), 1)

    def test_changed_prompt_is_not_a_cache_hit(self) -> None:
        client, model = self.make({LITE: ok(LITE)})
        client.complete(Tier.FILE, "SYS", "file A v1")
        client.complete(Tier.FILE, "SYS", "file A v2")
        self.assertEqual(len(model.calls), 2)

    def test_daily_usage_is_recorded(self) -> None:
        client, _ = self.make({LITE: ok(LITE)})
        client.complete(Tier.FILE, "SYS", "a")
        self.assertEqual(DailyUsage(self.db).requests("gemini-2.5-flash-lite"), 1)


class TestIdentity(ClientCase):
    def test_an_answer_is_cached_under_its_identity(self) -> None:
        client, model = self.make({LITE: ok(LITE)})
        client.complete(Tier.FILE, "SYS", "file A + context 1", identity="file A")
        again = client.complete(Tier.FILE, "SYS", "file A + context 2",
                                identity="file A")
        self.assertTrue(again.cached)
        self.assertEqual(len(model.calls), 1)

    def test_quote_and_cached_read_the_same_identity(self) -> None:
        client, _ = self.make({LITE: ok(LITE)})
        client.complete(Tier.FILE, "SYS", "file A + context 1", identity="file A")
        quote = client.quote(Tier.FILE, "SYS", "file A + context 2", identity="file A")
        self.assertTrue(quote.cached)
        hit = client.cached(Tier.FILE, "SYS", "file A + context 2", identity="file A")
        self.assertIsNotNone(hit)
        self.assertIsNone(client.cached(Tier.FILE, "SYS", "file A + context 2"))

    def test_the_prompt_sent_is_the_full_one(self) -> None:
        sent: list[str] = []

        class Recording(Scripted):
            def generate(self, request: Request) -> Completion:
                sent.append(request.prompt)
                return super().generate(request)

        client, _ = self.make({LITE: ok(LITE)})
        client.model = Recording({LITE: ok(LITE)})
        client.complete(Tier.FILE, "SYS", "file A + context", identity="file A")
        self.assertEqual(sent, ["file A + context"])


class TestEscalation(ClientCase):
    def test_empty_answer_climbs_one_rung(self) -> None:
        client, _ = self.make({
            "gemini-2.5-flash-lite": nothing("gemini-2.5-flash-lite"),
            "gemini-2.5-flash": ok("gemini-2.5-flash"),
        })
        result = client.complete(Tier.FILE, "SYS", "x")
        self.assertEqual(result.model, "gemini-2.5-flash")
        self.assertEqual(self.kinds(), ["call", "escalate", "call"])

    def test_rerun_after_escalation_hits_the_stronger_models_cache(self) -> None:
        client, model = self.make({
            "gemini-2.5-flash-lite": nothing("gemini-2.5-flash-lite"),
            "gemini-2.5-flash": ok("gemini-2.5-flash"),
        })
        client.complete(Tier.FILE, "SYS", "x")
        again = client.complete(Tier.FILE, "SYS", "x")
        self.assertTrue(again.cached)
        self.assertEqual(model.calls, ["gemini-2.5-flash-lite", "gemini-2.5-flash"])

    def test_safety_block_does_not_escalate(self) -> None:
        client, model = self.make({
            "gemini-2.5-flash-lite": nothing("gemini-2.5-flash-lite", Finish.BLOCKED),
            "gemini-2.5-flash": ok("gemini-2.5-flash"),
        })
        result = client.complete(Tier.FILE, "SYS", "x")
        self.assertIs(result.finish, Finish.BLOCKED)
        self.assertEqual(model.calls, ["gemini-2.5-flash-lite"])

    def test_empty_everywhere_returns_the_last_attempt(self) -> None:
        client, _ = self.make({m: nothing(m) for m in ALL_GEMINI})
        result = client.complete(Tier.FILE, "SYS", "x")
        self.assertIsNone(result.text)
        self.assertEqual(result.model, "gemini-2.5-pro")

    def test_missing_model_is_retired_for_the_run(self) -> None:
        client, model = self.make({
            "gemini-2.5-flash-lite": ModelNotFound("gone"),
            "gemini-2.5-flash": ok("gemini-2.5-flash"),
        })
        client.complete(Tier.FILE, "SYS", "a")
        client.complete(Tier.FILE, "SYS", "b")
        self.assertEqual(model.calls, ["gemini-2.5-flash-lite", "gemini-2.5-flash",
                                       "gemini-2.5-flash"])

    def test_spent_daily_quota_moves_to_the_next_model(self) -> None:
        client, _ = self.make({
            "gemini-2.5-flash-lite": QuotaExhausted("day"),
            "gemini-2.5-flash": ok("gemini-2.5-flash"),
        })
        result = client.complete(Tier.FILE, "SYS", "a")
        self.assertEqual(result.model, "gemini-2.5-flash")

    def test_quota_everywhere_raises(self) -> None:
        client, _ = self.make({m: QuotaExhausted("day") for m in ALL_GEMINI})
        with self.assertRaises(QuotaExhausted):
            client.complete(Tier.FILE, "SYS", "a")

    def test_billing_failure_does_not_try_other_models(self) -> None:
        client, model = self.make({
            "gemini-2.5-flash-lite": QuotaExhausted("billing", status=402),
            "gemini-2.5-flash": ok("gemini-2.5-flash"),
        })
        with self.assertRaises(QuotaExhausted):
            client.complete(Tier.FILE, "SYS", "a")
        self.assertEqual(model.calls, ["gemini-2.5-flash-lite"])


class TestFailures(ClientCase):
    def test_transient_error_is_retried(self) -> None:
        attempts = {"n": 0}

        def flaky() -> Completion | BaseException:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return ProviderError("down", status=503)
            return ok("gemini-2.5-flash-lite")

        client, _ = self.make({"gemini-2.5-flash-lite": flaky})
        self.assertEqual(client.complete(Tier.FILE, "SYS", "a").text, "doc")
        self.assertEqual(self.kinds().count("retry"), 2)

    def test_bad_key_fails_once_and_fast(self) -> None:
        client, model = self.make({m: AuthError("bad key") for m in ALL_GEMINI})
        with self.assertRaises(AuthError):
            client.complete(Tier.FILE, "SYS", "a")
        self.assertEqual(len(model.calls), 1)   # v3 made 8 per file

    def test_budget_refuses_before_any_call(self) -> None:
        client, model = self.make(
            {"gemini-2.5-pro": ok("gemini-2.5-pro")}, budget=Budget(max_cost=0.001)
        )
        with self.assertRaises(BudgetExceeded):
            client.complete(Tier.ARCHITECTURE, "SYS", "x" * 20_000,
                            max_output_tokens=4000)
        self.assertEqual(model.calls, [])

    def test_failed_call_releases_its_reservation(self) -> None:
        budget = Budget(max_cost=1.0)
        client, _ = self.make(
            {m: ProviderError("bad", status=400) for m in ALL_GEMINI}, budget=budget
        )
        with self.assertRaises(ProviderError):
            client.complete(Tier.FILE, "SYS", "a")
        self.assertEqual(budget.requests, 1)      # it was sent
        self.assertEqual(budget.remaining, 1.0)   # but costs nothing

    def test_retries_count_against_the_request_ceiling(self) -> None:
        # Regression: one reservation covered every retry, so a flaky
        # provider could take four requests while max_requests saw one.
        budget = Budget(max_requests=2)
        client, model = self.make(
            {LITE: ProviderError("down", status=503)}, budget=budget
        )
        with self.assertRaises(BudgetExceeded):
            client.complete(Tier.FILE, "SYS", "a")
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(budget.requests, 2)
        self.assertEqual(budget.spent, 0.0)

    def test_no_available_model_is_a_config_error(self) -> None:
        client, _ = self.make({})
        client.router.available = set()
        with self.assertRaises(ConfigError):
            client.complete(Tier.FILE, "SYS", "a")

    def test_discover_restricts_routing(self) -> None:
        client, _ = self.make({})
        self.assertEqual(client.discover(), set(ALL_GEMINI))
        self.assertEqual(client.router.available, set(ALL_GEMINI))

    def test_an_observer_that_fails_never_costs_the_answer(self) -> None:
        # Regression: the call was reported before its answer was cached, so
        # a progress line to a closed pipe threw a paid answer away.
        client, model = self.make({LITE: ok(LITE)})

        def gone(event: CallEvent) -> None:
            raise BrokenPipeError(32, "Broken pipe")

        client.on_event = gone
        with self.assertRaises(BrokenPipeError):
            client.complete(Tier.FILE, "SYS", "a")
        client.on_event = None
        self.assertTrue(client.complete(Tier.FILE, "SYS", "a").cached)
        self.assertEqual(model.calls, [LITE])


class TestStop(ClientCase):
    """A stop request: nothing more is sent, retried or escalated, and the
    cache still answers."""

    def test_the_cache_answers_and_nothing_is_sent(self) -> None:
        client, model = self.make({LITE: ok(LITE)})
        client.complete(Tier.FILE, "SYS", "a")
        client.stop.set()
        self.assertTrue(client.complete(Tier.FILE, "SYS", "a").cached)
        with self.assertRaises(Interrupted):
            client.complete(Tier.FILE, "SYS", "b")
        self.assertEqual(model.calls, [LITE])
        self.assertEqual(client.budget.requests, 1)

    def test_an_answer_on_the_wire_is_kept(self) -> None:
        def answer() -> Completion:
            client.stop.set()   # the stop comes while the request is out
            return ok(LITE)

        client, model = self.make({LITE: answer})
        self.assertEqual(client.complete(Tier.FILE, "SYS", "a").text, "doc")
        self.assertAlmostEqual(client.spent, 0.00018)
        self.assertTrue(client.complete(Tier.FILE, "SYS", "a").cached)
        self.assertEqual(model.calls, [LITE])

    def test_no_escalation_after_a_stop(self) -> None:
        def empty() -> Completion:
            client.stop.set()
            return nothing(LITE)

        client, model = self.make({LITE: empty, "gemini-2.5-flash": ok("x")})
        with self.assertRaises(Interrupted):
            client.complete(Tier.FILE, "SYS", "a")
        self.assertEqual(model.calls, [LITE])
        self.assertEqual(self.kinds(), ["call"])

    def test_no_retry_after_a_stop(self) -> None:
        def down() -> BaseException:
            client.stop.set()
            return ProviderError("down", status=503)

        client, model = self.make({LITE: down})
        with self.assertRaises(Interrupted):
            client.complete(Tier.FILE, "SYS", "a")
        self.assertEqual((model.calls, self.kinds()), ([LITE], []))
        self.assertEqual((client.budget.requests, client.spent), (1, 0.0))

    def test_a_stop_ends_a_wait_for_a_request_slot(self) -> None:
        stop = Watched()
        client, model = self.make({LITE: ok(LITE)}, stop=stop)
        info = find(LITE)
        assert info is not None
        limiter = client.limiter(info)
        for _ in range(limiter.rpm or 0):   # this minute's free slots are used
            limiter.acquire()
        raised, elapsed = stopped_while_waiting(
            lambda: client.complete(Tier.FILE, "SYS", "a"), stop)
        self.assertIsInstance(raised, Interrupted)
        self.assertLess(elapsed, 5)   # not the minute to the next slot
        self.assertEqual(model.calls, [])
        self.assertEqual(client.budget.requests, 0)   # never sent: not a request


class TestQuote(ClientCase):
    def test_quote_prices_the_first_rung_with_the_full_cap(self) -> None:
        client, model = self.make({LITE: ok(LITE)})
        quote = client.quote(Tier.FILE, "SYS", "x" * 3500, max_output_tokens=500)
        # (3 + 3500) bytes at a token each, plus 64 of message framing.
        self.assertEqual((quote.model, quote.input_tokens), (LITE, 3567))
        self.assertEqual(quote.output_tokens, 500)
        self.assertFalse(quote.cached)
        self.assertAlmostEqual(quote.cost, (3567 * 0.10 + 500 * 0.40) / 1e6)
        self.assertEqual(model.calls, [])   # quoting never sends

    def test_quote_is_what_the_call_reserves(self) -> None:
        budget = Budget(max_cost=1.0)
        client, _ = self.make({LITE: ok(LITE)}, budget=budget)
        prompt = "数据处理模块" * 500   # 3 bytes a character
        quote = client.quote(Tier.FILE, "SYS", prompt, max_output_tokens=500)
        self.assertEqual(quote.input_tokens, token_ceiling("SYS", prompt))
        budget.reserve(1.0 - quote.cost)
        client.complete(Tier.FILE, "SYS", prompt, max_output_tokens=500)   # still fits

    def test_answered_request_quotes_as_free(self) -> None:
        client, _ = self.make({LITE: ok(LITE)})
        client.complete(Tier.FILE, "SYS", "file A", max_output_tokens=300)
        quote = client.quote(Tier.FILE, "SYS", "file A", max_output_tokens=300)
        self.assertTrue(quote.cached)
        self.assertEqual(quote.cost, 0.0)

    def test_quote_needs_a_model(self) -> None:
        client, _ = self.make({})
        client.router.available = set()
        with self.assertRaises(ConfigError):
            client.quote(Tier.FILE, "SYS", "a")


class TestAccept(ClientCase):
    def test_rejected_text_escalates_and_is_not_cached(self) -> None:
        flash = "gemini-2.5-flash"
        client, model = self.make({LITE: ok(LITE, "junk"), flash: ok(flash, "good")})
        result = client.complete(Tier.FILE, "SYS", "a", accept=lambda t: t == "good")
        self.assertEqual((result.text, model.calls), ("good", [LITE, flash]))
        # The accepted answer is cached under its own rung; the junk is not.
        again = client.complete(Tier.FILE, "SYS", "a", accept=lambda t: t == "good")
        self.assertTrue(again.cached)
        self.assertEqual(model.calls, [LITE, flash])

    def test_cached_text_is_rechecked(self) -> None:
        client, model = self.make({LITE: ok(LITE, "old")})
        client.complete(Tier.FILE, "SYS", "a")
        model.script[LITE] = ok(LITE, "new")
        result = client.complete(Tier.FILE, "SYS", "a", accept=lambda t: t == "new")
        self.assertEqual(result.text, "new")
        self.assertEqual(len(model.calls), 2)

    def test_nothing_accepted_returns_the_last_answer(self) -> None:
        client, _ = self.make({m: ok(m, "junk") for m in ALL_GEMINI})
        result = client.complete(Tier.FILE, "SYS", "a", accept=lambda t: False)
        self.assertEqual(result.text, "junk")


class Claude(Scripted):
    """Scripted, listing models as the Anthropic API does: Haiku 4.5 under
    its dated id, the 4.6-and-later ids as they are."""

    provider = "anthropic"

    def list_models(self) -> list[str]:
        return ["claude-haiku-4-5-20251001", "claude-sonnet-5", "claude-opus-5",
                "claude-opus-5-5"]


class CacheWriter(Scripted):
    """Bills the most a request can: every input token it could carry written
    to the prompt cache and the whole output cap, `attempts` times (a declined
    attempt billed beside its server-side fallback)."""

    provider = "anthropic"
    attempts = 1

    def generate(self, request: Request) -> Completion:
        self.calls.append(request.model)
        tokens = token_ceiling(request.system, request.prompt) * self.attempts
        usage = Usage(input_tokens=tokens, cache_write_tokens=tokens,
                      output_tokens=request.max_output_tokens * self.attempts)
        return Completion("doc", request.model, "anthropic", Finish.STOP, usage)


def plain(model: Scripted, router: Router, budget: Budget | None = None) -> LLMClient:
    return LLMClient(model, router=router, budget=budget,
                     retry=RetryPolicy(base_delay=0))


class TestModelIds(unittest.TestCase):
    def test_a_dated_listing_keeps_the_cheapest_rung(self) -> None:
        # Regression: discover() matched the listing exactly, lost Haiku 4.5
        # (listed as claude-haiku-4-5-20251001) and sent file work to Sonnet 5.
        model = Claude({"claude-haiku-4-5": ok("claude-haiku-4-5-20251001")})
        client = plain(model, Router("anthropic"))
        client.discover()           # lists the previous generation only
        result = client.complete(Tier.FILE, "SYS", "a.py")
        self.assertEqual(model.calls, ["claude-haiku-4-5"])
        self.assertAlmostEqual(result.cost_usd, (1000 * 1.00 + 200 * 5.00) / 1e6)

    def test_a_pinned_model_is_requested_and_priced_as_named(self) -> None:
        # Regression: `--model claude-opus-5-5` was sent as claude-opus-5.
        model = Claude({"claude-opus-5-5": ok("claude-opus-5-5")})
        client = plain(model, Router("anthropic", pinned="claude-opus-5-5"))
        result = client.complete(Tier.FILE, "SYS", "a.py")
        self.assertEqual(model.calls, ["claude-opus-5-5"])
        self.assertAlmostEqual(result.cost_usd, (1000 * 4.00 + 200 * 20.00) / 1e6)

    def test_a_missing_pin_is_asked_for_once(self) -> None:
        # Regression: a `models/` pin was retired under another spelling, so
        # its 404 was sent again on every call.
        model = Scripted({"gemini-2.5-flash": ModelNotFound("gone")})
        client = plain(model, Router("gemini", pinned="models/gemini-2.5-flash"))
        for prompt in ("a", "b", "c"):
            with self.assertRaises(ConfigError):
                client.complete(Tier.FILE, "SYS", prompt)
        self.assertEqual(model.calls, ["gemini-2.5-flash"])

    def test_a_fallback_is_never_booked_below_the_model_asked(self) -> None:
        # A server-side fallback answers under its own id; a cheaper one must
        # not discount the declined attempt.
        model = Claude({"claude-opus-5": ok("claude-sonnet-5")})
        client = plain(model, Router("anthropic", available=PREVIOUS_CLAUDE))
        result = client.complete(Tier.ARCHITECTURE, "SYS", "x")
        self.assertAlmostEqual(result.cost_usd, (1000 * 5.00 + 200 * 25.00) / 1e6)


class TestSpendCeiling(unittest.TestCase):
    def test_the_dearest_bill_stays_inside_the_ceiling(self) -> None:
        # Regression: input was reserved at 1.0x and 3.5 characters a token,
        # while a request can bill its whole prompt as a 1.25x cache write and
        # Claude's tokenizer averages 2.5 characters a token, so settled spend
        # passed max_cost.
        prompt = "数据 def f(x): return x\n" * 2_000
        for tier, name, attempts in ((Tier.MODULE, "claude-sonnet-5", 1),
                                     (Tier.ARCHITECTURE, "claude-opus-5", 2)):
            info = find(name)
            assert info is not None
            reserve = round(info.estimate(token_ceiling("SYS", prompt), 800) * 1e6)
            for micros, sent in ((reserve, [name]), (reserve - 1, [])):
                with self.subTest(model=name, ceiling=micros):
                    budget = Budget(max_cost=micros / 1e6)
                    model = CacheWriter({})
                    model.attempts = attempts
                    router = Router("anthropic", available=PREVIOUS_CLAUDE)
                    client = plain(model, router, budget)
                    with contextlib.suppress(BudgetExceeded):
                        client.complete(tier, "SYS", prompt, max_output_tokens=800)
                    self.assertEqual(model.calls, sent)
                    self.assertLessEqual(budget.spent, budget.max_cost)


class TestRetiredModels(ClientCase):
    def test_answers_cached_under_a_retired_model_are_still_served(self) -> None:
        # Regression: retiring a model hid its cached answers, so a repeat
        # (or a resumed run's finished item) was paid again on a dearer rung.
        flash = "gemini-2.5-flash"
        client, model = self.make({LITE: ok(LITE), flash: ok(flash)})
        client.complete(Tier.FILE, "SYS", "a")
        model.script[LITE] = QuotaExhausted("day")
        client.complete(Tier.FILE, "SYS", "b")           # retires flash-lite
        self.assertTrue(client.complete(Tier.FILE, "SYS", "a").cached)
        client.router.available = {flash}                # no longer listed either
        self.assertTrue(client.complete(Tier.FILE, "SYS", "a").cached)
        self.assertTrue(client.quote(Tier.FILE, "SYS", "a").cached)
        self.assertEqual(model.calls, [LITE, LITE, flash])

    def test_quota_on_every_rung_keeps_raising_quota(self) -> None:
        # Regression: once quota had retired every rung, later calls raised
        # ConfigError (exit 2) instead of QuotaExhausted (exit 10).
        for pinned in (None, LITE):
            with self.subTest(pinned=pinned):
                client, model = self.make({m: ok(m) for m in ALL_GEMINI})
                client.router = Router("gemini", pinned=pinned,
                                       available=set(ALL_GEMINI))
                client.complete(Tier.FILE, "SYS", "answered")
                for name in ALL_GEMINI:
                    model.script[name] = QuotaExhausted("day")
                with self.assertRaises(QuotaExhausted):
                    client.complete(Tier.FILE, "SYS", "first")
                sent = len(model.calls)
                with self.assertRaises(QuotaExhausted):
                    client.complete(Tier.FILE, "SYS", "second")
                with self.assertRaises(QuotaExhausted):
                    client.quote(Tier.FILE, "SYS", "second")
                self.assertTrue(client.complete(Tier.FILE, "SYS", "answered").cached)
                self.assertEqual(len(model.calls), sent)

    def test_quota_retirement_lifts_when_the_quota_day_turns(self) -> None:
        # Regression: a model retired for its daily quota stayed retired past
        # midnight Pacific, so later work escalated to dearer models.
        flash = "gemini-2.5-flash"
        day = ["2026-11-01"]
        with mock.patch("devdocs.index.ledger.quota_day", lambda now=None: day[0]):
            client, model = self.make({LITE: QuotaExhausted("day"), flash: ok(flash)})
            client.complete(Tier.FILE, "SYS", "a")
            model.script[LITE] = ok(LITE)
            client.complete(Tier.FILE, "SYS", "b")      # same day: still retired
            day[0] = "2026-11-02"
            client.complete(Tier.FILE, "SYS", "c")
        self.assertEqual(model.calls, [LITE, flash, flash, LITE])

    def test_local_daily_cap_resets_when_the_quota_day_turns(self) -> None:
        # Regression: the limiter kept counting yesterday's requests, refused
        # the model locally and retired it for the rest of the run.
        pro = "gemini-2.5-pro"
        day = ["2026-11-01"]
        with mock.patch("devdocs.index.ledger.quota_day", lambda now=None: day[0]):
            client, model = self.make({pro: ok(pro)})
            usage = DailyUsage(self.db)
            for _ in range(99):                         # free tier: 100 a day
                usage.record(pro)
            client.complete(Tier.ARCHITECTURE, "SYS", "a")
            with self.assertRaises(QuotaExhausted):
                client.complete(Tier.ARCHITECTURE, "SYS", "b")
            day[0] = "2026-11-02"
            self.assertEqual(client.complete(Tier.ARCHITECTURE, "SYS", "b").model, pro)
        self.assertEqual(model.calls, [pro, pro])


class TestResponseCache(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.cache = ResponseCache(self.db)

    def request(self, **overrides: Any) -> Request:
        fields: dict[str, Any] = {"model": "m", "system": "s", "prompt": "p"}
        fields.update(overrides)
        return Request(**fields)

    def test_key_changes_with_anything_that_shapes_output(self) -> None:
        base = self.cache.key(self.request(), provider="gemini")
        variants = [
            self.cache.key(self.request(), provider="anthropic"),
            self.cache.key(self.request(model="m2"), provider="gemini"),
            self.cache.key(self.request(system="s2"), provider="gemini"),
            self.cache.key(self.request(prompt="p2"), provider="gemini"),
            self.cache.key(self.request(max_output_tokens=9), provider="gemini"),
            self.cache.key(self.request(temperature=0.9), provider="gemini"),
            self.cache.key(self.request(effort="high"), provider="gemini"),
        ]
        self.assertEqual(len({base, *variants}), 1 + len(variants))

    def test_tag_does_not_change_the_key(self) -> None:
        self.assertEqual(self.cache.key(self.request(tag="a"), provider="g"),
                         self.cache.key(self.request(tag="b"), provider="g"))

    def test_round_trip_marks_cached(self) -> None:
        self.assertTrue(self.cache.put("k", ok("m", "hello")))
        hit = self.cache.get("k")
        assert hit is not None
        self.assertEqual(hit.text, "hello")
        self.assertTrue(hit.cached)
        self.assertEqual(hit.cost_usd, 0.0)

    def test_unusable_completions_are_not_stored(self) -> None:
        self.assertFalse(self.cache.put("k", nothing("m", Finish.BLOCKED)))
        self.assertIsNone(self.cache.get("k"))

    def test_stats_and_clear(self) -> None:
        self.cache.put("a", Completion("x", "m", "g", Finish.STOP, cost_usd=0.5))
        self.assertEqual(self.cache.stats(), (1, 0.5))
        self.assertEqual(self.cache.clear(), 1)


class TestRunLedger(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.ledger = RunLedger(self.db)

    def test_new_run_then_resume(self) -> None:
        run, resumed = self.ledger.start("cfg")
        self.assertFalse(resumed)
        self.ledger.finish(run, "interrupted")
        again, resumed = self.ledger.start("cfg")
        self.assertEqual((again, resumed), (run, True))

    def test_different_config_starts_fresh(self) -> None:
        run, _ = self.ledger.start("cfg")
        self.ledger.finish(run, "interrupted")
        other, resumed = self.ledger.start("other-cfg")
        self.assertNotEqual(other, run)
        self.assertFalse(resumed)

    def test_finished_runs_are_not_resumed(self) -> None:
        run, _ = self.ledger.start("cfg")
        self.ledger.finish(run)
        self.assertNotEqual(self.ledger.start("cfg")[0], run)


class TestDailyUsage(unittest.TestCase):
    def test_counts_accumulate_per_model_and_day(self) -> None:
        db = Database(":memory:")
        self.addCleanup(db.close)
        usage = DailyUsage(db)
        usage.record("m", cost=0.1, day="2026-06-01")
        usage.record("m", cost=0.1, day="2026-06-01")
        usage.record("m", day="2026-06-02")
        self.assertEqual(usage.requests("m", day="2026-06-01"), 2)
        self.assertEqual(usage.requests("m", day="2026-06-02"), 1)
        self.assertEqual(usage.requests("other", day="2026-06-01"), 0)

    def test_quota_day_follows_us_pacific(self) -> None:
        # 05:00 UTC is still the previous day in California.
        early = datetime(2026, 6, 2, 5, 0, tzinfo=UTC)
        midday = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
        self.assertEqual(quota_day(early), "2026-06-01")
        self.assertEqual(quota_day(midday), "2026-06-02")


if __name__ == "__main__":
    unittest.main()
