"""Transport behaviour against a real local server, and retry policy."""

from __future__ import annotations

import base64
import contextlib
import gzip
import http.client
import json
import math
import os
import socket
import ssl
import struct
import tempfile
import threading
import time
import unittest
import zlib
from collections.abc import Callable, Iterator
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest import mock

from devdocs.errors import AuthError, ProviderError, RateLimited
from devdocs.llm import http as http_module
from devdocs.llm.http import HttpResponse, HttpTransport, RetryPolicy, parse_retry_after

# A self-signed certificate for 127.0.0.1, trusted by nothing (test data only).
_KEY = """-----BEGIN EC PRIVATE KEY-----
MHcCAQEEIGo+ZbPDyTfJNhi2RTg/N8rrmxdceVoVnzjOOzFTYxQJoAoGCCqGSM49
AwEHoUQDQgAEn/0H73JDtWtVFM2BItZ5Q0uMMiu1Ww9H7jt/37nCspQ/KuZ0rvxj
pza4VJ/4lO5fQH28vVvjpF/FcXDG0jdACQ==
-----END EC PRIVATE KEY-----
"""
_CERT = """-----BEGIN CERTIFICATE-----
MIIBPTCB5aADAgECAgkA7feipCSyjAowCgYIKoZIzj0EAwIwFDESMBAGA1UEAwwJ
bG9jYWxob3N0MCAXDTI2MTEwMTEzMTQ0MFoYDzIxMjYxMDA4MTMxNDQwWjAUMRIw
EAYDVQQDDAlsb2NhbGhvc3QwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAASf/Qfv
ckO1a1UUzYEi1nlDS4wyK7VbD0fuO3/fucKylD8q5nSu/GOnNrhUn/iU7l9Afby9
W+OkX8VxcMbSN0AJox4wHDAaBgNVHREEEzARgglsb2NhbGhvc3SHBH8AAAEwCgYI
KoZIzj0EAwIDRwAwRAIgV0jEGgu1fDcKrWyAZUOM0BVra8GGu5v/vrpgEibUgn4C
IHAX5W+6qaTcy0Cb9m4xcXe0aD64OkwDlSyMNn8znDSu
-----END CERTIFICATE-----
"""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive
    connections: ClassVar[set[tuple[str, int]]] = set()

    def log_message(self, *args: object) -> None:   # keep test output quiet
        pass

    def _send(self, status: int, body: bytes, **headers: str) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        type(self).connections.add(self.client_address)
        if self.path == "/gzip":
            self._send(200, gzip.compress(b'{"ok": true}'), Content_Encoding="gzip")
        elif self.path == "/deflate":
            self._send(200, zlib.compress(b"deflated"), Content_Encoding="deflate")
        elif self.path == "/close":
            self._send(200, b"bye", Connection="close")
            self.close_connection = True
        elif self.path == "/echo-headers":
            self._send(200, json.dumps(dict(self.headers)).encode())
        elif self.path == "/notjson":
            self._send(500, b"<html>oops</html>")
        else:
            self._send(200, b'{"path": "' + self.path.encode() + b'"}')

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        self._send(200, self.rfile.read(length))


@contextlib.contextmanager
def _environment(**values: str) -> Iterator[None]:
    """os.environ without any *_proxy variable, plus `values`."""
    kept = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    with mock.patch.dict(os.environ, {**kept, **values}, clear=True):
        yield


def _read_request(conn: socket.socket, pending: bytes) -> tuple[bytes | None, bytes]:
    """One request's head, after reading its whole body; None at EOF."""
    while b"\r\n\r\n" not in pending:
        chunk = conn.recv(65536)
        if not chunk:
            return None, b""
        pending += chunk
    head, _, rest = pending.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value)
    while len(rest) < length:
        chunk = conn.recv(65536)
        if not chunk:
            return None, b""
        rest += chunk
    return head, rest[length:]


def _reply(status: str, body: bytes = b"{}", *headers: str) -> bytes:
    """A complete HTTP/1.1 response with a Content-Length."""
    lines = (f"HTTP/1.1 {status}", f"Content-Length: {len(body)}", *headers)
    return "".join(f"{line}\r\n" for line in lines).encode() + b"\r\n" + body


def _chunked(body: bytes, *headers: str) -> bytes:
    lines = ("HTTP/1.1 200 OK", "Transfer-Encoding: chunked", *headers)
    head = "".join(f"{line}\r\n" for line in lines).encode() + b"\r\n"
    return head + b"%x\r\n" % len(body) + body + b"\r\n0\r\n\r\n"


Reply = bytes | tuple[bytes, str] | None


class _Scripted:
    """A raw socket server whose replies are scripted.

    `reply(n)` gets the 1-based number of the request across all connections
    and returns the bytes to send; `(bytes, "close")` to send them and close;
    `(bytes, "reset")` to send them, wait for `release`, then reset the
    connection; or None to close without answering. Counts are updated
    before the reply goes out, so the client always sees them current.
    """

    def __init__(self, reply: Callable[[int], Reply]) -> None:
        self.reply = reply
        self.requests = 0
        self.connections = 0
        self.closed = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._listener.getsockname()[1]}/v1/x"
        threading.Thread(target=self._accept, daemon=True).start()

    def stop(self) -> None:
        self._listener.close()
        self.release.set()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            with self._lock:
                self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        pending = b""
        try:
            while True:
                head, pending = _read_request(conn, pending)
                if head is None:
                    return
                with self._lock:
                    self.requests += 1
                    number = self.requests
                answer = self.reply(number)
                if answer is None:
                    return
                data, after = answer if isinstance(answer, tuple) else (answer, "keep")
                conn.sendall(data)
                if after == "reset":
                    self.release.wait(5)
                    conn.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                    )
                if after != "keep":
                    return
        except OSError:
            return
        finally:
            conn.close()
            self.closed.set()


def _pipe(source: socket.socket, sink: socket.socket) -> None:
    try:
        while data := source.recv(65536):
            sink.sendall(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            sink.shutdown(socket.SHUT_WR)


class _Proxy:
    """A CONNECT proxy that checks Basic credentials, then pipes bytes."""

    def __init__(self, credentials: str = "") -> None:
        self.credentials = credentials
        self.tunnels: list[str] = []
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def stop(self) -> None:
        self._listener.close()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._tunnel, args=(client,), daemon=True).start()

    def _tunnel(self, client: socket.socket) -> None:
        with client, contextlib.suppress(OSError):
            head, _ = _read_request(client, b"")
            if head is None:
                return
            lines = head.decode("latin-1").split("\r\n")
            given = ""
            for line in lines[1:]:
                name, _, value = line.partition(":")
                if name.strip().lower() == "proxy-authorization":
                    given = base64.b64decode(value.split()[-1]).decode()
            if given != self.credentials:
                client.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                               b"Content-Length: 0\r\n\r\n")
                return
            target = lines[0].split()[1]
            self.tunnels.append(target)
            host, _, port = target.rpartition(":")
            with socket.create_connection((host, int(port))) as upstream:
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                threading.Thread(target=_pipe, args=(client, upstream),
                                 daemon=True).start()
                _pipe(upstream, client)


class LocalCase(unittest.TestCase):
    """A transport that ignores the developer's proxy settings."""

    def setUp(self) -> None:
        self.enterContext(_environment())
        self.transport = HttpTransport(timeout=5)
        self.addCleanup(self.transport.close)

    def scripted(self, reply: Callable[[int], Reply]) -> _Scripted:
        server = _Scripted(reply)
        self.addCleanup(server.stop)
        return server


class TransportCase(LocalCase):
    server: ThreadingHTTPServer
    base: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        serve = threading.Thread(target=cls.server.serve_forever,
                                 kwargs={"poll_interval": 0.05}, daemon=True)
        serve.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _Handler.connections = set()
        super().setUp()


class TestTransport(TransportCase):
    def test_connection_is_reused(self) -> None:
        for _ in range(3):
            self.assertTrue(self.transport.request("GET", self.base + "/a").ok)
        self.assertEqual(len(_Handler.connections), 1)

    def test_gzip_and_deflate_are_decoded(self) -> None:
        self.assertEqual(self.transport.request("GET", self.base + "/gzip").json(),
                         {"ok": True})
        self.assertEqual(self.transport.request("GET", self.base + "/deflate").body,
                         b"deflated")

    def test_server_close_is_honoured_then_reconnects(self) -> None:
        self.transport.request("GET", self.base + "/close")
        self.assertTrue(self.transport.request("GET", self.base + "/after").ok)
        self.assertEqual(len(_Handler.connections), 2)

    def test_default_and_caller_headers_are_sent(self) -> None:
        sent = self.transport.request(
            "GET", self.base + "/echo-headers", headers={"X-Test": "1"}
        ).json()
        self.assertIn("gzip", sent["Accept-Encoding"])
        self.assertTrue(sent["User-Agent"].startswith("devdocs/"))
        self.assertEqual(sent["X-Test"], "1")

    def test_post_body_round_trips(self) -> None:
        response = self.transport.request("POST", self.base + "/", body=b"payload")
        self.assertEqual(response.body, b"payload")

    def test_non_2xx_is_returned_not_raised(self) -> None:
        response = self.transport.request("GET", self.base + "/notjson")
        self.assertEqual(response.status, 500)
        self.assertFalse(response.ok)
        with self.assertRaises(ProviderError):
            response.json()

    def test_unreachable_host_raises_provider_error(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            self.transport.request("GET", "http://127.0.0.1:1/")
        self.assertTrue(caught.exception.retryable)

    def test_complete_chunked_responses_keep_the_connection(self) -> None:
        server = self.scripted(lambda n: _chunked(b'{"n": %d}' % n))
        for n in (1, 2):
            response = self.transport.request("POST", server.url)
            self.assertEqual(response.json(), {"n": n})
        self.assertEqual(server.connections, 1)


class TestNeverSentTwice(LocalCase):
    """A paid request reaches the server at most once per `request` call."""

    def post(self, server: _Scripted, body: bytes = b"{}") -> HttpResponse:
        return self.transport.request("POST", server.url, body=body)

    def test_a_written_request_is_not_resent_when_the_reply_never_comes(self) -> None:
        server = self.scripted(lambda n: _reply("200 OK") if n == 1 else None)
        self.post(server)
        with self.assertRaises(ProviderError) as caught:
            self.post(server)
        self.assertTrue(caught.exception.retryable)   # RetryPolicy's call, visibly
        self.assertEqual(server.requests, 2)

    def test_a_reply_that_is_not_http_is_neither_resent_nor_retried(self) -> None:
        server = self.scripted(
            lambda n: _reply("200 OK") if n == 1 else (b"HTTP/1.1 OK\r\n\r\n", "close")
        )
        self.post(server)
        with self.assertRaises(ProviderError) as caught:
            self.post(server)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(server.requests, 2)

    def test_a_pooled_connection_the_server_closed_is_replaced_before_use(self) -> None:
        server = self.scripted(lambda n: (_reply("200 OK"), "close"))
        self.post(server)
        self.assertTrue(server.closed.wait(5))
        self.assertTrue(self.post(server).ok)
        self.assertEqual((server.requests, server.connections), (2, 2))

    def test_a_failed_write_to_a_dead_pooled_connection_is_resent_once(self) -> None:
        server = self.scripted(lambda n: (_reply("200 OK"), "reset") if n == 1
                               else _reply("200 OK"))
        self.post(server)
        server.release.set()
        self.assertTrue(server.closed.wait(5))
        with mock.patch.object(http_module, "_alive", return_value=True):
            self.assertTrue(self.post(server).ok)   # write fails, then one resend
        self.assertEqual((server.requests, server.connections), (2, 2))

    def test_an_interrupted_exchange_is_never_reused(self) -> None:
        server = self.scripted(lambda n: _reply("200 OK"))
        with (
            mock.patch.object(http.client.HTTPResponse, "read",
                              side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.post(server)
        self.assertTrue(self.post(server).ok)
        self.assertEqual((server.requests, server.connections), (2, 2))


class TestBrokenReplies(LocalCase):
    """Everything that stops a response arriving is a typed ProviderError."""

    def fails(self, reply: bytes, *, timeout: float = 5) -> ProviderError:
        server = self.scripted(lambda n: (reply, "close"))
        with self.assertRaises(ProviderError) as caught:
            HttpTransport(timeout=timeout).request("POST", server.url, body=b"{}")
        return caught.exception

    def test_a_chunked_body_cut_off_is_a_retryable_failure(self) -> None:
        body = json.dumps({"text": "docs " * 300}).encode()
        for encoded, headers in ((body, ()), (gzip.compress(body),
                                              ("Content-Encoding: gzip",))):
            with self.subTest(headers=headers):
                full = _chunked(encoded, *headers)
                error = self.fails(full[: len(full) - 7 - len(encoded) // 2])
                self.assertTrue(error.retryable)

    def test_a_body_short_of_its_content_length_is_not_returned(self) -> None:
        error = self.fails(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{"text":')
        self.assertTrue(error.retryable)
        self.assertIn("8 of 100 bytes", error.message)

    def test_protocol_violations_are_not_retried(self) -> None:
        many = b"".join(b"X-%d: y\r\n" % i for i in range(150))
        replies = {
            "headers": b"HTTP/1.1 200 OK\r\n" + many + b"Content-Length: 2\r\n\r\n{}",
            "line": b"HTTP/1.1 200 OK\r\nX: " + b"a" * 70_000 + b"\r\n\r\n",
            "version": b"HTTP/2.0 200 OK\r\nContent-Length: 2\r\n\r\n{}",
            "switch": b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: h2c\r\n\r\n",
        }
        for name, reply in replies.items():
            with self.subTest(name):
                self.assertFalse(self.fails(reply).retryable)

    def test_interim_responses_are_read_past(self) -> None:
        def reply(n: int) -> bytes:
            hints = b"HTTP/1.1 103 Early Hints\r\nLink: </a.css>; rel=preload\r\n\r\n"
            return hints + _reply("200 OK", b'{"answer": %d}' % n)

        server = self.scripted(reply)
        answers = [self.transport.request("POST", server.url).json() for _ in "ab"]
        self.assertEqual(answers, [{"answer": 1}, {"answer": 2}])

    def test_interim_responses_are_bounded(self) -> None:
        limit = http_module._MAX_INTERIM
        hint = b"HTTP/1.1 103 Early Hints\r\n\r\n"
        server = self.scripted(lambda n: (hint * limit + _reply("200 OK"), "close"))
        self.assertTrue(self.transport.request("POST", server.url).ok)
        self.assertFalse(self.fails(hint * (limit + 1) + _reply("200 OK")).retryable)

    def test_a_server_that_never_answers_times_out_retryably(self) -> None:
        hold = threading.Event()
        self.addCleanup(hold.set)

        def stall(n: int) -> Reply:
            hold.wait(5)
            return None

        server = self.scripted(stall)
        with self.assertRaises(ProviderError) as caught:
            HttpTransport(timeout=0.05).request("POST", server.url, body=b"{}")
        self.assertTrue(caught.exception.retryable)


class TestDecoding(LocalCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(http_module, "MAX_RESPONSE_BYTES", 4096))

    def fetch(self, reply: bytes) -> HttpResponse:
        server = self.scripted(lambda n: (reply, "close"))
        return self.transport.request("GET", server.url)

    def test_the_size_cap_holds_after_decompression(self) -> None:
        bomb = gzip.compress(b"\0" * 1_000_000)
        for reply in (_reply("200 OK", bomb, "Content-Encoding: gzip"),
                      _reply("200 OK", b"x" * 5000)):
            with self.assertRaises(ProviderError) as caught:
                self.fetch(reply)
            self.assertFalse(caught.exception.retryable)

    def test_gzip_members_and_raw_deflate_are_decoded(self) -> None:
        members = gzip.compress(b'{"a": ') + gzip.compress(b"1}")
        raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        deflated = raw.compress(b"raw") + raw.flush()
        self.assertEqual(
            self.fetch(_reply("200 OK", members, "Content-Encoding: gzip")).json(),
            {"a": 1},
        )
        self.assertEqual(
            self.fetch(_reply("200 OK", deflated, "Content-Encoding: deflate")).body,
            b"raw",
        )

    def test_an_unrequested_encoding_on_a_success_is_refused(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            self.fetch(_reply("200 OK", b"\x1b\x00\x00", "Content-Encoding: br"))
        self.assertFalse(caught.exception.retryable)

    def test_an_undecodable_error_body_keeps_its_status(self) -> None:
        for status, coding in ((401, "gzip"), (503, "gzip"), (429, "zstd")):
            response = self.fetch(_reply(
                f"{status} Error", b"\x28\xb5", "Retry-After: 7",
                f"Content-Encoding: {coding}",
            ))
            self.assertEqual((response.status, response.body), (status, b""))
            self.assertEqual(response.header("retry-after"), "7")

    def test_an_empty_body_is_not_decoded(self) -> None:
        reply = b"HTTP/1.1 204 No Content\r\nContent-Encoding: deflate\r\n\r\n"
        self.assertEqual(self.fetch(reply).status, 204)

    def test_redirects_are_returned_not_followed(self) -> None:
        response = self.fetch(_reply("302 Found", b"", "Location: http://x.example/"))
        self.assertEqual((response.status, response.header("location")),
                         (302, "http://x.example/"))


class TestUnsendable(LocalCase):
    def test_a_bad_header_is_refused_without_quoting_it(self) -> None:
        server = self.scripted(lambda n: _reply("200 OK"))
        for key in ("SECRET-KEY\nX-Evil: 1", "SECRET-KEY​"):
            with self.assertRaises(ProviderError) as caught:
                self.transport.request("POST", server.url, headers={"x-api-key": key})
            self.assertFalse(caught.exception.retryable)
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertIn("x-api-key", str(caught.exception))
        self.assertEqual(server.requests, 0)

    def test_malformed_urls_are_provider_errors(self) -> None:
        resolver = self.enterContext(mock.patch("socket.getaddrinfo"))
        for url in ("https://api.example:99999/", "https://[::1/", "api.example/v1",
                    "ftp://api.example/", "https://bad..host/",
                    "https://" + "a" * 64 + ".example/", "https://[::zz]/",
                    "http://127.0.0.1:1/models/a b"):
            with self.subTest(url), self.assertRaises(ProviderError) as caught:
                self.transport.request("GET", url)
            self.assertFalse(caught.exception.retryable)
        resolver.assert_not_called()   # refused before the resolver, never slowly

    def test_well_formed_hosts_are_accepted(self) -> None:
        for host in ("api.example", "API.Example.", "127.0.0.1", "[::1]",
                     "[fe80::1%25en0]", "bücher.example", "my_host.internal"):
            with self.subTest(host):
                self.assertEqual(http_module._split(f"https://{host}/v1")[3], "/v1")


class TestProxy(TransportCase):
    def proxy(self, credentials: str = "") -> _Proxy:
        proxy = _Proxy(credentials)
        self.addCleanup(proxy.stop)
        return proxy

    def test_connect_tunnel_with_percent_encoded_credentials(self) -> None:
        proxy = self.proxy("us@r:p:ss w")
        url = f"http://us%40r:p%3Ass%20w@127.0.0.1:{proxy.port}"
        with _environment(HTTP_PROXY=url):
            self.assertEqual(self.transport.request("GET", self.base + "/a").json(),
                             {"path": "/a"})
        self.assertEqual(proxy.tunnels, [self.base.removeprefix("http://")])

    def test_no_proxy_goes_direct(self) -> None:
        proxy = self.proxy()
        with _environment(HTTP_PROXY=f"http://127.0.0.1:{proxy.port}",
                          NO_PROXY="127.0.0.1"):
            self.assertTrue(self.transport.request("GET", self.base + "/a").ok)
        self.assertEqual(proxy.tunnels, [])

    def test_a_bare_host_port_proxy_is_an_http_proxy(self) -> None:
        proxy = self.proxy()
        with _environment(HTTP_PROXY=f"127.0.0.1:{proxy.port}"):
            self.assertTrue(self.transport.request("GET", self.base + "/a").ok)
        self.assertEqual(len(proxy.tunnels), 1)

    def test_a_missing_proxy_port_is_the_scheme_default(self) -> None:
        for scheme, port in (("http", 80), ("https", 443)):
            with _environment(HTTPS_PROXY=f"{scheme}://proxy.example"):
                connection = self.transport._open("https", "api.example", 443)
            self.assertEqual((connection.host, connection.port),
                             ("proxy.example", port))

    def test_refused_credentials_are_not_retried(self) -> None:
        proxy = self.proxy("right:secret")
        with (
            _environment(HTTP_PROXY=f"http://wrong:guess@127.0.0.1:{proxy.port}"),
            self.assertRaises(ProviderError) as caught,
        ):
            self.transport.request("GET", self.base + "/a")
        self.assertFalse(caught.exception.retryable)
        self.assertIn("HTTPS_PROXY", caught.exception.hint or "")
        self.assertNotIn("guess", str(caught.exception))

    def test_unusable_proxy_settings_fail_without_echoing_them(self) -> None:
        resolver = self.enterContext(mock.patch("socket.getaddrinfo"))
        for proxy in ("http://user:hunter2@proxy.example:abc",
                      "http://user:hunter2@proxy.example:99999",
                      "http://user:hunter2@proxy..example:8080",
                      "socks5://user:hunter2@proxy.example:1080"):
            with (
                self.subTest(proxy),
                _environment(HTTPS_PROXY=proxy),
                self.assertRaises(ProviderError) as caught,
            ):
                self.transport.request("GET", "https://api.example/v1/models")
            self.assertFalse(caught.exception.retryable)
            self.assertNotIn("hunter2", str(caught.exception))
        resolver.assert_not_called()


class TestTLS(unittest.TestCase):
    def test_an_untrusted_certificate_fails_fast_with_a_hint(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (folder / "cert.pem").write_text(_CERT)
        (folder / "key.pem").write_text(_KEY)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(folder / "cert.pem", folder / "key.pem")
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)

        def serve() -> None:
            conn, _ = listener.accept()
            with (
                conn,
                contextlib.suppress(OSError),
                context.wrap_socket(conn, server_side=True) as tls,
            ):
                tls.recv(1)

        threading.Thread(target=serve, daemon=True).start()
        port = listener.getsockname()[1]
        with _environment(), self.assertRaises(ProviderError) as caught:
            HttpTransport(timeout=5).request("GET", f"https://127.0.0.1:{port}/")
        self.assertFalse(caught.exception.retryable)
        self.assertIn("SSL_CERT_FILE", caught.exception.hint or "")


class TestResponse(unittest.TestCase):
    def test_header_lookup_is_case_insensitive_by_construction(self) -> None:
        response = HttpResponse(200, {"retry-after": "3"})
        self.assertEqual(response.header("Retry-After"), "3")
        self.assertIsNone(response.header("missing"))

    def test_bodies_the_decoder_cannot_hold_are_provider_errors(self) -> None:
        for body in (b"[" * 100_000, b'{"n": ' + b"1" * 5000 + b"}", b"\xff\xfe"):
            with self.assertRaises(ProviderError):
                HttpResponse(200, {}, body).json()


class TestRetryAfter(unittest.TestCase):
    def test_seconds(self) -> None:
        self.assertEqual(parse_retry_after("12"), 12.0)
        self.assertEqual(parse_retry_after("1.5"), 1.5)

    def test_past_http_date_is_zero(self) -> None:
        self.assertEqual(parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT"), 0.0)

    def test_garbage_and_missing(self) -> None:
        self.assertIsNone(parse_retry_after("soon"))
        self.assertIsNone(parse_retry_after(None))

    def test_numbers_outside_the_header_grammar_are_ignored(self) -> None:
        for value in ("inf", "nan", "1e309", "-5", "+5", "1e3", "0x10"):
            with self.subTest(value):
                self.assertIsNone(parse_retry_after(value))

    def test_future_http_date(self) -> None:
        seconds = parse_retry_after(formatdate(time.time() + 30, usegmt=True))
        assert seconds is not None
        self.assertAlmostEqual(seconds, 30, delta=2)

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset")
    def test_a_date_without_a_zone_is_gmt(self) -> None:
        self.addCleanup(time.tzset)
        with mock.patch.dict(os.environ, {"TZ": "IST-5:30"}):
            time.tzset()
            value = formatdate(time.time() + 3600, usegmt=True).replace(" GMT", "")
            seconds = parse_retry_after(value)
        assert seconds is not None
        self.assertAlmostEqual(seconds, 3600, delta=2)


class TestRetryPolicy(unittest.TestCase):
    def run_with(self, failures: list[BaseException], policy: RetryPolicy) -> tuple[
        str, list[float]
    ]:
        queue = list(failures)
        slept: list[float] = []

        def operation() -> str:
            if queue:
                raise queue.pop(0)
            return "done"

        return policy.run(operation, sleep=slept.append), slept

    def test_transient_failures_are_retried(self) -> None:
        result, slept = self.run_with(
            [ProviderError("down", status=503), ProviderError("net")], RetryPolicy()
        )
        self.assertEqual(result, "done")
        self.assertEqual(len(slept), 2)

    def test_server_retry_after_is_used(self) -> None:
        _, slept = self.run_with([RateLimited("slow", retry_after=7)], RetryPolicy())
        self.assertEqual(slept, [7])

    def test_client_errors_are_not_retried(self) -> None:
        for error in (ProviderError("bad", status=400), AuthError("key")):
            with self.assertRaises(type(error)):
                self.run_with([error], RetryPolicy())

    def test_long_retry_after_raises_instead_of_stalling(self) -> None:
        for delay in (3600, math.inf):
            with self.assertRaises(RateLimited):
                self.run_with([RateLimited("quota", retry_after=delay)], RetryPolicy())

    def test_unusable_server_delays_fall_back_to_backoff(self) -> None:
        for delay in (math.nan, -5.0):
            _, slept = self.run_with([RateLimited("slow", retry_after=delay)],
                                     RetryPolicy(base_delay=0.5))
            self.assertEqual(len(slept), 1)
            self.assertTrue(0 <= slept[0] <= 0.5)   # what time.sleep accepts

    def test_keyboard_interrupt_is_never_retried(self) -> None:
        calls: list[int] = []

        def operation() -> str:
            calls.append(1)
            raise KeyboardInterrupt

        slept: list[float] = []
        with self.assertRaises(KeyboardInterrupt):
            RetryPolicy().run(operation, sleep=slept.append)
        self.assertEqual((calls, slept), ([1], []))

    def test_gives_up_after_the_attempt_limit(self) -> None:
        failures: list[BaseException] = [ProviderError("down", status=503)] * 5
        with self.assertRaises(ProviderError):
            self.run_with(failures, RetryPolicy(attempts=3))

    def test_backoff_is_bounded(self) -> None:
        policy = RetryPolicy(base_delay=1, max_delay=4)
        for attempt in (*range(1, 10), 5000):
            self.assertLessEqual(policy.delay(attempt), 4)


if __name__ == "__main__":
    unittest.main()
