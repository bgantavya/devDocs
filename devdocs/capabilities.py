"""What this interpreter and machine can actually do, detected at runtime.

A zero-dependency tool cannot lean on a package manager to guarantee its
environment, so it has to ask. Every check here is cheap, side-effect free, and
never raises: a probe that fails returns a Capability describing the failure,
because "no CA certificates" must be reported, not crash the run.

`devdocs doctor` is a thin printer over `probe_all`.
"""

from __future__ import annotations

import sqlite3
import ssl
import sys
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["Capability", "Report", "probe_all"]

# Syntax that only parses on the interpreter version that introduced it.
# Used to warn when documenting a codebase newer than the running Python.
_SYNTAX_PROBES: tuple[tuple[str, str, str], ...] = (
    ("match", "3.10", "match x:\n    case 1: pass"),
    ("except-star", "3.11", "try: pass\nexcept* TypeError: pass"),
    ("pep695-generics", "3.12", "def f[T](x: T) -> T: return x"),
    ("pep701-fstrings", "3.12", 'f"{"a"}"'),
)


@dataclass(frozen=True)
class Capability:
    """One probe result. `ok` False is a finding, never an exception."""

    name: str
    ok: bool
    value: str = ""
    detail: str = ""

    def __str__(self) -> str:
        mark = "ok " if self.ok else "!! "
        line = f"{mark}{self.name:22} {self.value}"
        return f"{line}\n{'':25}{self.detail}" if self.detail else line


@dataclass(frozen=True)
class Report:
    """A full probe sweep, plus whether anything blocks a run."""

    capabilities: tuple[Capability, ...]

    @property
    def failures(self) -> tuple[Capability, ...]:
        return tuple(c for c in self.capabilities if not c.ok)

    @property
    def ok(self) -> bool:
        return not self.failures

    def get(self, name: str) -> Capability | None:
        return next((c for c in self.capabilities if c.name == name), None)

    def __str__(self) -> str:
        return "\n".join(str(c) for c in self.capabilities)


def _probe(name: str, fn: Callable[[], Capability]) -> Capability:
    """Run one probe, converting an unexpected crash into a failed Capability."""
    try:
        return fn()
    except Exception as err:  # a probe must never end the run
        return Capability(name, False, "probe failed", f"{type(err).__name__}: {err}")


def _python() -> Capability:
    version = sys.version.split()[0]
    return Capability(
        "python",
        sys.version_info >= (3, 11),
        version,
        "" if sys.version_info >= (3, 11) else "devdocs needs 3.11 or newer",
    )


def _syntax_level() -> Capability:
    """Highest syntax this interpreter parses, which bounds parse fidelity.

    Syntax newer than the interpreter blocks no run: a file that uses it gets
    the token scan's fewer facts, or none, and the rest are unaffected. So it
    is noted, never failed; an interpreter too old to run devdocs at all is
    the python probe's to fail.
    """
    import ast

    supported: list[str] = []
    newer: list[str] = []
    for label, since, source in _SYNTAX_PROBES:
        try:
            ast.parse(source)
            supported.append(label)
        except SyntaxError:
            newer.append(f"{label} ({since}+)")

    return Capability(
        "syntax",
        True,
        f"{len(supported)}/{len(_SYNTAX_PROBES)} constructs",
        "" if not newer else (f"cannot parse: {', '.join(newer)}; files that "
                              "use them get fewer facts or none"),
    )


def _sqlite() -> Capability:
    return Capability("sqlite", True, sqlite3.sqlite_version)


def _sqlite_threads() -> Capability:
    """threadsafety 1 means one connection per thread; 3 means shareable."""
    level = sqlite3.threadsafety
    meaning = {
        0: "single-thread; concurrency disabled",
        1: "module-safe; one connection per thread",
        2: "connections not shareable",
        3: "connections shareable",
    }.get(level, "unknown")
    return Capability("sqlite-threads", level > 0, str(level), meaning)


def _tls() -> Capability:
    """TLS 1.3 is not required, but its absence dates the interpreter."""
    has13 = getattr(ssl, "HAS_TLSv1_3", False)
    return Capability(
        "tls",
        True,
        ssl.OPENSSL_VERSION,
        "" if has13 else "no TLS 1.3; upgrade Python if a provider requires it",
    )


def _certificates() -> Capability:
    """An empty trust store means every HTTPS call fails with a confusing error."""
    context = ssl.create_default_context()
    count = len(context.get_ca_certs())
    return Capability(
        "certificates",
        count > 0,
        f"{count:,} CA certificate{'' if count == 1 else 's'}",
        "" if count else "run 'Install Certificates.command' or set SSL_CERT_FILE",
    )


_PROBES: tuple[tuple[str, Callable[[], Capability]], ...] = (
    ("python", _python),
    ("syntax", _syntax_level),
    ("sqlite", _sqlite),
    ("sqlite-threads", _sqlite_threads),
    ("tls", _tls),
    ("certificates", _certificates),
)


def probe_all() -> Report:
    """Run every probe. Always returns; never raises."""
    return Report(tuple(_probe(name, fn) for name, fn in _PROBES))
