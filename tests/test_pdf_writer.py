"""PDF writer: object syntax, and the numbering and assembly of a whole file."""

from __future__ import annotations

import re
import unittest
import zlib
from typing import Any

from devdocs.render.pdf.writer import (
    Bookmark,
    LinkArea,
    Name,
    Objects,
    Page,
    Ref,
    build,
    serialize,
)


class TestSyntax(unittest.TestCase):
    def test_numbers_are_fixed_point_and_trimmed(self) -> None:
        cases = {1.5: b"1.5", 2.0: b"2", 0.0001: b"0", -0.0: b"0", 7: b"7",
                 123456789.125: b"123456789.125", 1e20: b"100000000000000000000",
                 -12.3456: b"-12.346"}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(serialize(value), expected)
        for bad in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                serialize(bad)

    def test_strings_escape_backslash_parentheses_and_non_printables(self) -> None:
        self.assertEqual(serialize(b"a(b)c\\d\n\x01\xff"),
                         b"(a\\(b\\)c\\\\d\\n\\001\\377)")
        self.assertEqual(serialize("plain"), b"(plain)")
        self.assertEqual(serialize("caf\u00e9"),
                         b"(\\376\\377\\000c\\000a\\000f\\000\\351)")

    def test_text_strings_keep_control_characters_in_utf16(self) -> None:
        # PDFDocEncoding reads 0x09 as a tab, but 0x18 as a breve
        self.assertEqual(serialize("a\tb"), b"(\\376\\377\\000a\\000\\t\\000b)")
        self.assertEqual(serialize("a\x18"), b"(\\376\\377\\000a\\000\\030)")

    def test_names_escape_delimiters_and_spaces(self) -> None:
        self.assertEqual(serialize(Name("A B#(x)/")), b"/A#20B#23#28x#29#2F")

    def test_space_only_where_tokens_need_it(self) -> None:
        value = {"Type": Name("Page"), "Count": 3, "Kids": [Ref(4), Ref(5)],
                 "Open": True, "Rect": [0, 1.25, None]}
        self.assertEqual(
            serialize(value),
            b"<</Type/Page/Count 3/Kids[4 0 R 5 0 R]/Open true/Rect[0 1.25 null]>>",
        )

    def test_unknown_types_are_refused(self) -> None:
        with self.assertRaises(TypeError):
            serialize({"Set": {1, 2}})


def small_pdf(**overrides: Any) -> bytes:
    """Two pages, a link between them, two bookmarks and an info dictionary."""
    pages = [
        Page(200, 300, b"BT /F1 12 Tf 1 0 0 1 10 250 Tm (Page one) Tj ET",
             (LinkArea((10, 240, 80, 260), "second"),)),
        Page(200, 300, b"BT /F1 12 Tf 1 0 0 1 10 250 Tm (Page two) Tj ET"),
    ]
    arguments: dict[str, Any] = {
        "fonts": {"F1": "Helvetica"},
        "destinations": {"first": (0, 290.0), "second": (1, 290.0)},
        "outline": [Bookmark("One", "first", (Bookmark("Two", "second"),), open=True),
                    Bookmark("Again", "second")],
        "info": {"Title": "Small", "Creator": "test"},
    }
    arguments.update(overrides)
    return build(pages, **arguments)


class TestObjects(unittest.TestCase):
    def test_objects_may_refer_to_each_other_through_reserved_numbers(self) -> None:
        objects = Objects()
        first = objects.reserve()
        second = objects.add({"Other": first})
        objects.define(first, {"Other": second})
        data = objects.serialize(root=first)
        self.assertIn(b"1 0 obj\n<</Other 2 0 R>>", data)
        self.assertIn(b"2 0 obj\n<</Other 1 0 R>>", data)

    def test_a_reservation_never_defined_is_refused(self) -> None:
        objects = Objects()
        root = objects.add({"Type": Name("Catalog")})
        objects.reserve()
        with self.assertRaisesRegex(ValueError, "never defined"):
            objects.serialize(root=root)

    def test_a_reference_to_no_object_is_refused(self) -> None:
        objects = Objects()
        root = objects.add({"Next": Ref(9)})
        with self.assertRaisesRegex(ValueError, "missing object 9"):
            objects.serialize(root=root)
        with self.assertRaises(ValueError):
            objects.define(Ref(5), {})
        with self.assertRaises(ValueError):
            objects.define(root, {})

    def test_xref_entries_are_20_bytes_and_land_on_their_objects(self) -> None:
        data = small_pdf()
        self.assertTrue(data.startswith(b"%PDF-1.7\n%"))
        self.assertGreaterEqual(sum(b >= 128 for b in data.split(b"\n")[1]), 4)
        start = int(re.search(rb"startxref\n(\d+)\n%%EOF\n$", data).group(1))  # type: ignore[union-attr]
        self.assertTrue(data.startswith(b"xref\n0 ", start))
        count = int(data[start:].split(b"\n")[1].split()[1])
        table = data.index(b"\n", data.index(b"\n", start) + 1) + 1
        for number in range(count):
            entry = data[table + 20 * number:table + 20 * (number + 1)]
            self.assertRegex(entry, rb"^\d{10} \d{5} [nf]\r\n$")
            if number:
                offset = int(entry[:10])
                self.assertTrue(data.startswith(b"%d 0 obj" % number, offset), number)
        trailer = rb"trailer\n<</Size %d/Root 1 0 R/Info \d+ 0 R/ID\[" % count
        self.assertRegex(data, trailer)

    def test_streams_are_compressed_with_their_exact_length(self) -> None:
        data = small_pdf()
        streams = rb"/Length (\d+)/Filter/FlateDecode>>\nstream\n"
        for match in re.finditer(streams, data):
            body = data[match.end():match.end() + int(match.group(1))]
            self.assertTrue(data.startswith(b"\nendstream", match.end() + len(body)))
            self.assertIn(b"Tj", zlib.decompress(body))

    def test_links_and_bookmarks_must_name_a_destination(self) -> None:
        with self.assertRaisesRegex(ValueError, "undefined destination 'second'"):
            small_pdf(destinations={"first": (0, 290.0)})
        with self.assertRaisesRegex(ValueError, "bookmark"):
            small_pdf(outline=[Bookmark("Nowhere", "nowhere")])


if __name__ == "__main__":
    unittest.main()
