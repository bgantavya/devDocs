"""The SDK-backed provider, against a stand-in `anthropic` module.

The real SDK is an optional extra, so these tests install a fake with the
shapes of the real one (anthropic-sdk-python, src/anthropic/_exceptions.py and
_client.py): the exception hierarchy and constructors, the status -> class
mapping and "Error code: N - {body}" messages of `_make_status_error`, and
`messages.create` / `beta.messages.create` signatures that take only their
documented keywords, so a parameter the SDK dropped (temperature, in 1.0)
raises TypeError here exactly as it does there. The tests then check that
requests, responses and errors come out as the raw-HTTP provider's do.
"""

from __future__ import annotations

import importlib.machinery
import json
import sys
import types
import unittest
from collections.abc import Mapping
from typing import Any

from devdocs.errors import (
    AuthError,
    ConfigError,
    DevDocsError,
    ModelNotFound,
    ProviderError,
    QuotaExhausted,
    RateLimited,
)
from devdocs.llm import Finish, Request, Usage
from devdocs.llm.catalog import find
from devdocs.llm.http import HttpResponse
from devdocs.llm.providers import AnthropicProvider

# Keyword parameters of Messages.create / Beta.Messages.create (anthropic 2.3.0).
_MESSAGES = frozenset({
    "max_tokens", "messages", "model", "cache_control", "container", "diagnostics",
    "inference_geo", "metadata", "output_config", "service_tier", "speed",
    "stop_sequences", "stream", "system", "thinking", "tool_choice", "tools",
    "user_profile_id", "workspace_id", "extra_headers", "extra_query", "extra_body",
    "timeout",
})
_BETA = _MESSAGES | {"compaction", "context_management", "fallback_credit_token",
                     "fallbacks", "mcp_servers", "thread", "betas"}


class AnthropicError(Exception):
    pass


class APIError(AnthropicError):
    def __init__(self, message: str, request: Any, *, body: object | None) -> None:
        super().__init__(message)
        self.request = request
        self.message = message
        self.body = body


class APIStatusError(APIError):
    def __init__(self, message: str, *, response: Any, body: object | None) -> None:
        super().__init__(message, response.request, body=body)
        self.response = response
        self.status_code = response.status_code


class APIConnectionError(APIError):
    def __init__(self, *, message: str = "Connection error.", request: Any) -> None:
        super().__init__(message, request, body=None)


class APITimeoutError(APIConnectionError):
    def __init__(self, request: Any) -> None:
        super().__init__(message="Request timed out or interrupted.", request=request)


class BadRequestError(APIStatusError): ...
class AuthenticationError(APIStatusError): ...
class PermissionDeniedError(APIStatusError): ...
class NotFoundError(APIStatusError): ...
class RequestTooLargeError(APIStatusError): ...
class RateLimitError(APIStatusError): ...
class OverloadedError(APIStatusError): ...
class InternalServerError(APIStatusError): ...


_BY_STATUS: dict[int, type[APIStatusError]] = {
    400: BadRequestError, 401: AuthenticationError, 403: PermissionDeniedError,
    404: NotFoundError, 413: RequestTooLargeError, 429: RateLimitError,
    529: OverloadedError,
}


def status_error(
    status: int, body: object, headers: dict[str, str] | None = None
) -> APIStatusError:
    """What the SDK raises for this HTTP response (_make_status_error)."""
    response = types.SimpleNamespace(status_code=status, headers=dict(headers or {}),
                                     request=object())
    cls = _BY_STATUS.get(status) or (
        InternalServerError if status >= 500 else APIStatusError)
    return cls(f"Error code: {status} - {body}", response=response, body=body)


def _fake_sdk(recorder: dict[str, Any]) -> types.ModuleType:
    module = types.ModuleType("anthropic")
    module.__spec__ = importlib.machinery.ModuleSpec("anthropic", None)
    for cls in (AnthropicError, APIError, APIStatusError, APIConnectionError,
                APITimeoutError, *_BY_STATUS.values(), InternalServerError):
        setattr(module, cls.__name__, cls)

    def endpoint(allowed: frozenset[str], name: str) -> Any:
        def create(**params: Any) -> Any:
            unknown = set(params) - allowed
            if unknown:
                raise TypeError(f"{name}.create() got an unexpected keyword "
                                f"argument {min(unknown)!r}")
            recorder["params"] = params
            recorder["endpoint"] = name
            outcome = recorder.get("raise")
            if outcome is not None:
                raise outcome
            return recorder["response"]
        return create

    class Anthropic:
        def __init__(self, **options: Any) -> None:
            if recorder.get("construct_raises") is not None:
                raise recorder["construct_raises"]
            recorder["options"] = options
            messages = endpoint(_MESSAGES, "Messages")
            beta = endpoint(_BETA, "Beta.Messages")
            self.messages = types.SimpleNamespace(create=messages)
            self.beta = types.SimpleNamespace(
                messages=types.SimpleNamespace(create=beta))

            def listing(*, limit: int = 20) -> list[Any]:
                recorder["list_limit"] = limit
                if recorder.get("raise") is not None:
                    raise recorder["raise"]
                return [types.SimpleNamespace(id="claude-opus-5"),
                        types.SimpleNamespace(id="claude-haiku-4-5-20251001")]
            self.models = types.SimpleNamespace(list=listing)

    vars(module)["Anthropic"] = Anthropic
    return module


def _namespace(value: Any) -> Any:
    """A decoded JSON response as attribute objects, the way the SDK returns it."""
    if isinstance(value, Mapping):
        return types.SimpleNamespace(**{k: _namespace(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_namespace(item) for item in value]
    return value


class _NoTransport:
    """For a raw provider that only encodes, decodes or maps errors."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        raise AssertionError("no request expected")


def _raw() -> AnthropicProvider:
    return AnthropicProvider("k", _NoTransport())


# Opus 5 declined mid-output (billed) and Opus 4.8 answered, as documented in
# build-with-claude/refusals-and-fallback; both bill at $5 / $25 per MTok.
_FALLBACK = {
    "model": "claude-opus-4-8",
    "content": [
        {"type": "fallback", "from": {"model": "claude-opus-5"},
         "to": {"model": "claude-opus-4-8"},
         "trigger": {"type": "refusal", "category": "cyber"}},
        {"type": "text", "text": "Architecture overview"},
    ],
    "stop_reason": "end_turn",
    "stop_details": None,
    "usage": {
        "input_tokens": 20_000, "output_tokens": 1_200,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "iterations": [
            {"type": "message", "model": "claude-opus-5", "input_tokens": 20_000,
             "output_tokens": 1_500, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0},
            {"type": "fallback_message", "model": "claude-opus-4-8",
             "input_tokens": 20_000, "output_tokens": 1_200,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        ],
    },
}


def _response(text: str = "Doc", stop: str = "end_turn", model: str = "") -> Any:
    return types.SimpleNamespace(
        stop_reason=stop,
        stop_details=None,
        model=model,
        content=[types.SimpleNamespace(type="text", text=text)],
        usage=types.SimpleNamespace(input_tokens=10, output_tokens=5,
                                    cache_read_input_tokens=90,
                                    cache_creation_input_tokens=None),
    )


class SDKCase(unittest.TestCase):
    def setUp(self) -> None:
        self.recorder: dict[str, Any] = {"response": _response()}
        self.sdk = _fake_sdk(self.recorder)
        self.addCleanup(sys.modules.pop, "anthropic", None)
        sys.modules["anthropic"] = self.sdk
        from devdocs.llm.anthropic_sdk import AnthropicSDKProvider

        self.provider_class = AnthropicSDKProvider
        self.provider = AnthropicSDKProvider("key")

    def request(self, model: str) -> Request:
        return Request(model=model, system="SYS", prompt="p", temperature=0.2,
                       effort="low", max_output_tokens=400)


class TestConstruction(SDKCase):
    def test_sdk_retries_are_disabled(self) -> None:
        self.assertEqual(self.recorder["options"]["max_retries"], 0)
        self.assertEqual(self.recorder["options"]["api_key"], "key")

    def test_missing_sdk_is_a_config_error(self) -> None:
        sys.modules.pop("anthropic", None)
        from devdocs.llm import anthropic_sdk

        original = anthropic_sdk.sdk_available
        anthropic_sdk.sdk_available = lambda: False
        self.addCleanup(setattr, anthropic_sdk, "sdk_available", original)
        with self.assertRaises(ConfigError) as caught:
            anthropic_sdk.AnthropicSDKProvider("key")
        self.assertEqual(caught.exception.hint, anthropic_sdk.SDK_HINT)   # doctor's too

    def test_failing_sdk_client_is_a_config_error(self) -> None:
        self.recorder["construct_raises"] = AnthropicError("api_key must be set")
        with self.assertRaises(ConfigError):
            self.provider_class("")

    def test_what_it_shares_with_the_raw_provider_is_public(self) -> None:
        # Regression: it imported them as private names of providers.
        from devdocs.llm import anthropic_sdk, providers

        for name in ("encode_messages", "decode_message", "anthropic_error"):
            with self.subTest(name):
                self.assertIn(name, providers.__all__)
                self.assertIs(getattr(anthropic_sdk, name), getattr(providers, name))


class TestRequests(SDKCase):
    def test_parameters_follow_the_catalog(self) -> None:
        self.provider.generate(self.request("claude-sonnet-5"))
        params = self.recorder["params"]
        self.assertEqual(self.recorder["endpoint"], "Messages")
        self.assertNotIn("temperature", params)
        self.assertEqual(params["output_config"], {"effort": "low"})
        self.assertNotIn("fallbacks", params)

    def test_cache_breakpoint_is_on_the_system_block(self) -> None:
        self.provider.generate(self.request("claude-sonnet-5"))
        params = self.recorder["params"]
        self.assertNotIn("cache_control", params)
        self.assertEqual(params["system"], [{"type": "text", "text": "SYS",
                                             "cache_control": {"type": "ephemeral"}}])
        uncached = self.provider_class("key", cache=False)
        uncached.generate(self.request("claude-sonnet-5"))
        self.assertNotIn("cache_control", json.dumps(self.recorder["params"]))

    def test_opus_goes_through_beta_with_fallbacks(self) -> None:
        self.provider.generate(self.request("claude-opus-5"))
        params = self.recorder["params"]
        self.assertEqual(self.recorder["endpoint"], "Beta.Messages")
        self.assertEqual(params["fallbacks"], "default")
        self.assertEqual(params["betas"], ["server-side-fallback-2026-07-01"])

    def test_haiku_sends_no_temperature_and_no_effort(self) -> None:
        # The SDK removed temperature in 1.0: passing it is a TypeError.
        completion = self.provider.generate(self.request("claude-haiku-4-5"))
        params = self.recorder["params"]
        self.assertEqual(completion.text, "Doc")
        self.assertNotIn("temperature", params)
        self.assertNotIn("output_config", params)

    def test_same_body_as_the_raw_provider_bar_temperature(self) -> None:
        for model in ("claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"):
            request = self.request(model)
            self.provider.generate(request)
            sent = dict(self.recorder["params"])
            betas = sent.pop("betas", [])
            raw, raw_betas = _raw().encode(request, find(model))
            raw.pop("temperature", None)
            self.assertEqual(sent, raw, model)
            self.assertEqual(betas, list(raw_betas), model)


class TestResponses(SDKCase):
    def test_decode_counts_cached_input(self) -> None:
        completion = self.provider.generate(self.request("claude-haiku-4-5"))
        self.assertEqual(completion.text, "Doc")
        self.assertEqual(completion.usage.input_tokens, 100)
        self.assertEqual(completion.usage.cached_input_tokens, 90)

    def test_refusal_has_no_text(self) -> None:
        self.recorder["response"] = _response("partial", stop="refusal")
        completion = self.provider.generate(self.request("claude-opus-5"))
        self.assertIsNone(completion.text)
        self.assertIs(completion.finish, Finish.REFUSED)

    def test_context_window_stop_is_a_truncation(self) -> None:
        self.recorder["response"] = _response(stop="model_context_window_exceeded")
        completion = self.provider.generate(self.request("claude-sonnet-5"))
        self.assertIs(completion.finish, Finish.LENGTH)

    def test_declined_fallback_attempt_is_billed_like_raw(self) -> None:
        self.recorder["response"] = _namespace(_FALLBACK)
        completion = self.provider.generate(self.request("claude-opus-5"))
        self.assertEqual(completion, _raw().decode(_FALLBACK, "claude-opus-5"))
        self.assertEqual(completion.model, "claude-opus-4-8")
        self.assertEqual(completion.usage, Usage(input_tokens=40_000,
                                                 output_tokens=2_700))

    def test_list_models_asks_for_large_pages(self) -> None:
        self.assertEqual(self.provider.list_models(),
                         ["claude-haiku-4-5-20251001", "claude-opus-5"])
        self.assertEqual(self.recorder["list_limit"], 1000)


SPEND_CAP = {"type": "error", "error": {
    "type": "rate_limit_error",
    "message": "You have reached your API usage limits: your organization has crossed "
               "its monthly API usage threshold.",
    "details": {"error_code": "enforced_spend_limit_reached"}}}


class TestErrors(SDKCase):
    def mapped(self, error: Exception, model: str = "claude-haiku-4-5") -> Exception:
        self.recorder["raise"] = error
        try:
            self.provider.generate(self.request(model))
        except Exception as err:
            return err
        raise AssertionError("expected an error")

    def body(self, kind: str, message: str = "m") -> dict[str, Any]:
        return {"type": "error", "error": {"type": kind, "message": message}}

    def test_typed_exceptions_map_like_raw_http(self) -> None:
        cases = [
            (status_error(401, self.body("authentication_error")), AuthError),
            (status_error(403, self.body("permission_error")), AuthError),
            (status_error(404, self.body("not_found_error")), ModelNotFound),
            (status_error(429, self.body("rate_limit_error"), {"retry-after": "4"}),
             RateLimited),
            (status_error(402, self.body("billing_error")), QuotaExhausted),
            (status_error(529, self.body("overloaded_error")), ProviderError),
            (APIConnectionError(request=object()), ProviderError),
            (APITimeoutError(object()), ProviderError),
        ]
        for raised, expected in cases:
            self.assertIsInstance(self.mapped(raised), expected, type(raised).__name__)

    def test_same_errors_as_the_raw_provider(self) -> None:
        cases = [
            (401, self.body("authentication_error"), {}),
            (402, self.body("billing_error"), {}),
            (403, self.body("permission_error"), {}),
            (404, self.body("not_found_error"), {}),
            (413, self.body("request_too_large"), {}),
            (429, self.body("rate_limit_error"), {"retry-after": "4"}),
            (429, SPEND_CAP, {}),
            (400, self.body("invalid_request_error",
                            "You have reached your specified API usage limits."), {}),
            (400, self.body("invalid_request_error"), {}),
            (500, self.body("api_error"), {}),
            (504, self.body("timeout_error"), {}),
            (529, self.body("overloaded_error"), {}),
            (529, self.body("overloaded_error"), {"retry-after": "4"}),
            (503, "upstream connect error", {}),
            (503, "upstream connect error", {"retry-after": "7"}),
        ]
        for status, body, headers in cases:
            via_sdk = self.mapped(status_error(status, body, headers))
            response = HttpResponse(status, headers, json.dumps(body).encode())
            raw = _raw()._error(response)
            assert isinstance(via_sdk, DevDocsError)
            summary = (type(via_sdk), getattr(via_sdk, "status", None),
                       getattr(via_sdk, "retryable", None),
                       getattr(via_sdk, "retry_after", None))
            expected = (type(raw), getattr(raw, "status", None),
                        getattr(raw, "retryable", None),
                        getattr(raw, "retry_after", None))
            self.assertEqual(summary, expected, f"{status} {body}")

    def test_message_is_the_api_message_not_the_sdk_summary(self) -> None:
        err = self.mapped(status_error(404, self.body("not_found_error",
                                                      "model: claude-x")))
        assert isinstance(err, DevDocsError)
        self.assertEqual(err.message, "model: claude-x")

    def test_spend_cap_is_account_wide(self) -> None:
        err = self.mapped(status_error(429, SPEND_CAP))
        assert isinstance(err, QuotaExhausted)
        self.assertEqual(err.status, 402)

    def test_rate_limit_keeps_retry_after(self) -> None:
        err = self.mapped(status_error(429, self.body("rate_limit_error"),
                                       {"retry-after": "4"}))
        assert isinstance(err, RateLimited)
        self.assertEqual(err.retry_after, 4.0)

    def test_a_503_keeps_its_retry_after(self) -> None:
        err = self.mapped(status_error(503, "busy", {"retry-after": "7"}))
        assert isinstance(err, ProviderError)
        self.assertEqual((err.retry_after, err.exit_code), (7.0, 4))

    def test_overload_and_timeouts_are_retryable(self) -> None:
        for raised in (status_error(529, self.body("overloaded_error")),
                       APITimeoutError(object())):
            err = self.mapped(raised)
            assert isinstance(err, ProviderError)
            self.assertTrue(err.retryable, type(raised).__name__)

    def test_unexpected_error_attributes_still_map(self) -> None:
        odd = status_error(503, "busy")
        odd.status_code = "503"
        odd.response = types.SimpleNamespace(headers=None, request=None)
        self.assertIsInstance(self.mapped(odd), ProviderError)

    def test_non_api_failures_are_not_retried(self) -> None:
        err = self.mapped(TypeError("create() got an unexpected keyword argument"))
        assert isinstance(err, ProviderError)
        self.assertFalse(err.retryable)

    def test_list_models_maps_errors(self) -> None:
        self.recorder["raise"] = status_error(401, self.body("authentication_error"))
        with self.assertRaises(AuthError):
            self.provider.list_models()


if __name__ == "__main__":
    unittest.main()
