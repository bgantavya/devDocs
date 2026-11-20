"""Gitignore semantics, checked against what git itself does."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
import warnings
from pathlib import Path, PurePosixPath

from devdocs.ignore import GitignoreMatcher, Rule


def matcher(*lines: str) -> GitignoreMatcher:
    return GitignoreMatcher.from_lines(list(lines))


class TestParsing(unittest.TestCase):
    def test_blank_and_comment_lines_are_skipped(self) -> None:
        self.assertIsNone(Rule.parse(""))
        self.assertIsNone(Rule.parse("   "))
        self.assertIsNone(Rule.parse("# comment"))

    def test_escaped_hash_is_a_pattern(self) -> None:
        self.assertTrue(matcher(r"\#notes").ignored("#notes"))

    def test_trailing_space_is_ignored_unless_escaped(self) -> None:
        self.assertTrue(matcher("trail   ").ignored("trail"))
        self.assertTrue(matcher("foo\\ ").ignored("foo "))

    def test_lone_slash_is_not_a_rule(self) -> None:
        self.assertIsNone(Rule.parse("/"))


class TestMatching(unittest.TestCase):
    def test_bare_glob_matches_at_any_depth(self) -> None:
        m = matcher("*.log")
        self.assertTrue(m.ignored("app.log"))
        self.assertTrue(m.ignored("deep/nested/app.log"))
        self.assertFalse(m.ignored("app.logger"))

    def test_star_does_not_cross_directories(self) -> None:
        m = matcher("src/*.js")
        self.assertTrue(m.ignored("src/a.js"))
        self.assertFalse(m.ignored("src/sub/a.js"))

    def test_double_star_crosses_directories(self) -> None:
        m = matcher("src/**/*.js")
        self.assertTrue(m.ignored("src/a.js"))
        self.assertTrue(m.ignored("src/x/y/a.js"))

    def test_leading_double_star_matches_at_root_too(self) -> None:
        m = matcher("**/generated")
        self.assertTrue(m.ignored("generated", is_dir=True))
        self.assertTrue(m.ignored("a/b/generated", is_dir=True))

    def test_trailing_double_star_matches_contents(self) -> None:
        m = matcher("logs/**")
        self.assertTrue(m.ignored("logs/a.txt"))
        self.assertTrue(m.ignored("logs/deep/b.txt"))

    def test_question_mark_is_one_character(self) -> None:
        m = matcher("doc?.md")
        self.assertTrue(m.ignored("doc1.md"))
        self.assertFalse(m.ignored("doc12.md"))

    def test_character_class_and_negated_class(self) -> None:
        self.assertTrue(matcher("[Bb]in").ignored("Bin"))
        self.assertTrue(matcher("[!a]x").ignored("bx"))
        self.assertFalse(matcher("[!a]x").ignored("ax"))

    def test_unterminated_class_is_literal(self) -> None:
        self.assertTrue(matcher("a[b").ignored("a[b"))


class TestDirectories(unittest.TestCase):
    def test_dir_only_pattern_skips_files_of_that_name(self) -> None:
        m = matcher("build/")
        self.assertTrue(m.ignored("build", is_dir=True))
        self.assertFalse(m.ignored("build", is_dir=False))

    def test_dir_only_pattern_covers_everything_inside(self) -> None:
        # Regression: files beneath a dir-only match were once kept.
        m = matcher("build/")
        self.assertTrue(m.ignored("build/out.js"))
        self.assertTrue(m.ignored("build/a/b/c.js"))
        self.assertTrue(m.ignored("pkg/build/out.js"))

    def test_similar_names_are_not_caught(self) -> None:
        m = matcher("build/", "out/")
        self.assertFalse(m.ignored("rebuild.py"))
        self.assertFalse(m.ignored("routes.py"))
        self.assertFalse(m.ignored("layout.tsx"))


class TestAnchoring(unittest.TestCase):
    def test_leading_slash_anchors_to_root(self) -> None:
        m = matcher("/root.txt")
        self.assertTrue(m.ignored("root.txt"))
        self.assertFalse(m.ignored("sub/root.txt"))

    def test_middle_slash_anchors_too(self) -> None:
        m = matcher("docs/api")
        self.assertTrue(m.ignored("docs/api", is_dir=True))
        self.assertFalse(m.ignored("x/docs/api", is_dir=True))


class TestNegation(unittest.TestCase):
    def test_later_negation_wins(self) -> None:
        m = matcher("*.log", "!keep.log")
        self.assertTrue(m.ignored("app.log"))
        self.assertFalse(m.ignored("keep.log"))

    def test_earlier_negation_loses_to_later_rule(self) -> None:
        self.assertTrue(matcher("!keep.log", "*.log").ignored("keep.log"))

    def test_negation_inside_ignored_directory_contents(self) -> None:
        m = matcher("dist/*", "!dist/keep.txt")
        self.assertTrue(m.ignored("dist/x.js"))
        self.assertFalse(m.ignored("dist/keep.txt"))

    def test_match_returns_deciding_rule(self) -> None:
        rule = matcher("*.log").match("a.log")
        assert rule is not None
        self.assertEqual(rule.source, "*.log")
        self.assertIsNone(matcher("*.log", "!a.log").match("a.log"))


class TestFromRoot(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / ".git").mkdir()   # work-tree boundary, whatever holds tempdir

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_nested_gitignore_is_scoped_to_its_directory(self) -> None:
        self.write(".gitignore", "*.log\n")
        self.write("pkg/.gitignore", "*.tmp\n")
        m = GitignoreMatcher.from_root(self.root)
        self.assertTrue(m.ignored("pkg/a.tmp"))
        self.assertFalse(m.ignored("a.tmp"))
        self.assertTrue(m.ignored("pkg/a.log"))

    def test_git_info_exclude_is_read(self) -> None:
        self.write(".git/info/exclude", "secret.txt\n")
        self.assertTrue(GitignoreMatcher.from_root(self.root).ignored("secret.txt"))

    def test_extra_rules_apply(self) -> None:
        m = GitignoreMatcher.from_root(self.root, extra=["node_modules/"])
        self.assertTrue(m.ignored("node_modules", is_dir=True))

    def test_windows_separators_are_normalised(self) -> None:
        self.assertTrue(matcher("src/*.js").ignored("src\\a.js"))


class TreeCase(unittest.TestCase):
    """A temporary work tree, bounded by its own `.git` directory."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / ".git").mkdir()

    def write(self, relative: str, data: str | bytes = "x") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
        return path

    def symlink(self, target: str | Path, relative: str) -> None:
        try:
            os.symlink(target, self.root / relative)
        except (OSError, NotImplementedError) as err:
            self.skipTest(f"symlinks unavailable: {err}")


class TestDotPaths(TreeCase):
    """Regression: `lstrip("./")` once turned `.env` into `env`."""

    def test_top_level_dot_names_match_their_rules(self) -> None:
        m = matcher(".env", ".venv/", ".tox/", ".git/")
        self.assertTrue(m.ignored(".env"))
        self.assertTrue(m.ignored(".venv", is_dir=True))
        self.assertTrue(m.ignored(".venv/lib/site.py"))
        self.assertTrue(m.ignored(".tox", is_dir=True))
        self.assertTrue(m.ignored(".git", is_dir=True))

    def test_only_a_leading_dot_slash_is_removed(self) -> None:
        self.assertTrue(matcher(".env").ignored("./.env"))
        self.assertFalse(matcher("env").ignored(".env"))
        self.assertFalse(matcher("venv/").ignored(".venv", is_dir=True))

    def test_gitignore_inside_a_top_level_dot_directory_applies(self) -> None:
        self.write(".gitignore", ".secrets/\n")
        self.write(".github/.gitignore", "*.local.yml\n")
        m = GitignoreMatcher.from_root(self.root)
        self.assertTrue(m.ignored(".github/ci.local.yml"))
        self.assertFalse(m.ignored(".github/ci.yml"))
        self.assertTrue(m.ignored(".secrets/credentials.json"))


class TestBrackets(unittest.TestCase):
    """Bracket expressions follow git's wildmatch and never reach Python's re."""

    def test_lines_git_accepts_never_raise(self) -> None:
        for line in ("a[\\]]", "b[z-a]", "c[\\]", "[\\e]x", "[a-Z]", "[[:foo:]]",
                     "[[:"):
            with self.subTest(line=line):
                matcher(line).ignored("c")

    def test_escaped_close_bracket_is_a_member(self) -> None:
        self.assertTrue(matcher("a[\\]]").ignored("a]"))
        self.assertFalse(matcher("a[\\]]").ignored("a\\"))

    def test_reversed_range_matches_only_its_first_character(self) -> None:
        m = matcher("b[z-a]")
        self.assertTrue(m.ignored("bz"))
        self.assertFalse(m.ignored("ba"))
        self.assertFalse(m.ignored("bm"))

    def test_bracket_never_matches_a_slash(self) -> None:
        self.assertFalse(matcher("a[!x]b").ignored("a/b"))
        self.assertFalse(matcher("/a[!x]b").ignored("a/b"))
        self.assertTrue(matcher("/a[!x]b").ignored("ayb"))
        self.assertFalse(matcher("x/a[--0]b").ignored("x/a/b"))   # range spans `/`
        self.assertTrue(matcher("x/a[--0]b").ignored("x/a.b"))

    def test_posix_classes(self) -> None:
        self.assertTrue(matcher("v[[:digit:]]").ignored("v1"))
        self.assertFalse(matcher("v[[:digit:]]").ignored("vd"))
        self.assertTrue(matcher("[[:upper:]][[:lower:]]").ignored("Ab"))
        self.assertTrue(matcher("x[[:space:][:punct:]]").ignored("x-"))
        self.assertFalse(matcher("v[[:foo:]]").ignored("v1"))   # unknown class

    def test_backslash_is_an_escape_not_a_regex_class(self) -> None:
        m = matcher("x[\\d]")
        self.assertTrue(m.ignored("xd"))
        self.assertFalse(m.ignored("x7"))

    def test_close_bracket_first_and_dash_at_the_edges_are_members(self) -> None:
        self.assertTrue(matcher("[]a]").ignored("]"))
        self.assertTrue(matcher("[!]a]").ignored("b"))
        self.assertFalse(matcher("[!]a]").ignored("]"))
        self.assertTrue(matcher("[a-]").ignored("-"))
        self.assertTrue(matcher("[-a]").ignored("-"))

    def test_no_regex_warnings(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            for line in ("v[[:digit:]]", "[[a]", "[a&&b]", "[a||b]", "[~~]", "[--/]"):
                Rule.parse(line)


class TestDoubleStar(unittest.TestCase):
    def test_double_star_inside_a_segment_is_a_single_star(self) -> None:
        self.assertFalse(matcher("logs/**.log").ignored("logs/2024/app.log"))
        self.assertTrue(matcher("logs/**.log").ignored("logs/app.log"))
        self.assertFalse(matcher("src/**.js").ignored("src/lib/util.js"))
        self.assertFalse(matcher("a**b").ignored("ax/yb"))
        self.assertTrue(matcher("a**b").ignored("axyb"))
        self.assertTrue(matcher("/**.x").ignored("a.x"))
        self.assertFalse(matcher("/**.x").ignored("d/a.x"))

    def test_runs_of_stars_collapse(self) -> None:
        self.assertTrue(matcher("***/c").ignored("c"))
        self.assertTrue(matcher("***/c").ignored("a/b/c"))

    def test_middle_double_star_matches_zero_or_more_directories(self) -> None:
        m = matcher("a/**/b")
        self.assertTrue(m.ignored("a/b"))
        self.assertTrue(m.ignored("a/x/y/b"))
        self.assertFalse(m.ignored("ab"))

    def test_pathological_patterns_stay_linear(self) -> None:
        cases = (
            ("*a*a*a*a*a*a*a*a*a*a*a*a*b", "a" * 5000),
            ("**/a*/**/a*/**/a*/**/b", "/".join(["aa"] * 300) + "/c"),
            ("**/a/**/a/**/a/**/a/**/b", "/".join(["a"] * 400) + "/c"),
            ("*/*/*/*/*/*/*/*/*/x", "/".join(["a"] * 500)),
        )
        started = time.perf_counter()
        for line, path in cases:
            self.assertFalse(matcher(line).ignored(path))
        self.assertLess(time.perf_counter() - started, 2.0)


class TestNegatedDirectories(unittest.TestCase):
    """A `!dir/` re-includes the directory only; files need their own rule."""

    def test_whitelist_idiom(self) -> None:
        m = matcher("*", "!*/", "!*.py")
        self.assertTrue(m.ignored("top.txt"))
        self.assertFalse(m.ignored("src", is_dir=True))
        self.assertFalse(m.ignored("src/main.py"))
        self.assertTrue(m.ignored("src/notes.txt"))

    def test_negated_directory_does_not_re_include_its_files(self) -> None:
        m = matcher("*.log", "!logs/")
        self.assertTrue(m.ignored("logs/app.log"))
        self.assertFalse(m.ignored("logs", is_dir=True))


class TestParentExclusion(TreeCase):
    """git: a file cannot be re-included when a parent directory is excluded."""

    def test_negation_under_an_excluded_directory_is_powerless(self) -> None:
        self.assertTrue(matcher("logs/", "!logs/keep.log").ignored("logs/keep.log"))
        m = matcher("build/*", "!build/gen/main.py")
        self.assertTrue(m.ignored("build/gen/main.py"))
        self.assertTrue(m.ignored("build/gen", is_dir=True))

    def test_match_names_the_rule_that_excluded_the_parent(self) -> None:
        rule = matcher("logs/", "!logs/keep.log").match("logs/keep.log")
        assert rule is not None
        self.assertEqual(rule.source, "logs/")

    def test_gitignore_inside_an_excluded_directory_is_never_read(self) -> None:
        self.write(".gitignore", "vendor/\n")
        self.write("vendor/pkg/.gitignore", "!*.go\n")
        m = GitignoreMatcher.from_root(self.root)
        self.assertTrue(m.ignored("vendor/pkg/a.go"))
        self.assertEqual([r.base for r in m.rules if r.base], [])

    def test_rule_matches_only_the_exact_path(self) -> None:
        rule = Rule.parse("build/")
        assert rule is not None
        self.assertTrue(rule.matches("build", True))
        self.assertFalse(rule.matches("build/out.js", False))


class TestPrecedence(TreeCase):
    def test_deeper_gitignore_wins_whatever_its_name(self) -> None:
        self.write("pkg/.gitignore", "*.json\n")
        for sub in (".config", "-x", "conf"):
            with self.subTest(sub=sub):
                self.write(f"pkg/{sub}/.gitignore", "!settings.json\n")
                m = GitignoreMatcher.from_root(self.root)
                self.assertFalse(m.ignored(f"pkg/{sub}/settings.json"))
                self.assertTrue(m.ignored("pkg/other.json"))

    def test_deeper_base_wins_regardless_of_rule_order(self) -> None:
        deep = Rule.parse("!a.log", base="pkg")
        shallow = Rule.parse("*.log")
        assert deep is not None and shallow is not None
        self.assertFalse(GitignoreMatcher([deep, shallow]).ignored("pkg/a.log"))

    def test_info_exclude_ranks_below_gitignore(self) -> None:
        self.write(".git/info/exclude", "*.log\n")
        self.write(".gitignore", "!keep.log\n")
        m = GitignoreMatcher.from_root(self.root)
        self.assertTrue(m.ignored("a.log"))
        self.assertFalse(m.ignored("keep.log"))

    def test_extra_ranks_below_every_file(self) -> None:
        self.write(".gitignore", "!keep.log\n")
        m = GitignoreMatcher.from_root(self.root, extra=["*.log"])
        self.assertTrue(m.ignored("a.log"))
        self.assertFalse(m.ignored("keep.log"))


class TestFileFormat(TreeCase):
    def test_utf8_bom_is_skipped(self) -> None:
        self.write(".gitignore", b"\xef\xbb\xbfnode_modules/\n*.log\n")
        m = GitignoreMatcher.from_root(self.root)
        self.assertTrue(m.ignored("node_modules", is_dir=True))
        self.assertTrue(m.ignored("node_modules/lib/index.js"))
        self.assertTrue(m.ignored("a.log"))

    def test_crlf_line_endings(self) -> None:
        self.write(".gitignore", b"*.log\r\nbuild/\r\n")
        m = GitignoreMatcher.from_root(self.root)
        self.assertTrue(m.ignored("a.log"))
        self.assertTrue(m.ignored("build", is_dir=True))

    def test_only_trailing_spaces_are_trimmed(self) -> None:
        m = matcher("build\t")
        self.assertFalse(m.ignored("build"))
        self.assertFalse(m.ignored("src/build"))
        self.assertTrue(m.ignored("build\t"))

    def test_hash_is_a_comment_only_in_the_first_column(self) -> None:
        self.assertTrue(matcher("  #tmp").ignored("  #tmp"))
        self.assertIsNone(Rule.parse("#tmp"))

    def test_trailing_backslash_never_matches(self) -> None:
        self.assertFalse(matcher("foo\\").ignored("foo"))
        self.assertFalse(matcher("foo\\").ignored("foo\\"))

    def test_symlinked_gitignore_is_not_read(self) -> None:
        self.write("rules", "*.log\n")
        (self.root / "sub").mkdir()
        self.symlink("../rules", "sub/.gitignore")
        self.assertFalse(GitignoreMatcher.from_root(self.root).ignored("sub/a.log"))

    def test_backslash_in_a_posix_name_is_not_a_separator(self) -> None:
        m = matcher("a")
        self.assertFalse(m.ignored(PurePosixPath("a\\b")))
        self.assertTrue(m.ignored("a\\b"))   # a str may use Windows separators


class TestIgnoreCase(TreeCase):
    def test_case_sensitive_by_default(self) -> None:
        self.assertFalse(matcher("build/").ignored("Build", is_dir=True))

    def test_ignore_case_folds_names(self) -> None:
        m = GitignoreMatcher.from_lines(["build/", "*.log"], ignore_case=True)
        self.assertTrue(m.ignored("Build", is_dir=True))
        self.assertTrue(m.ignored("Build/out.js"))
        self.assertTrue(m.ignored("Debug.LOG"))

    def test_core_ignorecase_is_read_from_the_repository(self) -> None:
        self.write(".git/config", "[core]\n\tbare = false\n\tignorecase = true\n")
        self.write(".gitignore", "build/\n*.log\n")
        m = GitignoreMatcher.from_root(self.root)
        self.assertTrue(m.ignore_case)
        self.assertTrue(m.ignored("Build", is_dir=True))
        self.assertTrue(m.ignored("Debug.LOG"))

    def test_core_ignorecase_false_or_elsewhere_does_not_count(self) -> None:
        self.write(
            ".git/config", '[core]\n\tignorecase = false\n[x "core"]\n\tignorecase\n'
        )
        self.assertFalse(GitignoreMatcher.from_root(self.root).ignore_case)

    def test_uppercase_bracket_or_escaped_literal_never_matches(self) -> None:
        # git folds the path, not these pattern characters.
        def folded(line: str) -> GitignoreMatcher:
            return GitignoreMatcher.from_lines([line], ignore_case=True)

        self.assertFalse(folded("[A]bc").ignored("Abc"))
        self.assertFalse(folded("\\Abc").ignored("abc"))
        self.assertTrue(folded("[!A]x").ignored("Ax"))
        self.assertTrue(folded("[A-C]x").ignored("bx"))
        self.assertTrue(folded("[a]bc").ignored("Abc"))


class TestRepository(TreeCase):
    """Scanning a subdirectory still applies the repository's rules."""

    def test_ancestor_gitignore_and_info_exclude_apply_rebased(self) -> None:
        self.write(".git/info/exclude", "secret.txt\n")
        self.write(".gitignore", "coverage/\n*.local.ts\n/packages/app/dist/\n")
        self.write("packages/.gitignore", "*.tmp\n")
        app = self.root / "packages" / "app"
        app.mkdir(parents=True)
        m = GitignoreMatcher.from_root(app)
        self.assertTrue(m.ignored("src/keys.local.ts"))
        self.assertTrue(m.ignored("coverage", is_dir=True))
        self.assertTrue(m.ignored("dist", is_dir=True))
        self.assertTrue(m.ignored("secret.txt"))
        self.assertTrue(m.ignored("a.tmp"))
        self.assertFalse(m.ignored("src/index.ts"))
        self.assertFalse(m.ignored("lib/dist", is_dir=True))

    def test_root_itself_is_never_ignored(self) -> None:
        self.write(".gitignore", "generated/\n")
        sub = self.root / "generated"
        sub.mkdir()
        self.assertFalse(GitignoreMatcher.from_root(sub).ignored("api.py"))

    def test_git_file_points_at_the_git_directory(self) -> None:
        shutil.rmtree(self.root / ".git")
        self.write("real.git/info/exclude", "secret.txt\n")
        self.write("real.git/config", "[core]\n\tignorecase = true\n")
        self.write("work/.git", "gitdir: ../real.git\n")
        m = GitignoreMatcher.from_root(self.root / "work")
        self.assertTrue(m.ignored("SECRET.txt"))

    def test_nested_files_are_read_lazily(self) -> None:
        self.write("pkg/.gitignore", "*.tmp\n")
        m = GitignoreMatcher.from_root(self.root)
        self.assertNotIn("pkg", {rule.base for rule in m.rules})
        self.assertTrue(m.ignored("pkg/a.tmp"))
        self.assertIn("pkg", {rule.base for rule in m.rules})


if __name__ == "__main__":
    unittest.main()
