"""v3 command lines are recognised and translated to their v4 equivalent."""

from __future__ import annotations

import unittest

from devdocs.errors import ConfigError
from devdocs.legacy import is_v3_invocation, translate
from devdocs.llm import Request
from devdocs.llm.factory import OfflineModel, make_provider


class TestDetection(unittest.TestCase):
    def test_v3_only_flags_mark_a_v3_invocation(self) -> None:
        for argv in (["-p", "src"], ["--path=src"], ["--output", "out"],
                     ["--overwrite"], ["--name", "x", "--keywords", "a,b"]):
            with self.subTest(argv=argv):
                self.assertTrue(is_v3_invocation(argv))

    def test_v4_command_lines_are_left_alone(self) -> None:
        for argv in ([], ["generate", "--path", "x"], ["estimate", "."],
                     ["--version"], ["--include", "tests"]):
            with self.subTest(argv=argv):
                self.assertFalse(is_v3_invocation(argv))


class TestTranslation(unittest.TestCase):
    def test_full_v3_command_line(self) -> None:
        argv = ["-p", "app", "--output", "manual", "--include", "tests,build",
                "--exclude", "docs", "--api-key", "k", "--name", "Shop",
                "--description", "A shop.", "--authors", "me", "--overwrite"]
        self.assertEqual(translate(argv), [
            "generate", "app", "--per-file", "--out", "manual",
            "--include", "tests,build", "--exclude", "docs", "--api-key", "k",
            "--name", "Shop", "--description", "A shop.",
        ])

    def test_v3_placeholders_are_not_carried_over(self) -> None:
        argv = ["--name", "My Project", "--description", "No description provided."]
        self.assertEqual(translate(argv),
                         ["generate", ".", "--per-file", "--out", "docs"])


class TestFactory(unittest.TestCase):
    def test_unknown_provider_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            make_provider("openai", "k")

    def test_providers_are_built_by_name(self) -> None:
        self.assertEqual(make_provider("gemini", "k").provider, "gemini")
        self.assertEqual(make_provider("anthropic", "k").provider, "anthropic")

    def test_offline_model_refuses_to_send(self) -> None:
        model = OfflineModel("gemini")
        self.assertEqual(model.list_models(), [])
        with self.assertRaises(ConfigError):
            model.generate(Request(model="gemini-2.5-flash-lite", system="", prompt=""))


if __name__ == "__main__":
    unittest.main()
