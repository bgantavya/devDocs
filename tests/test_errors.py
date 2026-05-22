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
    RateLimited,
    StorageError,
    exit_code_for,
)

ALL = (
    DevDocsError, ConfigError, AuthError, ProviderError, RateLimited,
    ModelNotFound, BudgetExceeded, ParseError, StorageError,
)


class TestExitCodes(unittest.TestCase):
    def test_codes_are_unique(self) -> None:
        codes = [cls.exit_code for cls in ALL]
        self.assertEqual(len(codes), len(set(codes)))

    def test_codes_are_stable(self) -> None:
        # Changing any of these is a breaking change for callers.
        expected = {
            DevDocsError: 1, ConfigError: 2, AuthError: 3, ProviderError: 4,
            RateLimited: 5, ModelNotFound: 6, BudgetExceeded: 7, ParseError: 8,
            StorageError: 9,
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
        for cls in (RateLimited, ModelNotFound):
            self.assertTrue(issubclass(cls, ProviderError))


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
