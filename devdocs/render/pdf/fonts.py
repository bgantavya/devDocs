"""Metrics and encoding for the standard PDF fonts the built-in engine uses.

The fourteen standard fonts are present in every conforming PDF reader, so a
file that uses them embeds no font program at all. Six cover everything here:
Helvetica in four styles for text, Courier in two for code. The reader, not
the writer, draws the glyphs, so lines must be broken on the reader's own
advance widths; a guess would overflow margins or leave ragged gaps.

The widths are Adobe's, in thousandths of an em, for every WinAnsiEncoding
code. Helvetica-Oblique and Helvetica-BoldOblique share the widths of their
upright faces and Courier is 600 for every glyph, so two tables suffice. They
were generated once, at development time, from the Adobe Core14 AFM files
Helvetica.afm and Helvetica-Bold.afm (version 002.000, 1997) that matplotlib
3.11.2 bundles in mpl-data/fonts/pdfcorefonts. Each code from 32 to 255 was
decoded as cp1252 and named through the Adobe Glyph List, except that
WinAnsi calls 0xA0 "space" and 0xAD "hyphen", and the AFMs call 0xB2, 0xB3
and 0xB9 "twosuperior", "threesuperior" and "onesuperior". Codes WinAnsi
leaves unused show a bullet and take its width. Each face's 224 widths were
packed as big-endian unsigned 16-bit integers, compressed with zlib at level
9 and base85-encoded. The older phvr8a.afm and phvb8a.afm agree on every
glyph they share but lack the Euro, which WinAnsi needs.

WinAnsiEncoding is Windows code page 1252 as PDF readers draw it, with the
five codes cp1252 leaves undefined unused. A character outside it cannot be
shown with these fonts at all: `encode` writes "?" in its place and returns
it, so the renderer can report it by name instead of losing it silently.
"""

from __future__ import annotations

import base64
import struct
import unicodedata
import zlib
from dataclasses import dataclass

__all__ = [
    "BOLD",
    "BOLD_ITALIC",
    "CODE",
    "CODE_BOLD",
    "FONTS",
    "ITALIC",
    "REGULAR",
    "Font",
    "describe",
    "encode",
]

_HELVETICA = (
    "c-n1Hu?@m75cI!OWq?TR;3c4PV@61mnuaa|(9?T~Y`_kdXf4q(fF)Y)oP;2RPWin5&j0?CPC6OXs"
    "k5U-bCUpE)T0{kb!2`~>%PL-mqs7~Q3u;<0$Y$fyVrzsWQ`o7ovM0Z2jGrgp>`~DUq+KnmSQsQuk"
    "(Twa!@5?#8DbK;*~wBy~)(r=d8s)GJ+eVCyRdE3R+aj6vPP}-pM^bbDH{|hn#WpH4AcOMxeH<t^_"
    "%u(}BQNaRVl&HD5OD=!|VwsIP9M#V@+@<*OZMA6Ae5`0uwH)Jk~+si9bT"
)
_HELVETICA_BOLD = (
    "c-mc1yA{GP3`9zo0sJyVmVnAlR!HNThK>Q!(|d_*!h0_1=%}bN0C#5+KJQ+flTJQMvS5SVy!XC2d"
    ")Yk^4K{f+=7R+)|6q+rAvISoZ)8NR7u_k~doPGHbJTb?GZ?tM=>acPr}}{>XnNHG`bib&r)oXC`2"
    "J#(b?S0NG^&1S9<?|8Gf!v<+DJd#SXhfJbym$#7E}gxQ0MCO&fQLkqS#;;GEK}5gZL3n2SQVAfOF"
    "A&S-yhu?Fx_D#=5Vp$v0r)&yLso=?C$PYiBPdrB+G"
)
_FIRST = 32   # first code with a glyph
_UNNAMED = {
    "Cc": "<control>", "Co": "<private use>", "Cs": "<surrogate>",
    "Cn": "<unassigned>",
}


def _table(packed: str) -> tuple[int, ...]:
    """Advance widths for all 256 codes; the control codes below 32 have none."""
    widths = struct.unpack(">224H", zlib.decompress(base64.b85decode(packed)))
    return (0,) * _FIRST + widths


@dataclass(frozen=True, eq=False)
class Font:
    """One standard font as pages use it: resource name, base font, metrics."""

    resource: str               # name in the page resources, e.g. "F1"
    base: str                   # PostScript name of the standard font
    widths: tuple[int, ...]     # advance per WinAnsi code, in 1/1000 em
    ascent: float               # above the baseline, in em
    descent: float              # below the baseline, in em
    monospaced: bool = False

    def measure(self, data: bytes, size: float) -> float:
        """The advance width of WinAnsi `data` at `size` points."""
        if self.monospaced:
            return len(data) * 0.6 * size
        return sum(map(self.widths.__getitem__, data)) * size / 1000


_SANS = _table(_HELVETICA)
_SANS_BOLD = _table(_HELVETICA_BOLD)
_MONO = (0,) * _FIRST + (600,) * (256 - _FIRST)

REGULAR = Font("F1", "Helvetica", _SANS, 0.718, 0.207)
BOLD = Font("F2", "Helvetica-Bold", _SANS_BOLD, 0.718, 0.207)
ITALIC = Font("F3", "Helvetica-Oblique", _SANS, 0.718, 0.207)
BOLD_ITALIC = Font("F4", "Helvetica-BoldOblique", _SANS_BOLD, 0.718, 0.207)
CODE = Font("F5", "Courier", _MONO, 0.629, 0.157, monospaced=True)
CODE_BOLD = Font("F6", "Courier-Bold", _MONO, 0.629, 0.157, monospaced=True)
FONTS = (REGULAR, BOLD, ITALIC, BOLD_ITALIC, CODE, CODE_BOLD)


def _codes() -> dict[str, int]:
    codes = {chr(code): code for code in range(_FIRST, 0x7F)}
    for code in range(0x80, 0x100):
        try:
            codes[bytes([code]).decode("cp1252")] = code
        except UnicodeDecodeError:   # one of cp1252's five holes
            continue
    return codes


_CODES = _codes()


def encode(text: str) -> tuple[bytes, str]:
    """`text` as WinAnsi, one byte per character, and the characters that have
    no code there, in order of appearance; each of those is written as "?".

    Control characters count as missing: callers turn the whitespace they
    mean to keep into spaces or line breaks first.
    """
    if text.isascii() and text.isprintable():
        return text.encode("ascii"), ""
    data = bytearray()
    missing: list[str] = []
    for char in text:
        code = _CODES.get(char)
        if code is None:
            missing.append(char)
            code = 0x3F
        data.append(code)
    return bytes(data), "".join(missing)


def describe(char: str) -> str:
    """A character's code point and Unicode name, e.g.
    `U+4E2D CJK UNIFIED IDEOGRAPH-4E2D`."""
    name = unicodedata.name(char, "") or _UNNAMED.get(
        unicodedata.category(char), "<unnamed>"
    )
    return f"U+{ord(char):04X} {name}"
