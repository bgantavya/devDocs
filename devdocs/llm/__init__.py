"""Talking to model providers, and keeping what that costs under control.

Everything a run spends goes through this package, so it is where "cheap by
default" is enforced rather than hoped for:

- `http` — the stdlib transport: keep-alive, gzip, a TLS floor, retries.
- `catalog` — models as data: tier, price, limits, supported parameters.
- `providers` — Gemini and Anthropic behind one `ChatModel` protocol.
- `budget` — which model to use, how fast to call it, and when to stop.
- `client` — the facade the pipeline calls; composes all of the above.

The provider-neutral request and response types live here so that callers
never import a provider module directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

__all__ = [
    "ChatModel",
    "Completion",
    "Embedder",
    "Finish",
    "Request",
    "Usage",
]


class Finish(StrEnum):
    """Why generation stopped, normalised across providers.

    Only STOP means the text is complete. LENGTH means it was cut at the output
    cap. BLOCKED and REFUSED mean there is no usable text at all, which callers
    must treat as "this model produced nothing", not as a transport error.
    """

    STOP = "stop"
    LENGTH = "length"
    BLOCKED = "blocked"
    REFUSED = "refused"
    OTHER = "other"


@dataclass(frozen=True)
class Request:
    """One generation request, before provider-specific encoding.

    `system` is the stable prefix and `prompt` the volatile part. Keeping them
    apart is what lets providers cache the prefix: it must be byte-identical
    across calls, so nothing per-file may leak into it.
    """

    model: str
    system: str
    prompt: str
    max_output_tokens: int = 1024
    temperature: float | None = 0.2
    effort: str | None = None          # Claude effort level, where supported
    tag: str = ""                      # caller's label, e.g. the source path


@dataclass(frozen=True)
class Usage:
    """Tokens a call consumed, as the provider billed them."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    thinking_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.thinking_tokens + other.thinking_tokens,
        )


@dataclass(frozen=True)
class Completion:
    """A provider's answer. `text` is None when it produced nothing usable."""

    text: str | None
    model: str
    provider: str
    finish: Finish
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    cached: bool = False               # served from the local response cache
    detail: str = ""                   # provider's raw stop reason, for logs

    @property
    def ok(self) -> bool:
        return bool(self.text) and self.finish in (Finish.STOP, Finish.LENGTH)


@runtime_checkable
class ChatModel(Protocol):
    """A text-generation backend.

    `generate` must raise the typed errors from `devdocs.errors` for transport
    and API failures, and return a Completion with `text=None` for a response
    that arrived but carried no content. That split is what lets the client
    retry the first and escalate or skip the second.
    """

    provider: str

    def generate(self, request: Request) -> Completion: ...

    def list_models(self) -> list[str]: ...


@runtime_checkable
class Embedder(Protocol):
    """Turns text into vectors for retrieval."""

    provider: str
    dimensions: int

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]: ...
