"""The one door every completion goes through; embedding batches keep the same
rules in `devdocs.index.embed`.

`LLMClient.complete` is where the cost rules compose, in this order:

1. Cache — if any model that could serve the tier has already answered this
   exact request, return that answer for free, before anything is sent. A
   model retired since, or no longer listed, still counts: its answer is paid.
2. Budget — reserve an upper bound on the call's cost (every input byte a
   token billed as a cache write, the whole output cap); refuse if it would
   pass the ceiling.
3. Pace — wait for the model's rate limiter (free-tier limits by default).
4. Call — with retries for transient failures only.
5. Settle — price the real usage at the dearer of the requested model and the
   one that actually served, record it against today's quota, and cache a
   usable answer.

A stop request (`stop`, set) ends everything but the cache: no call is
sent, retried or escalated after it, and a wait for a slot or a backoff
ends at once. A call already sent finishes, and its answer is kept.

Escalation to a stronger model happens only when a cheaper one returns
nothing usable, or has been retired (404, or its daily quota is spent). A
quota retirement lasts until the provider's quota day turns, and once quota
has retired every model a request could use, calls raise QuotaExhausted, so
a run stops as resumable rather than as misconfigured. A safety block or
refusal is returned as-is: a bigger model from the same vendor applies the
same policy, so climbing the ladder would just pay twice.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace

from devdocs.errors import ConfigError, Interrupted, ModelNotFound, QuotaExhausted
from devdocs.index import ledger
from devdocs.index.cache import ResponseCache
from devdocs.index.ledger import DailyUsage
from devdocs.llm import ChatModel, Completion, Finish, Request
from devdocs.llm.budget import Budget, RateLimiter, Router, token_ceiling
from devdocs.llm.catalog import ModelInfo, Tier, find
from devdocs.llm.http import RetryPolicy

__all__ = ["CallEvent", "LLMClient", "Quote"]

# Outcomes a stronger model of the same vendor would reproduce.
_TERMINAL = frozenset({Finish.BLOCKED, Finish.REFUSED})


def _quota_day() -> str:
    # Looked up per call: the same day DailyUsage counts against.
    return ledger.quota_day()


@dataclass(frozen=True)
class CallEvent:
    """Something the client did, for progress output and run logs."""

    kind: str          # cache-hit | call | retry | escalate | retire | quota
    model: str
    tag: str = ""
    detail: str = ""
    cost: float = 0.0


@dataclass(frozen=True)
class Quote:
    """What a call would cost, priced without sending it.

    Token counts and cost are the upper bounds the budget reserves, so a plan
    priced from quotes never admits a call the budget would refuse.
    """

    model: str
    input_tokens: int
    output_tokens: int
    cost: float        # upper bound: the whole output cap is used
    cached: bool       # already answered, so free


class LLMClient:
    """Composes router, budget, limiters, cache and retries around a ChatModel.

    `stop` is the run's stop signal (a new one unless given), shared with
    whatever else the run starts; see `devdocs.llm.stop`. Once set it stays
    set, so a client stopped once sends nothing until it is cleared.
    """

    def __init__(
        self,
        model: ChatModel,
        *,
        router: Router,
        budget: Budget | None = None,
        cache: ResponseCache | None = None,
        usage: DailyUsage | None = None,
        retry: RetryPolicy | None = None,
        free_tier: bool = True,
        effort: str | None = "low",
        temperature: float | None = 0.2,
        on_event: Callable[[CallEvent], None] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self.model = model
        self.router = router
        self.budget = budget or Budget()
        self.cache = cache
        self.usage = usage
        self.retry = retry or RetryPolicy()
        self.free_tier = free_tier
        self.effort = effort
        self.temperature = temperature
        self.on_event = on_event
        self.stop = stop if stop is not None else threading.Event()
        self._limiters: dict[str, RateLimiter] = {}
        self._quota: dict[str, tuple[str, QuotaExhausted]] = {}  # id -> (day, why)
        self._lock = threading.Lock()

    def _emit(
        self, kind: str, model: str, tag: str, detail: str = "", cost: float = 0.0
    ) -> None:
        if self.on_event is not None:
            self.on_event(CallEvent(kind, model, tag, detail, cost))

    def _key(self, request: Request, *, lent: str = "") -> str:
        assert self.cache is not None
        return self.cache.key(request, provider=self.model.provider, lent=lent)

    def _keyed(
        self, tier: Tier, info: ModelInfo, system: str, text: str, max_output: int
    ) -> str:
        """The cache key of `info`'s answer to `tier` work whose prompt is
        identified by `text`: apart from its own tier's when it is on loan."""
        request = self._request(info, system, text, max_output, "")
        return self._key(request, lent=self.router.lent(tier, info))

    def retried(
        self, model: str, tag: str
    ) -> Callable[[int, BaseException, float], None]:
        """An `on_retry` for `RetryPolicy.run`: each retry, as a "retry" event."""

        def report(attempt: int, err: BaseException, wait: float) -> None:
            self._emit("retry", model, tag, detail=f"#{attempt} in {wait:.1f}s: {err}")

        return report

    def discover(self) -> set[str]:
        """Ask the provider which models this key can use, and route only to those.

        Doing this once up front turns a wrong key or a missing model into one
        clear failure before any file is read, instead of one per file. The
        listing is kept as the provider spells it; the router counts a dated
        Claude id as the alias the catalog uses.
        """
        available = set(self.model.list_models())
        self.router.available = available
        return available

    def limiter(self, info: ModelInfo) -> RateLimiter:
        with self._lock:
            existing = self._limiters.get(info.id)
            if existing is None:
                used = self.usage.requests(info.id) if self.usage else 0
                existing = RateLimiter.for_model(
                    info, free_tier=self.free_tier, used_today=used, day=_quota_day,
                    stop=self.stop,
                )
                self._limiters[info.id] = existing
            return existing

    def _request(
        self, info: ModelInfo, system: str, prompt: str, max_output: int, tag: str
    ) -> Request:
        return Request(
            model=info.id,
            system=system,
            prompt=prompt,
            max_output_tokens=min(max_output, info.max_output_tokens or max_output),
            temperature=self.temperature,
            effort=self.effort if info.accepts_effort else None,
            tag=tag,
        )

    def _thaw(self) -> None:
        """Offer quota-retired models again once the provider's quota day turns."""
        with self._lock:
            if not self._quota:
                return
            today = _quota_day()
            thawed = [model for model, (day, _) in self._quota.items() if day != today]
            for model in thawed:
                del self._quota[model]
        for model in thawed:
            self.router.restore(model)

    def _rungs(self, tier: Tier, candidates: list[ModelInfo]) -> list[ModelInfo]:
        """The models that may be called now; why there are none, otherwise."""
        rungs = self.router.ladder(tier)
        if rungs:
            return rungs
        loans = self.router.free_only           # a paid ladder never lends
        with self._lock:
            spent = [self._quota[info.id][1] for info in candidates
                     if info.id in self._quota
                     and (loans or not self.router.lent(tier, info))]
        if spent:
            why = spent[0]
            raise QuotaExhausted(why.message, status=why.status, provider=why.provider)
        raise ConfigError(
            f"no {self.model.provider} model available for {tier.value} work",
            hint="run `devdocs doctor` to see which models your key can use",
        )

    def quote(
        self,
        tier: Tier,
        system: str,
        prompt: str,
        *,
        max_output_tokens: int = 800,
        identity: str | None = None,
    ) -> Quote:
        """Price a call the way `complete` would make it, without sending it.

        A request any model of the tier has already answered is free.
        Otherwise the price is what the first callable rung would reserve: an
        upper bound, since escalation only happens when a cheaper answer was
        unusable.
        """
        self._thaw()
        candidates = self.router.candidates(tier)
        if self.cache is not None:
            text = prompt if identity is None else identity
            for info in candidates:
                key = self._keyed(tier, info, system, text, max_output_tokens)
                if self.cache.get(key) is not None:
                    return Quote(info.id, 0, 0, 0.0, cached=True)
        info = self._rungs(tier, candidates)[0]
        request = self._request(info, system, prompt, max_output_tokens, "")
        tokens = token_ceiling(request.system, request.prompt)
        output = request.max_output_tokens
        cost = info.estimate(tokens, output)
        return Quote(info.id, tokens, output, cost, cached=False)

    def cached(
        self,
        tier: Tier,
        system: str,
        prompt: str,
        *,
        max_output_tokens: int = 800,
        accept: Callable[[str], bool] | None = None,
        identity: str | None = None,
    ) -> Completion | None:
        """The answer `complete` would return from the cache, without sending.

        Lets a caller price work that depends on earlier answers exactly as a
        run would build it, since those answers are free to read back.
        """
        self._thaw()

        def usable(completion: Completion) -> bool:
            return completion.ok and (accept is None or accept(completion.text or ""))

        text = prompt if identity is None else identity
        return self._cached(
            tier, self.router.candidates(tier), system, text, max_output_tokens, usable
        )

    def _cached(
        self,
        tier: Tier,
        candidates: list[ModelInfo],
        system: str,
        text: str,
        max_output_tokens: int,
        usable: Callable[[Completion], bool],
    ) -> Completion | None:
        if self.cache is None:
            return None
        for info in candidates:
            key = self._keyed(tier, info, system, text, max_output_tokens)
            hit = self.cache.get(key)
            if hit is not None and usable(hit):
                return hit
        return None

    def complete(
        self,
        tier: Tier,
        system: str,
        prompt: str,
        *,
        max_output_tokens: int = 800,
        tag: str = "",
        accept: Callable[[str], bool] | None = None,
        identity: str | None = None,
    ) -> Completion:
        """Get a completion for `prompt` as cheaply as the rules allow.

        `accept` lets the caller reject text that arrived but is unusable to
        it (unparseable, empty after cleaning); a rejected answer escalates
        like an empty one and is never cached. `identity`, when given, is what
        the answer is cached under instead of `prompt`, so reference material
        that changes often does not void it. Returns a Completion whose
        `text` may be None (blocked, refused, or no rung produced anything).
        Raises AuthError, BudgetExceeded, QuotaExhausted (also once quota has
        retired every model the request could use) and non-retryable
        ProviderError to the caller; and Interrupted once `stop` is set, for
        a request the cache cannot answer.
        """
        self._thaw()
        candidates = self.router.candidates(tier)

        def usable(completion: Completion) -> bool:
            return completion.ok and (accept is None or accept(completion.text or ""))

        text = prompt if identity is None else identity
        hit = self._cached(tier, candidates, system, text, max_output_tokens, usable)
        if hit is not None:
            self._emit("cache-hit", hit.model, tag)
            return hit

        self._rungs(tier, candidates)        # nothing callable: say why, now
        last: Completion | None = None
        quota_error: QuotaExhausted | None = None
        tried: list[str] = []

        # The ladder is re-read after every attempt: a model retired by a 404
        # or a spent quota hands its tier to the next stand-in, while one that
        # answered unusably stays, so the next rung is the tier above.
        while True:
            rung = next(
                (m for m in self.router.ladder(tier) if m.id not in tried), None
            )
            if rung is None:
                break
            if self.stop.is_set():
                raise Interrupted(f"stopped before calling {rung.id}")
            if tried:
                self._emit("escalate", rung.id, tag, detail=f"after {tried[-1]}")
            tried.append(rung.id)
            request = self._request(rung, system, prompt, max_output_tokens, tag)
            try:
                completion = self._call(rung, request)
            except ModelNotFound as err:
                self.router.retire(rung.id)
                self._emit("retire", rung.id, tag, detail=str(err))
                continue
            except QuotaExhausted as err:
                if err.status == 402:
                    raise   # billing is account-wide; another model will not help
                with self._lock:
                    self._quota[rung.id] = (_quota_day(), err)
                self.router.retire(rung.id)
                self._emit("quota", rung.id, tag, detail=str(err))
                quota_error = err
                continue

            good = usable(completion)
            if good and self.cache is not None:
                self.cache.put(
                    self._keyed(tier, rung, system, text, max_output_tokens), completion
                )
            # Reported once kept: a failing observer cannot lose a paid answer.
            self._emit("call", completion.model, tag,
                       detail=completion.finish.value, cost=completion.cost_usd)
            if good:
                return completion
            last = completion
            if completion.finish in _TERMINAL:
                return completion

        if last is not None:
            return last
        if quota_error is not None:
            raise quota_error
        raise ConfigError(
            f"every {tier.value}-tier model was unavailable",
            hint="the key may not have access to any catalogued model",
        )

    def _call(self, info: ModelInfo, request: Request) -> Completion:
        """One paid attempt against one model: reserve, pace, send, settle."""
        estimate = info.estimate(
            token_ceiling(request.system, request.prompt), request.max_output_tokens
        )

        def attempt() -> Completion:
            # Every attempt is a request: reserved, paced, counted. One that
            # fails keeps its request count but not its money.
            self.budget.reserve(estimate)
            try:
                self.limiter(info).acquire()
            except BaseException:
                self.budget.release(estimate)
                raise
            try:
                return self.model.generate(request)
            except BaseException:
                self.budget.fail(estimate)
                raise

        completion = self.retry.run(
            attempt, on_retry=self.retried(info.id, request.tag), stop=self.stop
        )

        # A server-side fallback answers under another model's id; never
        # book it below the price of the model that was asked.
        served = find(completion.model)
        cost = info.cost(completion.usage)
        if served is not None:
            cost = max(cost, served.cost(completion.usage))
        self.budget.settle(estimate, cost)
        if self.usage is not None:
            self.usage.record(info.id, cost=cost)
        return replace(completion, cost_usd=cost)

    @property
    def spent(self) -> float:
        return self.budget.spent
