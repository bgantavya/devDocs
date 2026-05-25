"""Exit codes are public contract: CI scripts branch on them."""

from __future__ import annotations

import unittest

from devdocs.errors import (
    AuthError,
    BudgetExceeded,
    ConfigError,
    DevDocsError,
    ModelNotFound,
    ParseError,
    ProviderError,
    QuotaExhausted,
    RateLimited,
    StorageError,
    exit_code_for,
)

ALL: tuple[type[DevDocsError], ...] = (
    DevDocsError, ConfigError, AuthError, ProviderError, RateLimited,
    ModelNotFound, BudgetExceeded, ParseError, StorageError, QuotaExhausted,
)


class TestExitCodes(unittest.TestCase):
    def test_codes_are_unique(self) -> None:
        codes = [cls.exit_code for cls in ALL]
        self.assertEqual(len(codes), len(set(codes)))

    def test_codes_are_stable(self) -> None:
        # Changing any of these is a breaking change for callers.
        expected: dict[type[DevDocsError], int] = {
            DevDocsError: 1, ConfigError: 2, AuthError: 3, ProviderError: 4,
            RateLimited: 5, ModelNotFound: 6, BudgetExceeded: 7, ParseError: 8,
            StorageError: 9, QuotaExhausted: 10,
        }
        for cls, code in expected.items():
            self.assertEqual(cls.exit_code, code, cls.__name__)

    def test_unknown_exception_is_one(self) -> None:
        self.assertEqual(exit_code_for(ValueError("x")), 1)

    def test_interrupt_is_130(self) -> None:
        self.assertEqual(exit_code_for(KeyboardInterrupt()), 130)

    def test_known_error_uses_its_own_code(self) -> None:
        self.assertEqual(exit_code_for(BudgetExceeded("stop")), 7)


class TestHierarchy(unittest.TestCase):
    def test_everything_is_a_devdocs_error(self) -> None:
        for cls in ALL:
            self.assertTrue(issubclass(cls, DevDocsError))

    def test_provider_failures_share_a_base(self) -> None:
        for cls in (RateLimited, ModelNotFound, QuotaExhausted):
            self.assertTrue(issubclass(cls, ProviderError))


class TestRetryable(unittest.TestCase):
    def test_network_failure_without_status_is_retryable(self) -> None:
        self.assertTrue(ProviderError("dns").retryable)

    def test_rate_limit_and_server_errors_are_retryable(self) -> None:
        self.assertTrue(RateLimited("slow", status=429).retryable)
        self.assertTrue(ProviderError("down", status=503).retryable)
        self.assertTrue(ProviderError("overloaded", status=529).retryable)

    def test_client_errors_are_not(self) -> None:
        self.assertFalse(ProviderError("bad request", status=400).retryable)
        self.assertFalse(ModelNotFound("no such model").retryable)

    def test_explicit_flag_overrides_status(self) -> None:
        # A certificate failure has no status but will never heal on retry.
        self.assertFalse(ProviderError("tls", retryable=False).retryable)

    def test_spent_quota_is_not_retryable(self) -> None:
        self.assertFalse(QuotaExhausted("daily limit").retryable)
        self.assertFalse(QuotaExhausted("billing", status=402).retryable)


class TestPayload(unittest.TestCase):
    def test_hint_is_rendered_under_the_message(self) -> None:
        error = ConfigError("no key", hint="set GEMINI_API_KEY")
        self.assertEqual(str(error), "no key\n  hint: set GEMINI_API_KEY")

    def test_message_alone_without_hint(self) -> None:
        self.assertEqual(str(AuthError("bad key")), "bad key")

    def test_provider_error_carries_status_and_provider(self) -> None:
        error = ProviderError("boom", status=500, provider="gemini")
        self.assertEqual((error.status, error.provider), (500, "gemini"))

    def test_rate_limited_carries_retry_after(self) -> None:
        error = RateLimited("slow", retry_after=2.5, status=429, provider="anthropic")
        self.assertEqual(error.retry_after, 2.5)
        self.assertEqual(error.status, 429)

    def test_budget_records_spend_and_limit(self) -> None:
        error = BudgetExceeded("over", spent=0.61, limit=0.5)
        self.assertEqual((error.spent, error.limit), (0.61, 0.5))


if __name__ == "__main__":
    unittest.main()
