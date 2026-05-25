"""Typed failures, each carrying the exit code the CLI should return.

A caller in CI needs to tell "your key is wrong" apart from "the provider is
down" apart from "the budget ran out", because only some of those are worth
retrying. Every failure therefore has a distinct class and a stable exit code,
and `DevDocsError.exit_code` is what `cli.main` returns.

Exit codes are part of the public contract and follow the same deprecation
rules as the Python API.
"""

from __future__ import annotations

__all__ = [
    "AuthError",
    "BudgetExceeded",
    "ConfigError",
    "DevDocsError",
    "ModelNotFound",
    "ParseError",
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
    ) -> None:
        super().__init__(message, hint=hint)
        self.status = status
        self.provider = provider
        self._retryable = retryable

    @property
    def retryable(self) -> bool:
        if self._retryable is not None:
            return self._retryable
        return self.status is None or self.status == 429 or self.status >= 500


class RateLimited(ProviderError):
    """429 or 503. `retry_after` is seconds when the provider supplied it."""

    exit_code = 5

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        status: int | None = None,
        provider: str | None = None,
    ) -> None:
        super().__init__(message, status=status, provider=provider)
        self.retry_after = retry_after


class ModelNotFound(ProviderError):
    """404 for a model id. Retired for the rest of the run rather than retried."""

    exit_code = 6

    def __init__(
        self, message: str, *, status: int | None = 404, provider: str | None = None
    ) -> None:
        super().__init__(message, status=status, provider=provider, retryable=False)


class QuotaExhausted(ProviderError):
    """A daily or plan quota is used up. Waiting minutes will not help.

    Distinct from RateLimited so the run stops cleanly and can resume from the
    ledger tomorrow, instead of sleeping inside a request until a timeout.
    The default hint names a command the CLI accepts: `devdocs generate`
    resumes by default. A 402 is a billing problem, which no wait fixes.
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
    """The run hit its spend or token ceiling and stopped on purpose.

    Not an error in the usual sense: work completed before the ceiling is kept
    and the run ledger can resume it later.
    """

    exit_code = 7

    def __init__(self, message: str, *, spent: float = 0.0, limit: float = 0.0) -> None:
        super().__init__(message)
        self.spent = spent
        self.limit = limit


class ParseError(DevDocsError):
    """A source file could not be parsed. Degrades to a stub, rarely fatal."""

    exit_code = 8


class StorageError(DevDocsError):
    """The SQLite index is unreadable, locked, or from a newer schema."""

    exit_code = 9


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
