"""Config precedence, validation, and key resolution.

No network and no os.environ mutation: `load_config` and `resolve_api_key` both
take an `env` mapping so these run in any order and in parallel. The commands
that take a project path run too, reading no DEVDOCS_* variable of the shell's
and with making a model patched to fail, to show that a refused path stops
each before it creates or buys anything.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from devdocs import cli
from devdocs.config import (
    Config,
    load_config,
    project_root,
    resolve_api_key,
)
from devdocs.errors import ConfigError

REPO = Path(__file__).resolve().parent.parent


def write_config(body: str) -> Path:
    directory = Path(tempfile.mkdtemp())
    path = directory / "devdocs.toml"
    path.write_text(body, encoding="utf-8")
    return path


def refused(test: unittest.TestCase, overrides: dict[str, Any] | None = None, *,
            config_file: Path | None = None,
            env: dict[str, str] | None = None) -> ConfigError:
    """The ConfigError `load_config` raises; a failure of `test` without one."""
    with test.assertRaises(ConfigError) as caught:
        load_config(overrides, config_file=config_file, env=env or {})
    return caught.exception


def project(*names: str) -> Path:
    """A new directory, resolved, holding an empty file at each of `names`."""
    root = Path(tempfile.mkdtemp()).resolve()
    for name in names:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).touch()
    return root


class TestPrecedence(unittest.TestCase):
    def test_defaults_when_nothing_supplied(self) -> None:
        config = load_config(config_file=None, env={})
        self.assertEqual(config.provider, "gemini")
        self.assertEqual(config.quality, "cheap")
        self.assertTrue(config.free_tier)
        self.assertEqual(config.origin_of("provider"), "default")

    def test_file_overrides_defaults(self) -> None:
        path = write_config('[devdocs]\nprovider = "anthropic"\nmax_cost = 2.5\n')
        config = load_config(config_file=path, env={})
        self.assertEqual(config.provider, "anthropic")
        self.assertEqual(config.max_cost, 2.5)
        self.assertEqual(config.origin_of("provider"), "file")

    def test_flag_overrides_file(self) -> None:
        path = write_config('[devdocs]\nprovider = "anthropic"\nquality = "high"\n')
        config = load_config({"provider": "gemini"}, config_file=path, env={})
        self.assertEqual(config.provider, "gemini")
        self.assertEqual(config.origin_of("provider"), "flag")
        # An unrelated flag must not disturb a configured value.
        self.assertEqual(config.quality, "high")
        self.assertEqual(config.origin_of("quality"), "file")

    def test_absent_flag_does_not_mask_file_value(self) -> None:
        path = write_config('[devdocs]\nquality = "balanced"\n')
        config = load_config({"quality": None}, config_file=path, env={})
        self.assertEqual(config.quality, "balanced")

    def test_bare_table_without_devdocs_section(self) -> None:
        path = write_config('provider = "anthropic"\n')
        config = load_config(config_file=path, env={})
        self.assertEqual(config.provider, "anthropic")

    def test_hyphenated_keys_map_to_underscores(self) -> None:
        path = write_config("[devdocs]\nmax-cost = 3.0\nfree-tier = false\n")
        config = load_config(config_file=path, env={})
        self.assertEqual(config.max_cost, 3.0)
        self.assertFalse(config.free_tier)

    def test_comma_string_becomes_tuple(self) -> None:
        path = write_config('[devdocs]\nexclude = "vendor, build , "\n')
        self.assertEqual(load_config(config_file=path, env={}).exclude,
                         ("vendor", "build"))

    def test_list_flags_arrive_as_tuples(self) -> None:
        # Regression: the CLI hands list flags over as tuples, which were
        # refused, so every --include, --exclude and --format exited 2.
        config = load_config({"exclude": ("vendor",), "formats": ("html", "pdf")},
                             config_file=None, env={})
        self.assertEqual(config.exclude, ("vendor",))
        self.assertEqual(config.formats, ("html", "pdf"))


class TestOutputSettings(unittest.TestCase):
    def test_defaults(self) -> None:
        config = load_config(config_file=None, env={})
        self.assertEqual(config.formats, ("markdown",))
        self.assertEqual((config.pdf_engine, config.pdf_split, config.page_size),
                         ("builtin", False, "A4"))

    def test_from_the_file_with_either_spelling(self) -> None:
        path = write_config('[devdocs]\nformats = ["markdown", "html", "pdf"]\n'
                            'pdf-engine = "chrome"\npage_size = "Letter"\n')
        config = load_config(config_file=path, env={})
        self.assertEqual(config.formats, ("markdown", "html", "pdf"))
        self.assertEqual((config.pdf_engine, config.page_size), ("chrome", "Letter"))
        self.assertEqual(config.origin_of("pdf_engine"), "file")
        path = write_config("[devdocs]\npdf_split = true\n")
        self.assertTrue(load_config(config_file=path, env={}).pdf_split)

    def test_from_the_environment_and_flags(self) -> None:
        env = {"DEVDOCS_FORMATS": "html, pdf", "DEVDOCS_PDF_SPLIT": "yes",
               "DEVDOCS_PAGE_SIZE": "Letter"}
        config = load_config({"page_size": "A4"}, config_file=None, env=env)
        self.assertEqual(config.formats, ("html", "pdf"))
        self.assertTrue(config.pdf_split)
        self.assertEqual(config.origin_of("pdf_split"), "env")
        self.assertEqual((config.page_size, config.origin_of("page_size")),
                         ("A4", "flag"))
        config = load_config(config_file=None, env={"DEVDOCS_PDF_ENGINE": "weasyprint"})
        self.assertEqual(config.pdf_engine, "weasyprint")

    def test_page_sizes_are_named_in_any_case(self) -> None:
        for given, size in (("letter", "Letter"), ("LETTER", "Letter"), ("a4", "A4")):
            with self.subTest(given):
                config = load_config({"page_size": given}, config_file=None, env={})
                self.assertEqual(config.page_size, size)

    def test_unknown_values_are_rejected_with_the_choices(self) -> None:
        cases = (({"pdf_engine": "pandoc"}, "chrome"), ({"page_size": "A5"}, "Letter"),
                 ({"formats": ("docx",)}, "pdf"))
        for overrides, choice in cases:
            with self.subTest(overrides), self.assertRaises(ConfigError) as caught:
                load_config(overrides, config_file=None, env={})
            self.assertIn(choice, caught.exception.hint or "")

    def test_an_external_engine_cannot_split(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            load_config({"pdf_engine": "chrome", "pdf_split": True, "formats": ["pdf"]},
                        config_file=None, env={})
        self.assertIn("cannot split", caught.exception.message)
        self.assertIn("--no-split", caught.exception.hint or "")
        config = load_config({"pdf_split": True}, config_file=None, env={})
        self.assertEqual((config.pdf_engine, config.pdf_split), ("builtin", True))

    def test_split_and_engine_only_clash_when_pdf_is_written(self) -> None:
        # Regression: a file's pdf_split broke every command once the
        # environment named an external engine, even with no PDF to make.
        path = write_config("[devdocs]\npdf_split = true\n")
        config = load_config(config_file=path, env={"DEVDOCS_PDF_ENGINE": "chrome"})
        self.assertEqual((config.formats, config.pdf_engine, config.pdf_split),
                         (("markdown",), "chrome", True))
        config = load_config({"pdf_engine": "chrome", "pdf_split": False,
                              "formats": ("pdf",)}, config_file=path, env={})
        self.assertEqual((config.pdf_split, config.origin_of("pdf_split")),
                         (False, "flag"))

    def test_an_empty_format_list_is_refused(self) -> None:
        # Regression: it wrote nothing but the manifest, pruned every page
        # the last run wrote, and exited 0.
        cases = {
            "file": ({}, write_config("formats = []\n"), {}),
            "environment": ({}, None, {"DEVDOCS_FORMATS": ","}),
            "flag": ({"formats": ()}, None, {}),
        }
        for name, (overrides, path, env) in cases.items():
            with self.subTest(name), self.assertRaises(ConfigError) as caught:
                load_config(overrides, config_file=path, env=env)
            self.assertEqual(caught.exception.message, "no output format configured")
            self.assertIn("markdown, html, pdf", caught.exception.hint or "")

    def test_split_must_be_a_boolean(self) -> None:
        path = write_config('[devdocs]\npdf_split = "yes"\n')
        with self.assertRaises(ConfigError):
            load_config(config_file=path, env={})
        with self.assertRaises(ConfigError) as caught:
            load_config(config_file=None, env={"DEVDOCS_PDF_SPLIT": "sometimes"})
        self.assertIn("DEVDOCS_PDF_SPLIT", str(caught.exception))


class TestValidation(unittest.TestCase):
    def test_rejects_unknown_provider(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({"provider": "openai"}, config_file=None, env={})

    def test_rejects_unknown_quality(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({"quality": "ultra"}, config_file=None, env={})

    def test_rejects_unknown_context(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({"context": "deep"}, config_file=None, env={})

    def test_semantic_context_needs_gemini(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            load_config({"context": "semantic", "provider": "anthropic"},
                        config_file=None, env={})
        self.assertIn("Gemini", caught.exception.message)
        config = load_config({"context": "semantic"}, config_file=None, env={})
        self.assertEqual(config.context, "semantic")

    def test_rejects_unknown_format(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({"formats": ["xml"]}, config_file=None, env={})

    def test_rejects_zero_concurrency(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({"concurrency": 0}, config_file=None, env={})

    def test_rejects_negative_cost(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({"max_cost": -1.0}, config_file=None, env={})

    def test_rejects_a_cost_that_is_not_a_number(self) -> None:
        # Regression: TOML, the flag and the environment all accept nan and
        # inf, which passed validation and crashed Budget() with a raw error.
        for value in ("nan", "inf", "-inf"):
            path = write_config(f"max_cost = {value}\n")
            with self.subTest(file=value), self.assertRaises(ConfigError):
                load_config(config_file=path, env={})
            with self.subTest(env=value), self.assertRaises(ConfigError):
                load_config(config_file=None, env={"DEVDOCS_MAX_COST": value})
            with self.subTest(flag=value), self.assertRaises(ConfigError):
                load_config({"max_cost": float(value)}, config_file=None, env={})

    def test_rejects_negative_requests(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({"max_requests": -1}, config_file=None, env={})

    def test_unknown_option_names_the_valid_ones(self) -> None:
        path = write_config("[devdocs]\nprovder = 1\n")
        with self.assertRaises(ConfigError) as caught:
            load_config(config_file=path, env={})
        self.assertIn("provider", str(caught.exception))

    def test_malformed_toml_names_the_file(self) -> None:
        path = write_config("[devdocs\nprovider =\n")
        with self.assertRaises(ConfigError) as caught:
            load_config(config_file=path, env={})
        self.assertIn(str(path), str(caught.exception))

    def test_wrong_type_is_rejected(self) -> None:
        path = write_config('[devdocs]\nconcurrency = "four"\n')
        with self.assertRaises(ConfigError):
            load_config(config_file=path, env={})


class TestProjectRoot(unittest.TestCase):
    def test_a_missing_root_is_refused_from_every_layer(self) -> None:
        # Regression: estimate and generate made it, with .devdocs/devdocs.db
        # in it, scanned nothing and exited 0.
        missing = project() / "no" / "such"
        cases = {
            "flag": ({"path": str(missing)}, None, {}),
            "environment": ({}, None, {"DEVDOCS_PATH": str(missing)}),
            "file": ({}, write_config(f"path = '{missing}'\n"), {}),
        }
        for name, (overrides, path, env) in cases.items():
            with self.subTest(name):
                error = refused(self, overrides, config_file=path, env=env)
                self.assertEqual(error.message,
                                 f"project root {missing} does not exist")
        self.assertFalse(missing.parent.exists())

    def test_the_flags_root_is_checked_before_a_config_file_above_it(self) -> None:
        # So a broken devdocs.toml found above a typo cannot hide the typo.
        parent = project()
        (parent / "devdocs.toml").write_text("out = 5\n", encoding="utf-8")
        typo = parent / "typo"
        error = refused(self, {"path": str(typo)})
        self.assertEqual(error.message, f"project root {typo} does not exist")

    def test_a_file_is_not_a_root(self) -> None:
        # Regression: a storage error, from making .devdocs inside the file.
        root = project("setup.py")
        error = refused(self, {"path": str(root / "setup.py")})
        self.assertEqual(error.message,
                         f"project root {root / 'setup.py'} is not a directory")
        self.assertEqual(error.hint, f"pass the directory that holds it, {root}")

    def test_an_empty_path_is_not_the_current_directory(self) -> None:
        # Regression: read as ".", so `devdocs generate "$UNSET"` documented,
        # and paid for, whatever directory it ran in.
        cases = {
            "flag": ({"path": ""}, None),
            "blank flag": ({"path": "  "}, None),
            "file": ({}, write_config('path = ""\n')),
        }
        for name, (overrides, path) in cases.items():
            with self.subTest(name):
                error = refused(self, overrides, config_file=path)
                self.assertEqual(error.message, "path is empty")
                self.assertIn('"."', error.hint or "")

    def test_a_root_that_cannot_be_opened_is_a_config_error(self) -> None:
        # Regression: a symlink loop was an internal error on 3.11 and a
        # storage error on 3.13.
        loop = project() / "loop"
        try:
            loop.symlink_to(loop)
        except OSError:
            self.skipTest("this system cannot make symlinks")
        error = refused(self, {"path": str(loop)})
        self.assertIn(f"project root {loop}", error.message)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0,
                         "needs POSIX permissions, not root")
    def test_a_root_it_cannot_list_or_search_is_refused(self) -> None:
        # Regression: one it could search but not list was scanned as empty,
        # with .devdocs made in it, and exit 0; one it could not search was
        # an internal error on 3.11 and 3.13.
        root = project("app.py")
        self.addCleanup(root.chmod, 0o700)
        denied = os.strerror(errno.EACCES)
        for mode in (0o000, 0o300, 0o400):   # neither, search and write, list only
            with self.subTest(mode=oct(mode)):
                root.chmod(mode)
                error = refused(self, {"path": str(root)})
                self.assertEqual(error.message,
                                 f"cannot open project root {root}: {denied}")

    @unittest.skipIf(os.name == "nt", "Windows cannot remove the current directory")
    def test_a_removed_current_directory_is_a_config_error(self) -> None:
        # Regression: resolving "." raised FileNotFoundError, an internal error.
        script = (
            "import os, sys, tempfile\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from devdocs.config import load_config\n"
            "from devdocs.errors import ConfigError\n"
            "gone = tempfile.mkdtemp()\n"
            "os.chdir(gone)\n"
            "os.rmdir(gone)\n"
            "try:\n"
            "    load_config(config_file=None, env={})\n"
            "except ConfigError as err:\n"
            "    print(err.message)\n"
        )
        run = subprocess.run([sys.executable, "-c", script, str(REPO)],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(run.stdout.strip(), "project root . does not exist",
                         run.stderr)

    def test_project_root_resolves_and_refuses_as_load_config_does(self) -> None:
        root = project("setup.py")
        self.assertEqual(project_root(str(root)), root)
        self.assertEqual(project_root("."), Path.cwd().resolve())
        for bad in ("", str(root / "setup.py"), str(root / "missing")):
            with self.subTest(bad), self.assertRaises(ConfigError):
                project_root(bad)


class TestOutputDirectory(unittest.TestCase):
    def setUp(self) -> None:
        self.root = project("shop/cart.py")

    def out_refused(self, out: str) -> ConfigError:
        return refused(self, {"path": str(self.root), "out": out})

    def test_an_empty_out_is_refused(self) -> None:
        # Regression: read as ".", the project root; see below.
        cases = {
            "flag": ({"out": ""}, None),
            "blank flag": ({"out": " "}, None),
            "file": ({}, write_config('out = ""\n')),
        }
        for name, (overrides, path) in cases.items():
            with self.subTest(name):
                error = refused(self, {"path": str(self.root), **overrides},
                                config_file=path)
                self.assertEqual(error.message, "out is empty")
                self.assertIn("docs", error.hint or "")

    def test_a_blank_variable_is_unset_not_empty(self) -> None:
        # As every DEVDOCS_* variable: the next layer decides.
        config = load_config({"path": str(self.root)},
                             config_file=write_config('out = "manual"\n'),
                             env={"DEVDOCS_OUT": ""})
        self.assertEqual((config.out, config.origin_of("out")),
                         (Path("manual"), "file"))

    def test_out_cannot_be_the_root(self) -> None:
        # Regression: README.md, architecture.md, index.json and a page per
        # module were written among the sources, after the calls were paid.
        for out in (".", "./", "shop/..", str(self.root)):
            with self.subTest(out=out):
                error = self.out_refused(out)
                self.assertEqual(error.message, f"out is the project root, {self.root}")
                self.assertIn("docs", error.hint or "")

    def test_out_cannot_be_above_the_root(self) -> None:
        # Regression: the same pages went into the directory holding the project.
        parents = self.root.parents
        for out, above in (("..", parents[0]), ("../..", parents[1]),
                           (self.root.anchor, parents[-1])):
            with self.subTest(out=out):
                self.assertEqual(self.out_refused(out).message,
                                 f"out {above} contains the project root, {self.root}")

    def test_out_through_a_directory_not_made_yet(self) -> None:
        # Regression: new/.. could not be looked at, so it passed; making new
        # then put every page in the root, over its README.md without --force.
        root, parent = self.root, self.root.parent
        cases = {
            "new/..": f"out is the project root, {root}",
            "new/deeper/../..": f"out is the project root, {root}",
            "new/../..": f"out {parent} contains the project root, {root}",
        }
        for out, message in cases.items():
            with self.subTest(out=out):
                self.assertEqual(self.out_refused(out).message, message)
        self.assertFalse((root / "new").exists())

    def test_another_name_for_the_root_is_the_root(self) -> None:
        names: dict[str, str] = {}
        with contextlib.suppress(OSError):   # no symlinks without the privilege
            (self.root / "site").symlink_to(self.root, target_is_directory=True)
            names["a symlink"] = "site"
        if Path(str(self.root).swapcase()).exists():   # a case-insensitive disk
            names["another case"] = str(self.root).swapcase()
        if not names:
            self.skipTest("no other name for a directory here")
        for name, out in names.items():
            with self.subTest(name):
                self.assertEqual(self.out_refused(out).message,
                                 f"out is the project root, {self.root}")

    def test_out_may_be_anywhere_else(self) -> None:
        # A source directory too: telling it from a docs/ of hand-written
        # pages takes a scan, and the writer replaces no file it did not write.
        elsewhere = project()
        for out in ("docs", "shop", "../site", str(elsewhere)):
            with self.subTest(out=out):
                config = load_config({"path": str(self.root), "out": out},
                                     config_file=None, env={})
                self.assertEqual(config.out, Path(out))

    def test_paths_are_strings_without_nul(self) -> None:
        # Regression: `out = false` wrote to a directory named False, and a
        # NUL in a path was an internal error.
        for body, message in (("out = false", "out must be a string"),
                              ("out = 5", "out must be a string"),
                              ("path = []", "path must be a string"),
                              ('out = "docs\\u0000"', "out contains a NUL character")):
            with self.subTest(body):
                error = refused(self, {"path": str(self.root)},
                                config_file=write_config(f"{body}\n"))
                self.assertEqual(error.message, message)


class CommandCase(unittest.TestCase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        """The exit code and all output. Making a model fails the command, and
        no DEVDOCS_* variable set in the shell is read."""
        printed = io.StringIO()
        settings = functools.partial(load_config, env={})
        with mock.patch.object(cli, "make_provider",
                               side_effect=AssertionError("a model was made")), \
                mock.patch.object(cli, "load_config", settings), \
                contextlib.redirect_stdout(printed), \
                contextlib.redirect_stderr(printed):
            code = cli.main(list(argv))
        return code, printed.getvalue()


class TestCommandsRefuseFirst(CommandCase):
    def test_a_missing_root_is_never_made(self) -> None:
        # Regression: generate, estimate and cache made it, with
        # .devdocs/devdocs.db inside, and exited 0; doctor passed it.
        missing = project() / "no" / "such"
        commands = {
            "generate": ["generate", "--api-key", "k"],
            "estimate": ["estimate"],
            "export": ["export"],
            "doctor": ["doctor", "--offline", "--api-key", "k"],
            "cache stats": ["cache", "stats"],
            "cache clear": ["cache", "clear"],
        }
        for name, command in commands.items():
            with self.subTest(name):
                code, printed = self.run_cli(*command, str(missing))
                self.assertEqual(code, 2, printed)
                self.assertIn(f"project root {missing} does not exist", printed)
                self.assertFalse(missing.parent.exists())

    def test_an_empty_out_is_refused_before_anything_is_bought(self) -> None:
        # Regression: the calls were bought, then the pages written among the sources.
        root = project("app.py")
        code, printed = self.run_cli("generate", str(root), "--out", "",
                                     "--api-key", "k")
        self.assertEqual(code, 2, printed)
        self.assertIn("out is empty", printed)
        self.assertEqual([path.name for path in root.iterdir()], ["app.py"])


class TestEnvironment(unittest.TestCase):
    def test_env_overrides_file_and_flag_overrides_env(self) -> None:
        path = write_config('[devdocs]\nquality = "high"\nmax_cost = 1.0\n')
        env = {"DEVDOCS_QUALITY": "balanced", "DEVDOCS_MAX_COST": "2.5"}
        config = load_config({"max_cost": 0.1}, config_file=path, env=env)
        self.assertEqual(config.quality, "balanced")
        self.assertEqual(config.origin_of("quality"), "env")
        self.assertEqual(config.max_cost, 0.1)
        self.assertEqual(config.origin_of("max_cost"), "flag")

    def test_env_values_are_typed(self) -> None:
        env = {
            "DEVDOCS_FREE_TIER": "off",
            "DEVDOCS_CONCURRENCY": "8",
            "DEVDOCS_EXCLUDE": "vendor, dist",
            "DEVDOCS_PROVIDER": "anthropic-sdk",
        }
        config = load_config(config_file=None, env=env)
        self.assertFalse(config.free_tier)
        self.assertEqual(config.concurrency, 8)
        self.assertEqual(config.exclude, ("vendor", "dist"))
        self.assertEqual(config.provider, "anthropic-sdk")

    def test_bad_env_values_name_the_variable(self) -> None:
        for name, value in (("DEVDOCS_FREE_TIER", "maybe"),
                            ("DEVDOCS_CONCURRENCY", "four")):
            with self.subTest(name=name), self.assertRaises(ConfigError) as caught:
                load_config(config_file=None, env={name: value})
            self.assertIn(name, str(caught.exception))

    def test_blank_env_values_are_ignored(self) -> None:
        config = load_config(config_file=None, env={"DEVDOCS_QUALITY": "  "})
        self.assertEqual(config.origin_of("quality"), "default")

    def test_the_key_never_enters_config_from_env(self) -> None:
        config = load_config(config_file=None, env={"DEVDOCS_API_KEY": "secret"})
        self.assertEqual(config.api_key, "")


class TestKeyResolution(unittest.TestCase):
    def test_sdk_provider_reads_the_anthropic_variable(self) -> None:
        key, source = resolve_api_key(
            Config(provider="anthropic-sdk"), env={"ANTHROPIC_API_KEY": "ak"}
        )
        self.assertEqual((key, source.detail), ("ak", "ANTHROPIC_API_KEY"))

    def test_flag_wins_and_is_stripped(self) -> None:
        key, source = resolve_api_key(Config(), explicit="  k  ", env={})
        self.assertEqual(key, "k")
        self.assertEqual(source.origin, "flag")

    def test_env_is_searched_in_order(self) -> None:
        env = {"GEMINI_API_KEY": "second", "DEVDOCS_API_KEY": "first"}
        key, source = resolve_api_key(Config(), env=env)
        self.assertEqual(key, "first")
        self.assertEqual(source.detail, "DEVDOCS_API_KEY")

    def test_provider_picks_its_own_variable(self) -> None:
        env = {"ANTHROPIC_API_KEY": "ak", "GEMINI_API_KEY": "gk"}
        key, _ = resolve_api_key(Config(provider="anthropic"), env=env)
        self.assertEqual(key, "ak")

    def test_config_file_key_is_used_before_demo(self) -> None:
        key, source = resolve_api_key(Config(api_key="fk"), env={}, demo_key="demo")
        self.assertEqual(key, "fk")
        self.assertEqual(source.origin, "file")

    def test_demo_key_is_last_and_flagged_shared(self) -> None:
        key, source = resolve_api_key(Config(), env={}, demo_key="demo")
        self.assertEqual(key, "demo")
        self.assertTrue(source.shared)

    def test_no_key_anywhere_explains_how_to_supply_one(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            resolve_api_key(Config(), env={})
        self.assertIn("GEMINI_API_KEY", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
