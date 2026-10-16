"""v3 command lines are recognised and translated to their v4 equivalent."""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import unittest
from unittest import mock

from devdocs import cli, legacy
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

    def test_every_v4_command_is_known(self) -> None:
        # --output is v3-only: behind a command legacy missed, this reads as v3
        for command in cli.COMMANDS:
            with self.subTest(command=command):
                self.assertFalse(is_v3_invocation([command, "--output", "x"]))

    def test_the_v4_commands_are_the_parsers(self) -> None:
        (commands,) = [action.choices for action in cli.build_parser()._actions
                       if isinstance(action, argparse._SubParsersAction)]
        self.assertEqual(sorted(cli.COMMANDS), sorted(commands))


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

    def test_a_v4_command_line_comes_back_as_it_is(self) -> None:
        for command in cli.COMMANDS:
            argv = [command, "app", "--format", "pdf"]
            with self.subTest(command=command):
                self.assertEqual(translate(argv), argv)

    def test_arguments_v3_lacked_are_left_for_v4_to_judge(self) -> None:
        # dropped, `app` gave way to `.` and `--format pdf` to markdown
        self.assertEqual(
            translate(["--output", "manual", "--format", "pdf", "app"]),
            ["generate", ".", "--per-file", "--out", "manual",
             "--format", "pdf", "app"],
        )


class TestMain(unittest.TestCase):
    """`legacy.main`, the v3 entry point, hands `cli.main` a command line.
    `cli.main` is a stand-in here, so nothing runs."""

    def run_main(self, *argv: str) -> tuple[list[str], str]:
        """What `devDocs *argv` passes to `cli.main`, and its stderr."""
        err = io.StringIO()
        with mock.patch.object(cli, "main", return_value=0) as run, \
                mock.patch.object(sys, "argv", ["devDocs", *argv]), \
                contextlib.redirect_stderr(err):
            self.assertEqual(legacy.main(), 0)
        (passed,), _ = run.call_args
        return passed, err.getvalue()

    def test_a_v4_command_line_runs_as_given(self) -> None:
        # read as v3, this was a paid generate of the current directory
        passed, err = self.run_main("export", "app", "--format", "pdf")
        self.assertEqual(passed, ["export", "app", "--format", "pdf"])
        self.assertEqual(err, "")

    def test_a_line_v3_would_read_runs_translated_after_one_notice(self) -> None:
        # --out abbreviates v3's --output; v4's --format rides along
        for argv in ([], ["-p", "app"], ["--name", "Shop"], ["--out", "manual"],
                     ["--format", "pdf", "-p", "app"]):
            with self.subTest(argv=argv):
                passed, err = self.run_main(*argv)
                self.assertEqual(passed, translate(argv))
                self.assertEqual(err, legacy.notice(argv) + "\n")

    def test_any_other_line_goes_to_v4_as_it_is(self) -> None:
        # v3 ran none of these; translated, they ran a paid generate of .
        for argv in (["--version"], ["-h"], ["--format", "pdf"], ["generat", "app"],
                     ["--", "-p", "app"]):
            with self.subTest(argv=argv):
                passed, err = self.run_main(*argv)
                self.assertEqual(passed, argv)
                self.assertEqual(err, "")

    def test_argv_may_be_given(self) -> None:
        with mock.patch.object(cli, "main", return_value=0) as run:
            legacy.main(["export", "app"])
        run.assert_called_once_with(["export", "app"])


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
