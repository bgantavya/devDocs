"""Deciding which model to call, how fast, and when to stop spending.

Three small classes, each owning one decision:

- `Router` picks the cheapest model that can serve a task's tier and climbs
  to a stronger one only when the cheaper one produces nothing usable. A
  pinned model is called under exactly the id the user gave.
- `RateLimiter` paces requests to a model's per-minute and per-day limits.
  The default posture is the free tier's limits, so a run slows down rather
  than burning through quota and failing halfway. Its day is the provider's
  quota day, so a run that crosses midnight Pacific gets the fresh quota.
- `Budget` is a hard ceiling on dollars and requests. Each call reserves an
  upper bound on its cost *before* it is sent (`token_ceiling` priced by
  `ModelInfo.estimate`), so neither one call nor parallel workers together
  can overshoot the limit; the real cost replaces the reservation after.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace

from devdocs.errors import BudgetExceeded, ConfigError, Interrupted, QuotaExhausted
from devdocs.llm.catalog import (
    ModelInfo,
    Tier,
    assume,
    find,
    ladder,
    lendable,
    tier_models,
)

__all__ = ["Budget", "RateLimiter", "Router", "estimate_tokens", "token_ceiling"]

# Roughly 4 characters per token for prose and code on Gemini and on Claude's
# earlier tokenizer, about 2.5 on Claude's current one (Opus 4.7 and later).
# Good enough to size a prompt; never a bound on what a call can cost.
_CHARS_PER_TOKEN = 3.5

# Role and turn markers around one system block and one user message.
_FRAMING_TOKENS = 64


def estimate_tokens(*texts: str) -> int:
    """Typical local token count, for sizing prompts; the real one arrives in usage."""
    return int(sum(len(text) for text in texts) / _CHARS_PER_TOKEN) + 1


def token_ceiling(*texts: str) -> int:
    """The most input tokens `texts` can bill, without a tokenizer.

    Subword tokenizers spend at least one byte of text per token (Claude's
    byte-level BPE, Gemini's SentencePiece with byte fallback), so the UTF-8
    length bounds the count for any text: code, CJK, emoji or base64. That is
    2.5 to 4 times a typical count, which is the price of a ceiling that
    holds. Lone surrogates count as the 3 bytes they would encode to.
    """
    size = sum(len(text.encode("utf-8", "surrogatepass")) for text in texts)
    return size + _FRAMING_TOKENS


def _bare(model_id: str) -> str:
    return model_id.strip().removeprefix("models/")


class Router:
    """Chooses a model per tier and remembers which ones failed this run.

    `quality` shifts where the ladder starts: "cheap" uses the task's own tier,
    "balanced" moves per-file work up to the module model, and "high" sends
    everything to the architecture model. `pinned` overrides it all with one
    model the user named, sent under that id (a `models/` prefix aside) and
    priced by its catalog entry, or by `assume` when it has none. With
    `free_only`, models outside the provider's free tier are never called:
    a free key cannot pay for them, and pacing to free limits says the key
    is free.
    """

    def __init__(
        self,
        provider: str,
        *,
        quality: str = "cheap",
        available: set[str] | None = None,
        pinned: str | None = None,
        free_only: bool = False,
    ) -> None:
        self.provider = provider
        self.quality = quality
        self.available = available
        self.free_only = free_only
        self.pinned = _bare(pinned) if pinned and pinned.strip() else None
        self._retired: set[str] = set()
        self._lock = threading.Lock()

    def ladder(self, tier: Tier) -> list[ModelInfo]:
        """Models that may be called for `tier` now, cheapest first."""
        if self.pinned:
            with self._lock:
                if self.pinned in self._retired:
                    return []
            return [self._pin()]
        with self._lock:
            retired = set(self._retired)
        return ladder(self.provider, self._start(tier), available=self.available,
                      exclude=retired, free_only=self.free_only)

    def candidates(self, tier: Tier) -> list[ModelInfo]:
        """Every model whose answer for `tier` may be reused, callable or not.

        A cached answer is already paid for, so a model since retired or no
        longer listed can still serve it; only calling it is off the table.
        The free models below the tier come last, for what they answered it
        on loan (`lent`).
        """
        if self.pinned:
            return [self._pin()]
        start = self._start(tier)
        return tier_models(self.provider, start) + lendable(self.provider, start)

    def lent(self, tier: Tier, info: ModelInfo) -> str:
        """The tier `info` serves on loan for `tier` work, or "" if none.

        When no free model of a tier can serve it, a free-only ladder lends it
        the strongest free model below. Those answers are cached under the
        tier they were lent to, apart from what the model answers for its own
        tier: later runs find them, and a quality setting that starts higher
        never takes a lower tier's own answer for one of its own.
        """
        if self.pinned:
            return ""
        start = self._start(tier)
        return start.value if info in lendable(self.provider, start) else ""

    def _pin(self) -> ModelInfo:
        assert self.pinned is not None
        base = find(self.pinned)
        if base is None:
            return assume(self.pinned, self.provider)
        return replace(base, id=self.pinned)

    def _start(self, tier: Tier) -> Tier:
        if tier is Tier.EMBED:
            return tier
        if self.quality == "high":
            return Tier.ARCHITECTURE
        if self.quality == "balanced" and tier is Tier.FILE:
            return Tier.MODULE
        return tier

    def retire(self, model_id: str) -> None:
        """Stop offering a model for the rest of the run, e.g. after a 404."""
        with self._lock:
            self._retired.add(_bare(model_id))

    def restore(self, model_id: str) -> None:
        """Offer a retired model again, e.g. once its daily quota has reset."""
        with self._lock:
            self._retired.discard(_bare(model_id))

    @property
    def retired(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._retired)


class RateLimiter:
    """Sliding-window pacing for one model: requests per minute and per day.

    The daily count starts from `used_today`, what earlier runs recorded, since
    the provider's own counter cannot be read. It stops a long run from
    ploughing into the daily wall mid-file; the provider's 429 covers the rest.
    With `day` (the provider's quota day) the count starts again from zero when
    that day turns, as the provider's does.
    """

    def __init__(
        self,
        rpm: int | None,
        rpd: int | None = None,
        *,
        used_today: int = 0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] | None = None,
        day: Callable[[], str] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self.rpm = rpm
        self.rpd = rpd
        self._clock = clock
        if sleep is None:
            sleep = time.sleep if stop is None else stop.wait
        self._sleep = sleep
        self._stop = stop
        self._window: deque[float] = deque()
        self._today = used_today
        self._day = day
        self._counting = day() if day is not None else ""
        self._lock = threading.Lock()

    @classmethod
    def for_model(
        cls,
        info: ModelInfo,
        *,
        free_tier: bool = True,
        used_today: int = 0,
        day: Callable[[], str] | None = None,
        stop: threading.Event | None = None,
    ) -> RateLimiter:
        """Free-tier limits by default; `used_today` carries earlier runs' count."""
        if free_tier and info.free_rpm:
            return cls(info.free_rpm, info.free_rpd, used_today=used_today, day=day,
                       stop=stop)
        return cls(info.paid_rpm, None, stop=stop)

    def _roll(self) -> None:
        """Start the daily count again once the quota day has turned. Locked."""
        if self._day is not None:
            today = self._day()
            if today != self._counting:
                self._counting, self._today = today, 0

    def acquire(self) -> float:
        """Block until one request may be sent. Returns seconds spent waiting.

        Raises QuotaExhausted without waiting once the daily cap is reached:
        sleeping to the next quota day would hold a worker for hours, so the
        caller moves on and the next call after the day turns is counted
        afresh. The wait for a slot is a wait on `stop` unless `sleep` is
        given; a stop request ends it at once with Interrupted.
        """
        waited = 0.0
        while True:
            with self._lock:
                self._roll()
                if self.rpd is not None and self._today >= self.rpd:
                    raise QuotaExhausted(
                        f"reached this model's limit of {self.rpd} requests per day",
                        status=None,
                    )
                now = self._clock()
                while self._window and now - self._window[0] >= 60.0:
                    self._window.popleft()
                if self.rpm is None or len(self._window) < self.rpm:
                    self._window.append(now)
                    self._today += 1
                    return waited
                pause = 60.0 - (now - self._window[0]) + 0.01
            self._sleep(pause)
            if self._stop is not None and self._stop.is_set():
                raise Interrupted("stopped while waiting for a request slot")
            waited += pause

    @property
    def used_today(self) -> int:
        with self._lock:
            self._roll()
            return self._today


def _micros(dollars: float) -> int:
    return round(dollars * 1_000_000)


class Budget:
    """A ceiling on what one run may spend, in dollars and in requests.

    `reserve` holds back an upper bound on a call's cost before it is sent;
    `settle` swaps it for the real cost once usage is known; `release` drops
    a reservation for a call that never happened. A limit of 0 disables that
    ceiling; a negative or non-finite one is a ConfigError. Amounts are kept
    as integer micro-dollars: summing floats drifts, and a ceiling that
    refuses a call landing exactly on it is wrong.
    """

    def __init__(self, max_cost: float = 0.0, max_requests: int = 0) -> None:
        if not math.isfinite(max_cost) or max_cost < 0:
            raise ConfigError(
                f"max_cost must be a non-negative number of dollars, not {max_cost}",
                hint="use 0 for no cost ceiling",
            )
        if max_requests < 0:
            raise ConfigError(
                f"max_requests cannot be negative ({max_requests})",
                hint="use 0 for no request limit",
            )
        self.max_cost = max_cost
        self.max_requests = max_requests
        # A sub-micro-dollar ceiling must not round to 0, which means none.
        self._limit = max(1, _micros(max_cost)) if max_cost > 0 else 0
        self._spent = 0
        self._reserved = 0
        self._requests = 0
        self._lock = threading.Lock()

    def reserve(self, estimate: float) -> None:
        amount = _micros(estimate)
        with self._lock:
            if self.max_requests and self._requests >= self.max_requests:
                raise BudgetExceeded(
                    f"request limit of {self.max_requests} reached",
                    spent=self._spent / 1e6, limit=self.max_cost,
                )
            if self._limit and self._spent + self._reserved + amount > self._limit:
                raise BudgetExceeded(
                    f"next call (up to ${estimate:.4f}) would pass the "
                    f"${self.max_cost:g} limit; ${self._spent / 1e6:.4f} spent so far",
                    spent=self._spent / 1e6, limit=self.max_cost,
                )
            self._reserved += amount
            self._requests += 1

    def settle(self, estimate: float, actual: float) -> None:
        with self._lock:
            self._reserved = max(0, self._reserved - _micros(estimate))
            self._spent += _micros(actual)

    def release(self, estimate: float) -> None:
        """Undo a reservation for a call that was never charged."""
        with self._lock:
            self._reserved = max(0, self._reserved - _micros(estimate))
            self._requests = max(0, self._requests - 1)

    def fail(self, estimate: float) -> None:
        """Drop the money held for a request that was sent but failed.

        The request still counts against `max_requests`: it reached the
        provider, and a retry is another one. Its money is released because
        vendors do not bill errors.
        """
        with self._lock:
            self._reserved = max(0, self._reserved - _micros(estimate))

    @property
    def spent(self) -> float:
        with self._lock:
            return self._spent / 1e6

    @property
    def requests(self) -> int:
        with self._lock:
            return self._requests

    @property
    def remaining(self) -> float | None:
        """Dollars left, or None when no cost ceiling is set."""
        if not self._limit:
            return None
        with self._lock:
            return max(0, self._limit - self._spent - self._reserved) / 1e6
