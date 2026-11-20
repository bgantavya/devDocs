"""Known-issue markers are read from comments, and only from comments."""

from __future__ import annotations

import unittest

from devdocs.issues import Issue, find_issues


class TestPython(unittest.TestCase):
    def test_markers_in_comments(self) -> None:
        source = (
            "import os  # TODO: drop os\n"
            "def f():\n"
            "    # FIXME(ana): leaks a handle\n"
            "    return 1\n"
        )
        self.assertEqual(find_issues(source, "python"), [
            Issue(1, "TODO", "drop os"),
            Issue(3, "FIXME", "leaks a handle"),
        ])

    def test_markers_in_strings_are_data(self) -> None:
        source = 'MSG = "TODO: not a note"\nDOC = """\nFIXME: still a string\n"""\n'
        self.assertEqual(find_issues(source, "python"), [])

    def test_lowercase_and_empty_markers_are_ignored(self) -> None:
        source = "# todo: someday\n# TODO\n# XXX - real one\n"
        self.assertEqual(find_issues(source, "python"), [Issue(3, "XXX", "real one")])

    def test_untokenizable_source_falls_back_to_comment_masking(self) -> None:
        source = (
            'def f(:\n    # HACK: works around a parser bug\n    x = "unterminated\n'
        )
        self.assertEqual(find_issues(source, "python"),
                         [Issue(2, "HACK", "works around a parser bug")])


class TestBraceLanguages(unittest.TestCase):
    def test_line_and_block_comments_with_line_numbers(self) -> None:
        source = (
            'const url = "http://x/TODO";\n'
            "// TODO: cache this\n"
            "/*\n"
            " * FIXME: wrong on DST days\n"
            " */\n"
            "let s = `BUG: in a template`;\n"
        )
        self.assertEqual(find_issues(source, "typescript"), [
            Issue(2, "TODO", "cache this"),
            Issue(4, "FIXME", "wrong on DST days"),
        ])

    def test_block_comment_closer_is_trimmed(self) -> None:
        self.assertEqual(find_issues("/* TODO: tidy up */ int x;\n", "java"),
                         [Issue(1, "TODO", "tidy up")])

    def test_hash_comment_languages(self) -> None:
        source = 'echo "# TODO: not this"\n# TODO: retry on failure\n'
        self.assertEqual(find_issues(source, "shell"),
                         [Issue(2, "TODO", "retry on failure")])

    def test_long_text_is_cut_at_a_word(self) -> None:
        issue = find_issues("// TODO: " + "word " * 60 + "\n", "go")[0]
        self.assertLessEqual(len(issue.text), 160)
        self.assertTrue(issue.text.endswith("..."))

    def test_cap_per_file(self) -> None:
        source = "".join(f"// TODO: item {n}\n" for n in range(40))
        self.assertEqual(len(find_issues(source, "rust")), 25)

    def test_no_marker_no_work(self) -> None:
        self.assertEqual(find_issues("fn main() {}\n", "rust"), [])


if __name__ == "__main__":
    unittest.main()
