"""Models as data: what each costs, what it accepts, and which job it suits.

Every task is assigned a *tier* — per-file prose, per-module summaries, or the
one or two architecture calls a run makes — and the catalog maps each tier to
models per provider, in order of preference. One model serves each tier; the
others in it stand in only when that one is out of reach, so escalating after
an unusable answer costs at most one call per tier. Routing is therefore
provider-agnostic, and changing which model serves a tier is an edit here,
not in the pipeline. Entries with `routed=False` never join a ladder: they
are here so that a model the user pins, or one a vendor reports having
served, is billed at its own rates and sent only the parameters it accepts.

Parameters differ per model in ways that return a 400 if ignored: current
Claude models reject `temperature`, Haiku 4.5 rejects `effort`, Sonnet 5.5
rejects disabled thinking and Opus 5.5 any thinking setting, and Gemini 2.5
takes a thinking budget where Gemini 3 takes a thinking level. Those facts
live on `ModelInfo` so request encoders never special-case model names.

An id matches an entry exactly, or as a snapshot of it: a date
(`claude-haiku-4-5-20251001`), a version number, or a dated preview
(`gemini-2.5-flash-preview-09-2025`). Any other extension names a different
model — `claude-opus-5-5` is Opus 5.5, not a snapshot of Opus 5, and is priced
differently — so it is unknown, and `assume` prices it at the most its vendor
charges for anything. An unfamiliar model can then never make a spending
ceiling look further away than it is.

`ModelInfo.estimate` is the bound a spending ceiling needs, not a forecast:
every input token priced as a prompt-cache write, the whole output cap used,
and a declined attempt billed beside its server-side fallback.

Prices change. They are recorded with the date they were checked, and
`devdocs doctor` warns once that date is old. Verify against the vendors'
pricing pages before relying on an estimate for anything that matters.
Google no longer publishes Gemini's free-tier limits (AI Studio shows them per
project); the values here only pace requests, and the provider's 429 decides.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from devdocs.llm import Usage

__all__ = [
    "CATALOG",
    "PRICES_CHECKED",
    "ModelInfo",
    "Tier",
    "assume",
    "catalogued",
    "find",
    "ladder",
    "tier_models",
]

PRICES_CHECKED = "2026-11-02"


def _today() -> str:
    """Today's UTC date, ISO. A seam for tests of announced price changes."""
    return datetime.now(UTC).date().isoformat()


class Tier(StrEnum):
    """How much reasoning a task needs, which bounds what we pay for it."""

    FILE = "file"
    MODULE = "module"
    ARCHITECTURE = "architecture"
    EMBED = "embed"


@dataclass(frozen=True)
class ModelInfo:
    """One model's price, limits, and accepted parameters.

    Prices are USD per million tokens. A prompt longer than
    `long_prompt_tokens` bills every token at the `long_*` rates instead.
    `free_rpm`/`free_rpd` are the free-tier request limits the rate limiter
    paces against by default; None means the provider has no free tier for
    this model.
    """

    id: str
    provider: str
    tier: Tier
    input_per_mtok: float
    output_per_mtok: float
    context_tokens: int
    max_output_tokens: int
    cached_input_ratio: float = 1.0      # price multiplier for cache reads
    cache_write_ratio: float = 1.0       # price multiplier for cache writes
    batch_ratio: float = 1.0             # price multiplier via batch API
    free_rpm: int | None = None
    free_rpd: int | None = None
    paid_rpm: int = 50
    accepts_temperature: bool = True
    accepts_effort: bool = False
    thinking_off: dict[str, Any] | None = None   # config that disables thinking
    server_fallbacks: bool = False       # supports `fallbacks: "default"`
    long_prompt_tokens: int | None = None
    long_input_per_mtok: float = 0.0
    long_output_per_mtok: float = 0.0
    routed: bool = True                  # False: priced here, never routed to
    # Announced price changes, as (ISO date, input, output) per MTok.
    price_changes: tuple[tuple[str, float, float], ...] = ()

    def rates(self, prompt_tokens: int) -> tuple[float, float]:
        """(input, output) USD per million tokens for a prompt this long.

        Both vendors count the whole prompt, cached tokens included.
        """
        threshold = self.long_prompt_tokens
        if threshold is not None and prompt_tokens > threshold:
            return self.long_input_per_mtok, self.long_output_per_mtok
        rates = (self.input_per_mtok, self.output_per_mtok)
        today = _today()
        for start, input_rate, output_rate in sorted(self.price_changes):
            if today >= start:
                rates = (input_rate, output_rate)
        return rates

    def cost(self, usage: Usage, *, batch: bool = False) -> float:
        """USD for one call's usage.

        Cached input bills at the cache-read ratio, cache writes at the write
        ratio, and thinking tokens as output — which is what both vendors do.
        """
        input_rate, output_rate = self.rates(usage.input_tokens)
        fresh = max(
            0, usage.input_tokens - usage.cached_input_tokens - usage.cache_write_tokens
        )
        input_cost = (
            fresh
            + usage.cached_input_tokens * self.cached_input_ratio
            + usage.cache_write_tokens * self.cache_write_ratio
        ) * input_rate
        output = usage.output_tokens + usage.thinking_tokens
        output_cost = output * output_rate
        total = (input_cost + output_cost) / 1_000_000
        return total * (self.batch_ratio if batch else 1.0)

    def estimate(self, input_tokens: int, output_tokens: int) -> float:
        """Upper-bound USD for a call of at most these many tokens.

        Input is priced as if all of it were written to the prompt cache
        (1.25x on Claude at the default 5-minute TTL the providers ask for;
        Gemini has no write premium), output at the full cap, which bounds
        thinking and text together on both vendors. With server-side
        fallbacks a declined attempt can bill beside the one that answers, so
        the call counts twice at these rates: the fallbacks Anthropic
        documents run on a model that costs no more. Pass upper bounds
        (`budget.token_ceiling`), not forecasts.
        """
        input_rate, output_rate = self.rates(input_tokens)
        input_cost = input_tokens * max(1.0, self.cache_write_ratio) * input_rate
        once = (input_cost + output_tokens * output_rate) / 1_000_000
        return once * (2 if self.server_fallbacks else 1)


_GEMINI_BUDGET_OFF = {"thinkingConfig": {"thinkingBudget": 0}}
_GEMINI_LOW = {"thinkingConfig": {"thinkingLevel": "low"}}
_GEMINI_MINIMAL = {"thinkingConfig": {"thinkingLevel": "minimal"}}
_CLAUDE_OFF = {"thinking": {"type": "disabled"}}

# Within a tier, order is preference: the first model a key can reach serves
# the tier, and the rest stand in only when it cannot (unlisted, retired after
# a 404 or a spent quota, or outside the free tier when pacing to it).
CATALOG: tuple[ModelInfo, ...] = (
    # -- Gemini ----------------------------------------------------------
    # Cache reads bill at 0.1x and the Batch API halves everything. The 2.5
    # models are the cheapest but Google now serves them only to projects
    # that used them before (others get a 404 and move on to 3.x). Gemini 3
    # and later take a thinking level, not a budget, and reason worse away
    # from their default temperature, so none is sent.
    ModelInfo(
        id="gemini-2.5-flash-lite", provider="gemini", tier=Tier.FILE,
        input_per_mtok=0.10, output_per_mtok=0.40,
        context_tokens=1_048_576, max_output_tokens=65_536,
        cached_input_ratio=0.1, batch_ratio=0.5,
        free_rpm=15, free_rpd=1000, paid_rpm=4000,
        thinking_off=_GEMINI_BUDGET_OFF,
    ),
    ModelInfo(
        id="gemini-3.1-flash-lite", provider="gemini", tier=Tier.FILE,
        input_per_mtok=0.25, output_per_mtok=1.50,
        context_tokens=1_048_576, max_output_tokens=65_536,
        cached_input_ratio=0.1, batch_ratio=0.5,
        free_rpm=15, free_rpd=1000, paid_rpm=4000, accepts_temperature=False,
    ),
    ModelInfo(
        id="gemini-3.5-flash-lite", provider="gemini", tier=Tier.FILE,
        input_per_mtok=0.30, output_per_mtok=2.50,
        context_tokens=1_048_576, max_output_tokens=65_536,
        cached_input_ratio=0.1, batch_ratio=0.5,
        free_rpm=15, free_rpd=1000, paid_rpm=4000, accepts_temperature=False,
        thinking_off=_GEMINI_MINIMAL,
    ),
    ModelInfo(
        id="gemini-2.5-flash", provider="gemini", tier=Tier.MODULE,
        input_per_mtok=0.30, output_per_mtok=2.50,
        context_tokens=1_048_576, max_output_tokens=65_536,
        cached_input_ratio=0.1, batch_ratio=0.5,
        free_rpm=10, free_rpd=250, paid_rpm=1000,
        thinking_off=_GEMINI_BUDGET_OFF,
    ),
    ModelInfo(
        id="gemini-3.8-flash", provider="gemini", tier=Tier.MODULE,
        input_per_mtok=0.75, output_per_mtok=3.75,
        context_tokens=1_048_576, max_output_tokens=65_536,
        cached_input_ratio=0.1, batch_ratio=0.5,
        free_rpm=10, free_rpd=250, paid_rpm=1000, accepts_temperature=False,
        thinking_off=_GEMINI_LOW,
        price_changes=(("2027-01-01", 1.50, 7.50),),
    ),
    ModelInfo(
        id="gemini-3.6-flash", provider="gemini", tier=Tier.MODULE,
        input_per_mtok=0.75, output_per_mtok=3.75,
        context_tokens=1_048_576, max_output_tokens=65_536,
        cached_input_ratio=0.1, batch_ratio=0.5,
        free_rpm=10, free_rpd=250, paid_rpm=1000, accepts_temperature=False,
        thinking_off=_GEMINI_MINIMAL,
        price_changes=(("2027-01-01", 1.50, 7.50),),
    ),
    # 2.5 Pro cannot disable thinking; its budget has a floor instead.
    ModelInfo(
        id="gemini-2.5-pro", provider="gemini", tier=Tier.ARCHITECTURE,
        input_per_mtok=1.25, output_per_mtok=10.00,
        context_tokens=1_048_576, max_output_tokens=65_536,
        cached_input_ratio=0.1, batch_ratio=0.5,
        free_rpm=5, free_rpd=100, paid_rpm=150,
        thinking_off={"thinkingConfig": {"thinkingBudget": 128}},
        long_prompt_tokens=200_000,
        long_input_per_mtok=2.50, long_output_per_mtok=15.00,
    ),
    # No free tier on the two below: a free-tier run uses the best free model.
    ModelInfo(
        id="gemini-3.1-pro-preview", provider="gemini", tier=Tier.ARCHITECTURE,
        input_per_mtok=2.00, output_per_mtok=12.00,
        context_tokens=1_048_576, max_output_tokens=65_536,
        cached_input_ratio=0.1, batch_ratio=0.5, paid_rpm=150,
        accepts_temperature=False, thinking_off=_GEMINI_LOW,
        long_prompt_tokens=200_000,
        long_input_per_mtok=4.00, long_output_per_mtok=18.00,
    ),
    ModelInfo(
        id="gemini-4-argon", provider="gemini", tier=Tier.ARCHITECTURE,
        input_per_mtok=2.00, output_per_mtok=10.00,
        context_tokens=1_048_576, max_output_tokens=65_536,
        cached_input_ratio=0.05, batch_ratio=0.5, paid_rpm=150,
        accepts_temperature=False, thinking_off=_GEMINI_LOW,
        price_changes=(("2026-12-01", 4.00, 20.00),),
    ),
    ModelInfo(
        id="gemini-embedding-001", provider="gemini", tier=Tier.EMBED,
        input_per_mtok=0.15, output_per_mtok=0.0,
        context_tokens=2048, max_output_tokens=0,
        batch_ratio=0.5, free_rpm=100, free_rpd=1000, paid_rpm=3000,
    ),
    # -- Anthropic -------------------------------------------------------
    # No free tier. Cache reads bill at 0.1x (0.05x on Opus 5.5, 0.025x on
    # Fable 5.1), 5-minute cache writes at 1.25x, and the Batch API halves
    # everything. `temperature` is never sent: current models reject it.
    # Thinking is the cheapest each model allows for writing docs: off on
    # Haiku 5.5, Sonnet 5 and Opus 5, `between_tools` on Sonnet 5.5 (which
    # rejects "disabled"), and low effort on Opus 5.5, which cannot turn it off.
    ModelInfo(
        id="claude-haiku-5-5", provider="anthropic", tier=Tier.FILE,
        input_per_mtok=0.10, output_per_mtok=0.50,
        context_tokens=1_000_000, max_output_tokens=128_000,
        cached_input_ratio=0.1, cache_write_ratio=1.25, batch_ratio=0.5,
        accepts_temperature=False, accepts_effort=True, thinking_off=_CLAUDE_OFF,
        long_prompt_tokens=100_000, long_input_per_mtok=0.50,
        long_output_per_mtok=2.50,
    ),
    ModelInfo(
        id="claude-haiku-4-5", provider="anthropic", tier=Tier.FILE,
        input_per_mtok=1.00, output_per_mtok=5.00,
        context_tokens=200_000, max_output_tokens=64_000,
        cached_input_ratio=0.1, cache_write_ratio=1.25, batch_ratio=0.5,
        accepts_temperature=False, accepts_effort=False,
    ),
    ModelInfo(
        id="claude-sonnet-5-5", provider="anthropic", tier=Tier.MODULE,
        input_per_mtok=2.00, output_per_mtok=10.00,
        context_tokens=1_000_000, max_output_tokens=128_000,
        cached_input_ratio=0.1, cache_write_ratio=1.25, batch_ratio=0.5,
        accepts_temperature=False, accepts_effort=True,
        thinking_off={"thinking": {"type": "between_tools"}}, server_fallbacks=True,
    ),
    ModelInfo(
        id="claude-sonnet-5", provider="anthropic", tier=Tier.MODULE,
        input_per_mtok=2.00, output_per_mtok=10.00,
        context_tokens=1_000_000, max_output_tokens=128_000,
        cached_input_ratio=0.1, cache_write_ratio=1.25, batch_ratio=0.5,
        accepts_temperature=False, accepts_effort=True, thinking_off=_CLAUDE_OFF,
    ),
    ModelInfo(
        id="claude-opus-5-5", provider="anthropic", tier=Tier.ARCHITECTURE,
        input_per_mtok=4.00, output_per_mtok=20.00,
        context_tokens=1_000_000, max_output_tokens=128_000,
        cached_input_ratio=0.05, cache_write_ratio=1.25, batch_ratio=0.5,
        accepts_temperature=False, accepts_effort=True, server_fallbacks=True,
    ),
    ModelInfo(
        id="claude-opus-5", provider="anthropic", tier=Tier.ARCHITECTURE,
        input_per_mtok=5.00, output_per_mtok=25.00,
        context_tokens=1_000_000, max_output_tokens=128_000,
        cached_input_ratio=0.1, cache_write_ratio=1.25, batch_ratio=0.5,
        accepts_temperature=False, accepts_effort=True, thinking_off=_CLAUDE_OFF,
        server_fallbacks=True,
    ),
    # Priced only, for a pin: thinking is always on.
    ModelInfo(
        id="claude-fable-5-1", provider="anthropic", tier=Tier.ARCHITECTURE,
        input_per_mtok=10.00, output_per_mtok=50.00,
        context_tokens=1_000_000, max_output_tokens=128_000,
        cached_input_ratio=0.025, cache_write_ratio=1.25, batch_ratio=0.5,
        accepts_temperature=False, accepts_effort=True, server_fallbacks=True,
        routed=False,
    ),
)

# The most each vendor charges per million tokens (input, output) for any text
# model it serves, long prompts and announced increases included: Claude
# Fable 5.1 and Mythos 5.1; Gemini 3.1 Pro preview over 200k tokens and
# Gemini 4 Argon from December 2026 (input), 3.5 Transcribe Live (output).
_CEILING: dict[str, tuple[float, float]] = {
    "anthropic": (10.00, 50.00),
    "gemini": (4.00, 21.00),
}

# A snapshot of the same model at the same price: a date (Claude before 4.6),
# a version number, or a preview or experimental tag with an optional date.
_SNAPSHOT = re.compile(
    r"-(?:\d{8}|\d{3}|(?:preview|exp)(?:-\d{2}-(?:\d{2}|\d{4})|-\d{4})?)"
)
# A dated Claude id, whose undated alias calls that same model.
_DATED = re.compile(r"-\d{8}")

_ORDER = (Tier.FILE, Tier.MODULE, Tier.ARCHITECTURE)


def _bare(model_id: str) -> str:
    return model_id.strip().removeprefix("models/")


def find(model_id: str) -> ModelInfo | None:
    """Look a model up by id, tolerating a `models/` prefix and snapshot suffix.

    Exact match wins; otherwise a catalogued id followed by a snapshot tag
    (date, version number, dated preview). Any other extension is another
    model and returns None: `claude-opus-5-5` is not `claude-opus-5`, and
    `gemini-2.5-flash-preview-tts` is not `gemini-2.5-flash`.
    """
    bare = _bare(model_id)
    for info in CATALOG:
        if info.id == bare:
            return info
    for info in CATALOG:
        if bare.startswith(info.id) and _SNAPSHOT.fullmatch(bare, len(info.id)):
            return info
    return None


def catalogued(listed: Iterable[str]) -> set[str]:
    """Catalog ids that a provider's model listing makes callable.

    A listing names models by id, and before the 4.6 generation a Claude
    model's id is its dated snapshot (`claude-haiku-4-5-20251001`); the
    undated alias the catalog uses calls that same model.
    """
    ids: set[str] = set()
    for model in listed:
        bare = _bare(model)
        for info in CATALOG:
            if info.id == bare or (
                bare.startswith(info.id) and _DATED.fullmatch(bare, len(info.id))
            ):
                ids.add(info.id)
    return ids


def assume(model_id: str, provider: str) -> ModelInfo:
    """A stand-in entry for a model the catalog does not know.

    Priced at the most its vendor charges for any model, on every rate the
    catalog records as well: cache reads bill as fresh input and writes at
    the dearest ratio. An unfamiliar model can therefore never make a
    spending ceiling look further away than it is. Parameters that some
    models reject are left off, since a 400 from an unsupported parameter
    would look like a broken model.
    """
    known = [info for info in CATALOG if info.provider == provider] or list(CATALOG)
    ceilings = [_CEILING[provider]] if provider in _CEILING else list(_CEILING.values())
    inputs = [rate for rate, _ in ceilings]
    outputs = [rate for _, rate in ceilings]
    for info in known:
        inputs += [info.input_per_mtok, info.long_input_per_mtok]
        outputs += [info.output_per_mtok, info.long_output_per_mtok]
    return ModelInfo(
        id=model_id,
        provider=provider,
        tier=Tier.ARCHITECTURE,
        input_per_mtok=max(inputs),
        output_per_mtok=max(outputs),
        context_tokens=max(info.context_tokens for info in known),
        max_output_tokens=max(info.max_output_tokens for info in known),
        cache_write_ratio=max(info.cache_write_ratio for info in known),
        paid_rpm=min(info.paid_rpm for info in known),
        accepts_temperature=False,
        accepts_effort=False,
        routed=False,
    )


def _steps(tier: Tier) -> list[Tier]:
    return [Tier.EMBED] if tier is Tier.EMBED else list(_ORDER[_ORDER.index(tier) :])


def _has_free_tier(provider: str) -> bool:
    return any(i.free_rpm for i in CATALOG if i.provider == provider and i.routed)


def ladder(
    provider: str,
    tier: Tier,
    *,
    available: set[str] | None = None,
    exclude: set[str] | None = None,
    free_only: bool = False,
) -> list[ModelInfo]:
    """One model per tier from `tier` upward: the escalation ladder.

    Each tier is served by its first eligible entry; the entries after it are
    stand-ins, used only when it is unreachable (`available` is the live
    listing, as the provider spells it), retired this run (`exclude`) or,
    with `free_only`, outside the free tier. Escalation after an unusable
    answer therefore costs at most one call per tier. A free-only tier left
    empty is served by the strongest free model below it.
    """
    reachable = None if available is None else catalogued(available)
    free = free_only and _has_free_tier(provider)

    def eligible(info: ModelInfo, step: Tier) -> bool:
        return (
            info.routed and info.provider == provider and info.tier is step
            and (reachable is None or info.id in reachable)
            and not (exclude and info.id in exclude)
            and not (free and not info.free_rpm)
        )

    chosen: list[ModelInfo] = []
    for step in _steps(tier):
        pick = next((i for i in CATALOG if eligible(i, step)), None)
        if pick is None and free and step in _ORDER:
            for lower in reversed(_ORDER[: _ORDER.index(step)]):
                pick = next(
                    (i for i in CATALOG if eligible(i, lower) and i not in chosen),
                    None,
                )
                if pick is not None:
                    break
        if pick is not None and pick not in chosen:
            chosen.append(pick)
    return chosen


def tier_models(provider: str, tier: Tier) -> list[ModelInfo]:
    """Every routed model of `tier` and above, in ladder order: the models
    whose earlier answers to a request may be reused from the cache."""
    return [
        info for step in _steps(tier) for info in CATALOG
        if info.routed and info.provider == provider and info.tier is step
    ]
