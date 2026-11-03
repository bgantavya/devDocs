"""Typed failures, each carrying the exit code the CLI should return.

A caller in CI needs to tell "your key is wrong" apart from "the provider is
down" apart from "the budget ran out", because only some of those are worth
retrying. Every failure therefore has a distinct class and a stable exit code,
and `DevDocsError.exit_code` is what `cli.main` returns.

Exit codes are part of the public contract and follow the same deprecation
rules as the Python API:

    1    anything unexpected, and DevDocsError itself
    2    ConfigError
    3    AuthError
    4    ProviderError
    5    RateLimited
    6    ModelNotFound
    7    BudgetExceeded
    8    unused: a file that cannot be parsed degrades to a stub, so no failure
         maps to it, and the code stays free rather than the others moving
    9    StorageError
    10   QuotaExhausted
    130  Interrupted, and KeyboardInterrupt
    143  Interrupted, when SIGTERM asked for the stop; the CLI tells them apart
"""

from __future__ import annotations

__all__ = [
    "AuthError",
    "BudgetExceeded",
    "ConfigError",
    "DevDocsError",
    "Interrupted",
    "ModelNotFound",
    "ProviderError",
    "QuotaExhausted",
    "RateLimited",
    "StorageError",
    "exit_code_for",
]


class DevDocsError(Exception):
    """Base for everything this package raises deliberately.

    `exit_code` is a class attribute so the CLI can map any failure to a
    process status without a lookup table that would drift from the classes.
    """

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message}\n  hint: {self.hint}" if self.hint else self.message


class ConfigError(DevDocsError):
    """Bad or missing configuration. Retrying will not help."""

    exit_code = 2


class AuthError(DevDocsError):
    """Provider rejected the credentials: 401, 403, or a malformed key."""

    exit_code = 3


class ProviderError(DevDocsError):
    """Provider or network failed for a reason that is not the caller's fault.

    `retryable` decides whether a second attempt can help. By default it
    follows the status: none (a network failure), 429 and 5xx are worth
    retrying; other 4xx are not. Pass it explicitly when the status alone
    misleads, e.g. a TLS certificate failure has no status but never heals.

    `retry_after` is seconds when the provider said how long to wait, as a
    429 or a 503 may. `RetryPolicy` then waits that long instead of backing
    off, or gives up at once when it is too long to wait inside one request.
    """

    exit_code = 4

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        provider: str | None = None,
        hint: str | None = None,
        retryable: bool | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.status = status
        self.provider = provider
        self.retry_after = retry_after
        self._retryable = retryable

    @property
    def retryable(self) -> bool:
        if self._retryable is not None:
            return self._retryable
        return self.status is None or self.status == 429 or self.status >= 500


class RateLimited(ProviderError):
    """A 429: too many requests for now. A 503 is a plain ProviderError, the
    provider being down, and its `retry_after` is honoured all the same."""

    exit_code = 5

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        status: int | None = None,
        provider: str | None = None,
    ) -> None:
        super().__init__(message, status=status, provider=provider,
                         retry_after=retry_after)


class ModelNotFound(ProviderError):
    """404 for a model id. Retired for the rest of the run rather than retried."""

    exit_code = 6

    def __init__(
        self, message: str, *, status: int | None = 404, provider: str | None = None
    ) -> None:
        super().__init__(message, status=status, provider=provider, retryable=False)


class QuotaExhausted(ProviderError):
    """A daily or plan quota is used up. Waiting minutes will not help.

    Distinct from RateLimited so the run stops cleanly, its answers cached for
    a later run to buy only the rest, instead of sleeping inside a request
    until a timeout. The default hint names a command the CLI accepts:
    `devdocs generate` resumes by default. A 402 is a billing problem, which
    no wait fixes.
    """

    exit_code = 10

    def __init__(
        self,
        message: str,
        *,
        status: int | None = 429,
        provider: str | None = None,
        hint: str | None = None,
    ) -> None:
        if hint is None:
            hint = (
                "fix the account's billing, then run `devdocs generate` again "
                "(it resumes where it stopped)"
                if status == 402 else
                "run `devdocs generate` again once the quota resets (it resumes "
                "where it stopped), or use a paid key"
            )
        super().__init__(
            message, status=status, provider=provider, retryable=False, hint=hint
        )


class BudgetExceeded(DevDocsError):
    """The run hit its spend or request ceiling and stopped on purpose.

    Not an error in the usual sense: work completed before the ceiling is kept,
    its answers cached, and running again buys only the rest.
    """

    exit_code = 7

    def __init__(self, message: str, *, spent: float = 0.0, limit: float = 0.0) -> None:
        super().__init__(message)
        self.spent = spent
        self.limit = limit


class StorageError(DevDocsError):
    """The SQLite index is unreadable, locked, or from a newer schema."""

    exit_code = 9


class Interrupted(DevDocsError):
    """Work a stop request refused to start; see `devdocs.llm.stop`.

    Exits 130, as Ctrl-C does. The CLI, which knows the signal, reports 143
    when SIGTERM asked for the stop.
    """

    exit_code = 130


def exit_code_for(error: BaseException) -> int:
    """Map any exception to a process exit code.

    Known failures carry their own; anything unexpected is 1, so an unhandled
    bug is never mistaken for a clean budget stop.
    """
    if isinstance(error, DevDocsError):
        return error.exit_code
    if isinstance(error, KeyboardInterrupt):
        return 130
    return 1
