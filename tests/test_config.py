"""Config precedence, the search for the file, validation, and key resolution.

No network and no os.environ mutation: `load_config` and `resolve_api_key` both
take an `env` mapping, and `find_config_file` a `home`, so these run in any
order. The commands that take a project path run too, reading no DEVDOCS_*
variable of the shell's and with making a model patched to fail, to show that a
refused path stops each before it creates or buys anything; one has a scripted
model, to show where the file's paths lead a paid run.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from devdocs import cli, demo
from devdocs.config import (
    Config,
    find_config_file,
    key_variables,
    load_config,
    project_root,
    resolve_api_key,
    search_start,
)
from devdocs.errors import ConfigError
from devdocs.generate import Pipeline
from tests.test_generate import PROJECT, TaggedModel

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


def config_in(directory: Path, body: str) -> Path:
    """`directory`'s devdocs.toml, holding `body`; the directory made if need be."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "devdocs.toml"
    path.write_text(body, encoding="utf-8")
    return path


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

    def test_a_file_tomllib_chokes_on_is_refused_naming_it(self) -> None:
        # Regression: none of these is a TOMLDecodeError, so each was an
        # internal error and exit 1, on every Python.
        cases = {
            "not UTF-8": b"\xff\xfep\x00",
            "an integer too long to read": b"a = " + b"1" * 5000 + b"\n",
            "arrays nested too deep": b"a = " + b"[" * 100_000 + b"]" * 100_000 + b"\n",
            "tables nested too deep": b"a = " + b"{a=" * 100_000 + b"1"
                                      + b"}" * 100_000 + b"\n",
        }
        for name, body in cases.items():
            with self.subTest(name):
                path = write_config("")
                path.write_bytes(body)
                self.assertIn(f"{path} is not valid TOML: ",
                              refused(self, config_file=path).message)

    def test_a_cost_past_any_float_is_refused(self) -> None:
        # Regression: OverflowError, an internal error, though 1e400 was refused.
        path = write_config(f"max_cost = 1{'0' * 400}\n")
        self.assertEqual(refused(self, config_file=path).message,
                         "max_cost must be a number of dollars, not inf")

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


class TestConfigSearch(unittest.TestCase):
    """The file is looked for from the project path a flag or DEVDOCS_PATH gives."""

    def setUp(self) -> None:
        self.base = project()
        for name in ("flagged", "variable", "here"):
            config_in(self.base / name, f'name = "{name}"\n')
        self.enterContext(contextlib.chdir(self.base / "here"))

    def test_the_variable_steers_the_search(self) -> None:
        # Regression: only the flag did. The current directory's file applied
        # to the project the variable named, whose own file was never read.
        config = load_config(env={"DEVDOCS_PATH": str(self.base / "variable")})
        self.assertEqual((config.name, config.origin_of("path")), ("variable", "env"))
        self.assertEqual(config.path, self.base / "variable")

    def test_the_flag_steers_it_over_the_variable(self) -> None:
        config = load_config({"path": str(self.base / "flagged")},
                             env={"DEVDOCS_PATH": str(self.base / "variable")})
        self.assertEqual((config.name, config.origin_of("path")), ("flagged", "flag"))
        # The flag makes the variable irrelevant, even a typo in it.
        config = load_config({"path": str(self.base / "flagged")},
                             env={"DEVDOCS_PATH": str(self.base / "typo")})
        self.assertEqual(config.name, "flagged")

    def test_the_current_directory_when_neither_names_a_project(self) -> None:
        for env in ({}, {"DEVDOCS_PATH": ""}, {"DEVDOCS_PATH": "  "}):
            with self.subTest(env=env):
                config = load_config({"path": None}, env=env)
                self.assertEqual((config.name, config.origin_of("path")),
                                 ("here", "default"))

    def test_the_variable_is_read_as_that_layer_reads_it(self) -> None:
        # Relative to the current directory, and stripped.
        for value in ("../variable", " ../variable "):
            with self.subTest(value=value):
                config = load_config(env={"DEVDOCS_PATH": value})
                self.assertEqual(config.name, "variable")
                self.assertEqual(project_root(config.path), self.base / "variable")

    def test_the_search_walks_up_from_the_project_the_variable_names(self) -> None:
        nested = self.base / "variable" / "deep" / "er"
        nested.mkdir(parents=True)
        config = load_config(env={"DEVDOCS_PATH": str(nested)})
        self.assertEqual((config.name, config.path), ("variable", nested))

    def test_the_variables_root_is_checked_before_a_config_file_above_it(self) -> None:
        config_in(self.base / "variable", "out = 5\n")
        typo = self.base / "variable" / "typo"
        error = refused(self, env={"DEVDOCS_PATH": str(typo)})
        self.assertEqual(error.message, f"project root {typo} does not exist")

    def test_a_path_in_the_file_cannot_steer_the_search_for_it(self) -> None:
        config_in(self.base / "here", 'name = "here"\npath = "../variable"\n')
        config = load_config(env={})
        self.assertEqual((config.name, config.origin_of("path")), ("here", "file"))
        self.assertEqual(project_root(config.path), self.base / "variable")

    def test_the_search_starts_where_the_flag_or_variable_says(self) -> None:
        variable = {"DEVDOCS_PATH": str(self.base / "variable")}
        self.assertEqual(search_start(str(self.base / "flagged"), variable),
                         self.base / "flagged")
        self.assertEqual(search_start(None, variable), self.base / "variable")
        for env in ({}, {"DEVDOCS_PATH": " "}):
            with self.subTest(env=env):
                self.assertEqual(search_start(None, env), self.base / "here")
        with self.assertRaises(ConfigError):
            search_start(None, {"DEVDOCS_PATH": str(self.base / "typo")})

    @unittest.skipIf(os.name == "nt", "Windows cannot remove the current directory")
    def test_a_removed_current_directory_is_no_matter_to_the_variable(self) -> None:
        # Regression: refused as "project root . does not exist", for a run
        # that names its project.
        script = (
            "import os, sys, tempfile\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from devdocs.config import load_config\n"
            "gone = tempfile.mkdtemp()\n"
            "os.chdir(gone)\n"
            "os.rmdir(gone)\n"
            "print(load_config(env={'DEVDOCS_PATH': sys.argv[2]}).name)\n"
        )
        run = subprocess.run([sys.executable, "-c", script, str(REPO),
                              str(self.base / "variable")],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(run.stdout.strip(), "variable", run.stderr)


class TestSearchStops(unittest.TestCase):
    """The walk up ends at a repo root or the home directory; only outside home
    can it reach the disk's root."""

    def setUp(self) -> None:
        self.home = project()
        self.below = self.home / "work" / "app"
        self.below.mkdir(parents=True)
        self.stray = config_in(self.home, "max_cost = 99.0\n")

    def in_a_child(self, *lines: str, cwd: Path | None = None) -> str:
        """What `lines` print in a Python whose home is `self.home`, with
        load_config, project_root and the path of `self.below` to hand."""
        script = "\n".join(("import sys",
                            "sys.path.insert(0, sys.argv[1])",
                            "from devdocs.config import load_config, project_root",
                            "below = sys.argv[2]",
                            *lines))
        home = {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        run = subprocess.run([sys.executable, "-c", script, str(REPO), str(self.below)],
                             capture_output=True, text=True, timeout=60, check=False,
                             cwd=cwd, env={**os.environ, **home})
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.strip()

    def test_a_search_from_below_home_does_not_read_its_file(self) -> None:
        # Regression: it set the budget of every project under home with no
        # file of its own and no repo around it.
        self.assertIsNone(find_config_file(self.below, home=self.home))

    def test_a_search_from_home_reads_it(self) -> None:
        self.assertEqual(find_config_file(self.home, home=self.home), self.stray)

    def test_the_file_a_search_from_home_reads_can_name_a_project_below(self) -> None:
        # The stop is where the search starts, not where the project is.
        config_in(self.home, 'path = "work/app"\nmax_cost = 0\n')
        printed = self.in_a_child("config = load_config(env={})",
                                  "print(project_root(config.path), config.max_cost)",
                                  cwd=self.home)
        self.assertEqual(printed, f"{self.below} 0.0")

    def test_nothing_above_home_is_read(self) -> None:
        above = project()
        config_in(above, "max_cost = 99.0\n")
        home = above / "me"
        (home / "app").mkdir(parents=True)
        self.assertIsNone(find_config_file(home, home=home))
        self.assertIsNone(find_config_file(home / "app", home=home))

    def test_a_file_below_home_is_found_from_the_directories_under_it(self) -> None:
        file = config_in(self.home / "work", "max_cost = 1.0\n")
        self.assertEqual(find_config_file(self.below, home=self.home), file)

    def test_a_repo_at_home_does_not_make_its_file_a_projects(self) -> None:
        (self.home / ".git").mkdir()   # dotfiles
        self.assertIsNone(find_config_file(self.below, home=self.home))
        self.assertEqual(find_config_file(self.home, home=self.home), self.stray)

    def test_a_repo_below_home_stops_the_walk_first(self) -> None:
        config_in(self.home / "work", "max_cost = 1.0\n")
        (self.below / ".git").mkdir()
        self.assertIsNone(find_config_file(self.below, home=self.home))

    def test_outside_home_the_walk_runs_on(self) -> None:
        self.assertEqual(find_config_file(self.below, home=project()), self.stray)

    def test_a_home_that_is_not_there_stops_nothing(self) -> None:
        missing = self.home / "no" / "such"
        self.assertEqual(find_config_file(self.below, home=missing), self.stray)

    @unittest.skipIf(os.name == "nt", "Windows has no passwd database")
    def test_a_home_that_cannot_be_determined_stops_nothing(self) -> None:
        # No HOME and a uid with no passwd entry. Path.home() raises there,
        # which would be an internal error.
        printed = self.in_a_child(
            "import os, pwd, unittest.mock",
            "del os.environ['HOME']",
            "pwd.getpwuid = unittest.mock.Mock(side_effect=KeyError)",
            "print(load_config({'path': below}, env={}).max_cost)",
        )
        self.assertEqual(printed, "99.0")

    def test_another_name_for_home_is_home(self) -> None:
        names: dict[str, Path] = {}
        link = project() / "link"
        with contextlib.suppress(OSError):   # no symlinks without the privilege
            link.symlink_to(self.home, target_is_directory=True)
            names["a symlink"] = link
        if Path(str(self.home).swapcase()).exists():   # a case-insensitive disk
            names["another case"] = Path(str(self.home).swapcase())
        if not names:
            self.skipTest("no other name for a directory here")
        for name, home in names.items():
            with self.subTest(name):
                self.assertIsNone(find_config_file(self.below, home=home))

    def test_the_real_home_directory_is_the_stop(self) -> None:
        printed = self.in_a_child(
            "print(load_config({'path': below}, env={}).max_cost)",
        )
        self.assertEqual(printed, "0.5")


class TestLinksInTheWalk(unittest.TestCase):
    """A link the walk may not follow counts as there; one that leads nowhere
    does not. The file above has no cost ceiling, which is what skipping a
    project's own file could cost."""

    def setUp(self) -> None:
        self.base = project()
        config_in(self.base, 'name = "above"\nmax_cost = 0\n')
        self.root = self.base / "app"
        self.root.mkdir()

    def denied_target(self) -> Path:
        """A file in a directory devdocs may not search."""
        denied = project("x")
        denied.chmod(0o000)
        self.addCleanup(denied.chmod, 0o700)
        return denied / "x"

    def assert_refused_as_unreadable(self) -> None:
        error = refused(self, {"path": str(self.root)})
        self.assertIn(f"cannot read {self.root / 'devdocs.toml'}: ", error.message)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0,
                         "needs POSIX permissions, not root")
    def test_a_config_link_it_may_not_follow_is_refused(self) -> None:
        # Regression: an internal error on 3.11 and 3.13; on 3.15 it was
        # skipped, and the run went on under the file above, with no ceiling.
        (self.root / "devdocs.toml").symlink_to(self.denied_target())
        self.assert_refused_as_unreadable()

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0,
                         "needs POSIX permissions, not root")
    def test_as_is_a_config_file_it_may_not_read(self) -> None:
        file = config_in(self.root, 'name = "own"\n')
        file.chmod(0o000)
        self.addCleanup(file.chmod, 0o600)
        self.assert_refused_as_unreadable()

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0,
                         "needs POSIX permissions, not root")
    def test_a_repo_link_it_may_not_follow_stops_the_walk(self) -> None:
        # Regression: an internal error on 3.11 and 3.13; on 3.15 the walk
        # went on past it, to the file above.
        (self.root / ".git").symlink_to(self.denied_target())
        config = load_config({"path": str(self.root)}, env={})
        self.assertEqual((config.name, config.max_cost), ("", 0.5))

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0,
                         "needs POSIX permissions, not root")
    def test_a_directory_it_may_not_search_holds_no_file(self) -> None:
        # What it cannot see is not a file. Not a link either: the walk goes on.
        shut = self.root / "shut"
        (shut / "below").mkdir(parents=True)
        shut.chmod(0o000)
        self.addCleanup(shut.chmod, 0o700)
        for start in (shut, shut / "below"):
            with self.subTest(start=start.name):
                self.assertEqual(find_config_file(start, home=project()),
                                 self.base / "devdocs.toml")

    def test_a_directory_by_that_name_is_no_file(self) -> None:
        (self.root / "devdocs.toml").mkdir()
        self.assertEqual(load_config({"path": str(self.root)}, env={}).name, "above")

    def test_a_link_that_leads_nowhere_is_neither_file_nor_repo(self) -> None:
        for marker in ("devdocs.toml", ".git"):
            link = self.root / marker
            for target in (self.root / "gone", link):   # nothing; itself
                try:
                    link.symlink_to(target)
                except OSError:
                    self.skipTest("this system cannot make symlinks")
                with self.subTest(marker, to=target.name):
                    config = load_config({"path": str(self.root)}, env={})
                    self.assertEqual(config.name, "above")
                link.unlink()


class TestPathsInTheFile(unittest.TestCase):
    """A relative `path` there means from the file; a relative `out`, from the
    project, as it does from every layer."""

    def setUp(self) -> None:
        self.base = project("src/app.py", "elsewhere/src/stray.py")
        self.src = self.base / "src"
        self.enterContext(contextlib.chdir(self.base / "elsewhere"))

    def test_a_relative_path_is_read_from_the_files_directory(self) -> None:
        # Regression: read from the current directory, so one file named
        # another project, or none, depending on where the run started.
        file = config_in(self.base, 'path = "src"\n')
        for name, config_file in (("found by the search", None), ("given", file)):
            with self.subTest(name):
                config = load_config(config_file=config_file, env={})
                self.assertEqual((config.path, config.origin_of("path")),
                                 (self.src, "file"))

    def test_the_directory_is_the_one_that_holds_the_file(self) -> None:
        config_in(self.base, 'path = "src"\n')
        nested = self.base / "elsewhere" / "src"
        with contextlib.chdir(nested):
            self.assertEqual(load_config(env={}).path, self.src)
        file = config_in(self.base / "sub" / "dir", 'path = "../.."\n')
        self.assertEqual(project_root(load_config(config_file=file, env={}).path),
                         self.base)
        relative = Path(os.path.relpath(file))
        self.assertEqual(project_root(load_config(config_file=relative, env={}).path),
                         self.base)

    def test_a_file_with_no_path_leaves_the_project_where_the_run_is(self) -> None:
        config_in(self.base, 'name = "above"\n')
        config = load_config(env={})
        self.assertEqual((config.name, config.origin_of("path")), ("above", "default"))
        self.assertEqual(project_root(config.path), self.base / "elsewhere")

    def test_a_linked_file_belongs_where_it_is_found(self) -> None:
        # One file shared by several projects: each reads `path` from itself.
        shared = config_in(project(), 'path = "src"\n')
        try:
            (self.base / "devdocs.toml").symlink_to(shared)
        except OSError:
            self.skipTest("this system cannot make symlinks")
        self.assertEqual(load_config(env={}).path, self.src)

    def test_a_dot_is_the_files_directory_not_the_current_one(self) -> None:
        config_in(self.base, 'path = "."\n')
        self.assertEqual(project_root(load_config(env={}).path), self.base)

    def test_a_missing_path_is_named_as_the_file_places_it(self) -> None:
        config_in(self.base, 'path = "typo"\n')
        error = refused(self)
        self.assertEqual(error.message,
                         f"project root {self.base / 'typo'} does not exist")

    def test_an_absolute_or_home_path_is_left_alone(self) -> None:
        other = project()
        for value, expected in ((str(other), other), ("~", Path.home())):
            with self.subTest(value=value):
                file = config_in(self.base, f"path = '{value}'\n")
                self.assertEqual(load_config(config_file=file, env={}).path, expected)

    def test_a_flag_or_variable_is_still_read_from_the_current_directory(self) -> None:
        file = config_in(self.base, 'path = "src"\n')
        here = self.base / "elsewhere" / "src"
        for name, overrides, env in (("flag", {"path": "src"}, {}),
                                     ("variable", None, {"DEVDOCS_PATH": "src"})):
            with self.subTest(name):
                config = load_config(overrides, config_file=file, env=env)
                self.assertEqual(project_root(config.path), here)

    def test_a_relative_out_stays_relative_to_the_project(self) -> None:
        # Not to the file: the file only names the project.
        file = config_in(self.base, 'path = "src"\nout = "manual"\n')
        config = load_config(config_file=file, env={})
        self.assertEqual(config.out, Path("manual"))
        self.assertEqual(Pipeline(config).out, self.src / "manual")
        other = project()
        config = load_config({"path": str(other)}, config_file=file, env={})
        self.assertEqual(Pipeline(config).out, other / "manual")

    def test_the_checks_on_out_measure_from_the_project(self) -> None:
        cases = {
            ".": f"out is the project root, {self.src}",
            "..": f"out {self.base} contains the project root, {self.src}",
        }
        for out, message in cases.items():
            with self.subTest(out=out):
                file = config_in(self.base, f'path = "src"\nout = "{out}"\n')
                self.assertEqual(refused(self, config_file=file).message, message)
        file = config_in(self.base, 'path = "src"\nout = "../site"\n')
        self.assertEqual(Pipeline(load_config(config_file=file, env={})).out.resolve(),
                         self.base / "site")

    def test_the_hint_for_an_empty_path_says_what_a_dot_means_there(self) -> None:
        # Regression: "the current directory", which it no longer is.
        error = refused(self, config_file=config_in(self.base, 'path = ""\n'))
        self.assertIn("devdocs.toml for its own", error.hint or "")


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

    def test_a_leading_tilde_is_the_home_directory(self) -> None:
        # Regression: it made a directory named ~ in the project, which
        # `rm -rf ~` does not remove: it removes the home directory.
        config = load_config({"path": str(self.root), "out": "~/devdocs-site"},
                             config_file=None, env={})
        self.assertEqual(config.out, Path.home() / "devdocs-site")
        self.assertEqual(project_root("~"), Path.home().resolve())

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
    def run_cli(self, *argv: str, env: dict[str, str] | None = None,
                model: Any = None) -> tuple[int, str]:
        """The exit code and all output. Making a model fails the command
        unless `model` is given, and the DEVDOCS_* variables read are those of
        `env`, none set in the shell."""
        printed = io.StringIO()
        make = ({"side_effect": AssertionError("a model was made")} if model is None
                else {"return_value": model})
        settings = functools.partial(load_config, env=env or {})
        with mock.patch.object(cli, "make_provider", **make), \
                mock.patch.object(cli, "load_config", settings), \
                contextlib.redirect_stdout(printed), \
                contextlib.redirect_stderr(printed):
            code = cli.main(list(argv))
        return code, printed.getvalue()


class TestCommandsReadTheFile(CommandCase):
    def test_a_run_from_below_the_file_works_in_the_project_it_names(self) -> None:
        # Regression: `path` was read from the current directory, where it
        # named nothing, and the run was refused.
        base = project()
        for relpath, text in PROJECT.items():
            (base / "app" / relpath).parent.mkdir(parents=True, exist_ok=True)
            (base / "app" / relpath).write_text(text, encoding="utf-8")
        config_in(base, 'path = "app"\nout = "manual"\n')
        (base / "scripts").mkdir()
        with contextlib.chdir(base / "scripts"):
            code, printed = self.run_cli("generate", "--api-key", "k",
                                         model=TaggedModel())
        self.assertEqual(code, 0, printed)
        self.assertTrue((base / "app" / "manual" / "README.md").is_file())
        self.assertTrue((base / "app" / ".devdocs" / "devdocs.db").is_file())
        self.assertEqual(list((base / "scripts").iterdir()), [])

    def test_the_variable_names_the_project_and_so_its_file(self) -> None:
        # Regression: the project's own quality was not read.
        root = project()
        (root / "app.py").write_text("def main():\n    return 0\n", "utf-8")
        config_in(root, 'quality = "high"\n')
        here = project()
        env = {"DEVDOCS_PATH": str(root)}
        with contextlib.chdir(here):
            _, printed = self.run_cli("doctor", "--offline", "--api-key", "k", env=env)
            self.assertIn("quality=high", printed)
            code, printed = self.run_cli("estimate", "--json", "--quiet", env=env)
            self.assertEqual(code, 0, printed)
        self.assertEqual(json.loads(printed)["files"], 1)   # root's app.py
        self.assertEqual(list(here.iterdir()), [])


class TestDemoKey(CommandCase):
    def test_none_ships(self) -> None:
        # As config's docstrings say. A public key gets revoked: see devdocs.demo.
        self.assertEqual(demo.DEMO_KEY, "")

    def test_one_set_is_the_last_resort_for_gemini_and_warns(self) -> None:
        root = project()
        no_keys = functools.partial(resolve_api_key, env={})
        with mock.patch.object(demo, "DEMO_KEY", "shared"), \
                mock.patch.object(cli, "resolve_api_key", no_keys):
            _, gemini = self.run_cli("doctor", str(root), "--offline")
            _, anthropic = self.run_cli("doctor", str(root), "--offline",
                                        "--provider", "anthropic")
        self.assertIn("ok api key              from demo (shared demo key)", gemini)
        self.assertIn("warning: using the shared demo key", gemini)
        self.assertIn("!! api key              no API key", anthropic)


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

    def test_a_file_that_is_not_utf8_exits_2_and_makes_nothing(self) -> None:
        # Regression: "internal error: UnicodeDecodeError" and exit 1.
        root = project("app.py")
        config_in(root, "").write_bytes(b"\xff\xfep\x00")
        code, printed = self.run_cli("estimate", str(root))
        self.assertEqual(code, 2, printed)
        self.assertIn(f"{root / 'devdocs.toml'} is not valid TOML: ", printed)
        self.assertEqual(sorted(path.name for path in root.iterdir()),
                         ["app.py", "devdocs.toml"])


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

    def test_only_key_source_names_where_the_key_came_from(self) -> None:
        # As config's docstring says: origin_of never sees the winning key.
        env = {"DEVDOCS_API_KEY": "k"}
        config = load_config(config_file=None, env=env)
        self.assertEqual(config.origin_of("api_key"), "default")
        self.assertEqual(resolve_api_key(config, env=env)[1].origin, "env")
        config = load_config(config_file=write_config('api_key = "f"\n'), env=env)
        self.assertEqual(config.origin_of("api_key"), "file")
        _, source = resolve_api_key(config, explicit="x", env=env)
        self.assertEqual(source.origin, "flag")


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
        # Regression: the hint named one variable, hiding the one read first.
        variables = {
            "gemini": ("DEVDOCS_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
            "anthropic": ("DEVDOCS_API_KEY", "ANTHROPIC_API_KEY"),
            "anthropic-sdk": ("DEVDOCS_API_KEY", "ANTHROPIC_API_KEY"),
        }
        for provider, names in variables.items():
            with self.subTest(provider):
                with self.assertRaises(ConfigError) as caught:
                    resolve_api_key(Config(provider=provider), env={})
                listed = ", ".join(f"${name}" for name in names)
                self.assertEqual(caught.exception.hint,
                                 f"pass --api-key or set one of: {listed}")
                for name in names:   # what it names is what is read
                    _, source = resolve_api_key(Config(provider=provider),
                                                env={name: "k"})
                    self.assertEqual(source.detail, name)
                self.assertEqual(key_variables(provider), names)   # as the demo warns
        self.assertEqual(key_variables("openai"), ())


if __name__ == "__main__":
    unittest.main()
