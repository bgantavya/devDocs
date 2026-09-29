"""PDF fonts: the standard fonts' metrics and their WinAnsi encoding."""

from __future__ import annotations

import unittest

from devdocs.render.pdf.fonts import BOLD, CODE, ITALIC, REGULAR, describe, encode


class TestFonts(unittest.TestCase):
    def test_known_adobe_widths(self) -> None:
        widths = REGULAR.widths
        self.assertEqual([widths[ord(c)] for c in " aWi"], [278, 556, 944, 222])
        self.assertEqual([BOLD.widths[ord(c)] for c in "ai"], [556, 278])
        self.assertEqual(widths[0x80], 556)                     # Euro
        self.assertEqual(widths[0xA0], 278)                     # no-break space
        self.assertEqual(ITALIC.widths, REGULAR.widths)
        self.assertEqual(set(CODE.widths[32:]), {600})

    def test_measure(self) -> None:
        self.assertAlmostEqual(REGULAR.measure(b"Wai", 10), 17.22)
        self.assertAlmostEqual(CODE.measure(b"abcd", 10), 24.0)

    def test_winansi_encoding(self) -> None:
        data, missing = encode("caf\u00e9 \u2014 \u201cq\u201d \u2022 10\u20ac\u00a0")
        self.assertEqual(data, b"caf\xe9 \x97 \x93q\x94 \x95 10\x80\xa0")
        self.assertEqual(missing, "")

    def test_characters_outside_winansi_become_question_marks(self) -> None:
        self.assertEqual(encode("a\u4e2db\u2192\x81\x07"),
                         (b"a?b???", "\u4e2d\u2192\x81\x07"))

    def test_characters_are_described_by_code_point_and_name(self) -> None:
        self.assertEqual(describe("\u4e2d"), "U+4E2D CJK UNIFIED IDEOGRAPH-4E2D")
        self.assertEqual(describe("\x07"), "U+0007 <control>")
        self.assertEqual(describe("\U0001f600"), "U+1F600 GRINNING FACE")


if __name__ == "__main__":
    unittest.main()
