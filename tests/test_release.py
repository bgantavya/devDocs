"""scripts/release.py: versions, commit rules, the next release, notes and files.

The script is loaded by path, since scripts/ is not a package. Git runs only in
throwaway repositories under a temp dir, cut off from the user's git config and
any GIT_* variables, with fixed dates; nothing depends on this repository's
history, and nothing touches the network.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release.py"
DATE = "2026-08-17T09:00:00+00:00"
SUPERUSER = hasattr(os, "geteuid") and os.geteuid() == 0


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("devdocs_release", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module        # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


release = _load()


def run(*argv: str) -> tuple[int, str, str]:
    """`release.py ARGV` in-process: exit status, stdout, stderr."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = release.main(list(argv))
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def isolated_git() -> Iterator[None]:
    """No outside GIT_* (a hook's GIT_INDEX_FILE, say), no user or system config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_DATE=DATE,
        GIT_COMMITTER_DATE=DATE,
    )
    with mock.patch.dict(os.environ, env, clear=True):
        yield


class Repo:
    """A throwaway git repository; use it inside `isolated_git()`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Release Test")
        self.git("config", "user.email", "release@example.invalid")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True, check=True, text=True,
        ).stdout

    def commit(self, *paragraphs: str) -> str:
        """An empty commit with this message; its short hash."""
        message = [arg for paragraph in paragraphs for arg in ("-m", paragraph)]
        self.git("commit", "-q", "--allow-empty", *message)
        return self.git("rev-parse", "--short", "HEAD").strip()

    def set_version(self, version: str) -> None:
        init = self.path / "devdocs" / "__init__.py"
        init.parent.mkdir(exist_ok=True)
        init.write_text(f'__version__ = "{version}"\n', encoding="utf-8")


def sample_package(version: str) -> str:
    """package.json with traps: CRLF, tabs, JSON escapes, nested "version" keys."""
    return (
        "{\r\n"
        '\t"name": "@bgantavya/devdocs",\r\n'
        '\t"engines": {"node": ">=18", "version": "0.0.0"},\r\n'
        '\t"description": "Docs \\"from\\" code \\u2014 caf\u00e9",\r\n'
        f'\t"version" :  "{version}",\r\n'
        '\t"config": {"version": "4.0.0-dev.0"}\r\n'
        "}"
    )


def decide(current: str, tags: list[str], *messages: str) -> dict[str, Any]:
    result: dict[str, Any] = release.next_release(current, tags, messages).as_dict()
    return result


class TestVersions(unittest.TestCase):
    def test_normal_forms_round_trip(self) -> None:
        for text in ("0.0.0", "4.0.0", "10.20.30", "4.0.0.dev0", "4.0.0.dev12",
                     "4.0.0a1", "4.0.0b2", "4.0.0rc3"):
            with self.subTest(text=text):
                self.assertEqual(str(release.parse_version(text)), text)

    def test_everything_else_is_refused(self) -> None:
        for text in ("", "4", "4.0", "4.0.0.0", "v4.0.0", "04.0.0", "4.00.0",
                     "4.0.00", "4.0.0-dev.0", "4.0.0-dev0", "4.0.0dev0", "4.0.0.dev",
                     "4.0.0.a1", "4.0.0.rc1", "4.0.0c1", "4.0.0pre1", "4.0.0RC1",
                     "4.0.0rc01", "4.0.0.post1", "4.0.0a1.dev0", "4.0.0+local",
                     "1!4.0.0", " 4.0.0", "4.0.0\n", "\u0664.0.0"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                release.parse_version(text)

    def test_npm_mapping(self) -> None:
        table = {
            "4.0.0": "4.0.0",
            "4.0.0.dev0": "4.0.0-dev.0",
            "4.0.0a1": "4.0.0-alpha.1",
            "4.0.0b2": "4.0.0-beta.2",
            "4.0.0rc3": "4.0.0-rc.3",
            "12.3.45.dev10": "12.3.45-dev.10",
        }
        for pep440, npm in table.items():
            with self.subTest(version=pep440):
                self.assertEqual(release.npm_version(pep440), npm)

    def test_npm_mapping_refuses_what_parsing_refuses(self) -> None:
        for text in ("4.0.0-dev.0", "4.0", "v4.0.0"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                release.npm_version(text)

    def test_only_vxyz_names_a_release_tag(self) -> None:
        self.assertEqual(release.tag_version("v4.10.2"), release.Version(4, 10, 2))
        for name in ("devDocs-v3.0.0", "v-2.0.6", "v2.0", "v4.0.0rc1", "v4.0.0-rc.1",
                     "V4.0.0", "v04.0.0", "4.0.0", "v4.0.0.0", "release-4.0.0"):
            with self.subTest(name=name):
                self.assertIsNone(release.tag_version(name))

    def test_bumped(self) -> None:
        version = release.Version(4, 2, 7)
        self.assertEqual(str(version.bumped("major")), "5.0.0")
        self.assertEqual(str(version.bumped("minor")), "4.3.0")
        self.assertEqual(str(version.bumped("patch")), "4.2.8")


class TestCommits(unittest.TestCase):
    def test_type_and_description(self) -> None:
        commit = release.parse_commit("feat: add the notes command\n")
        self.assertEqual(
            (commit.type, commit.scope, commit.description, commit.breaking),
            ("feat", None, "add the notes command", False),
        )

    def test_scopes(self) -> None:
        for subject, scope in {
            "fix(llm): retry on 529": "llm",
            "fix(llm/http): retry on 529": "llm/http",
            "build(deps-dev): bump ruff": "deps-dev",
            "chore(release): v4.0.0": "release",
        }.items():
            with self.subTest(subject=subject):
                self.assertEqual(release.parse_commit(subject).scope, scope)

    def test_bang_marks_a_breaking_change(self) -> None:
        self.assertTrue(release.parse_commit("feat(cli)!: drop the v2 flags").breaking)
        self.assertTrue(release.parse_commit("refactor!: one client").breaking)

    def test_breaking_change_footer_and_its_text(self) -> None:
        commit = release.parse_commit(
            "feat: new flags\n\nWhy they changed.\n\n"
            "BREAKING CHANGE: --old is gone;\n  use --new instead.\nRefs: #12\n"
        )
        self.assertTrue(commit.breaking)
        self.assertEqual(commit.notes, ("--old is gone; use --new instead.",))

    def test_hyphenated_and_bare_footers(self) -> None:
        hyphenated = release.parse_commit("fix: x\n\nBREAKING-CHANGE: y")
        self.assertEqual(hyphenated.notes, ("y",))
        bare = release.parse_commit("fix: x\n\nBREAKING CHANGE:\n")
        self.assertTrue(bare.breaking)
        self.assertEqual(bare.notes, ())

    def test_each_breaking_footer_keeps_its_own_text(self) -> None:
        commit = release.parse_commit(
            "feat: x\n\nBREAKING CHANGE: a\nBREAKING-CHANGE: b\n"
        )
        self.assertEqual(commit.notes, ("a", "b"))

    def test_footer_is_uppercase_at_the_start_of_a_body_line(self) -> None:
        for body in ("breaking change: y", "Not a BREAKING CHANGE: y",
                     "  BREAKING CHANGE: y"):
            with self.subTest(body=body):
                self.assertFalse(release.parse_commit(f"fix: x\n\n{body}").breaking)

    def test_merge_commits_are_not_conventional(self) -> None:
        for message in ("Merge branch 'topic'",
                        "Merge pull request #12 from someone/topic\n\nfeat: x",
                        "Merge remote-tracking branch 'origin/v4'"):
            with self.subTest(message=message):
                self.assertIsNone(release.parse_commit(message))

    def test_invalid_subjects(self) -> None:
        for subject in ("Update README.md", "feat add x", "feat:add x", "feat:  x",
                        "feat: ", "feat:", "Feat: x", "FEAT: x", "feature: x",
                        "wip: x", "feat(): x", "feat(a b): x", "feat(a)(b): x",
                        "feat!(a): x", "feat !: x", "fixup! feat: x",
                        'Revert "feat: x"', "", "\n\n"):
            with self.subTest(subject=subject):
                self.assertIsNone(release.parse_commit(subject))

    def test_every_listed_type_is_accepted(self) -> None:
        for kind in ("feat", "fix", "perf", "refactor", "docs", "test", "build",
                     "ci", "chore", "style", "revert"):
            with self.subTest(type=kind):
                self.assertEqual(release.parse_commit(f"{kind}: x").type, kind)

    def test_leading_blank_lines_and_trailing_spaces_are_ignored(self) -> None:
        commit = release.parse_commit("\n\nfix: trailing   \n")
        self.assertEqual(commit.description, "trailing")

    def test_release_commits(self) -> None:
        self.assertTrue(release.parse_commit("chore(release): v4.0.0").is_release)
        self.assertFalse(release.parse_commit("chore(deps): v4.0.0").is_release)
        self.assertFalse(release.parse_commit("feat(release): x").is_release)

    def test_subject_of(self) -> None:
        self.assertEqual(release.subject_of("\n  \nUpdate README.md\n\nbody"),
                         "Update README.md")
        self.assertEqual(release.subject_of(""), "")


class TestClassify(unittest.TestCase):
    def test_release_kinds(self) -> None:
        cases = {
            "feat: x": "minor",
            "feat(cli): x": "minor",
            "fix: x": "patch",
            "perf(index): x": "patch",
            "feat!: x": "major",
            "fix(cli)!: x": "major",
            "docs!: x": "major",
            "fix: x\n\nBREAKING CHANGE: y": "major",
            "docs: x": None,
            "test: x": None,
            "build: x": None,
            "ci: x": None,
            "chore: x": None,
            "style: x": None,
            "refactor: x": None,
            "revert: feat: x": None,
            "chore(release): v4.0.0": None,
            "chore(release)!: v5.0.0": None,
            "chore(release): v4.0.0\n\nBREAKING CHANGE: quoted notes": None,
            "Update README.md": None,
            "Feat: x": None,
        }
        for message, kind in cases.items():
            with self.subTest(message=message):
                self.assertEqual(release.classify(message), kind)


class TestNextRelease(unittest.TestCase):
    def test_a_prerelease_holds_every_release(self) -> None:
        held = {"release": False, "version": "", "tag": "", "previous": "v1.0.3",
                "bump": "held"}
        self.assertEqual(
            decide("4.0.0.dev0", ["v1.0.3"], "docs: guide", "feat!: new command"),
            held,
        )
        self.assertEqual(decide("4.0.0.dev0", ["v1.0.3"], "docs: guide"), held)
        self.assertEqual(decide("4.0.0.dev0", [], "fix: crash")["bump"], "held")
        self.assertEqual(decide("4.0.0.dev0", ["v4.0.0"], "fix: a")["bump"], "held")

    def test_every_prerelease_kind_holds(self) -> None:
        for current in ("4.1.0.dev3", "4.1.0a1", "4.1.0b2", "4.1.0rc1"):
            with self.subTest(current=current):
                held = release.next_release(current, ["v4.0.0"], ["feat: x"])
                self.assertEqual((held.release, held.bump), (False, "held"))
                self.assertIn("bump 4.1.0", held.warning)

    def test_final_version_without_its_tag_ships_as_it_is(self) -> None:
        self.assertEqual(
            decide("4.0.0", ["v3.9.0"]),
            {"release": True, "version": "4.0.0", "tag": "v4.0.0",
             "previous": "v3.9.0", "bump": "pending"},
        )
        self.assertEqual(decide("4.0.0", [], "docs: x")["bump"], "pending")

    def test_existing_tag_is_bumped_by_the_highest_kind(self) -> None:
        cases = {
            ("fix: a",): ("4.0.1", "patch"),
            ("perf: a", "docs: b"): ("4.0.1", "patch"),
            ("fix: a", "feat: b", "docs: c"): ("4.1.0", "minor"),
            ("feat: a", "refactor!: b"): ("5.0.0", "major"),
            ("fix: a\n\nBREAKING CHANGE: gone",): ("5.0.0", "major"),
        }
        for messages, (version, bump) in cases.items():
            with self.subTest(messages=messages):
                self.assertEqual(
                    decide("4.0.0", ["v4.0.0"], *messages),
                    {"release": True, "version": version, "tag": f"v{version}",
                     "previous": "v4.0.0", "bump": bump},
                )

    def test_nothing_ships_when_no_commit_asks(self) -> None:
        nothing = {"release": False, "version": "", "tag": "", "previous": "v4.0.0",
                   "bump": "none"}
        self.assertEqual(decide("4.0.0", ["v4.0.0"], "docs: a", "test: b",
                                "Merge branch 'topic'"), nothing)
        self.assertEqual(decide("4.0.0", ["v4.0.0"]), nothing)

    def test_release_commits_never_trigger_a_release(self) -> None:
        self.assertEqual(
            decide("4.0.1", ["v4.0.0", "v4.0.1"], "chore(release): v4.0.1")["bump"],
            "none",
        )

    def test_only_vxyz_tags_count(self) -> None:
        junk = ["devDocs-v3.0.0", "v-2.0.6", "v4.0", "v4.0.0rc1", "V4.0.0",
                "v04.0.0", "release-4.0.0", "4.0.0"]
        self.assertEqual(
            decide("4.0.0", junk, "fix: a"),
            {"release": True, "version": "4.0.0", "tag": "v4.0.0", "previous": None,
             "bump": "pending"},
        )

    def test_newest_tag_is_the_highest_version_not_the_last_by_name(self) -> None:
        result = decide("4.0.0", ["v4.9.0", "v4.10.0", "v4.0.0"], "fix: a")
        self.assertEqual((result["version"], result["previous"]), ("4.10.1", "v4.10.0"))

    def test_a_version_behind_the_newest_tag_never_repeats_a_tag(self) -> None:
        self.assertEqual(decide("4.0.0", ["v4.2.0"], "fix: a")["version"], "4.2.1")
        self.assertEqual(decide("3.0.0", ["v4.0.0"], "feat: a")["version"], "4.1.0")
        self.assertEqual(decide("4.0.0", ["v4.2.0"], "docs: a")["bump"], "none")

    def test_warns_when_commits_outgrow_the_declared_version(self) -> None:
        outgrown = release.next_release("4.0.1", ["v4.0.0"], ["feat: a"])
        self.assertEqual(outgrown.version, "4.0.1")
        self.assertIn("minor release (4.1.0)", outgrown.warning)
        fits = release.next_release("4.1.0", ["v4.0.0"], ["feat: a"])
        self.assertEqual(fits.warning, "")

    def test_json_is_one_line_with_the_contract_keys_in_order(self) -> None:
        text = release.next_release("4.0.0", [], []).as_json()
        self.assertNotIn("\n", text)
        data = json.loads(text)
        self.assertEqual(list(data), ["release", "version", "tag", "previous", "bump"])
        self.assertIsNone(data["previous"])

    def test_invalid_current_version_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            release.next_release("4.0", [], [])


class TestNotes(unittest.TestCase):
    def test_markdown_escaping(self) -> None:
        cases = {
            "read __init__.py": r"read \_\_init\_\_.py",
            "a <script> and [links](x)": r"a &lt;script&gt; and \[links\](x)",
            "decode &amp; and &#169; for R&D": r"decode \&amp; and \&#169; for R\&D",
            "a ~~strike~~ and *stars*": r"a \~\~strike\~\~ and \*stars\*",
            r"C:\path": r"C:\\path",
            "an odd ` tick": r"an odd \` tick",
            "handle `*args` in signatures": "handle `*args` in signatures",
            "publish @bgantavya/devdocs on npm": "publish `@bgantavya/devdocs` on npm",
            "thank @someone": "thank `@someone`",
            "mail me@example.com": "mail me@example.com",
            "# not a heading": r"\# not a heading",
            "- not a list": r"\- not a list",
            "+ not a list": r"\+ not a list",
            "1. not a list": r"1\. not a list",
            "12) not a list": r"12\) not a list",
            "#12 stays an issue link": "#12 stays an issue link",
            "--flag stays": "--flag stays",
            "--": r"\--",
            "-- -": r"\-- -",
            "---": r"\---",
        }
        for text, escaped in cases.items():
            with self.subTest(text=text):
                self.assertEqual(release.escape_markdown(text), escaped)

    def test_urls_stay_as_written_so_github_links_them(self) -> None:
        for text in ("see https://docs.python.org/3/library/__main__.html",
                     "see www.x.org/a_b, (HTTPS://x.org/~a_b) or ftp://x.org/a*b*",
                     "query https://x.org/?a=1&b=2"):
            with self.subTest(text=text):
                self.assertEqual(release.escape_markdown(text), text)
        cases = {
            "see <https://x.org/a_b>": "see &lt;https://x.org/a_b&gt;",
            "a https://x.org/<img src=x>": "a https://x.org/&lt;img src=x&gt;",
            "xhttps://x.org/_a_ or https://_x_/b": r"xhttps://x.org/\_a\_ or https://\_x\_/b",
        }
        for text, escaped in cases.items():
            with self.subTest(text=text):
                self.assertEqual(release.escape_markdown(text), escaped)

    def test_a_breaking_note_never_becomes_a_rule(self) -> None:
        commits = [("d4", "feat!: rename things\n\nBREAKING CHANGE: ---")]
        self.assertEqual(
            release.render_notes(commits),
            "### Breaking changes\n\n- rename things (d4)\n  - \\---\n",
        )

    def test_sections_order_and_breaking_text(self) -> None:
        commits = [
            ("a1", "feat(cli): add --json"),
            ("b2", "fix: crash on __init__.py"),
            ("c3", "docs: guide"),
            ("d4", "perf: faster scans"),
            ("e5", "feat!: drop the v2 flags\n\nBREAKING CHANGE: --old is gone"),
            ("f6", "feat: second feature"),
            ("g7", "chore(release): v4.0.0"),
            ("h8", "Update README.md"),
            ("i9", "refactor(llm)!: one client"),
        ]
        self.assertEqual(
            release.render_notes(commits),
            "### Breaking changes\n\n"
            "- drop the v2 flags (e5)\n"
            "  - --old is gone\n"
            "- **llm:** one client (i9)\n\n"
            "### Features\n\n"
            "- **cli:** add --json (a1)\n"
            "- second feature (f6)\n\n"
            "### Fixes\n\n"
            "- crash on \\_\\_init\\_\\_.py (b2)\n\n"
            "### Performance\n\n"
            "- faster scans (d4)\n",
        )

    def test_nothing_notable(self) -> None:
        commits = [("a1", "docs: x"), ("b2", "chore(release): v1.0.0"), ("c3", "wip")]
        self.assertEqual(release.render_notes(commits), "No notable changes.\n")


class TestRewrite(unittest.TestCase):
    def test_init_changes_only_the_version_value(self) -> None:
        text = "'''Doc.'''\r\n\r\n__version__ = '1.2.3'  # keep\r\nOTHER = '1.2.3'\r\n"
        self.assertEqual(release.read_version(text), "1.2.3")
        self.assertEqual(
            release.rewrite_init(text, "2.0.0rc1"),
            text.replace("__version__ = '1.2.3'", "__version__ = '2.0.0rc1'"),
        )

    def test_init_needs_exactly_one_version_line(self) -> None:
        for text in ("OTHER = 1\n", '__version__ = "1.0.0"\n__version__ = "2.0.0"\n',
                     '    __version__ = "1.0.0"\n'):
            with self.subTest(text=text), self.assertRaises(release.ReleaseError):
                release.rewrite_init(text, "3.0.0")

    def test_package_changes_only_the_top_level_version(self) -> None:
        new = release.rewrite_package(sample_package("4.0.0-dev.0"), "4.0.0")
        self.assertEqual(new, sample_package("4.0.0"))
        self.assertEqual(release.package_version(new), "4.0.0")
        self.assertEqual(json.loads(new)["config"]["version"], "4.0.0-dev.0")

    def test_package_needs_one_top_level_version_string(self) -> None:
        for text in ('{"name": "x"}', '{"version": 4}', '["version"]', "not json",
                     '{"engines": {"version": "1.0.0"}}',
                     '{"version": "1.0.0", "version": "2.0.0"}'):
            with self.subTest(text=text), self.assertRaises(release.ReleaseError):
                release.rewrite_package(text, "1.0.0")


class TestVersionFiles(unittest.TestCase):
    """current, check and bump on temp copies of the two version files."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.init = self.root / "devdocs" / "__init__.py"
        self.package = self.root / "npm" / "package.json"
        self.init.parent.mkdir()
        self.package.parent.mkdir()
        shutil.copyfile(ROOT / "devdocs" / "__init__.py", self.init)
        self.version = release.read_version(self.init.read_text(encoding="utf-8"))
        self.write_package(release.npm_version(self.version))

    def write_package(self, version: str) -> None:
        self.package.write_bytes(sample_package(version).encode("utf-8"))

    def cli(self, *argv: str) -> tuple[int, str, str]:
        return run("--root", str(self.root), *argv)

    def snapshot(self) -> dict[str, bytes]:
        """Every file under the root, so stray temp files show up too."""
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }

    def test_current(self) -> None:
        self.assertEqual(self.cli("current"), (0, f"{self.version}\n", ""))

    def test_check_passes_when_the_versions_agree(self) -> None:
        code, out, err = self.cli("check")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(release.npm_version(self.version), out)

    def test_check_fails_and_prints_both_when_they_differ(self) -> None:
        self.write_package("0.0.1")
        code, out, err = self.cli("check")
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"devdocs/__init__.py: {self.version}", err)
        self.assertIn("npm/package.json: 0.0.1", err)

    def test_check_fails_on_a_version_npm_cannot_spell(self) -> None:
        self.init.write_text('__version__ = "4.0"\n', encoding="utf-8")
        self.write_package("4.0")
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("not a release version", err)

    def test_check_fails_when_neither_file_has_a_version(self) -> None:
        self.write_package("")
        for version in ("banana", ""):
            with self.subTest(version=version):
                self.init.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
                code, out, err = self.cli("check")
                self.assertEqual((code, out), (1, ""))
                self.assertIn("not a release version", err)

    def test_check_fails_without_package_json(self) -> None:
        self.package.unlink()
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("no such file", err)

    def test_bump_rewrites_only_the_two_values(self) -> None:
        before = self.init.read_bytes()
        code, out, err = self.cli("bump", "4.1.0rc2")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("4.1.0-rc.2", out)
        line = f'__version__ = "{self.version}"'.encode()
        self.assertEqual(self.init.read_bytes(),
                         before.replace(line, b'__version__ = "4.1.0rc2"'))
        self.assertEqual(self.package.read_bytes(),
                         sample_package("4.1.0-rc.2").encode("utf-8"))
        self.assertEqual(self.cli("check")[0], 0)

    def test_bump_refuses_an_invalid_version_with_exit_2(self) -> None:
        before = (self.init.read_bytes(), self.package.read_bytes())
        for version in ("v4.1.0", "4.1", "4.1.0-rc.2", ""):
            with self.subTest(version=version):
                code, out, err = self.cli("bump", version)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("not a release version", err)
        self.assertEqual((self.init.read_bytes(), self.package.read_bytes()), before)

    def test_bump_writes_nothing_unless_both_files_can_change(self) -> None:
        self.package.write_text('{"name": "x"}', encoding="utf-8")
        before = self.init.read_bytes()
        code, _, err = self.cli("bump", "4.1.0")
        self.assertEqual(code, 1)
        self.assertIn('"version"', err)
        self.assertEqual(self.init.read_bytes(), before)

    @unittest.skipIf(SUPERUSER, "root may write a read-only file")
    def test_bump_refuses_a_read_only_file_and_changes_nothing(self) -> None:
        self.package.chmod(0o444)
        self.addCleanup(self.package.chmod, 0o644)
        before = self.snapshot()
        code, out, err = self.cli("bump", "4.1.0")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("package.json", err)
        self.assertEqual(self.snapshot(), before)

    def test_bump_puts_init_back_when_package_json_cannot_be_replaced(self) -> None:
        real_replace = os.replace

        def replace(source: Any, target: Any) -> None:
            if Path(target).name == "package.json":
                raise PermissionError(13, "Permission denied", str(target))
            real_replace(source, target)

        before = self.snapshot()
        with mock.patch.object(release.os, "replace", replace):
            code, out, err = self.cli("bump", "4.1.0")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("package.json: Permission denied; no file was changed", err)
        self.assertEqual(self.snapshot(), before)

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_bump_keeps_each_file_mode_and_leaves_no_temp_file(self) -> None:
        self.init.chmod(0o640)
        self.package.chmod(0o604)
        files = sorted(self.snapshot())
        self.assertEqual(self.cli("bump", "4.1.0")[0], 0)
        self.assertEqual(stat.S_IMODE(self.init.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.package.stat().st_mode), 0o604)
        self.assertEqual(sorted(self.snapshot()), files)

    def test_a_file_that_is_not_utf8_exits_1(self) -> None:
        self.init.write_bytes(b'__version__ = "4.0.0"  # \xff\n')
        code, out, err = self.cli("current")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("__init__.py", err)

    def test_runs_as_a_script(self) -> None:
        done = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), "current"],
            capture_output=True, check=False, text=True,
        )
        self.assertEqual((done.returncode, done.stdout), (0, f"{self.version}\n"))

    def test_next_without_git_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as empty, \
                mock.patch.dict(os.environ, {"PATH": empty}):
            code, out, err = self.cli("next")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("git is not installed", err)

    def test_next_reads_the_commits_since_the_newest_tag(self) -> None:
        answers = {
            "rev-parse": "false\n",
            "tag": "v1.0.0\nv1.2.0\ndevDocs-v3.0.0\n",
            "log": "abc1234\x1ffix: a\n\0",
        }
        calls: list[tuple[str, ...]] = []

        def fake_git(root: Path, *args: str) -> str:
            calls.append(args)
            return answers[args[0]]

        self.init.write_text('__version__ = "1.2.0"\n', encoding="utf-8")
        with mock.patch.object(release, "git", fake_git):
            code, out, _ = self.cli("next", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["version"], "1.2.1")
        self.assertIn("v1.2.0..HEAD", calls[-1])


@unittest.skipIf(shutil.which("git") is None, "git is not installed")
class TestGitCommands(unittest.TestCase):
    """lint-commits, notes and next against a throwaway repository.

    v1.0.0 and three junk tags sit on the first commit; v9.0.0 sits on a branch
    that is never merged; a topic branch is merged with a merge commit.
    """

    root: Path
    repo: Repo
    hashes: dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name) / "repo"
        cls.root.mkdir()
        with isolated_git():
            repo = Repo(cls.root)
            repo.commit("chore: start")
            for tag in ("v1.0.0", "devDocs-v3.0.0", "v-2.0.6", "v2.0"):
                repo.git("tag", tag)
            repo.git("checkout", "-q", "-b", "elsewhere")
            repo.commit("feat: work that is never merged")
            repo.git("tag", "v9.0.0")
            repo.git("checkout", "-q", "main")
            hashes = {
                "feat": repo.commit("feat(cli): add the --json flag"),
                "fix": repo.commit("fix: read the version from __init__.py"),
                "docs": repo.commit("docs: explain the release model"),
            }
            repo.git("checkout", "-q", "-b", "topic")
            hashes["perf"] = repo.commit("perf(index): batch the embedding calls")
            repo.git("checkout", "-q", "main")
            repo.git("merge", "-q", "--no-ff", "--no-edit", "topic")
            hashes["breaking"] = repo.commit(
                "feat!: drop the v2 flags",
                "BREAKING CHANGE: --old-flag is gone;\nuse --new-flag instead.",
            )
            hashes["refactor"] = repo.commit("refactor(llm)!: one client for all")
            hashes["readme"] = repo.commit("Update README.md")
            cls.repo = repo
        cls.hashes = hashes

    def cli(self, *argv: str, root: Path | None = None) -> tuple[int, str, str]:
        with isolated_git():
            return run("--root", str(root or self.root), *argv)

    def test_lint_lists_only_non_conventional_subjects(self) -> None:
        code, out, err = self.cli("lint-commits", "v1.0.0..main")
        readme = self.hashes["readme"]
        self.assertEqual((code, out), (1, f"{readme} Update README.md\n"))
        self.assertIn("1 of 7 commits", err)

    def test_lint_skips_merge_commits(self) -> None:
        code, out, err = self.cli("lint-commits", f"v1.0.0..{self.hashes['refactor']}")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("6 commits checked", err)

    def test_lint_passes_an_empty_range(self) -> None:
        self.assertEqual(self.cli("lint-commits", "main..main")[:2], (0, ""))

    def test_a_git_failure_exits_1(self) -> None:
        code, _, err = self.cli("lint-commits", "no-such-ref..main")
        self.assertEqual(code, 1)
        self.assertIn("git log", err)

    def test_a_range_is_never_read_as_an_option(self) -> None:
        leak = self.root.parent / "leak"
        code, _, _ = self.cli("lint-commits", "--", f"--output={leak}")
        self.assertEqual(code, 1)
        self.assertFalse(leak.exists())

    def test_notes(self) -> None:
        hashes = self.hashes
        self.assertEqual(
            self.cli("notes", "v1.0.0..main"),
            (0,
             "### Breaking changes\n\n"
             f"- drop the v2 flags ({hashes['breaking']})\n"
             "  - --old-flag is gone; use --new-flag instead.\n"
             f"- **llm:** one client for all ({hashes['refactor']})\n\n"
             "### Features\n\n"
             f"- **cli:** add the --json flag ({hashes['feat']})\n\n"
             "### Fixes\n\n"
             f"- read the version from \\_\\_init\\_\\_.py ({hashes['fix']})\n\n"
             "### Performance\n\n"
             f"- **index:** batch the embedding calls ({hashes['perf']})\n",
             ""),
        )

    def test_output_is_utf8_whatever_the_locale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, isolated_git():
            repo = Repo(Path(tmp))
            repo.commit("chore: start")
            repo.git("tag", "v1.0.0")
            feat = repo.commit("feat: map → arrows")
            wrong = repo.commit("Wrong → subject")
            env = {**os.environ, "PYTHONIOENCODING": "cp1252"}   # Windows, redirected
            for command, code, out in (
                ("notes", 0, f"### Features\n\n- map → arrows ({feat})\n"),
                ("lint-commits", 1, f"{wrong} Wrong → subject\n"),
            ):
                with self.subTest(command=command):
                    done = subprocess.run(
                        [sys.executable, str(SCRIPT), "--root", tmp, command,
                         "v1.0.0..HEAD"],
                        capture_output=True, check=False, encoding="utf-8", env=env,
                    )
                    self.assertEqual((done.returncode, done.stdout), (code, out))

    def test_notes_for_a_range_with_nothing_notable(self) -> None:
        span = f"{self.hashes['fix']}..{self.hashes['docs']}"
        self.assertEqual(self.cli("notes", span), (0, "No notable changes.\n", ""))

    def test_next_bumps_the_newest_merged_release_tag(self) -> None:
        self.repo.set_version("1.0.0")
        code, out, err = self.cli("next", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            json.loads(out),
            {"release": True, "version": "2.0.0", "tag": "v2.0.0",
             "previous": "v1.0.0", "bump": "major"},
        )

    def test_next_prints_a_pending_version_and_warns_when_outgrown(self) -> None:
        self.repo.set_version("1.5.0")
        code, out, err = self.cli("next")
        self.assertEqual((code, out), (0, "1.5.0\n"))
        self.assertIn("major release (2.0.0)", err)

    def test_next_without_release_tags_ships_a_final_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, isolated_git():
            repo = Repo(Path(tmp))
            repo.commit("chore: start")
            repo.commit("fix: first fix")
            repo.set_version("0.1.0")
            self.assertEqual(
                json.loads(self.cli("next", "--json", root=repo.path)[1]),
                {"release": True, "version": "0.1.0", "tag": "v0.1.0",
                 "previous": None, "bump": "pending"},
            )

    def test_next_holds_a_prerelease_and_says_how_to_ship_it(self) -> None:
        self.repo.set_version("2.0.0.dev0")
        code, out, err = self.cli("next")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("bump 2.0.0", err)
        code, out, _ = self.cli("next", "--json")
        self.assertEqual((code, json.loads(out)["bump"]), (0, "held"))

    def test_next_prints_nothing_when_nothing_is_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, isolated_git():
            repo = Repo(Path(tmp))
            repo.commit("feat: first")
            repo.git("tag", "v1.0.0")
            repo.commit("docs: later")
            repo.set_version("1.0.0")
            self.assertEqual(self.cli("next", root=repo.path), (0, "", ""))
            code, out, _ = self.cli("next", "--json", root=repo.path)
            self.assertEqual((code, json.loads(out)["bump"]), (0, "none"))

    def test_next_refuses_a_shallow_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, isolated_git():
            clone = Path(tmp) / "clone"
            subprocess.run(
                ["git", "clone", "-q", "--depth", "1", self.root.as_uri(), str(clone)],
                capture_output=True, check=True,
            )
            (clone / "devdocs").mkdir()
            (clone / "devdocs" / "__init__.py").write_text(
                '__version__ = "1.0.0"\n', encoding="utf-8"
            )
            code, out, err = self.cli("next", root=clone)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("shallow clone", err)


if __name__ == "__main__":
    unittest.main()
