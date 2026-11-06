"""Catalog pricing, routing, pacing and the spend ceiling."""

from __future__ import annotations

import math
import threading
import time
import unittest

import devdocs.llm.catalog as catalog_module
from devdocs.errors import BudgetExceeded, ConfigError, Interrupted, QuotaExhausted
from devdocs.llm import Usage
from devdocs.llm.budget import (
    Budget,
    RateLimiter,
    Router,
    estimate_tokens,
    token_ceiling,
)
from devdocs.llm.catalog import (
    CATALOG,
    ModelInfo,
    Tier,
    assume,
    catalogued,
    find,
    ladder,
)
from tests.test_http import Watched, stopped_while_waiting

GEMINI = ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]


def known(model_id: str) -> ModelInfo:
    info = find(model_id)
    assert info is not None, model_id
    return info


class TestCatalog(unittest.TestCase):
    def test_every_model_has_sane_prices(self) -> None:
        for info in CATALOG:
            self.assertGreater(info.input_per_mtok, 0, info.id)
            self.assertGreaterEqual(info.output_per_mtok, 0, info.id)
            if info.long_prompt_tokens is not None:
                self.assertGreater(info.long_input_per_mtok, info.input_per_mtok)
                self.assertGreater(info.long_output_per_mtok, info.output_per_mtok)

    def test_exact_id_beats_prefix(self) -> None:
        self.assertEqual(known("gemini-2.5-flash-lite").id, "gemini-2.5-flash-lite")

    def test_dated_and_prefixed_ids_resolve(self) -> None:
        cases = {
            "models/gemini-2.5-flash": "gemini-2.5-flash",
            "gemini-2.5-flash-lite-preview-06-17": "gemini-2.5-flash-lite",
            "gemini-2.5-flash-lite-preview-09-2025": "gemini-2.5-flash-lite",
            "gemini-2.5-flash-preview-05-20": "gemini-2.5-flash",
            "claude-haiku-4-5-20251001": "claude-haiku-4-5",
        }
        for query, expected in cases.items():
            self.assertEqual(known(query).id, expected)
        self.assertIsNone(find("gpt-4o"))

    def test_a_longer_id_is_another_model(self) -> None:
        # Regression: claude-opus-5-5 resolved to claude-opus-5, an older
        # model billed 25% more, because any `-suffix` counted as a snapshot.
        opus = known("claude-opus-5-5")
        self.assertEqual((opus.id, opus.input_per_mtok, opus.output_per_mtok),
                         ("claude-opus-5-5", 4.0, 20.0))
        for other in ("claude-opus-5-7", "claude-sonnet-5-9", "gemini-2.5-flash-image",
                      "gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts"):
            self.assertIsNone(find(other), other)

    def test_a_dated_listing_keeps_its_alias_on_the_ladder(self) -> None:
        # Regression: /v1/models names Haiku 4.5 `claude-haiku-4-5-20251001`,
        # and matching the listing exactly dropped the cheapest rung.
        listed = {"claude-haiku-4-5-20251001", "claude-sonnet-5", "claude-opus-5"}
        rungs = ladder("anthropic", Tier.FILE, available=listed)
        self.assertEqual([m.id for m in rungs],
                         ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"])
        # A listed preview or newer version says nothing about another id.
        listed = {"gemini-2.5-flash-preview-09-2025", "claude-opus-5-5"}
        self.assertEqual(catalogued(listed), {"claude-opus-5-5"})

    def test_ladder_climbs_from_the_task_tier(self) -> None:
        self.assertEqual([m.id for m in ladder("gemini", Tier.FILE)], GEMINI)
        self.assertEqual([m.id for m in ladder("anthropic", Tier.FILE)],
                         ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"])
        self.assertEqual([m.id for m in ladder("anthropic", Tier.ARCHITECTURE)],
                         ["claude-opus-5-5"])

    def test_one_model_per_tier_with_stand_ins_on_demand(self) -> None:
        # A key that cannot reach Gemini 2.5 is served by 3.x, tier by tier.
        listed = {"gemini-3.1-flash-lite", "gemini-3.5-flash-lite",
                  "gemini-3.8-flash", "gemini-3.1-pro-preview"}
        self.assertEqual([m.id for m in ladder("gemini", Tier.FILE, available=listed)],
                         ["gemini-3.1-flash-lite", "gemini-3.8-flash",
                          "gemini-3.1-pro-preview"])
        retired = {"gemini-3.1-flash-lite"}
        rungs = ladder("gemini", Tier.FILE, available=listed, exclude=retired)
        self.assertEqual(rungs[0].id, "gemini-3.5-flash-lite")
        self.assertEqual(len(rungs), 3)

    def test_free_only_never_routes_to_a_paid_only_model(self) -> None:
        listed = {"gemini-3.1-flash-lite", "gemini-3.8-flash",
                  "gemini-3.1-pro-preview", "gemini-4-argon"}
        paid = [m.id for m in ladder("gemini", Tier.ARCHITECTURE, available=listed)]
        self.assertEqual(paid, ["gemini-3.1-pro-preview"])
        free = [m.id for m in ladder("gemini", Tier.ARCHITECTURE, available=listed,
                                     free_only=True)]
        self.assertEqual(free, ["gemini-3.8-flash"])    # best free model stands in
        # A provider with no free tier at all is unaffected.
        self.assertEqual(
            [m.id for m in ladder("anthropic", Tier.FILE, free_only=True)],
            ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"],
        )

    def test_announced_price_changes_apply_from_their_date(self) -> None:
        flash = known("gemini-3.8-flash")
        original = catalog_module._today
        self.addCleanup(setattr, catalog_module, "_today", original)
        catalog_module._today = lambda: "2026-12-31"
        self.assertEqual(flash.rates(1000), (0.75, 3.75))
        catalog_module._today = lambda: "2027-01-01"
        self.assertEqual(flash.rates(1000), (1.50, 7.50))

    def test_unrouted_entries_never_join_a_ladder(self) -> None:
        unrouted = {info.id for info in CATALOG if not info.routed}
        self.assertIn("claude-fable-5-1", unrouted)
        for provider in ("gemini", "anthropic"):
            for tier in Tier:
                ids = {m.id for m in ladder(provider, tier)}
                self.assertFalse(ids & unrouted, (provider, tier))

    def test_ladder_respects_available_and_excluded(self) -> None:
        ids = [m.id for m in ladder("gemini", Tier.FILE,
                                    available={"gemini-2.5-flash", "gemini-2.5-pro"},
                                    exclude={"gemini-2.5-pro"})]
        self.assertEqual(ids, ["gemini-2.5-flash"])

    def test_cost_prices_cache_and_batch(self) -> None:
        haiku = known("claude-haiku-4-5")
        plain = haiku.cost(Usage(input_tokens=10_000))
        cached = haiku.cost(Usage(input_tokens=10_000, cached_input_tokens=8_000))
        self.assertAlmostEqual(plain, 0.01)
        self.assertAlmostEqual(cached, 0.0028)
        self.assertAlmostEqual(
            haiku.cost(Usage(input_tokens=10_000), batch=True), 0.005)

    def test_gemini_cache_reads_and_batch_match_the_price_list(self) -> None:
        # Regression: cached input was priced at 0.25x and batch at 1.0x;
        # Google lists $0.03 cached and $0.15 / $1.25 batch for 2.5 Flash.
        flash = known("gemini-2.5-flash")
        usage = Usage(input_tokens=100_000, cached_input_tokens=80_000,
                      output_tokens=1_000)
        self.assertAlmostEqual(flash.cost(usage),
                               (20_000 * 0.30 + 80_000 * 0.03 + 1_000 * 2.50) / 1e6)
        batch = flash.cost(Usage(input_tokens=100_000, output_tokens=1_000), batch=True)
        self.assertAlmostEqual(batch, (100_000 * 0.15 + 1_000 * 1.25) / 1e6)

    def test_long_prompts_bill_at_the_long_rates(self) -> None:
        # Regression: a 300k-token prompt to 2.5 Pro was priced at the
        # <=200k rates, $0.475 instead of $0.90.
        pro = known("gemini-2.5-pro")
        long = Usage(input_tokens=300_000, output_tokens=8_000, thinking_tokens=2_000)
        self.assertAlmostEqual(pro.cost(long), 0.90)
        self.assertAlmostEqual(pro.estimate(300_000, 10_000), 0.90)
        at_threshold = Usage(input_tokens=200_000, output_tokens=10_000)
        self.assertAlmostEqual(pro.cost(at_threshold), 0.35)
        haiku = known("claude-haiku-5-5")
        self.assertAlmostEqual(haiku.cost(Usage(input_tokens=100_001)),
                               100_001 * 0.50 / 1e6)

    def test_estimate_bounds_every_way_a_call_can_bill(self) -> None:
        # Regression: input was reserved at 1.0x while Anthropic bills a
        # prompt-cache write at 1.25x, so a settled call passed max_cost.
        tokens, output = 50_000, 2_000
        for info in CATALOG:
            attempts = 2 if info.server_fallbacks else 1
            for usage in (
                Usage(input_tokens=tokens, output_tokens=output),
                Usage(input_tokens=tokens, output_tokens=output,
                      cache_write_tokens=tokens),
                Usage(input_tokens=tokens, output_tokens=output,
                      cached_input_tokens=tokens),
                Usage(input_tokens=tokens, thinking_tokens=output),
            ):
                self.assertGreaterEqual(info.estimate(tokens, output) + 1e-12,
                                        attempts * info.cost(usage), info.id)

    def test_thinking_tokens_bill_as_output(self) -> None:
        flash = known("gemini-2.5-flash")
        self.assertAlmostEqual(
            flash.cost(Usage(thinking_tokens=1_000_000)), flash.output_per_mtok)

    def test_unknown_model_is_priced_at_the_vendors_ceiling(self) -> None:
        # Regression: the stand-in copied the priciest catalogued model, half
        # of what Claude Fable 5.1 bills, so a pinned run spent ~2x max_cost.
        for provider in ("gemini", "anthropic"):
            stand_in = assume("brand-new-model", provider)
            for info in CATALOG:
                if info.provider != provider:
                    continue
                dearest_in = max(info.input_per_mtok, info.long_input_per_mtok)
                dearest_out = max(info.output_per_mtok, info.long_output_per_mtok)
                self.assertGreaterEqual(stand_in.input_per_mtok, dearest_in)
                self.assertGreaterEqual(stand_in.output_per_mtok, dearest_out)
                self.assertGreaterEqual(stand_in.cache_write_ratio,
                                        info.cache_write_ratio)
            self.assertEqual(stand_in.cached_input_ratio, 1.0)
            self.assertFalse(stand_in.accepts_temperature)
            self.assertFalse(stand_in.accepts_effort)
        claude = assume("claude-next", "anthropic")
        self.assertEqual((claude.input_per_mtok, claude.output_per_mtok), (10.0, 50.0))
        gemini = assume("gemini-next", "gemini")
        self.assertEqual((gemini.input_per_mtok, gemini.output_per_mtok), (4.0, 21.0))


class TestRouter(unittest.TestCase):
    def test_quality_moves_the_starting_rung(self) -> None:
        def first(quality: str) -> str:
            return Router("gemini", quality=quality).ladder(Tier.FILE)[0].id

        self.assertEqual(first("cheap"), "gemini-2.5-flash-lite")
        self.assertEqual(first("balanced"), "gemini-2.5-flash")
        self.assertEqual(first("high"), "gemini-2.5-pro")

    def test_retired_models_are_skipped(self) -> None:
        router = Router("gemini")
        router.retire("gemini-2.5-flash-lite")
        self.assertEqual(router.ladder(Tier.FILE)[0].id, "gemini-3.1-flash-lite")
        router = Router("gemini", available=set(GEMINI))
        router.retire("gemini-2.5-flash-lite")
        self.assertEqual(router.ladder(Tier.FILE)[0].id, "gemini-2.5-flash")

    def test_pinned_model_overrides_everything(self) -> None:
        router = Router("gemini", quality="high", pinned="gemini-2.5-flash-lite")
        self.assertEqual([m.id for m in router.ladder(Tier.ARCHITECTURE)],
                         ["gemini-2.5-flash-lite"])

    def test_pinned_unknown_model_still_routes(self) -> None:
        ladder_ = Router("gemini", pinned="gemini-3-preview").ladder(Tier.FILE)
        self.assertEqual([m.id for m in ladder_], ["gemini-3-preview"])

    def test_pinned_model_is_sent_under_the_id_given(self) -> None:
        # Regression: a pin was swapped for the catalog id that prefixed it,
        # so `--model claude-opus-5-5` called Opus 5 at Opus 5 prices.
        cases = {
            ("anthropic", "claude-opus-5-5"): (4.0, 20.0),             # its own entry
            ("anthropic", "claude-haiku-4-5-20251001"): (1.0, 5.0),    # a snapshot
            ("gemini", "gemini-2.5-flash-preview-09-2025"): (0.30, 2.50),
            ("anthropic", "claude-opus-4-8"): (10.0, 50.0),            # unknown
        }
        for (provider, pinned), rates in cases.items():
            info = Router(provider, pinned=pinned).ladder(Tier.FILE)[0]
            self.assertEqual(info.id, pinned)
            self.assertEqual((info.input_per_mtok, info.output_per_mtok), rates)

    def test_a_retired_pin_leaves_nothing_to_call(self) -> None:
        # Regression: a `models/` pin was retired under another spelling and
        # stayed on the ladder, so its 404 was re-sent on every call.
        router = Router("gemini", pinned="models/gemini-2.5-flash")
        self.assertEqual(router.pinned, "gemini-2.5-flash")
        router.retire(router.ladder(Tier.FILE)[0].id)
        self.assertEqual(router.ladder(Tier.FILE), [])
        self.assertEqual([m.id for m in router.candidates(Tier.FILE)],
                         ["gemini-2.5-flash"])

    def test_candidates_ignore_retirement_and_the_listing(self) -> None:
        router = Router("gemini", available={"gemini-2.5-pro"})
        router.retire("gemini-2.5-pro")
        self.assertEqual(router.ladder(Tier.FILE), [])
        candidates = [m.id for m in router.candidates(Tier.FILE)]
        self.assertEqual(candidates[0], "gemini-2.5-flash-lite")
        self.assertTrue(set(GEMINI) < set(candidates))
        self.assertIn("gemini-3.1-pro-preview", candidates)
        router.restore("gemini-2.5-pro")
        self.assertEqual([m.id for m in router.ladder(Tier.FILE)], ["gemini-2.5-pro"])

    def test_free_models_below_a_tier_follow_it_for_their_answers_on_loan(
        self,
    ) -> None:
        flash = known("gemini-3.8-flash")
        for free_only in (True, False):     # --paid finds them too
            router = Router("gemini", free_only=free_only)
            candidates = router.candidates(Tier.ARCHITECTURE)
            own = [m for m in candidates if m.tier is Tier.ARCHITECTURE]
            self.assertEqual(candidates[: len(own)], own)
            self.assertIn(flash, candidates[len(own) :])
            self.assertTrue(all(m.free_rpm for m in candidates[len(own) :]))
            self.assertEqual(router.lent(Tier.ARCHITECTURE, flash), "architecture")
            self.assertEqual(router.lent(Tier.MODULE, flash), "")
        high = Router("gemini", quality="high", free_only=True)
        self.assertEqual(high.lent(Tier.FILE, flash), "architecture")
        pinned = Router("gemini", pinned=flash.id)
        self.assertEqual(pinned.lent(Tier.ARCHITECTURE, flash), "")
        claude = Router("anthropic", free_only=True).candidates(Tier.ARCHITECTURE)
        self.assertEqual({m.tier for m in claude}, {Tier.ARCHITECTURE})


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class TestRateLimiter(unittest.TestCase):
    def test_paces_to_requests_per_minute(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(3, clock=clock, sleep=clock.sleep)
        for _ in range(4):
            limiter.acquire()
        self.assertEqual(len(clock.slept), 1)
        self.assertAlmostEqual(clock.slept[0], 60.01)

    def test_window_slides(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(2, clock=clock, sleep=clock.sleep)
        limiter.acquire()
        clock.now = 59.0
        limiter.acquire()
        clock.now = 61.0   # first request has left the window
        self.assertEqual(limiter.acquire(), 0.0)

    def test_daily_cap_raises_without_waiting(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(None, 2, used_today=1, clock=clock, sleep=clock.sleep)
        limiter.acquire()
        with self.assertRaises(QuotaExhausted):
            limiter.acquire()
        self.assertEqual(clock.slept, [])

    def test_daily_count_starts_again_when_the_quota_day_turns(self) -> None:
        # Regression: the count never reset, so a run crossing midnight
        # Pacific kept the cheap model capped despite its fresh quota.
        clock = FakeClock()
        day = ["2026-11-01"]
        limiter = RateLimiter(None, 2, used_today=1, clock=clock, sleep=clock.sleep,
                              day=lambda: day[0])
        limiter.acquire()
        with self.assertRaises(QuotaExhausted):
            limiter.acquire()
        day[0] = "2026-11-02"
        self.assertEqual(limiter.used_today, 0)
        limiter.acquire()
        limiter.acquire()
        with self.assertRaises(QuotaExhausted):
            limiter.acquire()

    def test_free_tier_is_the_default_posture(self) -> None:
        lite = known("gemini-2.5-flash-lite")
        self.assertEqual(RateLimiter.for_model(lite).rpm, lite.free_rpm)
        paid = RateLimiter.for_model(lite, free_tier=False)
        self.assertEqual(paid.rpm, lite.paid_rpm)
        self.assertIsNone(paid.rpd)


class TestRateLimiterStop(unittest.TestCase):
    """`stop=`: the wait for a slot ends with the run, and takes no slot."""

    def test_a_stop_ends_the_wait_at_once(self) -> None:
        stop = Watched()
        limiter = RateLimiter(1, stop=stop)
        limiter.acquire()   # this minute's one slot is used
        raised, elapsed = stopped_while_waiting(limiter.acquire, stop)
        self.assertIsInstance(raised, Interrupted)
        self.assertLess(elapsed, 5)   # not the minute to the next slot
        self.assertEqual(limiter.used_today, 1)   # refused: not a request

    def test_a_stop_already_set_never_waits(self) -> None:
        stop = threading.Event()
        limiter = RateLimiter(1, stop=stop)
        limiter.acquire()
        stop.set()
        started = time.monotonic()
        with self.assertRaises(Interrupted):
            limiter.acquire()
        self.assertLess(time.monotonic() - started, 5)

    def test_a_free_slot_needs_no_wait_to_end(self) -> None:
        # The client refuses to call once stopped; a slot itself is not a call.
        stop = threading.Event()
        stop.set()
        self.assertEqual(RateLimiter(1, stop=stop).acquire(), 0.0)

    def test_unset_it_paces_as_ever(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(1, clock=clock, sleep=clock.sleep,
                              stop=threading.Event())
        limiter.acquire()
        self.assertAlmostEqual(limiter.acquire(), 60.01)
        self.assertEqual(limiter.used_today, 2)

    def test_a_given_sleep_is_used_and_the_stop_still_read_after_it(self) -> None:
        clock = FakeClock()
        stop = threading.Event()

        def sleep(seconds: float) -> None:
            clock.sleep(seconds)
            stop.set()   # the stop comes during the sleep

        limiter = RateLimiter(1, clock=clock, sleep=sleep, stop=stop)
        limiter.acquire()
        with self.assertRaises(Interrupted):
            limiter.acquire()
        self.assertEqual(len(clock.slept), 1)
        self.assertEqual(limiter.used_today, 1)

    def test_the_daily_cap_comes_before_any_wait(self) -> None:
        stop = threading.Event()
        stop.set()
        with self.assertRaises(QuotaExhausted):
            RateLimiter(1, 1, used_today=1, stop=stop).acquire()

    def test_for_model_hands_it_on_at_either_tier(self) -> None:
        lite = known("gemini-2.5-flash-lite")
        for free_tier in (True, False):
            with self.subTest(free_tier=free_tier):
                stop = Watched()
                limiter = RateLimiter.for_model(lite, free_tier=free_tier, stop=stop)
                for _ in range(limiter.rpm or 0):
                    limiter.acquire()
                raised, elapsed = stopped_while_waiting(limiter.acquire, stop)
                self.assertIsInstance(raised, Interrupted)
                self.assertLess(elapsed, 5)


class TestBudget(unittest.TestCase):
    def test_refuses_before_overshooting(self) -> None:
        budget = Budget(max_cost=0.10)
        budget.reserve(0.06)
        with self.assertRaises(BudgetExceeded):
            budget.reserve(0.05)

    def test_settle_replaces_estimate_with_actual(self) -> None:
        budget = Budget(max_cost=0.10)
        budget.reserve(0.08)
        budget.settle(0.08, 0.01)
        self.assertAlmostEqual(budget.spent, 0.01)
        budget.reserve(0.09)   # now fits

    def test_release_undoes_a_reservation(self) -> None:
        budget = Budget(max_cost=0.10)
        budget.reserve(0.10)
        budget.release(0.10)
        self.assertEqual(budget.requests, 0)
        self.assertEqual(budget.remaining, 0.10)

    def test_exactly_at_limit_is_allowed(self) -> None:
        # Regression: float accumulation refused the call landing on the limit.
        budget = Budget(max_cost=0.25)
        for _ in range(25):
            budget.reserve(0.01)
        with self.assertRaises(BudgetExceeded):
            budget.reserve(0.01)

    def test_concurrent_reservations_never_overshoot(self) -> None:
        budget = Budget(max_cost=0.25)
        admitted: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                budget.reserve(0.01)
            except BudgetExceeded:
                return
            with lock:
                admitted.append(1)

        threads = [threading.Thread(target=worker) for _ in range(60)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(admitted), 25)

    def test_request_ceiling(self) -> None:
        budget = Budget(max_requests=2)
        budget.reserve(0)
        budget.reserve(0)
        with self.assertRaises(BudgetExceeded):
            budget.reserve(0)

    def test_zero_means_unlimited(self) -> None:
        budget = Budget()
        for _ in range(100):
            budget.reserve(1.0)
        self.assertIsNone(budget.remaining)

    def test_bad_limits_are_config_errors(self) -> None:
        # Regression: nan and inf raised a raw ValueError / OverflowError.
        for cost in (math.nan, math.inf, -math.inf, -0.01):
            with self.subTest(cost=cost), self.assertRaises(ConfigError):
                Budget(max_cost=cost)
        with self.assertRaises(ConfigError):
            Budget(max_requests=-1)

    def test_a_tiny_ceiling_is_still_a_ceiling(self) -> None:
        # Under half a micro-dollar rounded to 0, which means no ceiling.
        with self.assertRaises(BudgetExceeded):
            Budget(max_cost=1e-7).reserve(0.001)


class TestEstimate(unittest.TestCase):
    def test_errs_high(self) -> None:
        self.assertGreaterEqual(estimate_tokens("x" * 4000), 1000)

    def test_token_ceiling_bounds_any_text(self) -> None:
        # Regression: reservations counted 3.5 characters a token; Claude's
        # tokenizer averages 2.5, and CJK or emoji can spend a token per byte.
        for text in ("def f(x):\n    return x\n" * 50, "数据处理模块初始化配置" * 100,
                     "🔥✅🚀" * 300, "QUJDREVGR0g=" * 90, ""):
            self.assertGreaterEqual(token_ceiling(text), len(text.encode("utf-8")))
            self.assertGreaterEqual(token_ceiling(text), estimate_tokens(text))
        self.assertGreater(token_ceiling("\ud800"), 3)   # counted, not raised


if __name__ == "__main__":
    unittest.main()
