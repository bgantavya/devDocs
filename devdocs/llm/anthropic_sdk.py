"""Claude via Anthropic's official Python SDK, for those who install it.

The default Anthropic provider speaks raw HTTP to keep devdocs dependency-free.
Anthropic recommends its SDK, which tracks API changes for you; installing the
`anthropic` extra (`pip install devdocs[anthropic]`) and setting
`provider = "anthropic-sdk"` swaps this in. Behaviour is identical from the
pipeline's point of view: same `ChatModel` protocol, and the same request body,
response decoding and error mapping, taken from `providers` so the two cannot
drift apart.

One difference is forced by the SDK: it has no `temperature` (removed from
`messages.create` in 1.0, and 2.x sends API version 2026-10-21, which rejects
the parameter on every model), so requests sample at the model's default.

The SDK's own retries are disabled (`max_retries=0`) because `RetryPolicy`
already retries, and stacking the two would multiply attempts on a failing
request. Anything the SDK raises that is not an API response or a network
failure (a TypeError from an SDK version that does not take a parameter, say)
is a non-retryable ProviderError: sending the same call again cannot help.
The SDK is imported inside the constructor so the core package never imports
it unless this provider is chosen.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from typing import Any

from devdocs.errors import ConfigError, DevDocsError, ProviderError
from devdocs.llm import Completion, Request
from devdocs.llm.catalog import find
from devdocs.llm.providers import anthropic_error, decode_message, encode_messages

__all__ = ["AnthropicSDKProvider", "sdk_available"]

_SDK_HINT = "pip install 'devdocs[anthropic]', or use provider = 'anthropic'"


def sdk_available() -> bool:
    try:
        return importlib.util.find_spec("anthropic") is not None
    except (ImportError, ValueError):
        return False


class AnthropicSDKProvider:
    """`ChatModel` backed by `anthropic.Anthropic`.

    `cache` marks the system block for prompt caching, as the raw provider does.
    """

    provider = "anthropic"

    def __init__(
        self, api_key: str, *, timeout: float = 120.0, cache: bool = True
    ) -> None:
        if not sdk_available():
            raise ConfigError(
                "provider 'anthropic-sdk' needs the anthropic package", hint=_SDK_HINT
            )
        try:
            import anthropic

            client = anthropic.Anthropic(
                api_key=api_key, max_retries=0, timeout=timeout
            )
        except Exception as err:
            raise ConfigError(
                f"could not start the anthropic SDK: {type(err).__name__}: {err}",
                hint=_SDK_HINT,
            ) from err
        self._sdk: Any = anthropic
        self._client: Any = client
        self.cache = cache

    def generate(self, request: Request) -> Completion:
        params, betas = encode_messages(
            request, find(request.model), cache=self.cache, sampling=False
        )
        try:
            if betas:
                messages = self._client.beta.messages
                response = messages.create(betas=list(betas), **params)
            else:
                response = self._client.messages.create(**params)
        except Exception as err:
            raise self._error(err) from err
        return decode_message(response, request.model, self.provider)

    def list_models(self) -> list[str]:
        """Model ids the key can call; iterating the page fetches every page."""
        try:
            models = list(self._client.models.list(limit=1000))
        except Exception as err:
            raise self._error(err) from err
        ids = (getattr(model, "id", None) for model in models)
        return sorted({found for found in ids if isinstance(found, str) and found})

    def _error(self, err: Exception) -> DevDocsError:
        """Map the SDK's exceptions as `providers` maps the same raw response."""
        sdk = self._sdk
        message = str(getattr(err, "message", "") or err)
        if isinstance(err, sdk.APIStatusError):
            status = getattr(err, "status_code", 0)
            headers = getattr(getattr(err, "response", None), "headers", None)
            retry_after = (
                headers.get("retry-after") if isinstance(headers, Mapping) else None
            )
            return anthropic_error(
                status if isinstance(status, int) else 0,
                getattr(err, "body", None),
                retry_after if isinstance(retry_after, str) else None,
                fallback=message,
                provider=self.provider,
            )
        if isinstance(err, sdk.APIConnectionError):   # includes APITimeoutError
            return ProviderError(f"network error: {message}", provider=self.provider)
        return ProviderError(
            f"{type(err).__name__}: {message}",
            provider=self.provider,
            retryable=False,
            hint="the installed anthropic SDK may not match devdocs; "
            "provider = 'anthropic' needs no SDK",
        )
