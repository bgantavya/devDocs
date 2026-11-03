"""Building the configured provider, and the client around it.

The pipeline names a provider by string — "gemini", "anthropic", or
"anthropic-sdk" for the official SDK — and never imports a provider module.
This is the one place that maps the name to a class. Adding a provider takes
an entry here, its key variables in `config`, its models and price ceiling in
`catalog`, and, if the catalog files its models under another name, as for
"anthropic-sdk", that name for the offline model in the CLI's `_client`.
"""

from __future__ import annotations

from devdocs.errors import ConfigError
from devdocs.llm import ChatModel, Completion, Request
from devdocs.llm.http import HttpTransport, Transport

__all__ = ["PROVIDERS", "OfflineModel", "make_provider"]

PROVIDERS = ("gemini", "anthropic", "anthropic-sdk")


class OfflineModel:
    """A ChatModel that never sends: for pricing and cache-only commands."""

    def __init__(self, provider: str) -> None:
        self.provider = provider

    def generate(self, request: Request) -> Completion:
        raise ConfigError(f"offline: refusing to call {request.model}")

    def list_models(self) -> list[str]:
        return []


def make_provider(
    name: str, api_key: str, *, transport: Transport | None = None
) -> ChatModel:
    """The ChatModel for `name`. The SDK variant is imported only when asked for."""
    if name == "gemini":
        from devdocs.llm.providers import GeminiProvider

        return GeminiProvider(api_key, transport or HttpTransport())
    if name == "anthropic":
        from devdocs.llm.providers import AnthropicProvider

        return AnthropicProvider(api_key, transport or HttpTransport())
    if name == "anthropic-sdk":
        from devdocs.llm.anthropic_sdk import AnthropicSDKProvider

        return AnthropicSDKProvider(api_key)
    raise ConfigError(
        f"unknown provider {name!r}", hint=f"choose one of: {', '.join(PROVIDERS)}"
    )
