"""The text helpers every renderer shares: CommonMark code spans, relative
links between pages and URL encoding. Each was copied into two or three
renderers before, and the copies had drifted; these tests hold the one
behaviour they all get now."""

from __future__ import annotations

import string
import unittest

from devdocs.render.text import code_spans, relpath, url_path


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

    def test_text_may_be_all_code_or_none(self) -> None:
        self.assertEqual(self.split("`a`"), [(True, "`a`")])
        self.assertEqual(self.split(""), [])
        self.assertEqual(self.split("plain"), [(False, "plain")])


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
        # The PDF's copy kept the name; the Markdown and HTML copies gave the
        # directory itself, "." or "..".
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
        # to `fö.py`; the HTML copy encoded them all.
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


if __name__ == "__main__":
    unittest.main()
