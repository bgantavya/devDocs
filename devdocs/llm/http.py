"""The HTTP transport every provider shares, built on `http.client`.

`urllib.request` opens a fresh TLS connection per call, which measured at
~490 ms of handshake per request against the Gemini API. `http.client` keeps
the connection alive, so connections here are pooled per host and per thread
(an HTTPSConnection is not safe to share between threads).

Also handled here, because urllib does none of it: gzip/deflate decoding, a
TLS 1.2 floor (some system Pythons still accept TLS 1.0), HTTPS proxies via
CONNECT with NO_PROXY honoured, and a cap on response size that also holds
after decompression.

Every request is paid for, so the transport never delivers one twice. A
pooled connection the server has closed is noticed before it is written to,
and the one transparent resend is kept for a write that failed — proof that
the server never had the whole request. Anything that fails once the request
is written (a dropped connection, a body cut short, a reply that is not
HTTP/1.1) raises ProviderError and leaves the decision to `RetryPolicy`. A
connection is pooled again only after its response was read to the end, so
no request can be handed the answer to another.

Retrying failed *requests* is a separate concern, in `RetryPolicy`, because
whether a failure is worth retrying depends on the provider's error, not on
the socket.
"""

from __future__ import annotations

import base64
import http.client
import json
import math
import random
import re
import select
import ssl
import threading
import time
import urllib.request
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, TypeVar
from urllib.parse import unquote, urlsplit

from devdocs import __version__
from devdocs.errors import ProviderError, RateLimited

__all__ = [
    "HttpResponse",
    "HttpTransport",
    "RetryPolicy",
    "Transport",
    "parse_retry_after",
]

T = TypeVar("T")

MAX_RESPONSE_BYTES = 64 * 1024 * 1024

# Raised while *writing* to a pooled connection, these mean the server had
# already closed it. The write failed, so the server never had the whole
# request and one resend on a fresh connection cannot bill twice.
_DEAD_ON_WRITE = (
    ConnectionError,
    ssl.SSLEOFError,
    ssl.SSLZeroReturnError,
    http.client.ImproperConnectionState,
)

# What socket, ssl and http.client raise when an exchange fails.
_EXCHANGE_ERRORS = (OSError, ValueError, http.client.HTTPException)

_GZIP = 16 + zlib.MAX_WBITS
_MAX_INTERIM = 16
_HEADER_NAME = re.compile(rb"[^:\s][^:\r\n]*")   # http.client's own rule
_DELAY_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_TUNNEL_FAILED = re.compile(r"Tunnel connection failed: (\d{3})")


@dataclass(frozen=True)
class HttpResponse:
    """A fully read response. Header names are lowercased."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def json(self) -> Any:
        """Decode the body as JSON, or raise a ProviderError saying it was not.

        That includes bodies the decoder cannot hold: nesting deeper than the
        recursion limit, or an integer past Python's 4300-digit cap.
        """
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, RecursionError) as err:   # JSON and Unicode errors too
            preview = self.body[:200].decode("utf-8", errors="replace")
            raise ProviderError(
                f"expected JSON, got {len(self.body)} bytes: {preview!r}",
                status=self.status,
            ) from err

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Transport(Protocol):
    """What providers need from HTTP. Tests substitute a recorded fake."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse: ...


def _tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class _Response(http.client.HTTPResponse):
    """An HTTPResponse that reads past every interim (1xx) response.

    http.client skips only `100 Continue`, but RFC 9110 §15.2 requires a
    client to parse any 1xx before the final response. Otherwise a `103 Early
    Hints` comes back as the answer, and the real one stays queued on the
    connection for the next request to read.
    """

    def begin(self) -> None:
        super().begin()
        skipped = 0
        while 102 <= self.status < 200:   # 101 is refused by the caller
            skipped += 1
            if skipped > _MAX_INTERIM:
                raise http.client.HTTPException(
                    f"got more than {_MAX_INTERIM} interim responses"
                )
            self.headers = self.msg = None   # type: ignore[assignment]  # re-arms begin()
            super().begin()


class _HTTPConnection(http.client.HTTPConnection):
    response_class = _Response


class _HTTPSConnection(http.client.HTTPSConnection):
    response_class = _Response


class HttpTransport:
    """Pooled, keep-alive HTTP(S) client. One instance per run is enough.

    `timeout` bounds each socket operation (connect, every write and read),
    not the whole exchange.
    """

    def __init__(
        self,
        *,
        timeout: float = 120.0,
        context: ssl.SSLContext | None = None,
        user_agent: str = f"devdocs/{__version__}",
    ) -> None:
        self.timeout = timeout
        self.context = context or _tls_context()
        self.user_agent = user_agent
        self._local = threading.local()

    def _pool(self) -> dict[tuple[str, str, int], http.client.HTTPConnection]:
        pool = getattr(self._local, "pool", None)
        if pool is None:
            pool = {}
            self._local.pool = pool
        return pool

    def _open(self, scheme: str, host: str, port: int) -> http.client.HTTPConnection:
        """A new, not yet connected connection, through a proxy if one applies."""
        proxy = _proxy_for(scheme, host)
        connection: http.client.HTTPConnection
        try:
            if proxy is None:
                return self._connection(scheme, host, port)
            proxy_host, proxy_port, tunnel_headers = _proxy_address(proxy)
            connection = self._connection(scheme, proxy_host, proxy_port)
            connection.set_tunnel(host, port, headers=tunnel_headers)
        except (ValueError, http.client.HTTPException) as err:
            raise ProviderError(
                f"cannot open a connection to {host}: {err}", retryable=False
            ) from err
        return connection

    def _connection(
        self, scheme: str, host: str, port: int
    ) -> http.client.HTTPConnection:
        if scheme == "https":
            return _HTTPSConnection(
                host, port, timeout=self.timeout, context=self.context
            )
        return _HTTPConnection(host, port, timeout=self.timeout)

    def _checkout(
        self, key: tuple[str, str, int]
    ) -> tuple[http.client.HTTPConnection, bool]:
        """A pooled connection that is still open, or a new one; and which it is.

        It leaves the pool while in use and returns only after a clean
        exchange, so an interrupted one is never written to again.
        """
        connection = self._pool().pop(key, None)
        if connection is not None:
            if _alive(connection):
                return connection, True
            connection.close()
        return self._open(*key), False

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        """Send one request and read the whole response.

        Raises ProviderError for anything that stops a response arriving:
        a malformed URL, header or proxy setting, DNS, TLS, timeouts, refused
        or dropped connections, a body cut short, a reply that is not HTTP/1.1
        or one over MAX_RESPONSE_BYTES. Non-2xx responses are returned, not
        raised — interpreting them is the provider's job.
        """
        scheme, host, port, target = _split(url)
        sent = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            **(headers or {}),
        }
        _check_headers(sent)
        key = (scheme, host, port)

        for attempt in (1, 2):
            connection, reused = self._checkout(key)
            try:
                connection.request(method, target, body=body, headers=sent)
            except BaseException as err:
                connection.close()
                if reused and attempt == 1 and isinstance(err, _DEAD_ON_WRITE):
                    continue   # the server closed it first; it never had this request
                if isinstance(err, _EXCHANGE_ERRORS):
                    raise _failure(err, host) from err
                raise
            return self._receive(connection, key, host)

        raise ProviderError(f"could not reach {host}")   # unreachable in practice

    def _receive(
        self,
        connection: http.client.HTTPConnection,
        key: tuple[str, str, int],
        host: str,
    ) -> HttpResponse:
        """Read the response to the request just written, never resending it."""
        raw: http.client.HTTPResponse | None = None
        try:
            raw = connection.getresponse()
            if raw.status < 200:
                raise ProviderError(
                    f"{host} switched protocols (HTTP {raw.status}) instead of "
                    "answering",
                    retryable=False,
                )
            if raw.length is not None and raw.length > MAX_RESPONSE_BYTES:
                raise _too_large(host)
            data = raw.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise _too_large(host)
            if raw.length:   # http.client returns a short body when EOF comes early
                raise ProviderError(
                    f"connection to {host} dropped mid-response: got {len(data)} "
                    f"of {len(data) + raw.length} bytes"
                )
            status = raw.status
            response_headers = {k.lower(): v for k, v in raw.getheaders()}
            encoding = raw.getheader("content-encoding") or ""
        except BaseException as err:
            if raw is not None:
                raw.close()
            connection.close()
            if isinstance(err, _EXCHANGE_ERRORS):
                raise _failure(err, host) from err
            raise

        if raw.will_close or not raw.isclosed():
            raw.close()
            connection.close()
        else:
            self._pool()[key] = connection
        return HttpResponse(
            status=status,
            headers=response_headers,
            body=_content(data, encoding, status, host),
        )

    def _discard(self, key: tuple[str, str, int]) -> None:
        connection = self._pool().pop(key, None)
        if connection is not None:
            connection.close()

    def close(self) -> None:
        """Close this thread's pooled connections."""
        for key in list(self._pool()):
            self._discard(key)


def _alive(connection: http.client.HTTPConnection) -> bool:
    """Whether an idle pooled connection can carry another request.

    Between exchanges an HTTP/1.1 connection has nothing to read, so a
    readable socket means the server closed it (EOF) or sent something
    unasked. Either way, a request written to it may never be answered.
    """
    sock = connection.sock
    if sock is None:
        return False
    try:
        if isinstance(sock, ssl.SSLSocket) and sock.pending():
            return False
        if hasattr(select, "poll"):
            poller = select.poll()
            poller.register(sock, select.POLLIN)
            return not poller.poll(0)
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return False
    return not readable


def _split(url: str) -> tuple[str, str, int, str]:
    """Scheme, host, port and request target of an http(s) URL."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as err:
        raise ProviderError(f"malformed URL: {err}", retryable=False) from err
    scheme = parts.scheme or "https"
    host = parts.hostname or ""
    if scheme not in ("http", "https"):
        raise ProviderError(f"unsupported URL scheme {scheme!r}", retryable=False)
    if not host:
        where = url.partition("?")[0][:100]
        raise ProviderError(f"URL has no host: {where!r}", retryable=False)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return scheme, host, port or (443 if scheme == "https" else 80), target


def _check_headers(headers: Mapping[str, str]) -> None:
    """Refuse a header http.client would refuse, without quoting its value.

    http.client's own error repeats the value, and the value may be an API
    key with a stray newline: printing that error would print the key.
    """
    for name, value in headers.items():
        try:
            legal = bool(_HEADER_NAME.fullmatch(name.encode("ascii")))
            value.encode("latin-1")
        except UnicodeEncodeError:
            legal = False
        if not legal or "\r" in value or "\n" in value:
            raise ProviderError(
                f"request header {name!r} has a line break or a character HTTP "
                "headers cannot carry",
                retryable=False,
                hint="if it holds the API key, check the key for stray "
                "whitespace or pasted characters",
            )


def _failure(err: BaseException, host: str) -> ProviderError:
    """The ProviderError for an exchange that socket, ssl or http.client ended."""
    if isinstance(err, ssl.SSLCertVerificationError):
        return ProviderError(
            f"TLS certificate for {host} could not be verified: {err.reason}",
            retryable=False,
            hint="run `devdocs doctor`; on macOS python.org builds, run "
            "'Install Certificates.command' or set SSL_CERT_FILE",
        )
    if isinstance(err, http.client.IncompleteRead):
        return ProviderError(f"connection to {host} dropped mid-response ({err!r})")
    if isinstance(err, http.client.RemoteDisconnected):
        return ProviderError(f"{host} closed the connection without answering")
    if isinstance(err, http.client.ImproperConnectionState):
        return ProviderError(f"connection to {host} fell out of step: {err!r}")
    if isinstance(err, http.client.InvalidURL):
        return ProviderError(f"invalid URL for {host}: {err}", retryable=False)
    if isinstance(err, http.client.HTTPException):
        return ProviderError(
            f"{host} sent a reply that is not valid HTTP/1.1: {repr(err)[:200]}",
            retryable=False,
            hint="a proxy or middlebox may be in the way; check HTTPS_PROXY",
        )
    if isinstance(err, ValueError):   # headers are checked first, so no secrets here
        return ProviderError(f"cannot send a request to {host}: {err}", retryable=False)
    tunnel = _TUNNEL_FAILED.search(str(err))
    if tunnel:
        code = int(tunnel.group(1))
        return ProviderError(
            f"the proxy would not connect to {host}: {err}",
            retryable=code == 429 or code >= 500,
            hint="check the proxy and its credentials in HTTPS_PROXY, or exempt "
            "the host with NO_PROXY",
        )
    return ProviderError(f"network error talking to {host}: {err}")


def _too_large(host: str) -> ProviderError:
    return ProviderError(
        f"response from {host} exceeded {MAX_RESPONSE_BYTES} bytes", retryable=False
    )


class _Undecodable(Exception):
    """A body whose Content-Encoding could not be undone."""

    def __init__(self, reason: str, *, retryable: bool) -> None:
        super().__init__(reason)
        self.retryable = retryable


def _content(data: bytes, encoding: str, status: int, host: str) -> bytes:
    """The body with its Content-Encoding undone.

    A 2xx body that cannot be decoded is a failed response. An error body
    that cannot be is dropped instead: the status still says what failed,
    and the provider maps that (401 to AuthError, 429 to RateLimited...).
    """
    if not data:
        return data
    try:
        return _decode(data, encoding)
    except _Undecodable as err:
        if not 200 <= status < 300:
            return b""
        raise ProviderError(
            f"could not decode the {encoding} response from {host}: {err}",
            retryable=err.retryable,
        ) from err


def _decode(data: bytes, encoding: str) -> bytes:
    """Undo a Content-Encoding, last coding first, inflating to the size cap."""
    for coding in reversed([part.strip().lower() for part in encoding.split(",")]):
        if coding in ("", "identity"):
            continue
        if coding in ("gzip", "x-gzip"):
            data = _inflate(data, _GZIP)
        elif coding == "deflate":
            try:
                data = _inflate(data, zlib.MAX_WBITS)
            except _Undecodable:
                data = _inflate(data, -zlib.MAX_WBITS)   # raw deflate
        else:   # never asked for: Accept-Encoding offers gzip and deflate only
            raise _Undecodable(f"unsupported content-encoding {coding!r}",
                               retryable=False)
    return data


def _inflate(data: bytes, wbits: int) -> bytes:
    """Decompress every member of `data` into at most MAX_RESPONSE_BYTES.

    Bounded as it goes: a few kilobytes of gzip can expand to gigabytes.
    """
    limit = MAX_RESPONSE_BYTES
    out = bytearray()
    while True:
        stream = zlib.decompressobj(wbits)
        try:
            out += stream.decompress(data, limit + 1 - len(out))
        except zlib.error as err:
            raise _Undecodable(str(err), retryable=True) from err
        if len(out) > limit:
            raise ProviderError(
                f"response exceeded {limit} bytes once decompressed", retryable=False
            )
        if not stream.eof:
            raise _Undecodable("compressed body ended early", retryable=True)
        data = stream.unused_data.lstrip(b"\0")   # gzip members may follow
        if wbits != _GZIP or not data:
            return bytes(out)


def _proxy_for(scheme: str, host: str) -> str | None:
    """The proxy URL for this host from the environment, or None.

    Uses urllib's own reading of HTTPS_PROXY/HTTP_PROXY and NO_PROXY (and, on
    macOS, the system proxy settings), so behaviour matches other tools.
    """
    proxies = urllib.request.getproxies()
    proxy = proxies.get(scheme)
    if not proxy or urllib.request.proxy_bypass(host):
        return None
    return proxy


def _proxy_address(proxy: str) -> tuple[str, int, dict[str, str]]:
    """Host, port and CONNECT headers for a proxy URL.

    A bare `host:port` is an HTTP proxy, as urllib reads it, and a missing
    port is the scheme's default. Errors never quote the URL: it may hold a
    password.
    """
    hint = "check HTTPS_PROXY / HTTP_PROXY (and NO_PROXY) in the environment"
    if "://" not in proxy:
        proxy = "http://" + proxy
    try:
        parts = urlsplit(proxy)
        port = parts.port
    except ValueError:
        raise ProviderError(
            "the proxy URL has a malformed host or port", retryable=False, hint=hint
        ) from None
    if parts.scheme not in ("http", "https"):
        raise ProviderError(
            f"{parts.scheme} proxies are not supported, only HTTP CONNECT proxies",
            retryable=False,
            hint=hint,
        )
    if not parts.hostname:
        raise ProviderError("the proxy URL has no host", retryable=False, hint=hint)

    headers: dict[str, str] = {}
    if parts.username:
        credentials = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
        token = base64.b64encode(credentials.encode()).decode()
        headers["Proxy-Authorization"] = f"Basic {token}"
    return parts.hostname, port or (443 if parts.scheme == "https" else 80), headers


def parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait from a Retry-After header: delay-seconds or HTTP-date.

    Anything else is None, including "inf", "nan", "-5" and "1e3", which
    `float()` would accept: a malformed header must not become an instant
    retry or an endless wait. A past date is 0. A date without a zone is read
    as GMT, the only zone HTTP dates use.
    """
    if not value:
        return None
    value = value.strip()
    if _DELAY_SECONDS.fullmatch(value):
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        seconds = moment.timestamp() - time.time()
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0.0, seconds)


def _server_delay(value: float | None) -> float | None:
    """A server-given delay if usable: NaN and negative values are not."""
    if value is None or math.isnan(value) or value < 0:
        return None
    return float(value)


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with full jitter, honouring server-given delays.

    Only failures that can succeed on a second try are retried: rate limits,
    5xx, and network errors. Bad keys, unknown models and bad requests fail
    fast, because retrying them only spends quota to learn the same thing.
    KeyboardInterrupt and other non-Exception signals are never retried.
    """

    attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 60.0
    # A server asking for more than this is signalling a quota window, not a
    # blip; waiting it out inside one request would stall the whole run.
    max_retry_after: float = 120.0

    def should_retry(self, error: BaseException) -> bool:
        return isinstance(error, ProviderError) and error.retryable

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Seconds to sleep before retry number `attempt` (1-based).

        Always finite and non-negative: a NaN or negative `retry_after` is
        ignored in favour of the backoff.
        """
        server = _server_delay(retry_after)
        if server is not None:
            return max(0.0, min(server, self.max_retry_after))
        exponent = min(max(attempt - 1, 0), 32)
        ceiling = min(self.max_delay, self.base_delay * 2.0**exponent)
        return random.uniform(0, max(0.0, ceiling))

    def run(
        self,
        operation: Callable[[], T],
        *,
        sleep: Callable[[float], None] = time.sleep,
        on_retry: Callable[[int, BaseException, float], None] | None = None,
    ) -> T:
        """Call `operation` up to `attempts` times, retrying only retryable failures."""
        attempt = 1
        while True:
            try:
                return operation()
            except Exception as err:
                if attempt >= self.attempts or not self.should_retry(err):
                    raise
                retry_after = (
                    _server_delay(err.retry_after) if isinstance(err, RateLimited)
                    else None
                )
                if retry_after is not None and retry_after > self.max_retry_after:
                    raise
                wait = self.delay(attempt, retry_after)
                if on_retry is not None:
                    on_retry(attempt, err, wait)
                sleep(wait)
                attempt += 1
