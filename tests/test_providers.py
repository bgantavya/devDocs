"""Provider encoding, decoding and error mapping, against recorded responses.

No network: `Recorded` stands in for the transport and captures what would
have been sent, so encoding is asserted on the exact wire payload. Error and
response bodies follow the vendors' documented examples.
"""

from __future__ import annotations

import functools
import json
import unittest
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

from devdocs.errors import (
    AuthError,
    DevDocsError,
    ModelNotFound,
    ProviderError,
    QuotaExhausted,
    RateLimited,
)
from devdocs.llm import Finish, Request, Usage
from devdocs.llm.budget import Router
from devdocs.llm.catalog import Tier, find
from devdocs.llm.client import LLMClient
from devdocs.llm.http import HttpResponse, RetryPolicy
from devdocs.llm.providers import AnthropicProvider, GeminiProvider


class Recorded:
    """A transport that replays canned responses and records requests."""

    def __init__(self, *responses: tuple[int, Any, dict[str, str]]) -> None:
        self.responses = list(responses)
        self.sent: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        self.sent.append({
            "method": method, "url": url, "headers": dict(headers or {}),
            "json": json.loads(body) if body else None,
        })
        status, payload, response_headers = self.responses.pop(0)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return HttpResponse(status, response_headers, raw)


def replay(
    status: int, payload: Any, headers: dict[str, str] | None = None
) -> Recorded:
    return Recorded((status, payload, headers or {}))


def raised(call: Any) -> DevDocsError:
    try:
        call()
    except DevDocsError as err:
        return err
    raise AssertionError("expected an error")


GEMINI = Request(model="gemini-2.5-flash-lite", system="SYS", prompt="hi",
                 max_output_tokens=300, temperature=0.2)
CLAUDE = Request(model="claude-sonnet-5", system="SYS", prompt="hi",
                 max_output_tokens=500, temperature=0.2, effort="low")
OPUS = Request(model="claude-opus-5", system="SYS", prompt="hi",
               max_output_tokens=2000, effort="low")


class TestGeminiGenerations(unittest.TestCase):
    def config(self, model: str) -> dict[str, Any]:
        request = Request(model=model, system="S", prompt="p", temperature=0.2)
        provider = GeminiProvider("k", replay(200, {}))
        return dict(provider.encode(request, find(model))["generationConfig"])

    def test_thinking_budget_for_2_5_and_level_for_later_models(self) -> None:
        expected = {
            "gemini-2.5-flash-lite": {"temperature": 0.2,
                                      "thinkingConfig": {"thinkingBudget": 0}},
            "gemini-3.1-flash-lite": {},
            "gemini-3.5-flash-lite": {"thinkingConfig": {"thinkingLevel": "minimal"}},
            "gemini-3.8-flash": {"thinkingConfig": {"thinkingLevel": "low"}},
            "gemini-3.1-pro-preview": {"thinkingConfig": {"thinkingLevel": "low"}},
            "gemini-4-argon": {"thinkingConfig": {"thinkingLevel": "low"}},
        }
        for model, extra in expected.items():
            with self.subTest(model=model):
                config = self.config(model)
                config.pop("maxOutputTokens")
                self.assertEqual(config, extra)


class TestGeminiEncoding(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = replay(200, {"candidates": []})
        GeminiProvider("key", self.transport).generate(GEMINI)
        self.sent = self.transport.sent[0]

    def test_endpoint_and_key_header(self) -> None:
        self.assertTrue(self.sent["url"].endswith(
            "/v1beta/models/gemini-2.5-flash-lite:generateContent"))
        self.assertEqual(self.sent["headers"]["x-goog-api-key"], "key")
        self.assertNotIn("key=", self.sent["url"])

    def test_system_and_prompt_are_separate(self) -> None:
        body = self.sent["json"]
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "SYS")
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "hi")

    def test_thinking_off_and_output_cap(self) -> None:
        config = self.sent["json"]["generationConfig"]
        self.assertEqual(config["maxOutputTokens"], 300)
        self.assertEqual(config["thinkingConfig"], {"thinkingBudget": 0})
        self.assertEqual(config["temperature"], 0.2)

    def test_pro_floors_thinking_rather_than_disabling(self) -> None:
        body = GeminiProvider("k", replay(200, {})).encode(
            GEMINI, find("gemini-2.5-pro"))
        thinking = body["generationConfig"]["thinkingConfig"]
        self.assertEqual(thinking["thinkingBudget"], 128)

    def test_unknown_model_gets_no_sampling_or_thinking_parameters(self) -> None:
        # Gemini 3.6+ ignores temperature and is to start rejecting it.
        body = GeminiProvider("k", replay(200, {})).encode(GEMINI, None)
        self.assertEqual(body["generationConfig"], {"maxOutputTokens": 300})

    def test_models_prefix_is_not_doubled_in_the_url(self) -> None:
        transport = replay(200, {"candidates": []})
        request = Request(model="models/gemini-2.5-flash", system="S", prompt="p")
        GeminiProvider("k", transport).generate(request)
        self.assertTrue(transport.sent[0]["url"].endswith(
            "/v1beta/models/gemini-2.5-flash:generateContent"))


class TestGeminiDecoding(unittest.TestCase):
    def decode(self, body: dict[str, Any]) -> Any:
        return GeminiProvider("k", replay(200, body)).generate(GEMINI)

    def test_text_usage_and_thought_parts(self) -> None:
        completion = self.decode({
            "candidates": [{"content": {"parts": [
                {"text": "reasoning", "thought": True}, {"text": "Doc"}]},
                "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 5,
                              "cachedContentTokenCount": 10, "thoughtsTokenCount": 3},
            "modelVersion": "gemini-2.5-flash-lite",
        })
        self.assertEqual(completion.text, "Doc")
        self.assertIs(completion.finish, Finish.STOP)
        self.assertEqual(completion.usage.cached_input_tokens, 10)
        self.assertEqual(completion.usage.thinking_tokens, 3)

    def test_prompt_count_already_includes_cached_and_tool_prompt_adds(self) -> None:
        completion = self.decode({
            "candidates": [{"content": {"parts": [{"text": "Doc"}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 1000, "cachedContentTokenCount": 900,
                              "toolUsePromptTokenCount": 20,
                              "candidatesTokenCount": 10, "totalTokenCount": 1030},
        })
        self.assertEqual(completion.usage.input_tokens, 1020)
        self.assertEqual(completion.usage.cached_input_tokens, 900)

    def test_safety_with_no_parts_is_none_not_a_crash(self) -> None:
        completion = self.decode({"candidates": [{"finishReason": "SAFETY"}]})
        self.assertIsNone(completion.text)
        self.assertIs(completion.finish, Finish.BLOCKED)
        self.assertFalse(completion.ok)

    def test_blocked_prompt(self) -> None:
        completion = self.decode({"promptFeedback": {"blockReason": "OTHER"}})
        self.assertIs(completion.finish, Finish.BLOCKED)
        self.assertIn("OTHER", completion.detail)

    def test_truncated_text_is_still_usable(self) -> None:
        completion = self.decode({"candidates": [{
            "content": {"parts": [{"text": "partial"}]},
            "finishReason": "MAX_TOKENS",
        }]})
        self.assertTrue(completion.ok)
        self.assertIs(completion.finish, Finish.LENGTH)

    def test_policy_and_account_verdicts_are_blocks(self) -> None:
        for reason in ("LANGUAGE", "ESCALATION", "PUP_LIMITED_DISABLED"):
            completion = self.decode({"candidates": [{"finishReason": reason}]})
            self.assertIs(completion.finish, Finish.BLOCKED, reason)

    def test_continuation_is_a_truncation(self) -> None:
        completion = self.decode({"candidates": [{
            "content": {"parts": [{"text": "first part"}]},
            "finishReason": "CONTINUATION", "continuationToken": "t"}]})
        self.assertIs(completion.finish, Finish.LENGTH)
        self.assertTrue(completion.ok)

    def test_null_fields_read_as_absent(self) -> None:
        completion = self.decode({
            "candidates": [{"content": {"parts": [{"text": None}, {"text": "Doc"},
                                                  {"functionCall": {"name": "f"}}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 7,
                              "cachedContentTokenCount": None,
                              "thoughtsTokenCount": None},
        })
        self.assertEqual(completion.text, "Doc")
        self.assertEqual(completion.usage, Usage(input_tokens=40, output_tokens=7))

    def test_malformed_shapes_do_not_crash(self) -> None:
        completion = self.decode({"candidates": ["x", {"content": "y"}],
                                  "usageMetadata": [1], "promptFeedback": "z"})
        self.assertIsNone(completion.text)
        self.assertEqual(completion.usage, Usage())

    def test_unreadable_200_is_a_retryable_error(self) -> None:
        cut = b'{"candidates": [{"content": {"parts": [{"te'
        provider = GeminiProvider("k", Recorded((200, cut, {})))
        err = raised(lambda: provider.generate(GEMINI))
        assert isinstance(err, ProviderError)
        self.assertTrue(err.retryable)


VENDORS: tuple[tuple[Any, Request, Any], ...] = (
    (GeminiProvider, GEMINI, {"candidates": []}),
    (AnthropicProvider, CLAUDE, {"content": [], "stop_reason": "end_turn"}),
)


class TestServerDelay(unittest.TestCase):
    """A server error that says how long to wait is waited for, by either vendor."""

    def slept(self, vendor: tuple[Any, Request, Any], status: int,
              headers: dict[str, str]) -> list[float]:
        """What `RetryPolicy` sleeps through a failure, then an answer."""
        make, request, answer = vendor
        transport = Recorded((status, {}, headers), (200, answer, {}))
        provider = make("k", transport)
        slept: list[float] = []
        RetryPolicy().run(lambda: provider.generate(request), sleep=slept.append)
        self.assertEqual(len(transport.sent), 2)
        return slept

    def test_a_503_with_retry_after_is_waited_for(self) -> None:
        for vendor in VENDORS:
            with self.subTest(vendor[0].__name__):
                self.assertEqual(self.slept(vendor, 503, {"retry-after": "7"}), [7.0])

    def test_a_503_with_a_date_is_waited_for(self) -> None:
        date = "Wed, 21 Oct 2015 07:28:00 GMT"   # past: at once
        for vendor in VENDORS:
            with self.subTest(vendor[0].__name__):
                self.assertEqual(self.slept(vendor, 503, {"retry-after": date}), [0.0])

    def test_a_503_without_one_backs_off(self) -> None:
        for vendor in VENDORS:
            for headers in ({}, {"retry-after": "soon"}):
                with self.subTest(vendor[0].__name__, headers=headers):
                    (pause,) = self.slept(vendor, 503, headers)
                    self.assertLessEqual(pause, RetryPolicy().base_delay)

    def test_a_503_asking_too_long_a_wait_is_not_sat_out(self) -> None:
        for make, request, _ in VENDORS:
            with self.subTest(make.__name__):
                transport = Recorded((503, {}, {"retry-after": "3600"}))
                send = functools.partial(make("k", transport).generate, request)
                slept: list[float] = []
                with self.assertRaises(ProviderError) as caught:
                    RetryPolicy().run(send, sleep=slept.append)
                error = caught.exception
                self.assertEqual((error.status, error.retry_after), (503, 3600.0))
                self.assertEqual((len(transport.sent), slept), (1, []))

    def test_a_503_stays_a_provider_failure_with_its_exit_code(self) -> None:
        for make, request, _ in VENDORS:
            with self.subTest(make.__name__):
                provider = make("k", replay(503, {}, {"retry-after": "7"}))
                err = raised(functools.partial(provider.generate, request))
                self.assertIs(type(err), ProviderError)
                self.assertEqual(err.exit_code, 4)

    def test_gemini_retry_info_comes_first_as_for_a_429(self) -> None:
        details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "3s"}]
        provider = GeminiProvider("k", replay(
            503, {"error": {"status": "UNAVAILABLE", "details": details}},
            {"retry-after": "7"}))
        err = raised(lambda: provider.generate(GEMINI))
        assert isinstance(err, ProviderError)
        self.assertEqual(err.retry_after, 3.0)

    def test_an_anthropic_overload_with_retry_after_is_waited_for(self) -> None:
        self.assertEqual(self.slept(VENDORS[1], 529, {"retry-after": "4"}), [4.0])


class TestGeminiErrors(unittest.TestCase):
    def error(self, status: int, error: Any,
              headers: dict[str, str] | None = None) -> DevDocsError:
        provider = GeminiProvider("k", replay(status, {"error": error}, headers))
        return raised(lambda: provider.generate(GEMINI))

    def test_invalid_key_arrives_as_400_but_is_auth(self) -> None:
        message = "API key not valid. Please pass a valid API key."
        err = self.error(
            400, {"code": 400, "status": "INVALID_ARGUMENT", "message": message}
        )
        self.assertIsInstance(err, AuthError)

    def test_invalid_key_error_info_is_auth(self) -> None:
        # Recorded from the live API (key redacted): reason API_KEY_INVALID.
        err = self.error(400, {
            "code": 400, "status": "INVALID_ARGUMENT", "message": "API key expired.",
            "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                         "reason": "API_KEY_INVALID", "domain": "googleapis.com"}]})
        self.assertIsInstance(err, AuthError)

    def test_permission_denied_is_auth(self) -> None:
        err = self.error(403, {"status": "PERMISSION_DENIED"})
        self.assertIsInstance(err, AuthError)

    def test_missing_model(self) -> None:
        self.assertIsInstance(self.error(404, {"status": "NOT_FOUND"}), ModelNotFound)

    def test_rate_limit_carries_retry_delay_from_details(self) -> None:
        for delay, seconds in (("37s", 37.0), ("1.234567s", 1.234567), ("0s", 0.0)):
            retry_info = {
                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                "retryDelay": delay,
            }
            err = self.error(429, {"status": "RESOURCE_EXHAUSTED",
                                   "details": [retry_info]})
            assert isinstance(err, RateLimited)
            self.assertEqual(err.retry_after, seconds, delay)

    def test_daily_quota_is_not_a_rate_limit(self) -> None:
        # A daily quota still carries a short RetryInfo; the quotaId decides.
        quota = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
        err = self.error(429, {"status": "RESOURCE_EXHAUSTED", "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaId": quota}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo",
             "retryDelay": "34s"}]})
        assert isinstance(err, QuotaExhausted)
        self.assertNotEqual(err.status, 402)   # retires this model only

    def test_per_minute_quota_is_a_rate_limit(self) -> None:
        err = self.error(429, {"status": "RESOURCE_EXHAUSTED", "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
                {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]})
        self.assertIsInstance(err, RateLimited)

    def test_prepay_402_is_account_wide(self) -> None:
        err = self.error(402, {"code": 402, "status": "RESOURCE_EXHAUSTED",
                               "message": "Your prepayment credits are depleted."})
        assert isinstance(err, QuotaExhausted)
        self.assertEqual(err.status, 402)

    def test_monthly_spend_cap_is_account_wide(self) -> None:
        for message in ("Your project has exceeded its monthly spending cap.",
                        "Your billing account has exceeded its monthly spending cap"):
            err = self.error(429, {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                   "message": message})
            assert isinstance(err, QuotaExhausted)
            self.assertEqual(err.status, 402, message)

    def test_spend_cap_stops_the_run_after_one_request(self) -> None:
        message = "Your project has exceeded its monthly spending cap."
        body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                          "message": message}}
        transport = Recorded(*[(429, body, {})] * 12)
        client = LLMClient(GeminiProvider("k", transport), router=Router("gemini"),
                           retry=RetryPolicy(base_delay=0.0), free_tier=False)
        with self.assertRaises(QuotaExhausted):
            client.complete(Tier.FILE, "SYS", "doc")
        self.assertEqual(len(transport.sent), 1)

    def test_server_error_is_retryable(self) -> None:
        err = self.error(503, {"status": "UNAVAILABLE"})
        assert isinstance(err, ProviderError)
        self.assertTrue(err.retryable)

    def test_non_json_error_body_still_maps(self) -> None:
        provider = GeminiProvider("k", Recorded((502, "<html>bad gateway</html>", {})))
        err = raised(lambda: provider.generate(GEMINI))
        assert isinstance(err, ProviderError)
        self.assertEqual(err.status, 502)

    def test_error_that_is_not_an_object_maps_by_status(self) -> None:
        for status, error, expected in ((503, "Service Unavailable", ProviderError),
                                        (429, ["Too many requests"], RateLimited),
                                        (500, 500, ProviderError)):
            err = self.error(status, error)
            assert isinstance(err, ProviderError)
            self.assertIsInstance(err, expected)
            self.assertEqual(err.status, status)
            self.assertTrue(err.retryable)


class TestGeminiModelsAndEmbeddings(unittest.TestCase):
    def test_list_models_paginates_and_filters(self) -> None:
        transport = Recorded(
            (200, {"models": [
                {"name": "models/gemini-2.5-flash",
                 "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/aqa",
                 "supportedGenerationMethods": ["generateAnswer"]},
            ], "nextPageToken": "p2"}, {}),
            (200, {"models": [
                {"name": "models/gemini-embedding-001",
                 "supportedGenerationMethods": ["embedContent"]}]}, {}),
        )
        models = GeminiProvider("k", transport).list_models()
        self.assertEqual(models, ["gemini-2.5-flash", "gemini-embedding-001"])
        self.assertIn("pageToken=p2", transport.sent[1]["url"])

    def test_page_token_is_url_encoded(self) -> None:
        token = "AbC+d/E=&x=1"
        transport = Recorded((200, {"models": [], "nextPageToken": token}, {}),
                             (200, {"models": ["junk", {"name": None}]}, {}))
        self.assertEqual(GeminiProvider("k", transport).list_models(), [])
        query = parse_qs(urlsplit(transport.sent[1]["url"]).query)
        self.assertEqual(query["pageToken"], [token])

    def test_embeddings_are_unit_length_and_batched(self) -> None:
        transport = replay(200, {"embeddings": [{"values": [3.0, 4.0]},
                                                {"values": [0.0, 2.0]}]})
        vectors = GeminiProvider("k", transport).embed(["a", "b"], query=True)
        self.assertEqual(vectors, [[0.6, 0.8], [0.0, 1.0]])
        request = transport.sent[0]["json"]["requests"][0]
        self.assertEqual(request["taskType"], "RETRIEVAL_QUERY")
        self.assertEqual(request["outputDimensionality"], 768)
        self.assertEqual(request["model"], "models/gemini-embedding-001")

    def test_no_texts_sends_nothing(self) -> None:
        transport = Recorded()
        self.assertEqual(GeminiProvider("k", transport).embed([]), [])
        self.assertEqual(transport.sent, [])

    def test_embedding_count_mismatch_is_an_error(self) -> None:
        provider = GeminiProvider("k", replay(200, {"embeddings": []}))
        with self.assertRaises(ProviderError):
            provider.embed(["a"])

    def test_embedding_without_values_is_an_error(self) -> None:
        for embedding in ({}, {"values": None}, {"values": ["x"]}, "junk"):
            provider = GeminiProvider("k", replay(200, {"embeddings": [embedding]}))
            with self.assertRaises(ProviderError, msg=repr(embedding)):
                provider.embed(["a"])


class TestAnthropicEncoding(unittest.TestCase):
    def encode(self, model: str) -> tuple[dict[str, Any], tuple[str, ...]]:
        request = Request(model=model, system="S", prompt="p", temperature=0.2,
                          effort="low")
        return AnthropicProvider("k", replay(200, {})).encode(request, find(model))

    def body(self, model: str) -> dict[str, Any]:
        body, _ = self.encode(model)
        return {k: v for k, v in body.items() if k not in ("model", "messages",
                                                           "system", "max_tokens")}

    def test_each_model_gets_the_cheapest_parameters_it_accepts(self) -> None:
        # No model is sent temperature; thinking is off where it can be,
        # Sonnet 5.5 takes between_tools (it rejects "disabled"), and Opus 5.5
        # takes only effort, since any thinking setting is a 400 there.
        fallbacks = {"fallbacks": "default"}
        low = {"output_config": {"effort": "low"}}
        expected = {
            "claude-haiku-5-5": {**low, "thinking": {"type": "disabled"}},
            "claude-haiku-4-5": {},
            "claude-sonnet-5-5": {**low, "thinking": {"type": "between_tools"},
                                  **fallbacks},
            "claude-sonnet-5": {**low, "thinking": {"type": "disabled"}},
            "claude-opus-5-5": {**low, **fallbacks},
            "claude-opus-5": {**low, "thinking": {"type": "disabled"}, **fallbacks},
            "claude-fable-5-1": {**low, **fallbacks},
        }
        for model, extra in expected.items():
            with self.subTest(model=model):
                self.assertEqual(self.body(model), extra)

    def test_fallbacks_carry_their_beta_header(self) -> None:
        _, betas = self.encode("claude-opus-5-5")
        self.assertEqual(betas, ("server-side-fallback-2026-07-01",))
        self.assertEqual(self.encode("claude-haiku-5-5")[1], ())

    def test_unknown_model_gets_no_optional_parameters(self) -> None:
        body, _ = AnthropicProvider("k", replay(200, {})).encode(CLAUDE, None)
        self.assertNotIn("temperature", body)
        self.assertNotIn("output_config", body)

    def test_wire_headers_and_caching(self) -> None:
        transport = replay(200, {"content": [], "stop_reason": "end_turn"})
        AnthropicProvider("k", transport).generate(CLAUDE)
        sent = transport.sent[0]
        self.assertTrue(sent["url"].endswith("/v1/messages"))
        self.assertEqual(sent["headers"]["x-api-key"], "k")
        self.assertEqual(sent["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(sent["json"]["system"],
                         [{"type": "text", "text": "SYS",
                           "cache_control": {"type": "ephemeral"}}])

    def test_breakpoint_is_never_on_the_per_file_prompt(self) -> None:
        # A top-level cache_control would land on the last block, the prompt:
        # a 1.25x write on every call that no later call can read.
        body, _ = self.encode("claude-sonnet-5")
        self.assertNotIn("cache_control", body)
        self.assertEqual(body["messages"], [{"role": "user", "content": "p"}])
        self.assertEqual(body["system"][-1]["cache_control"], {"type": "ephemeral"})

    def test_cache_off_and_no_system_send_no_marker(self) -> None:
        request = Request(model="claude-sonnet-5", system="S", prompt="p")
        off, _ = AnthropicProvider("k", replay(200, {}), cache=False).encode(
            request, find("claude-sonnet-5"))
        self.assertNotIn("cache_control", json.dumps(off))
        bare = Request(model="claude-sonnet-5", system="", prompt="p")
        none, _ = AnthropicProvider("k", replay(200, {})).encode(
            bare, find("claude-sonnet-5"))
        self.assertNotIn("cache_control", json.dumps(none))
        self.assertNotIn("system", none)


def fallback_reply(
    declined_output: int, category: str | None, served: str = "claude-opus-4-8"
) -> dict[str, Any]:
    """A non-streaming server-side fallback response, as documented."""
    return {
        "model": served,
        "content": [
            {"type": "fallback", "from": {"model": "claude-opus-5"},
             "to": {"model": served},
             "trigger": {"type": "refusal", "category": category}},
            {"type": "text", "text": "Architecture overview"},
        ],
        "stop_reason": "end_turn",
        "stop_details": None,
        "usage": {
            "input_tokens": 20_000, "output_tokens": 1_200,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            "iterations": [
                {"type": "message", "model": "claude-opus-5",
                 "input_tokens": 20_000, "output_tokens": declined_output,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                {"type": "fallback_message", "model": served,
                 "input_tokens": 20_000, "output_tokens": 1_200,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            ],
        },
    }


class TestAnthropicDecoding(unittest.TestCase):
    def decode(self, body: dict[str, Any], request: Request = CLAUDE) -> Any:
        return AnthropicProvider("k", replay(200, body)).generate(request)

    def test_text_blocks_only_and_cached_usage(self) -> None:
        completion = self.decode({
            "model": "claude-sonnet-5", "stop_reason": "end_turn",
            "content": [{"type": "thinking", "thinking": ""},
                        {"type": "text", "text": "Hello"}],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 50,
                "cache_creation": {"ephemeral_5m_input_tokens": 50,
                                   "ephemeral_1h_input_tokens": 0},
                "output_tokens_details": {"thinking_tokens": 12},
            },
        })
        self.assertEqual(completion.text, "Hello")
        self.assertEqual(completion.usage.input_tokens, 1050)
        self.assertEqual(completion.usage.cached_input_tokens, 900)
        self.assertEqual(completion.usage.cache_write_tokens, 50)
        # output_tokens already includes thinking; counting it twice overbills.
        self.assertEqual(completion.usage.output_tokens, 20)
        self.assertEqual(completion.usage.thinking_tokens, 0)

    def test_refusal_discards_any_text(self) -> None:
        completion = self.decode({"stop_reason": "refusal",
                                  "content": [{"type": "text", "text": "partial"}]})
        self.assertIsNone(completion.text)
        self.assertIs(completion.finish, Finish.REFUSED)

    def test_serving_model_is_reported(self) -> None:
        completion = self.decode({"model": "claude-opus-4-8", "stop_reason": "end_turn",
                                  "content": [{"type": "text", "text": "x"}]})
        self.assertEqual(completion.model, "claude-opus-4-8")

    def test_context_window_stop_is_a_truncation(self) -> None:
        completion = self.decode({"stop_reason": "model_context_window_exceeded",
                                  "content": [{"type": "text", "text": "partial"}]})
        self.assertIs(completion.finish, Finish.LENGTH)
        self.assertTrue(completion.ok)

    def test_unknown_stop_reason_is_other(self) -> None:
        completion = self.decode({"stop_reason": "something_new",
                                  "content": [{"type": "text", "text": "x"}]})
        self.assertIs(completion.finish, Finish.OTHER)

    def test_null_and_malformed_fields_read_as_absent(self) -> None:
        completion = self.decode({
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": None}, "junk",
                        {"type": "redacted_thinking", "data": "x"},
                        {"type": "text", "text": "Doc"}],
            "usage": {"input_tokens": 10, "output_tokens": None,
                      "cache_read_input_tokens": None,
                      "cache_creation_input_tokens": "x"},
        })
        self.assertEqual(completion.text, "Doc")
        self.assertEqual(completion.usage, Usage(input_tokens=10))
        odd = self.decode({"stop_reason": None, "content": "x", "usage": [1, 2]})
        self.assertIsNone(odd.text)
        self.assertEqual(odd.usage, Usage())

    def test_unreadable_200_is_a_retryable_error(self) -> None:
        provider = AnthropicProvider("k", Recorded((200, b"<html>ok</html>", {})))
        err = raised(lambda: provider.generate(CLAUDE))
        assert isinstance(err, ProviderError)
        self.assertTrue(err.retryable)

    def test_declined_fallback_attempt_is_billed(self) -> None:
        # Opus 5 declined mid-output (non-streaming); Opus 4.8 answered. Both
        # attempts are billed, and both are $5/$25 per MTok.
        completion = self.decode(fallback_reply(1_500, "cyber"), OPUS)
        self.assertEqual(completion.model, "claude-opus-4-8")
        self.assertEqual(completion.usage, Usage(input_tokens=40_000,
                                                 output_tokens=2_700))
        opus = find("claude-opus-5")
        assert opus is not None
        self.assertAlmostEqual(opus.cost(completion.usage), 0.2675)

    def test_decline_before_output_in_an_unbilled_category_is_free(self) -> None:
        for category in ("cyber", "general_harms", None):
            completion = self.decode(fallback_reply(0, category), OPUS)
            self.assertEqual(completion.usage, Usage(input_tokens=20_000,
                                                     output_tokens=1_200), category)

    def test_decline_before_output_in_a_billed_category_is_billed(self) -> None:
        for category in ("bio", "frontier_llm", "reasoning_extraction", "new"):
            completion = self.decode(fallback_reply(0, category), OPUS)
            self.assertEqual(completion.usage.input_tokens, 40_000, category)

    def test_declined_attempt_keeps_its_own_price(self) -> None:
        # Served by a cheaper catalogued model: the client prices the whole
        # completion at the serving model's rates, so the declined Opus 5
        # attempt is restated to cost what Opus 5 billed.
        completion = self.decode(fallback_reply(1_500, "cyber",
                                                served="claude-sonnet-5"), OPUS)
        sonnet, opus = find("claude-sonnet-5"), find("claude-opus-5")
        assert sonnet is not None and opus is not None
        billed = (opus.cost(Usage(input_tokens=20_000, output_tokens=1_500))
                  + sonnet.cost(Usage(input_tokens=20_000, output_tokens=1_200)))
        self.assertAlmostEqual(sonnet.cost(completion.usage), billed, places=6)
        self.assertGreaterEqual(sonnet.cost(completion.usage), billed)

    def test_sticky_routing_and_plain_iterations_add_nothing(self) -> None:
        sticky = fallback_reply(1_500, "cyber")
        sticky["content"] = sticky["content"][1:]
        sticky["usage"]["iterations"] = sticky["usage"]["iterations"][1:]
        self.assertEqual(self.decode(sticky, OPUS).usage.input_tokens, 20_000)
        plain = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "x"}],
                 "usage": {"input_tokens": 5, "output_tokens": 2, "iterations": [
                     {"type": "message", "input_tokens": 5, "output_tokens": 2}]}}
        self.assertEqual(self.decode(plain).usage, Usage(input_tokens=5,
                                                         output_tokens=2))

    def test_refusal_before_output_is_billed_only_in_billed_categories(self) -> None:
        def refusal(category: Any) -> dict[str, Any]:
            return {"stop_reason": "refusal", "content": [],
                    "stop_details": {"type": "refusal", "category": category,
                                     "explanation": "declined"},
                    "usage": {"input_tokens": 412, "output_tokens": 0}}

        self.assertEqual(self.decode(refusal("cyber"), OPUS).usage, Usage())
        self.assertEqual(self.decode(refusal(None), OPUS).usage, Usage())
        self.assertEqual(self.decode(refusal("bio"), OPUS).usage.input_tokens, 412)
        missing = refusal("cyber")
        del missing["stop_details"]
        self.assertEqual(self.decode(missing, OPUS).usage.input_tokens, 412)


SPEND_CAP = {   # platform.claude.com/docs/en/api/rate-limits, "Reaching your spend cap"
    "type": "error",
    "error": {
        "type": "rate_limit_error",
        "message": "You have reached your API usage limits: your organization has "
                   "crossed its monthly API usage threshold, set based on your "
                   "organization's API tier. You will regain access on 2026-12-01 "
                   "at 00:00 UTC.",
        "details": {"error_code": "enforced_spend_limit_reached"},
    },
    "request_id": "req_018EeWyXxfu5pfWkrYcMdjWG",
}


class TestAnthropicErrors(unittest.TestCase):
    def error(self, status: int, kind: str,
              headers: dict[str, str] | None = None) -> DevDocsError:
        payload = {"type": "error", "error": {"type": kind, "message": "m"}}
        return self.envelope(status, payload, headers)

    def envelope(self, status: int, payload: Any,
                 headers: dict[str, str] | None = None) -> DevDocsError:
        provider = AnthropicProvider("k", replay(status, payload, headers))
        return raised(lambda: provider.generate(CLAUDE))

    def test_mapping(self) -> None:
        cases = {
            (401, "authentication_error"): AuthError,
            (403, "permission_error"): AuthError,
            (402, "billing_error"): QuotaExhausted,
            (404, "not_found_error"): ModelNotFound,
            (429, "rate_limit_error"): RateLimited,
        }
        for (status, kind), expected in cases.items():
            self.assertIsInstance(self.error(status, kind), expected, kind)

    def test_retry_after_header(self) -> None:
        err = self.error(429, "rate_limit_error", {"retry-after": "7"})
        assert isinstance(err, RateLimited)
        self.assertEqual(err.retry_after, 7.0)

    def test_overload_retries_but_bad_request_does_not(self) -> None:
        overloaded = self.error(529, "overloaded_error")
        bad = self.error(400, "invalid_request_error")
        assert isinstance(overloaded, ProviderError) and isinstance(bad, ProviderError)
        self.assertTrue(overloaded.retryable)
        self.assertFalse(bad.retryable)

    def test_other_documented_statuses(self) -> None:
        for status, kind, retryable in ((413, "request_too_large", False),
                                        (409, "conflict_error", False),
                                        (500, "api_error", True),
                                        (504, "timeout_error", True)):
            err = self.error(status, kind)
            assert isinstance(err, ProviderError)
            self.assertNotIsInstance(err, QuotaExhausted | RateLimited | ModelNotFound)
            self.assertEqual(err.retryable, retryable, kind)

    def test_billing_is_account_wide(self) -> None:
        err = self.error(402, "billing_error")
        assert isinstance(err, QuotaExhausted)
        self.assertEqual(err.status, 402)
        self.assertFalse(err.retryable)

    def test_monthly_spend_cap_is_account_wide_not_a_rate_limit(self) -> None:
        err = self.envelope(429, SPEND_CAP)
        assert isinstance(err, QuotaExhausted)
        self.assertEqual(err.status, 402)
        self.assertFalse(err.retryable)

    def test_spend_limits_set_by_the_user_are_account_wide(self) -> None:
        for prefix in ("You have reached your specified API usage limits",
                       "You have reached your specified workspace API usage limits"):
            payload = {"type": "error", "error": {
                "type": "invalid_request_error",
                "message": f"{prefix}. You will regain access on 2026-12-01."}}
            err = self.envelope(400, payload)
            assert isinstance(err, QuotaExhausted)
            self.assertEqual(err.status, 402, prefix)

    def test_spend_cap_stops_the_run_after_one_request(self) -> None:
        transport = Recorded(*[(429, SPEND_CAP, {})] * 12)
        client = LLMClient(AnthropicProvider("k", transport),
                           router=Router("anthropic"),
                           retry=RetryPolicy(base_delay=0), free_tier=False)
        with self.assertRaises(QuotaExhausted):
            client.complete(Tier.FILE, "SYS", "file 1")
        self.assertEqual(len(transport.sent), 1)

    def test_error_that_is_not_an_object_maps_by_status(self) -> None:
        err = self.envelope(503, {"error": "Service Unavailable"})
        assert isinstance(err, ProviderError)
        self.assertEqual((err.status, err.retryable, err.message),
                         (503, True, "Service Unavailable"))
        listed = self.envelope(429, {"error": ["x"]})
        self.assertIsInstance(listed, RateLimited)

    def test_list_models_follows_pagination(self) -> None:
        transport = Recorded(
            (200, {"data": [{"id": "claude-opus-5"}], "has_more": True,
                   "last_id": "claude-opus-5"}, {}),
            (200, {"data": [{"id": "claude-haiku-4-5-20251001"}, "junk"],
                   "has_more": False}, {}),
        )
        models = AnthropicProvider("k", transport).list_models()
        self.assertEqual(models, ["claude-haiku-4-5-20251001", "claude-opus-5"])
        self.assertIn("limit=1000", transport.sent[0]["url"])
        self.assertIn("after_id=claude-opus-5", transport.sent[1]["url"])


if __name__ == "__main__":
    unittest.main()
