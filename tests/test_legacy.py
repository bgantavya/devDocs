"""v3 command lines are recognised and translated to their v4 equivalent."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devdocs import cli, legacy
from devdocs.errors import ConfigError
from devdocs.legacy import is_v3_invocation, translate
from devdocs.llm import Request
from devdocs.llm.factory import OfflineModel, make_provider
from tests.test_cli import CliCase
from tests.test_generate import PROJECT

_V3_PAGE = "written by devDocs 3\n"
_HAND = "written by hand\n"


class InTempDir(unittest.TestCase):
    """Each test runs from a fresh temporary directory, `self.here`."""

    def setUp(self) -> None:
        self.enterContext(contextlib.chdir(
            self.enterContext(tempfile.TemporaryDirectory())))
        self.here = Path.cwd()


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


class TestTranslation(InTempDir):
    def test_full_v3_command_line(self) -> None:
        argv = ["-p", "app", "--output", "manual", "--include", "tests,build",
                "--exclude", "docs", "--api-key", "k", "--name", "Shop",
                "--description", "A shop.", "--authors", "me", "--overwrite"]
        self.assertEqual(translate(argv), [
            "generate", "app", "--per-file", "--out", str(self.here / "manual" / "app"),
            "--include", "tests,build", "--exclude", "docs", "--api-key", "k",
            "--name", "Shop", "--description", "A shop.",
        ])

    def test_v3_placeholders_are_not_carried_over(self) -> None:
        argv = ["--name", "My Project", "--description", "No description provided."]
        self.assertEqual(translate(argv),
                         ["generate", ".", "--per-file", "--out", "docs"])

    def test_nothing_is_forced(self) -> None:
        # v4 writes paths v3 never did; forced, it would replace hand-written
        # files there
        self.assertNotIn("--force", translate([]))
        self.assertEqual(translate(["--overwrite"]), translate([]))

    def test_pages_go_where_v3_put_them(self) -> None:
        # v3: <cwd>/<output>/<project path from the cwd>; v4 reads --out from
        # the project, so only a project at `.` keeps it relative
        here = self.here
        for argv, out in (
            ([], "docs"),
            (["-p", "."], "docs"),
            (["-p", "app"], here / "docs" / "app"),
            (["-p", "app/shop", "--output", "manual"],
             here / "manual" / "app" / "shop"),
            (["-p", "app", "--output", str(here / "site")], here / "site" / "app"),
            (["-p", str(here)], here / "docs"),
            (["-p", ".."], here / "docs" / str(here.parent).lstrip("\\/")),
        ):
            with self.subTest(argv=argv):
                translated = translate(argv)
                self.assertEqual(translated[translated.index("--out") + 1], str(out))

    def test_a_tilde_means_what_it_means_to_config(self) -> None:
        # Regression: no shell expands `--path=~/app`, so its --out was
        # <cwd>/docs/~/app, and config looked for the project at ./~/app.
        here = self.here
        home = here / "home"
        for argv, out in (
            (["--path=~/app"], here / "docs" / "home" / "app"),
            (["-p", "~/app"], here / "docs" / "home" / "app"),
            (["--path=~"], here / "docs" / "home"),
            (["--output=~/manual"], "~/manual"),   # config expands it
            (["-p", "app", "--output=~/manual"], home / "manual" / "app"),
            (["--path=~/app", "--output=~/manual"], home / "manual" / "home" / "app"),
        ):
            with self.subTest(argv=argv), \
                    mock.patch.dict(os.environ, {"HOME": str(home),
                                                 "USERPROFILE": str(home)}):
                translated = translate(argv)
                self.assertEqual(translated[translated.index("--out") + 1], str(out))
        self.assertEqual(translate(["--path=~/app"])[1], "~/app")   # config expands it

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


class TestNotice(InTempDir):
    def test_it_names_the_v4_command(self) -> None:
        argv = ["-p", "my app", "--name", "Shop"]
        self.assertIn(f"deprecated; running `devdocs {shlex.join(translate(argv))}`",
                      legacy.notice(argv))

    def test_it_says_how_to_take_over_v3_pages(self) -> None:
        for argv, docs in (([], "docs"),
                           (["-p", "app", "--output", "manual"], "manual/app")):
            with self.subTest(argv=argv):
                self.assertIn(
                    "v4 never replaces a page it has no record of writing: if "
                    f"{os.path.join(*docs.split('/'), '')} still holds v3 pages, "
                    "add --force once to take them over", legacy.notice(argv))

    def test_a_tilde_is_expanded_where_it_says_the_pages_are(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.here),
                                          "USERPROFILE": str(self.here)}):
            self.assertIn(f"if {os.path.join('docs', 'app', '')} still holds v3",
                          legacy.notice(["--path=~/app"]))

    def test_overwrite_no_longer_applies(self) -> None:
        self.assertIn("devdocs: --overwrite no longer applies; v4 never replaces",
                      legacy.notice(["--overwrite"]))
        self.assertNotIn("--overwrite", legacy.notice([]))

    def test_it_hides_the_api_key_however_spelled(self) -> None:
        for argv in (["--api-key", "SECRET"], ["--api-key=SECRET"], ["--api", "SECRET"],
                     ["--api-k=SECRET"], ["-p", ".", "--", "--api-key", "SECRET"],
                     ["-p", ".", "--", "--a=SECRET"],
                     ["-p", ".", "--api_key", "SECRET"], ["-p", ".", "--apikey=SECRET"],
                     ["-p", ".", "-api-key", "SECRET"], ["-p", ".", "--API-KEY=SECRET"],
                     ["-p", ".", "--gemini-api-key", "SECRET"],
                     ["-p", ".", "--token=SECRET"],
                     ["-p", ".", "--api_key", "--", "SECRET"],
                     ["-p", ".", "--", "--apikey", "SECRET"],
                     ["-p", ".", "GEMINI_API_KEY=SECRET"],
                     ["-p", "GEMINI_API_KEY=SECRET"],
                     ["-p", "app", "--output", "GEMINI_API_KEY=SECRET"]):
            with self.subTest(argv=argv):
                text = legacy.notice(argv)
                self.assertNotIn("SECRET", text)
                self.assertIn("***", text)


class TestKeyMaskingParser(unittest.TestCase):
    """The one parser class v3 and v4 share, so no error shows a key."""

    def refused(self, parser: argparse.ArgumentParser, *argv: str) -> str:
        """The error `parser` prints for `argv`, exiting 2."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            parser.parse_args(list(argv))
        self.assertEqual(caught.exception.code, 2)
        return err.getvalue().splitlines()[-1]

    def parsers(self) -> list[argparse.ArgumentParser]:
        """v3's parser, v4's and each of its commands'."""
        v4 = cli.build_parser()
        (commands,) = [action.choices for action in v4._actions
                       if isinstance(action, argparse._SubParsersAction)]
        return [legacy._v3_parser(), v4, *commands.values()]

    def test_both_parsers_and_every_subparser_are_one(self) -> None:
        for parser in self.parsers():
            self.assertIsInstance(parser, legacy.KeyMaskingParser)

    def test_what_names_a_key(self) -> None:
        for arg in ("--api-key", "--a", "--ap", "--api", "--api-", "--api-k", "-ap",
                    "-api-key", "---api-key", "--api_key", "-api_key", "--apikey",
                    "--API-KEY", "--ApiKey", "--api.key", "--api-keys", "--apikeys",
                    "--gemini-api-key", "--GEMINI_API_KEY", "--geminiApiKey", "--key",
                    "--token", "--api-token", "--secret", "--client_secret"):
            with self.subTest(arg=arg):
                self.assertTrue(legacy._names_key(arg))
        for arg in ("", "-", "--", "---", "-a", "-k", "--k", "--au", "--authors",
                    "--keywords", "--token-limit", "api-key", "api_key", "apikey",
                    "key", "token", "docs/key"):
            with self.subTest(arg=arg):
                self.assertFalse(legacy._names_key(arg))

    def test_an_assignment_keeps_its_value_unless_it_names_a_key(self) -> None:
        self.assertEqual(legacy._hidden("GEMINI_API_KEY=k"), "GEMINI_API_KEY=***")
        self.assertEqual(legacy._hidden("--api_key=k"), "--api_key=***")
        self.assertEqual(legacy._hidden("-api-key=k=k"), "-api-key=***")
        for arg in ("out=docs", "--out=docs", "--name=key", "=k", "docs", "a=b",
                    "api=v2"):
            with self.subTest(arg=arg):
                self.assertEqual(legacy._hidden(arg), arg)

    def test_a_project_path_that_holds_a_key(self) -> None:
        for path in ("--api-key=k", "--a=k", "--api_key=k/x", "GEMINI_API_KEY=k",
                     "token=k"):
            with self.subTest(path=path):
                self.assertTrue(legacy.holds_key(path))
        for path in ("", ".", "app", "a=b", "api=v2", "--api-key", "./key=1",
                     "src/token=2", "--out=docs"):
            with self.subTest(path=path):
                self.assertFalse(legacy.holds_key(path))
        with tempfile.TemporaryDirectory() as here, contextlib.chdir(here):
            os.mkdir("key=1")
            self.assertFalse(legacy.holds_key("key=1"))

    def test_no_other_flag_of_theirs_hides_its_value(self) -> None:
        for parser in self.parsers():
            for action in parser._actions:
                for flag in action.option_strings:
                    with self.subTest(flag=flag):
                        self.assertEqual(legacy._names_key(flag), flag == "--api-key")

    def test_any_key_is_masked_however_odd(self) -> None:
        parser = legacy.KeyMaskingParser(prog="p")
        parser.add_argument("--api-key")
        parser.add_argument("mode", choices=("a", "b"))
        for key in ("two words", "it's", r"back\slash", "a+b(c)?", "$[x]*"):
            with self.subTest(key=key):
                self.assertEqual(self.refused(parser, "a", "--", "--api-key", key),
                                 "p: error: unrecognized arguments: --api-key ***")
                self.assertEqual(self.refused(parser, "a", "--", f"--api-k={key}"),
                                 "p: error: unrecognized arguments: --api-k=***")
        top = legacy.KeyMaskingParser(prog="p")
        top.add_argument("mode", choices=("a", "b"))
        for key in ("it's", r"back\slash", "two words"):
            with self.subTest(key=key):
                text = self.refused(top, "--api-key", key, "a")
                # how the choices print varies: 3.12 leaves their quotes off
                self.assertTrue(text.startswith(
                    "p: error: argument mode: invalid choice: '***' (choose from "),
                    text)
                self.assertNotIn(key, text)

    def test_a_short_key_masks_no_word_it_is_part_of(self) -> None:
        parser = legacy.KeyMaskingParser(prog="p")
        parser.add_argument("--api-key")
        self.assertEqual(self.refused(parser, "--api-key", "k", "keep", "kk"),
                         "p: error: unrecognized arguments: keep kk")


class TestMain(InTempDir):
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

    def test_an_empty_path_or_output_exits_2(self) -> None:
        for argv in (["--output="], ["--output", ""], ["-p", ""], ["--path="]):
            with self.subTest(argv=argv), mock.patch.object(cli, "main") as run, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as caught:
                    legacy.main(argv)
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("expected a directory, got an empty string",
                              err.getvalue())
                run.assert_not_called()

    def test_argv_may_be_given(self) -> None:
        with mock.patch.object(cli, "main", return_value=0) as run:
            legacy.main(["export", "app"])
        run.assert_called_once_with(["export", "app"])


class TestOverV3Docs(CliCase):
    """A v3 command line run in the project, where v3 already wrote docs and
    no manifest."""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.enterContext(contextlib.chdir(self.root))
        self.docs = self.root / "docs"

    def plant(self, pages: dict[str, str]) -> None:
        """Leave `pages` in docs/, as v3 or a person did."""
        for name, text in pages.items():
            page = self.docs / name
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(text, encoding="utf-8")

    def found(self, pages: dict[str, str]) -> dict[str, str]:
        """What docs/ holds now where `pages` were left."""
        return {name: (self.docs / name).read_text(encoding="utf-8") for name in pages}

    def test_its_pages_are_kept_until_forced(self) -> None:
        # Regression: every v3 line over v3 docs paid for its calls, then
        # refused to write.
        pages = {"README.md": _V3_PAGE, "shop/cart.md": _V3_PAGE}
        self.plant(pages)
        code, _, err = self.run_cli("-p", ".", "--api-key", "k")
        self.assertEqual(code, 2)
        self.assertIn("has no record of writing: README.md, shop/cart.md", err)
        self.assertEqual(self.found(pages), pages)
        self.assertEqual(self.model.calls, [])
        code, _, err = self.run_cli("-p", ".", "--api-key", "k", "--force")
        self.assertEqual(code, 0, err)
        self.assertGreater(len(self.model.calls), 0)
        listed = json.loads((self.docs / "index.json").read_bytes())["files"]
        for name, text in self.found(pages).items():
            self.assertNotEqual(text, _V3_PAGE, name)
            self.assertIn(name, listed)

    def test_pages_only_v4_writes_are_left_alone(self) -> None:
        # v3 wrote README.md, <stem>.md and <dir>/<dir>.md, never these
        pages = {"shop/cart.md": _V3_PAGE, "architecture.md": _HAND,
                 "index.md": _HAND, "shop/index.md": _HAND}
        self.plant(pages)
        code, _, err = self.run_cli("-p", ".", "--api-key", "k")
        self.assertEqual(code, 2, err)
        self.assertEqual(self.found(pages), pages)

    def test_overwrite_forces_nothing_and_reaches_no_further(self) -> None:
        # v3 put the summary over this README; v4 writes only in docs/
        pages = {"README.md": _V3_PAGE}
        self.plant(pages)
        readme = self.root / "README.md"
        readme.write_text("# Shop\n", encoding="utf-8")
        code, _, err = self.run_cli("-p", ".", "--api-key", "k", "--overwrite")
        self.assertEqual(code, 2)
        self.assertIn("--overwrite no longer applies", err)
        self.assertEqual(self.found(pages), pages)
        code, _, err = self.run_cli("-p", ".", "--api-key", "k", "--overwrite",
                                    "--force")
        self.assertEqual(code, 0, err)
        self.assertNotEqual(self.found(pages), pages)
        self.assertEqual(readme.read_text(encoding="utf-8"), "# Shop\n")


class TestWhereV3Wrote(CliCase):
    """A v3 command line run above the project, at `self.root`."""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.enterContext(contextlib.chdir(self.root))
        self.app = self.root / "app"
        for relpath, text in PROJECT.items():
            path = self.app / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def test_the_pages_go_under_the_current_directory(self) -> None:
        # v3 `-p app` wrote docs/app/ here and never looked in app/docs/
        hand = self.app / "docs" / "README.md"
        hand.parent.mkdir()
        hand.write_text(_HAND, encoding="utf-8")
        code, _, err = self.run_cli("-p", "app", "--api-key", "k")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(p.name for p in hand.parent.iterdir()), ["README.md"])
        self.assertEqual(hand.read_text(encoding="utf-8"), _HAND)
        for page in ("README.md", "shop/cart.md"):
            self.assertTrue((self.root / "docs" / "app" / page).is_file(), page)

    def test_an_empty_path_or_output_runs_nothing(self) -> None:
        # v3 crashed on these before writing; v4 read them as the current directory
        for flags in (["--output="], ["-p", ""]):
            with self.subTest(flags=flags):
                with self.assertRaises(SystemExit) as caught:
                    self.run_cli(*flags, "--api-key", "k")
                self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.model.calls, [])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         sorted({"app", *(rel.split("/")[0] for rel in PROJECT)}))

    def test_a_tilde_finds_the_project_and_keeps_the_pages_here(self) -> None:
        # Regression: refused, as no project is at ./~/app; --out was docs/~/app.
        code, _, err = self.run_cli("--path=~/app", "--api-key", "k",
                                    env={"HOME": str(self.root),
                                         "USERPROFILE": str(self.root)})
        self.assertEqual(code, 0, err)
        self.assertTrue((self.root / "docs" / "app" / "README.md").is_file())
        self.assertFalse((self.root / "docs" / "~").exists())

    def test_an_output_on_the_project_or_above_it_runs_nothing(self) -> None:
        # v3 mixed its pages with the project's own files; config refuses them.
        def tree() -> list[Path]:
            return sorted(self.root.rglob("*"))

        before = tree()
        root, above = "out is the project root", "contains the project root"
        for flags, says in ((["-p", "app", "--output", "."], root),
                            (["-p", ".", "--output", "."], root),
                            (["-p", ".", "--output", ".."], above)):
            with self.subTest(flags=flags):
                code, _, err = self.run_cli(*flags, "--api-key", "k")
                self.assertEqual(code, 2)
                self.assertIn(says, err)
                self.assertEqual(self.model.calls, [])
                self.assertEqual(tree(), before)


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
