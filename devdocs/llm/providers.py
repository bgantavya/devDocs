"""Gemini and Anthropic over raw HTTP, behind the `ChatModel` protocol.

Zero runtime dependencies means no vendor SDK, so each provider is three small
jobs: encode a `Request` into the vendor's JSON, decode the response into a
`Completion`, and map the vendor's error envelope onto `devdocs.errors`. The
error mapping is the part that saves money — a bad key must raise AuthError
once, not fall through a model ladder spending requests to rediscover it, and
a spend cap or an empty prepaid balance must stop the run, not be retried
file after file.

Decoding is defensive for the same reason. By the time a body is read the call
has been billed, so a null or oddly shaped field reads as absent instead of
raising; and a 2xx body that is not a JSON object at all (cut off in transit, a
proxy's HTML page) is a failed call, never an empty answer that would be
reported as a block or escalated at $0.

Anthropic recommends its official SDK over raw HTTP. The SDK is available as
an opt-in extra (`pip install devdocs[anthropic]`, see `anthropic_sdk.py`),
which reuses the request body, decoding and error mapping below; raw HTTP here
is the documented cost of the zero-dependency default.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from devdocs.errors import (
    AuthError,
    DevDocsError,
    ModelNotFound,
    ProviderError,
    QuotaExhausted,
    RateLimited,
)
from devdocs.llm import Completion, Finish, Request, Usage
from devdocs.llm.catalog import ModelInfo, assume, find
from devdocs.llm.http import HttpResponse, Transport, parse_retry_after

__all__ = ["AnthropicProvider", "GeminiProvider"]

_MAX_PAGES = 20

# QuotaExhausted with this status is account-wide: LLMClient re-raises it
# instead of retiring one model, since no other model on the key can be paid.
_ACCOUNT_WIDE = 402


def _json_body(response: HttpResponse) -> dict[str, Any]:
    """Decode an error body, tolerating an empty or non-object body."""
    if not response.body:
        return {}
    try:
        decoded = response.json()
    except ProviderError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _success_body(response: HttpResponse, provider: str) -> dict[str, Any]:
    """The JSON object a 2xx response must carry; anything else lost the answer.

    Retryable: a body cut off in transit is worth sending again, and decoding
    it as `{}` would report a safety block or escalate a paid call at $0.
    """
    try:
        decoded = response.json()
    except ProviderError as err:
        raise ProviderError(
            f"unreadable {response.status} response: {err.message}",
            status=response.status, provider=provider, retryable=True,
        ) from err
    if not isinstance(decoded, dict):
        raise ProviderError(
            f"{response.status} response is not a JSON object",
            status=response.status, provider=provider, retryable=True,
        )
    return decoded


def _field(source: Any, key: str) -> Any:
    """`source[key]` for decoded JSON, else the attribute: SDK objects decode alike."""
    if isinstance(source, Mapping):
        return source.get(key)
    if source is None or isinstance(source, str | bytes | int | float | list | tuple):
        return None   # a JSON scalar or array has no fields, only methods
    return getattr(source, key, None)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def _count(value: Any) -> int:
    """A token count as decoded. Null, negative or junk reads as 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value)) if math.isfinite(value) else 0
    if isinstance(value, str):
        try:
            return max(0, int(value.strip()))
        except ValueError:
            return 0
    return 0


# -- Gemini ---------------------------------------------------------------------

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# FinishReason, ai.google.dev/api/generate-content. Verdicts every model of the
# vendor would repeat are BLOCKED, which the client returns instead of
# escalating; CONTINUATION is a cut at the per-request token limit.
_GEMINI_FINISH = {
    "STOP": Finish.STOP,
    "MAX_TOKENS": Finish.LENGTH,
    "CONTINUATION": Finish.LENGTH,
    "SAFETY": Finish.BLOCKED,
    "RECITATION": Finish.BLOCKED,
    "LANGUAGE": Finish.BLOCKED,
    "BLOCKLIST": Finish.BLOCKED,
    "PROHIBITED_CONTENT": Finish.BLOCKED,
    "SPII": Finish.BLOCKED,
    "IMAGE_SAFETY": Finish.BLOCKED,
    "IMAGE_PROHIBITED_CONTENT": Finish.BLOCKED,
    "IMAGE_RECITATION": Finish.BLOCKED,
    "ESCALATION": Finish.BLOCKED,
    "PUP_LIMITED_DISABLED": Finish.BLOCKED,
}


def _model_path(model: str, method: str) -> str:
    """`models/{id}:{method}`, whether or not the id already has `models/`."""
    return f"models/{quote(model.removeprefix('models/'), safe='')}:{method}"


class GeminiProvider:
    """Google Gemini via the Generative Language REST API (v1beta).

    v1beta rather than v1 because new models land there first. The key goes
    in the `x-goog-api-key` header, never the query string, where it would
    end up in proxy and server logs.
    """

    provider = "gemini"

    def __init__(
        self,
        api_key: str,
        transport: Transport,
        *,
        base_url: str = _GEMINI_BASE,
        embedding_model: str = "gemini-embedding-001",
        dimensions: int = 768,
    ) -> None:
        self.api_key = api_key
        self.transport = transport
        self.base_url = base_url.rstrip("/")
        self.embedding_model = embedding_model
        self.dimensions = dimensions

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = self.transport.request(
            "POST",
            f"{self.base_url}/{path}",
            headers=self._headers(),
            body=json.dumps(payload).encode("utf-8"),
        )
        if not response.ok:
            raise self._error(response)
        return _success_body(response, self.provider)

    # -- generation --------------------------------------------------------

    def encode(self, request: Request, info: ModelInfo | None) -> dict[str, Any]:
        """The generateContent body.

        `systemInstruction` travels apart from `contents`, and Gemini places it
        ahead of the conversation whatever the JSON key order. A byte-identical
        system prompt is therefore the prefix implicit caching can reuse.
        Thinking is switched off (or floored) per the catalog: for writing docs
        it costs output tokens without improving the result. `temperature`
        goes only to catalogued models that take it: Gemini 3.6 and later
        ignore it and are to start rejecting it, and earlier Gemini 3 models
        reason worse away from the default.
        """
        config: dict[str, Any] = {"maxOutputTokens": request.max_output_tokens}
        accepts_temperature = info is not None and info.accepts_temperature
        if request.temperature is not None and accepts_temperature:
            config["temperature"] = request.temperature
        if info is not None and info.thinking_off:
            config.update(info.thinking_off)

        payload: dict[str, Any] = {}
        if request.system:
            payload["systemInstruction"] = {"parts": [{"text": request.system}]}
        payload["contents"] = [{"role": "user", "parts": [{"text": request.prompt}]}]
        payload["generationConfig"] = config
        return payload

    def generate(self, request: Request) -> Completion:
        info = find(request.model)
        body = self._post(
            _model_path(request.model, "generateContent"), self.encode(request, info)
        )
        return self.decode(body, request.model)

    def decode(self, body: Mapping[str, Any], model: str) -> Completion:
        """Turn a generateContent response into a Completion.

        A 200 with no candidates, or a candidate with no parts, is a normal
        outcome (safety block, output cap) and yields `text=None` rather than
        raising — v3 crashed here by calling a method on None. Thought parts
        and non-text parts are skipped.
        """
        meta = body.get("usageMetadata")
        usage = self._usage(meta if isinstance(meta, Mapping) else {})
        served = _text(body.get("modelVersion")) or model
        candidates = [
            c for c in _items(body.get("candidates")) if isinstance(c, Mapping)
        ]

        if not candidates:
            reason = _text(_field(body.get("promptFeedback"), "blockReason"))
            return Completion(
                None, served, self.provider, Finish.BLOCKED, usage,
                detail=f"prompt blocked: {reason}" if reason else "no candidates",
            )

        candidate = candidates[0]
        raw_finish = _text(candidate.get("finishReason"))
        finish = _GEMINI_FINISH.get(raw_finish, Finish.OTHER)
        parts = _items(_field(candidate.get("content"), "parts"))
        text = "".join(
            _text(part.get("text")) for part in parts
            if isinstance(part, Mapping) and not part.get("thought")
        ).strip()
        return Completion(
            text or None, served, self.provider, finish, usage, detail=raw_finish
        )

    @staticmethod
    def _usage(meta: Mapping[str, Any]) -> Usage:
        """UsageMetadata as billed.

        `promptTokenCount` already includes the cached part, and thoughts are
        counted apart from candidates; both bill as output.
        """
        return Usage(
            input_tokens=_count(meta.get("promptTokenCount"))
            + _count(meta.get("toolUsePromptTokenCount")),
            output_tokens=_count(meta.get("candidatesTokenCount")),
            cached_input_tokens=_count(meta.get("cachedContentTokenCount")),
            thinking_tokens=_count(meta.get("thoughtsTokenCount")),
        )

    # -- models and embeddings ---------------------------------------------

    def list_models(self) -> list[str]:
        """Model ids this key can call generateContent on, without `models/`."""
        found: list[str] = []
        token = ""
        for _ in range(_MAX_PAGES):
            query = "?pageSize=1000"
            if token:
                query += "&pageToken=" + quote(token, safe="")
            response = self.transport.request(
                "GET", f"{self.base_url}/models{query}", headers=self._headers()
            )
            if not response.ok:
                raise self._error(response)
            body = _success_body(response, self.provider)
            for model in _items(body.get("models")):
                if not isinstance(model, Mapping):
                    continue
                methods = _items(model.get("supportedGenerationMethods"))
                name = _text(model.get("name")).removeprefix("models/")
                if name and ("generateContent" in methods or "embedContent" in methods):
                    found.append(name)
            token = _text(body.get("nextPageToken"))
            if not token:
                break
        return sorted(set(found))

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        """Unit-length vectors, 100 texts per request.

        Normalised here because gemini-embedding-001 only returns unit vectors
        at its full 3072 dimensions; truncated ones must be rescaled or cosine
        similarity silently becomes a dot product of unequal lengths.
        """
        vectors: list[list[float]] = []
        task = "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"
        model = self.embedding_model.removeprefix("models/")
        path = _model_path(model, "batchEmbedContents")
        for start in range(0, len(texts), 100):
            batch = texts[start : start + 100]
            payload = {
                "requests": [
                    {
                        "model": f"models/{model}",
                        "content": {"parts": [{"text": text}]},
                        "taskType": task,
                        "outputDimensionality": self.dimensions,
                    }
                    for text in batch
                ]
            }
            embeddings = _items(self._post(path, payload).get("embeddings"))
            if len(embeddings) != len(batch):
                raise ProviderError(
                    f"asked for {len(batch)} embeddings, got {len(embeddings)}",
                    provider=self.provider,
                )
            vectors.extend(_unit(self._values(item)) for item in embeddings)
        return vectors

    def _values(self, embedding: Any) -> list[float]:
        values = _items(_field(embedding, "values"))
        numbers = [
            float(value) for value in values
            if isinstance(value, int | float) and not isinstance(value, bool)
            and math.isfinite(value)
        ]
        if not numbers or len(numbers) != len(values):
            raise ProviderError(
                "an embedding came back without numeric values", provider=self.provider
            )
        return numbers

    # -- errors --------------------------------------------------------------

    def _error(self, response: HttpResponse) -> DevDocsError:
        """Map Gemini's `{"error": {code, message, status, details}}` envelope.

        Routes on the HTTP status and structured details, and on message text
        only where Google signals a case in text alone: an invalid key arrives
        as a 400 INVALID_ARGUMENT, and a monthly spend cap as a plain 429. A
        402 (Prepay balance at $0) and a spend cap stop every key on the
        billing account until it is topped up or the month turns, so both are
        account-wide QuotaExhausted. A per-day quota retires one model only.
        """
        error = _json_body(response).get("error")
        if isinstance(error, str) and error.strip():
            error = {"message": error}
        if not isinstance(error, Mapping):
            error = {}
        message = _text(error.get("message")) or f"HTTP {response.status}"
        lowered = message.lower()
        code = response.status
        details = json.dumps(error.get("details") or [])

        if code in (401, 403) or "API_KEY_INVALID" in details or (
            code == 400 and "api key not valid" in lowered
        ):
            return AuthError(
                f"Gemini rejected the API key: {message}",
                hint="check GEMINI_API_KEY; get one at aistudio.google.com",
            )
        if code == 402:
            return QuotaExhausted(
                f"Gemini billing: {message}",
                status=_ACCOUNT_WIDE, provider=self.provider,
            )
        if code == 404:
            return ModelNotFound(message, provider=self.provider)
        if code == 429:
            capped = "spending cap" in lowered or "spend cap" in lowered
            if capped and "monthly" in lowered:
                return QuotaExhausted(
                    f"Gemini spend cap reached: {message}",
                    status=_ACCOUNT_WIDE, provider=self.provider,
                )
            if "PerDay" in details or "per day" in lowered:
                return QuotaExhausted(
                    f"Gemini daily quota exhausted: {message}", provider=self.provider
                )
            delay = _gemini_retry_delay(error)
            return RateLimited(
                message,
                retry_after=delay if delay is not None else parse_retry_after(
                    response.header("retry-after")
                ),
                status=code,
                provider=self.provider,
            )
        return ProviderError(message, status=code, provider=self.provider)


def _gemini_retry_delay(error: Mapping[str, Any]) -> float | None:
    """Seconds from a google.rpc.RetryInfo detail, e.g. {"retryDelay": "37s"}."""
    for detail in _items(error.get("details")):
        delay = _text(_field(detail, "retryDelay")).strip()
        if delay.endswith("s"):
            try:
                seconds = float(delay[:-1])
            except ValueError:
                return None
            return max(0.0, seconds) if math.isfinite(seconds) else None
    return None


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


# -- Anthropic ------------------------------------------------------------------

_ANTHROPIC_BASE = "https://api.anthropic.com/v1"
_ANTHROPIC_VERSION = "2023-06-01"
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

_ANTHROPIC_FINISH = {
    "end_turn": Finish.STOP,
    "stop_sequence": Finish.STOP,
    "max_tokens": Finish.LENGTH,
    "model_context_window_exceeded": Finish.LENGTH,
    "refusal": Finish.REFUSED,
}

# Refusal categories that are not billed when the decline comes before any
# output; None is the documented null category. Any other value, including
# one added later, is counted as billed.
_UNBILLED_REFUSALS = frozenset({"cyber", "general_harms", None})
_UNKNOWN = object()   # no category was reported at all

# The tier's monthly cap is a 429 told apart by this code; a limit the user
# set is a 400 told apart only by its message (platform.claude.com, api/rate-limits).
_SPEND_CAP_CODE = "enforced_spend_limit_reached"
_SPEND_LIMIT_PREFIXES = (
    "You have reached your API usage limits",
    "You have reached your specified API usage limits",
    "You have reached your specified workspace API usage limits",
)


def _encode_messages(
    request: Request, info: ModelInfo | None, *, cache: bool, sampling: bool = True
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """The Messages body and its beta headers, for both Anthropic providers.

    `sampling=False` leaves `temperature` out, for the SDK, which removed it.
    """
    payload: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_output_tokens,
        "messages": [{"role": "user", "content": request.prompt}],
    }
    if request.system:
        system: dict[str, Any] = {"type": "text", "text": request.system}
        if cache:
            system["cache_control"] = {"type": "ephemeral"}
        payload["system"] = [system]

    accepts_temperature = info is not None and info.accepts_temperature
    if sampling and request.temperature is not None and accepts_temperature:
        payload["temperature"] = request.temperature
    if request.effort and info is not None and info.accepts_effort:
        payload["output_config"] = {"effort": request.effort}
    if info is not None and info.thinking_off:
        payload.update(info.thinking_off)   # the cheapest thinking the model takes

    betas: tuple[str, ...] = ()
    if info is not None and info.server_fallbacks:
        payload["fallbacks"] = "default"
        betas = (_FALLBACK_BETA,)
    return payload, betas


def _decode_message(message: Any, model: str, provider: str) -> Completion:
    """A Messages response, as decoded JSON or an SDK object, as a Completion."""
    stop = _text(_field(message, "stop_reason"))
    finish = _ANTHROPIC_FINISH.get(stop, Finish.OTHER)
    blocks = _items(_field(message, "content"))
    text = "".join(
        _text(_field(block, "text")) for block in blocks
        if _field(block, "type") == "text"
    ).strip()
    if finish is Finish.REFUSED:
        text = ""
    served = _text(_field(message, "model")) or model
    usage = _billed_usage(message, blocks, requested=model, served=served)
    return Completion(text or None, served, provider, finish, usage, detail=stop)


def _attempt_usage(raw: Any) -> Usage:
    """One attempt's counts. Anthropic's `input_tokens` excludes cache reads and
    writes; Usage counts them in."""
    read = _count(_field(raw, "cache_read_input_tokens"))
    write = _count(_field(raw, "cache_creation_input_tokens"))
    return Usage(
        input_tokens=_count(_field(raw, "input_tokens")) + read + write,
        output_tokens=_count(_field(raw, "output_tokens")),
        cached_input_tokens=read,
        cache_write_tokens=write,
    )


def _billed(output_tokens: int, category: object) -> bool:
    """Whether a declined attempt is billed: it produced output, or its refusal
    category is one billed even before any output."""
    return output_tokens > 0 or category not in _UNBILLED_REFUSALS


def _billed_usage(
    message: Any, blocks: list[Any], *, requested: str, served: str
) -> Usage:
    """What a response was billed for, in tokens of the model that served it.

    The top-level counts cover only the attempt that produced the message.
    Under server-side fallback every attempt is billed separately and reported
    in `usage.iterations`: declined hops as `message` entries, the serving hop
    as `fallback_message`. An attempt that declined before any output is
    billed only in some refusal categories, and a refusal returned to us
    follows the same rule (refusals-and-fallback, "How refusals are billed").
    The client prices a completion by the model that served it, so a billed
    declined attempt on a model with other rates is restated at the serving
    model's rates.
    """
    raw = _field(message, "usage")
    total = _attempt_usage(raw)
    if _field(message, "stop_reason") == "refusal":
        details = _field(message, "stop_details")
        category = _UNKNOWN if details is None else _field(details, "category")
        if not _billed(total.output_tokens, category):
            total = Usage()

    iterations = _items(_field(raw, "iterations"))
    serving = [
        entry for entry in iterations if _field(entry, "type") == "fallback_message"
    ]
    if not serving:
        return total
    serving_model = _field(serving[-1], "model")
    triggers = [
        _UNKNOWN if _field(block, "trigger") is None
        else _field(_field(block, "trigger"), "category")
        for block in blocks if _field(block, "type") == "fallback"
    ]
    priced = _pricing(served, requested)
    hop = 0
    for entry in iterations:
        if _field(entry, "type") != "message":
            continue
        if serving_model and _field(entry, "model") == serving_model:
            continue   # the serving hop's own tool-loop turns, already top-level
        category = triggers[hop] if hop < len(triggers) else _UNKNOWN
        hop += 1
        attempt = _attempt_usage(entry)
        if _billed(attempt.output_tokens, category):
            ran = _pricing(_text(_field(entry, "model")), requested)
            total = total + _restate(attempt, ran, priced)
    return total


def _pricing(model: str, requested: str) -> ModelInfo:
    """The entry LLMClient prices `model` by: its own, else the requested one's."""
    return find(model) or find(requested) or assume(requested, "anthropic")


def _rates(info: ModelInfo) -> tuple[float, float, float, float]:
    """USD per MTok for fresh input, cache reads, cache writes and output."""
    return (
        info.input_per_mtok,
        info.input_per_mtok * info.cached_input_ratio,
        info.input_per_mtok * info.cache_write_ratio,
        info.output_per_mtok,
    )


def _restate(usage: Usage, ran: ModelInfo, priced: ModelInfo) -> Usage:
    """`usage` billed at `ran`'s rates, as tokens `priced.cost()` bills the same.

    Rounded up, so a declined attempt is never under-counted.
    """
    have, want = _rates(ran), _rates(priced)
    if have == want:
        return usage

    def scale(tokens: int, index: int) -> int:
        if tokens <= 0 or want[index] <= 0:
            return max(0, tokens)
        return math.ceil(tokens * have[index] / want[index] - 1e-9)

    fresh = usage.input_tokens - usage.cached_input_tokens - usage.cache_write_tokens
    read = scale(usage.cached_input_tokens, 1)
    write = scale(usage.cache_write_tokens, 2)
    return Usage(
        input_tokens=scale(fresh, 0) + read + write,
        output_tokens=scale(usage.output_tokens + usage.thinking_tokens, 3),
        cached_input_tokens=read,
        cache_write_tokens=write,
    )


def _anthropic_error(
    status: int,
    body: Any,
    retry_after: str | None,
    *,
    fallback: str = "",
    provider: str = "anthropic",
) -> DevDocsError:
    """Map `{"type": "error", "error": {type, message, details}}` by error type.

    The two spend limits are told apart from a rate limit and a bad request
    as documented (api/rate-limits): the tier's monthly cap is a 429
    `rate_limit_error` with `details.error_code` set and no retry-after, and a
    limit the user set is a 400 named only by its message. Both, like a 402
    billing error, stop every model on the account until someone acts, so they
    are account-wide QuotaExhausted and never retried.
    """
    error = body.get("error") if isinstance(body, Mapping) else None
    if isinstance(error, str) and error.strip():
        error = {"message": error}
    if not isinstance(error, Mapping):
        error = {}
    kind = _text(error.get("type"))
    message = _text(error.get("message")) or fallback or f"HTTP {status}"
    code = _text(_field(error.get("details"), "error_code"))

    if code == _SPEND_CAP_CODE or message.startswith(_SPEND_LIMIT_PREFIXES):
        return QuotaExhausted(
            f"Anthropic spend limit reached: {message}",
            status=_ACCOUNT_WIDE, provider=provider,
        )
    if kind in ("authentication_error", "permission_error") or status in (401, 403):
        return AuthError(
            f"Anthropic rejected the request: {message}",
            hint="check ANTHROPIC_API_KEY and the key's workspace access",
        )
    if kind == "billing_error" or status == 402:
        return QuotaExhausted(
            f"Anthropic billing: {message}", status=_ACCOUNT_WIDE, provider=provider
        )
    if kind == "not_found_error" or status == 404:
        return ModelNotFound(message, provider=provider)
    if kind == "rate_limit_error" or status == 429:
        return RateLimited(
            message,
            retry_after=parse_retry_after(retry_after),
            status=status,
            provider=provider,
        )
    return ProviderError(message, status=status, provider=provider)


class AnthropicProvider:
    """Claude via the Messages API, over raw HTTP.

    Encoding follows each model's accepted parameters from the catalog:
    Sonnet 5 and Opus 5 reject `temperature` with a 400, Haiku 4.5 rejects
    `effort`. Opus 5 requests opt into server-side refusal fallbacks
    (`fallbacks: "default"`), so a declined request is re-run on Anthropic's
    recommended fallback model instead of coming back empty.
    """

    provider = "anthropic"

    def __init__(
        self,
        api_key: str,
        transport: Transport,
        *,
        base_url: str = _ANTHROPIC_BASE,
        cache: bool = True,
    ) -> None:
        self.api_key = api_key
        self.transport = transport
        self.base_url = base_url.rstrip("/")
        self.cache = cache

    def _headers(self, *, betas: tuple[str, ...] = ()) -> dict[str, str]:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        if betas:
            headers["anthropic-beta"] = ",".join(betas)
        return headers

    def encode(
        self, request: Request, info: ModelInfo | None
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """The Messages body and any beta headers it needs.

        With `cache`, the system block carries the cache breakpoint: it is the
        prefix that is byte-identical across calls. A top-level `cache_control`
        would put the breakpoint on the last block, the per-file prompt, so
        every call would pay the 1.25x write on its whole input and none would
        ever read it back. Below the model's minimum cacheable length (512 to
        4,096 tokens) the marker is a silent no-op, not an error.
        """
        return _encode_messages(request, info, cache=self.cache)

    def generate(self, request: Request) -> Completion:
        payload, betas = self.encode(request, find(request.model))
        response = self.transport.request(
            "POST",
            f"{self.base_url}/messages",
            headers=self._headers(betas=betas),
            body=json.dumps(payload).encode("utf-8"),
        )
        if not response.ok:
            raise self._error(response)
        return self.decode(_success_body(response, self.provider), request.model)

    def decode(self, body: Mapping[str, Any], model: str) -> Completion:
        """Turn a Messages response into a Completion.

        `stop_reason` is checked before content is read: a refusal carries no
        usable text. `model` in the response is the one that actually served,
        which differs from the request when a fallback ran. Usage is what was
        billed: declined fallback attempts are added, and a refusal billed by
        nobody costs nothing.
        """
        return _decode_message(body, model, self.provider)

    def list_models(self) -> list[str]:
        """Model ids the key can call, as /v1/models lists them, every page."""
        found: list[str] = []
        after = ""
        for _ in range(_MAX_PAGES):
            query = "?limit=1000"
            if after:
                query += "&after_id=" + quote(after, safe="")
            response = self.transport.request(
                "GET", f"{self.base_url}/models{query}", headers=self._headers()
            )
            if not response.ok:
                raise self._error(response)
            body = _success_body(response, self.provider)
            found.extend(
                _text(model.get("id")) for model in _items(body.get("data"))
                if isinstance(model, Mapping) and _text(model.get("id"))
            )
            if not body.get("has_more"):
                break
            after = _text(body.get("last_id"))
            if not after:
                break
        return sorted(set(found))

    def _error(self, response: HttpResponse) -> DevDocsError:
        """Map the error envelope; see `_anthropic_error`."""
        return _anthropic_error(
            response.status, _json_body(response), response.header("retry-after"),
            provider=self.provider,
        )
