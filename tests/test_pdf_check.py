"""PDF checker: the strict check against small files that each break one rule,
and the writer's output read back through its parser."""

from __future__ import annotations

import unittest
import zlib
from collections.abc import Callable
from typing import Any

from devdocs.render.pdf import check as read
from devdocs.render.pdf.check import PdfFile, check
from devdocs.render.pdf.writer import Name, Objects, Page, Ref, Stream, build
from tests.test_pdf_writer import small_pdf


def written(value: Any) -> Any:
    """A value as check.py reads it, in the writer's types, streams decoded."""
    if isinstance(value, read.Ref):
        return Ref(value.number)
    if isinstance(value, read.Name):
        return Name(value)
    if isinstance(value, read.Stream):
        flate = value.entries.get("Filter") == "FlateDecode"
        data = zlib.decompress(value.raw) if flate else value.raw
        return Stream(data, {key: written(item) for key, item in value.entries.items()
                             if key not in ("Length", "Filter")})
    if isinstance(value, dict):
        return {key: written(item) for key, item in value.items()}
    if isinstance(value, list):
        return [written(item) for item in value]
    return value


Change = Callable[[dict[int, Any], dict[str, Any]], None]


def rewrite(data: bytes, change: Change) -> bytes:
    """`data` written again after `change` edits its objects and trailer, so
    its cross-reference table stays exact and only the edit is wrong."""
    pdf = PdfFile(data)
    objects = {number: written(value) for number, value in pdf.objects.items()}
    trailer = written(pdf.trailer)
    change(objects, trailer)
    out = Objects()
    refs = [out.reserve() for _ in objects]
    for number, value in sorted(objects.items()):
        out.define(refs[number - 1], value)
    return out.serialize(root=trailer["Root"], info=trailer.get("Info"))


def name_tree(objects: dict[int, Any], trailer: dict[str, Any]) -> Any:
    catalog = objects[trailer["Root"].number]
    return objects[catalog["Names"]["Dests"].number]


def one_page(content: bytes, fonts: dict[str, str] | None = None) -> bytes:
    """A one-page file of `content`, Helvetica its /F1 unless `fonts` says."""
    return build([Page(200, 300, content)], fonts=fonts or {"F1": "Helvetica"},
                 destinations={})


class TestCheck(unittest.TestCase):
    def test_header_names_a_pdf_version(self) -> None:
        data = small_pdf()
        self.assertEqual(check(data.replace(b"%PDF-1.7", b"%PDF-2.0", 1)), [])
        problems = check(data.replace(b"%PDF-1.7", b"%PDF-9.9", 1))
        self.assertTrue(any("not a PDF version" in p for p in problems), problems)

    def test_content_streams_are_read_operator_by_operator(self) -> None:
        self.assertEqual(check(one_page(
            b"q BT /F1 12 Tf 10 250 Td (a\\(b) Tj [(c) -20 (d)] TJ ET Q "
            b"0 0 1 rg 0 0 50 50 re f 1 w 0 0 m 9 9 l S"
        )), [])
        cases = {
            b"BT /F1 12 Tf 10 250 Td (x) Tj": "never closed with ET",
            b"/F1 12 Tf 10 250 Td (x) Tj": "Td is not allowed at page level",
            b"BT /F1 12 Tf 10 250 Td (x) Tx ET": "unknown operator Tx",
            b"BT /F1 12 Tf 0 0 1 10 250 Tm (x) Tj ET": "Tm takes 6 operands, not 5",
            b"BT /F1 (12) Tf ET": "Tf operand 2, b'12', is not a number",
            b"BT /F1 12 Tf 10 250 Td (x Tj ET": "unterminated string",
            b"BT /F9 12 Tf ET": "/F9, which the page's /Resources /Font lacks",
            b"BT 10 250 Td (x) Tj ET": "shows text before Tf sets a font",
            b"BT /F1 12 Tf (a\\007b) Tj ET": "shows code 0x07",
            b"BT BT ET ET": "BT is not allowed inside a text object",
            b"BT 0 0 m 5 5 l S ET": "m is not allowed inside a text object",
            b"0 0 m 5 5 l": "a path is never painted",
            b"5 5 l S": "l is not allowed at page level",
            b"q 2 0 0 2 0 0 cm": "a q is never matched by Q",
            b"Q": "Q without a q before it",
            b"BI /W 1 ID x EI": "inline images are not supported",
        }
        for content, expected in cases.items():
            with self.subTest(content=content):
                problems = check(one_page(content))
                self.assertTrue(any(expected in p for p in problems), problems)

    def test_fonts_are_standard_type1_fonts_in_a_predefined_encoding(self) -> None:
        problems = check(one_page(b"", {"F1": "Helvetica-Bolx"}))
        self.assertTrue(any("not one of the 14 standard fonts" in p
                            for p in problems), problems)
        data = small_pdf()
        for old, new, expected in (
            (b"/Subtype/Type1", b"/Subtype/Type3", "only Type1 fonts are supported"),
            (b"/WinAnsiEncoding", b"/WinAnsiEncodinX", "not a predefined encoding"),
            (b"/Font<</F1 ", b"/Font<</FX ", "/F1, which the page's /Resources /Font"),
        ):
            with self.subTest(change=new):
                problems = check(data.replace(old, new, 1))
                self.assertTrue(any(expected in p for p in problems), problems)

    def test_name_tree_keys_ascend_across_leaves_and_appear_once(self) -> None:
        many = small_pdf(destinations={
            **{f"d{index:03d}": (0, 100.0) for index in range(150)},
            "first": (0, 290.0), "second": (1, 290.0),
        })

        def swap_leaves(objects: dict[int, Any], trailer: dict[str, Any]) -> None:
            kids = name_tree(objects, trailer)["Kids"]
            kids[:2] = kids[1::-1]

        def repeat_a_key(objects: dict[int, Any], trailer: dict[str, Any]) -> None:
            leaf = objects[name_tree(objects, trailer)["Kids"][0].number]
            leaf["Names"][2:2] = leaf["Names"][:2]

        for change, expected in ((swap_leaves, "comes after"), (repeat_a_key, "twice")):
            with self.subTest(change=change.__name__):
                problems = check(rewrite(many, change))
                self.assertTrue(any(expected in p for p in problems), problems)

    def test_explicit_destinations_take_their_operands_and_a_top_on_the_page(
        self,
    ) -> None:
        def drop_zoom(objects: dict[int, Any], trailer: dict[str, Any]) -> None:
            name_tree(objects, trailer)["Names"][1].pop()

        def raise_top(objects: dict[int, Any], trailer: dict[str, Any]) -> None:
            name_tree(objects, trailer)["Names"][1][3] = 99999

        for change, expected in ((drop_zoom, "/XYZ takes 3 operands, not 2"),
                                 (raise_top, "top 99999 is off the page")):
            with self.subTest(change=change.__name__):
                problems = check(rewrite(small_pdf(), change))
                self.assertTrue(any(expected in p for p in problems), problems)

    def test_information_holds_text_strings_and_dates(self) -> None:
        def info(fields: dict[str, object]) -> Change:
            def change(objects: dict[int, Any], trailer: dict[str, Any]) -> None:
                objects[trailer["Info"].number].update(fields)
            return change

        def not_a_dictionary(objects: dict[int, Any], trailer: dict[str, Any]) -> None:
            objects[trailer["Info"].number] = [1, 2]

        valid = info({"CreationDate": b"D:20261102120000+01'00'", "ModDate": b"D:2026",
                      "Trapped": Name("False"), "Keywords": "café"})
        self.assertEqual(check(rewrite(small_pdf(), valid)), [])
        cases = (
            (info({"CreationDate": b"yesterday"}), "is not a date"),
            (info({"ModDate": b"D:20261302"}), "is not a date"),
            (info({"Title": b"\xfe\xff\x00"}), "is not well-formed UTF-16"),
            (info({"Title": b"a\x01b"}), "PDFDocEncoding does not define: 0x01"),
            (info({"Trapped": Name("Maybe")}), "is not true, false or unknown"),
            (info({"Pages": 3}), "/Info /Pages 3 is not a string"),
            (not_a_dictionary, "is not a dictionary"),
        )
        for change, expected in cases:
            with self.subTest(expected=expected):
                problems = check(rewrite(small_pdf(), change))
                self.assertTrue(any(expected in p for p in problems), problems)

    def test_links_and_bookmarks_lead_one_way_and_count_what_they_hold(self) -> None:
        def link_with_action(objects: dict[int, Any], trailer: dict[str, Any]) -> None:
            link = next(value for value in objects.values()
                        if isinstance(value, dict) and value.get("Subtype") == "Link")
            link["A"] = {"S": Name("GoTo"), "D": b"first"}

        def count_without_children(
            objects: dict[int, Any], trailer: dict[str, Any]
        ) -> None:
            item = next(value for value in objects.values() if isinstance(value, dict)
                        and "Title" in value and "First" not in value)
            item["Count"] = 3

        for change, expected in ((link_with_action, "has both /Dest and /A"),
                                 (count_without_children, "/Count 3, but 0")):
            with self.subTest(change=change.__name__):
                problems = check(rewrite(small_pdf(), change))
                self.assertTrue(any(expected in p for p in problems), problems)


class TestReadBack(unittest.TestCase):
    """The writer's output read back with the checker's `PdfFile`."""

    def test_identifier_is_a_hash_of_the_content(self) -> None:
        self.assertEqual(small_pdf(), small_pdf())
        same, other = PdfFile(small_pdf()), PdfFile(small_pdf(info={"Title": "Other"}))
        identifier = same.trailer["ID"]
        assert isinstance(identifier, list)
        self.assertEqual(identifier[0], identifier[1])
        self.assertEqual(len(identifier[0]), 16)
        self.assertNotEqual(same.trailer["ID"], other.trailer["ID"])

    def test_outline_counts_follow_open_and_closed_entries(self) -> None:
        pdf = PdfFile(small_pdf())
        self.assertEqual(pdf.problems, [])
        root = pdf.dictionary(pdf.catalog["Outlines"])
        self.assertEqual(root["Count"], 3)          # One, its open child, Again
        first = pdf.dictionary(root["First"])
        self.assertEqual(first["Count"], 1)
        titles = [(item.title, [child.title for child in item.children])
                  for item in pdf.outline()]
        self.assertEqual(titles, [("One", ["Two"]), ("Again", [])])

    def test_many_destinations_split_the_name_tree(self) -> None:
        names = {f"d{index:03d}": (0, 100.0) for index in range(150)}
        pdf = PdfFile(small_pdf(destinations={
            **names, "first": (0, 290.0), "second": (1, 290.0),
        }))
        self.assertEqual(pdf.problems, [])
        tree = pdf.dictionary(pdf.dictionary(pdf.catalog["Names"])["Dests"])
        self.assertEqual(len(tree["Kids"]), 3)       # type: ignore[arg-type]
        self.assertEqual(len(pdf.names), 152)


if __name__ == "__main__":
    unittest.main()
