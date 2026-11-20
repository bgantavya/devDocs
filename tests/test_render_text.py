"""The text helpers every renderer shares: CommonMark code spans, relative
links between pages, URL encoding and the file-name fold, each defined once
(Markdown and the style guard each kept a copy of the code-span reader
before). These tests hold the one behaviour every renderer gets."""

from __future__ import annotations

import string
import unittest

from devdocs.render.text import code_spans, code_text, folded, relpath, url_path


class TestCodeSpans(unittest.TestCase):
    def split(self, text: str) -> list[tuple[bool, str]]:
        pieces = list(code_spans(text))
        self.assertEqual("".join(chunk for _, chunk in pieces), text)
        return pieces

    def test_a_run_closes_only_on_a_run_of_its_own_length(self) -> None:
        self.assertEqual(self.split("a `b` c ``d ` e`` f"), [
            (False, "a "), (True, "`b`"), (False, " c "), (True, "``d ` e``"),
            (False, " f"),
        ])

    def test_an_opener_without_a_closer_is_literal(self) -> None:
        self.assertEqual(self.split("a `` b ` c"), [(False, "a `` b ` c")])
        self.assertEqual(self.split("``a` `b`"), [(False, "``a"), (True, "` `"),
                                                 (False, "b`")])

    def test_a_run_of_another_length_is_content(self) -> None:
        self.assertEqual(self.split("`a``b` `c`"), [(True, "`a``b`"), (False, " "),
                                                   (True, "`c`")])
        self.assertEqual(code_text("`a``b`"), "a``b")

    def test_text_may_be_all_code_or_none(self) -> None:
        self.assertEqual(self.split("`a`"), [(True, "`a`")])
        self.assertEqual(self.split(""), [])
        self.assertEqual(self.split("plain"), [(False, "plain")])


class TestCodeText(unittest.TestCase):
    def test_backticks_and_one_padding_space_each_side_go(self) -> None:
        self.assertEqual(code_text("`a`"), "a")
        self.assertEqual(code_text("`` `x` ``"), "`x`")
        self.assertEqual(code_text("`  a  `"), " a ")
        self.assertEqual(code_text("` a`"), " a")      # padding on one side stays

    def test_content_of_spaces_alone_is_kept(self) -> None:
        self.assertEqual(code_text("` `"), " ")
        self.assertEqual(code_text("`   `"), "   ")

    def test_only_spaces_count_as_padding(self) -> None:
        # CommonMark strips one space from each end only when both ends are
        # spaces and the span is not all spaces; a tab is not padding.
        self.assertEqual(code_text("` \t `"), "\t")
        self.assertEqual(code_text("`\ta\t`"), "\ta\t")

    def test_a_line_break_is_a_space_before_padding_is_judged(self) -> None:
        # A line break is a space before the padding is judged, so `` a<LF>``
        # shows "a", as in CommonMark.
        self.assertEqual(code_text("`` a\n``"), "a")
        self.assertEqual(code_text("`a\nb`"), "a b")
        self.assertEqual(code_text("`a\r\nb`"), "a b")
        self.assertEqual(code_text("`\na\n`"), "a")


class TestRelpath(unittest.TestCase):
    def test_paths_between_pages(self) -> None:
        cases = [
            ("src/lib.py.html", "src/app", "../lib.py.html"),
            ("src/app/main.py.html", "src/app", "main.py.html"),
            ("index.html", "src/app", "../../index.html"),
            ("src/app/main.py.html", "", "src/app/main.py.html"),
            ("src/app/main.py.html", ".", "src/app/main.py.html"),
            ("assets/style.css", "a/b", "../../assets/style.css"),
            ("./a//b.md", "a/./", "b.md"),
        ]
        for path, start, expected in cases:
            with self.subTest(path=path, start=start):
                self.assertEqual(relpath(path, start), expected)

    def test_the_file_name_stays_where_a_directory_on_the_way_shares_it(self) -> None:
        # From a page under `a.md/`, the file `a.md` is ../a.md, never ".",
        # the directory itself.
        self.assertEqual(relpath("a.md", "a.md"), "../a.md")
        self.assertEqual(relpath("pkg/a", "pkg/a/b"), "../../a")

    def test_nothing_to_point_at_is_here(self) -> None:
        self.assertEqual(relpath("", "."), ".")
        self.assertEqual(relpath(".", ""), ".")


class TestUrlPath(unittest.TestCase):
    def test_unreserved_ascii_slashes_and_printable_non_ascii_stay(self) -> None:
        kept = string.ascii_letters + string.digits + "-._~/" + "café/中文/Ωmega"
        self.assertEqual(url_path(kept), kept)

    def test_everything_a_url_or_a_markdown_link_could_read_is_encoded(self) -> None:
        cases = {
            ":": "%3A", "#": "%23", "?": "%3F", "%": "%25", " ": "%20",
            "(": "%28", ")": "%29", "[": "%5B", "]": "%5D", "<": "%3C", ">": "%3E",
            "\\": "%5C", "|": "%7C", '"': "%22", "`": "%60", "^": "%5E",
            "{": "%7B", "}": "%7D", "\t": "%09", "\x7f": "%7F",
        }
        for char, expected in cases.items():
            with self.subTest(char=char):
                self.assertEqual(url_path(char), expected)

    def test_sub_delimiters_are_encoded_too(self) -> None:
        # Regression: Markdown's copy kept them, and CommonMark reads a
        # character reference in a link destination, so `f&ouml;.py` linked
        # to `fö.py`.
        self.assertEqual(url_path("f&ouml;.py.md"), "f%26ouml%3B.py.md")
        self.assertEqual(url_path("!$'*+,;=@"), "%21%24%27%2A%2B%2C%3B%3D%40")

    def test_a_scheme_never_survives(self) -> None:
        self.assertEqual(url_path("javascript:alert(1).py.html"),
                         "javascript%3Aalert%281%29.py.html")

    def test_invisible_non_ascii_is_encoded_as_utf8(self) -> None:
        self.assertEqual(url_path("a\xa0b\u2028c\ufeff"), "a%C2%A0b%E2%80%A8c%EF%BB%BF")

    def test_undecodable_bytes_and_lone_surrogates_degrade(self) -> None:
        self.assertEqual(url_path("bad\udcff.py"), "bad%FF.py")
        self.assertEqual(url_path("odd\ud800.py"), "odd%EF%BF%BD.py")


class TestFolded(unittest.TestCase):
    def test_case_and_normalization_fold_alike(self) -> None:
        same = [
            ("Index.html", "index.html"),
            ("caf\xe9.py", "cafe\u0301.py"),  # NFC and NFD
            ("CAF\xc9.PY", "cafe\u0301.py"),
            ("\u212a.py", "k.py"),  # Kelvin sign
            ("Stra\xdfe.py", "STRASSE.py"),  # folds more: a suffix
        ]
        for first, second in same:
            with self.subTest(first=first, second=second):
                self.assertEqual(folded(first), folded(second))

    def test_other_names_stay_apart(self) -> None:
        for first, second in (("a.py", "b.py"), ("cafe.py", "caf\xe9.py"),
                              ("a/b.py", "a-b.py")):
            with self.subTest(first=first, second=second):
                self.assertNotEqual(folded(first), folded(second))

    def test_folding_twice_changes_nothing(self) -> None:
        for name in ("Caf\xe9", "\u1e9e", "\u0390", "\ufb01le"):
            with self.subTest(name=name):
                self.assertEqual(folded(folded(name)), folded(name))


if __name__ == "__main__":
    unittest.main()
