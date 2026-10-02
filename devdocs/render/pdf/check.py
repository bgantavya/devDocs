"""A strict check of PDF files, run by the test suite.

Readers are forgiving: Preview, `file` and most viewers open a PDF whose
cross-reference table points at the wrong object and repair it silently, so
"it opens" proves little. `check` re-parses a file from its bytes as a strict
reader would, sharing no code with the writer, and lists every problem it
finds.

Structure: the header, a PDF version from 1.0 to 2.0, and its binary
comment; startxref landing on the table; every in-use entry exactly 20
bytes, its offset on "N G obj" for its own number; the trailer's /Size,
/Root, /Info and /ID; every reference resolving; every stream's /Length
exact and every FlateDecode stream decompressing; the page tree's /Count
against its leaves and every kid's /Parent pointing back; the outline's
/First, /Last, /Next, /Prev, /Parent and /Count agreeing, an item without
children carrying no /Count but zero.

Navigation: every link and bookmark leads somewhere that exists, by /Dest or
by an action, never both; an explicit destination has the operands its type
takes and a top on its page; the name tree's keys ascend strictly across
all its leaves, so none is listed twice, and every node's /Limits span the
keys below it, as a reader that searches by them needs.

Text: the document information holds text strings, dates of the form
`D:YYYYMMDDHHmmSSOHH'mm` and /Trapped; a text string there, in a bookmark or
in a file specification's /UF, is PDFDocEncoding without an undefined code,
or well-formed UTF-16.

Pages: each content stream is read token by token. Every operator must be
known, take the operands it is given, and appear where the graphics-object
state allows it (ISO 32000-1, figure 9): text operators only between BT and
ET, which do not nest; a path painted before anything else is drawn; q and
Q balanced, like marked content. Every font, graphics state, image or colour
space an operator names must be in the page's resources; text is shown only
once a font is set, and never with a code below 32, which no simple-font
encoding defines. A font must be one of the fourteen standard Type1 fonts,
or carry its own metrics, and use a predefined encoding.

It reads what the built-in writer produces, and reports the rest as
unsupported rather than half-read: one classic cross-reference table and
trailer, not incremental updates or cross-reference and object streams;
Type1 fonts; no inline images. Malformed input is reported, never raised,
including nesting and numbers made to exhaust a parser. `PdfFile` keeps what
was parsed, with helpers to walk pages, destinations and the outline, so
tests can assert on where things really landed.
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import TypeGuard

__all__ = ["Malformed", "Name", "Outline", "PdfFile", "Ref", "Stream", "check"]

_WHITESPACE = b"\x00\t\n\x0c\r "
_DELIMITERS = b"()<>[]{}/%"
_HEADER = re.compile(rb"%PDF-(\d)\.(\d)(?:\r\n|\r|\n)%([^\r\n]*)(?:\r\n|\r|\n)")
_ENTRY = re.compile(rb"(\d{10}) (\d{5}) ([nf])(?:\r\n| \r| \n)")
# Digit runs are bounded: int() refuses more than 4300 digits by raising.
_STARTXREF = re.compile(rb"startxref\s+(\d{1,10})\s+%%EOF")
_SUBSECTION = re.compile(rb"(\d{1,10}) (\d{1,10})[ \t]*(?:\r\n|\r|\n)")
_OBJECT = re.compile(rb"(\d{1,10})\s+(\d{1,5})\s+obj(?![^\s()<>\[\]{}/%])")
_NUMBER = re.compile(rb"[+-]?(?:\d{1,20}(?:\.\d*)?|\.\d+)")
_REF_TAIL = re.compile(rb"[\s]+(\d{1,5})[\s]+R(?![^\s()<>\[\]{}/%])")
_WORD = re.compile(rb"[^\s()<>\[\]{}/%]+")
_NAME_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
_ESCAPES = {
    ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b",
    ord("f"): b"\f", ord("("): b"(", ord(")"): b")", ord("\\"): b"\\",
}
_NESTING = 64           # arrays and dictionaries nested deeper are refused
_KEYWORDS: dict[bytes, object] = {b"true": True, b"false": False, b"null": None}

# A content-stream token the fast path reads; strings with escapes, hex
# strings, arrays and dictionaries fall through to `_Parser`.
_TOKEN = re.compile(
    rb"(?:[\x00\t\n\x0c\r ]|%[^\r\n]*)*"
    rb"(?:(?P<number>[+-]?(?:\d{1,20}(?:\.\d*)?|\.\d+))"
    rb"(?![^\x00\t\n\x0c\r ()<>\[\]{}/%])"
    rb"|(?P<name>/[^\x00\t\n\x0c\r ()<>\[\]{}/%]*)"
    rb"|(?P<string>\([^()\\]*\))"
    rb"|(?P<word>[^\x00\t\n\x0c\r ()<>\[\]{}/%]+)"
    rb"|(?P<end>\Z))"
)

# Operator: its operands, the states it may appear in, the state it leaves.
# Operands, one code each: n number, i integer, N name, s string, a array of
# numbers, t array of strings and numbers, p dictionary or name; "+" is one
# or more numbers, "*" numbers, a final name or both. States (ISO 32000-1,
# figure 9): P page level, A a path being built, C a path clipped and not
# yet painted, T a text object; "" leaves the state as it was.
_OPERATORS: dict[bytes, tuple[str, str, str]] = {
    **dict.fromkeys((b"w", b"M", b"i"), ("n", "PT", "")),
    b"J": ("i", "PT", ""), b"j": ("i", "PT", ""), b"d": ("an", "PT", ""),
    b"ri": ("N", "PT", ""), b"gs": ("N", "PT", ""),
    b"q": ("", "P", ""), b"Q": ("", "P", ""), b"cm": ("nnnnnn", "P", ""),
    b"m": ("nn", "PA", "A"), b"re": ("nnnn", "PA", "A"),
    b"l": ("nn", "A", ""), b"c": ("nnnnnn", "A", ""), b"v": ("nnnn", "A", ""),
    b"y": ("nnnn", "A", ""), b"h": ("", "A", ""),
    b"W": ("", "A", "C"), b"W*": ("", "A", "C"),
    **dict.fromkeys(
        (b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"n"), ("", "AC", "P")
    ),
    b"BT": ("", "P", "T"), b"ET": ("", "T", "P"),
    **dict.fromkeys((b"Tc", b"Tw", b"Tz", b"TL", b"Ts"), ("n", "PT", "")),
    b"Tf": ("Nn", "PT", ""), b"Tr": ("i", "PT", ""),
    b"Td": ("nn", "T", ""), b"TD": ("nn", "T", ""), b"Tm": ("nnnnnn", "T", ""),
    b"T*": ("", "T", ""),
    b"Tj": ("s", "T", ""), b"'": ("s", "T", ""), b'"': ("nns", "T", ""),
    b"TJ": ("t", "T", ""),
    b"CS": ("N", "PT", ""), b"cs": ("N", "PT", ""), b"SC": ("+", "PT", ""),
    b"sc": ("+", "PT", ""), b"SCN": ("*", "PT", ""), b"scn": ("*", "PT", ""),
    b"G": ("n", "PT", ""), b"g": ("n", "PT", ""), b"RG": ("nnn", "PT", ""),
    b"rg": ("nnn", "PT", ""), b"K": ("nnnn", "PT", ""), b"k": ("nnnn", "PT", ""),
    b"sh": ("N", "P", ""), b"Do": ("N", "P", ""),
    b"MP": ("N", "PT", ""), b"DP": ("Np", "PT", ""), b"BMC": ("N", "PT", ""),
    b"BDC": ("Np", "PT", ""), b"EMC": ("", "PT", ""),
    b"BX": ("", "PACT", ""), b"EX": ("", "PACT", ""),
    b"d0": ("nn", "", ""), b"d1": ("nnnnnn", "", ""),   # Type 3 glyphs only
}
_STATES = {
    "P": "at page level", "A": "while a path is built",
    "C": "between a clip and its painting", "T": "inside a text object",
}
_KINDS = {
    "n": "a number", "i": "an integer", "N": "a name", "s": "a string",
    "a": "an array of numbers", "t": "an array of strings and numbers",
    "p": "a dictionary or a name",
}
_SHOWS = frozenset((b"Tj", b"'", b'"', b"TJ"))
_RESOURCES = {
    b"Tf": "Font", b"gs": "ExtGState", b"Do": "XObject", b"sh": "Shading",
    b"CS": "ColorSpace", b"cs": "ColorSpace", b"DP": "Properties",
    b"BDC": "Properties",
}
_CATEGORIES = tuple(sorted(set(_RESOURCES.values())))
_DEVICE_SPACES = frozenset(("DeviceGray", "DeviceRGB", "DeviceCMYK", "Pattern"))
_STANDARD_FONTS = frozenset((
    "Courier", "Courier-Bold", "Courier-BoldOblique", "Courier-Oblique",
    "Helvetica", "Helvetica-Bold", "Helvetica-BoldOblique", "Helvetica-Oblique",
    "Symbol", "Times-Bold", "Times-BoldItalic", "Times-Italic", "Times-Roman",
    "ZapfDingbats",
))
_ENCODINGS = frozenset(("MacExpertEncoding", "MacRomanEncoding", "WinAnsiEncoding"))
_METRICS = ("FirstChar", "LastChar", "Widths", "FontDescriptor")
# Explicit destinations: the operands after the page and the type, and
# which of them, if any, is the top of the view.
_FITS = {"XYZ": 3, "Fit": 0, "FitH": 1, "FitV": 1, "FitR": 4, "FitB": 0,
         "FitBH": 1, "FitBV": 1}
_TOPS = {"XYZ": 1, "FitH": 0, "FitBH": 0}
# Codes PDFDocEncoding leaves undefined (ISO 32000-1, table D.2).
_UNDEFINED = frozenset((*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x18),
                        0x7F, 0x9F, 0xAD))
# D:YYYYMMDDHHmmSSOHH'mm', each field only after all those before it.
_DATE = re.compile(
    rb"D:\d{4}"
    rb"(?:(?:0[1-9]|1[0-2])"                                     # month
    rb"(?:(?:0[1-9]|[12]\d|3[01])"                               # day
    rb"(?:(?:[01]\d|2[0-3])"                                     # hour
    rb"(?:[0-5]\d"                                               # minute
    rb"(?:[0-5]\d"                                               # second
    rb"(?:[Z+-](?:(?:[01]\d|2[0-3])(?:'(?:[0-5]\d'?)?)?)?)?"     # offset
    rb")?)?)?)?)?"
)


class Name(str):
    """A PDF name, without its slash."""

    __slots__ = ()


@dataclass(frozen=True)
class Ref:
    number: int
    generation: int = 0

    def __str__(self) -> str:
        return f"{self.number} {self.generation} R"


@dataclass
class Stream:
    entries: dict[str, object]
    raw: bytes                      # as stored, before any filter


@dataclass
class Outline:
    """A bookmark as read back: its title, destination and children."""

    title: str
    destination: object
    children: list[Outline] = field(default_factory=list)


class Malformed(Exception):
    """Bytes that do not parse as PDF syntax."""


def check(data: bytes) -> list[str]:
    """Every problem in `data`; an empty list means a valid file."""
    return PdfFile(data).problems


# -- syntax --------------------------------------------------------------------


class _Parser:
    """PDF objects read from `data`, starting at `position`. With
    `references` off, as in a content stream, `1 0 R` is not a reference."""

    def __init__(
        self, data: bytes, position: int = 0, *, references: bool = True
    ) -> None:
        self.data = data
        self.position = position
        self.references = references
        self.depth = 0

    def skip(self) -> None:
        data, position = self.data, self.position
        while position < len(data):
            if data[position] in _WHITESPACE:
                position += 1
            elif data[position] == 0x25:            # % comment to end of line
                while position < len(data) and data[position] not in b"\r\n":
                    position += 1
            else:
                break
        self.position = position

    def at(self, keyword: bytes) -> bool:
        self.skip()
        end = self.position + len(keyword)
        return self.data.startswith(keyword, self.position) and (
            end >= len(self.data) or self.data[end] in _WHITESPACE + _DELIMITERS
        )

    def value(self) -> object:
        self.skip()
        data, position = self.data, self.position
        if position >= len(data):
            raise Malformed("unexpected end of file")
        if data.startswith(b"<<", position):
            return self.dictionary()
        first = data[position]
        if first == 0x3C:
            return self.hex_string()
        if first == 0x28:
            return self.literal_string()
        if first == 0x5B:
            return self.array()
        if first == 0x2F:
            return self.name()
        number = _NUMBER.match(data, position)
        if number is not None:
            self.position = number.end()
            text = number.group()
            if b"." in text:
                return float(text)
            reference = _REF_TAIL.match(data, self.position)
            if reference is not None and self.references and text.isdigit():
                self.position = reference.end()
                return Ref(int(text), int(reference.group(1)))
            return int(text)
        word = _WORD.match(data, position)
        if word is not None and word.group() in _KEYWORDS:
            self.position = word.end()
            return _KEYWORDS[word.group()]
        found = data[position:position + 16]
        raise Malformed(f"unexpected {found!r} at byte {position}")

    def enter(self) -> None:
        self.depth += 1
        if self.depth > _NESTING:
            raise Malformed(f"arrays or dictionaries nested deeper than {_NESTING} "
                            f"levels at byte {self.position}")

    def dictionary(self) -> dict[str, object]:
        self.enter()
        self.position += 2
        entries: dict[str, object] = {}
        while True:
            self.skip()
            if self.data.startswith(b">>", self.position):
                self.position += 2
                self.depth -= 1
                return entries
            key = self.value()
            if not isinstance(key, Name):
                raise Malformed(f"dictionary key {key!r} is not a name")
            entries[str(key)] = self.value()

    def array(self) -> list[object]:
        self.enter()
        self.position += 1
        items: list[object] = []
        while True:
            self.skip()
            if self.data.startswith(b"]", self.position):
                self.position += 1
                self.depth -= 1
                return items
            items.append(self.value())

    def name(self) -> Name:
        word = _WORD.match(self.data, self.position + 1)
        raw = word.group() if word is not None else b""
        self.position += 1 + len(raw)
        return _name(raw)

    def hex_string(self) -> bytes:
        end = self.data.find(b">", self.position)
        if end < 0:
            raise Malformed(f"unterminated hex string at byte {self.position}")
        digits = re.sub(rb"\s", b"", self.data[self.position + 1:end])
        if not re.fullmatch(rb"[0-9A-Fa-f]*", digits):
            raise Malformed(f"bad hex string at byte {self.position}")
        self.position = end + 1
        return bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode("ascii"))

    def literal_string(self) -> bytes:
        data, position = self.data, self.position + 1
        out = bytearray()
        depth = 1
        while position < len(data):
            char = data[position]
            if char == 0x5C:
                position += 1
                following = data[position] if position < len(data) else -1
                octal = re.match(rb"[0-7]{1,3}", data[position:position + 3])
                if following in _ESCAPES:
                    out += _ESCAPES[following]
                    position += 1
                elif octal is not None:
                    out.append(int(octal.group(), 8) & 0xFF)
                    position += len(octal.group())
                elif following in (0x0D, 0x0A):    # line continuation
                    position += 2 if data[position:position + 2] == b"\r\n" else 1
                elif following >= 0:
                    out.append(following)
                    position += 1
                continue
            if char == 0x28:
                depth += 1
            elif char == 0x29:
                depth -= 1
                if depth == 0:
                    self.position = position + 1
                    return bytes(out)
            out.append(char)
            position += 1
        raise Malformed(f"unterminated string at byte {self.position}")


def _name(raw: bytes) -> Name:
    """A name from its bytes after the slash, #xx escapes decoded."""
    if b"#" in raw:
        raw = _NAME_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), raw)
    return Name(raw.decode("latin-1"))


# -- the file ------------------------------------------------------------------


class PdfFile:
    """A PDF read strictly from its bytes, with every problem found."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.problems: list[str] = []
        self.objects: dict[int, object] = {}
        self.trailer: dict[str, object] = {}
        self.page_refs: list[Ref] = []
        self.names: dict[bytes, object] = {}
        self._indexes: dict[Ref, int] = {}  # page_refs, inverted
        self._fonts: set[int] = set()       # font objects already checked
        try:
            self._read()
        except Malformed as err:
            self.problems.append(str(err))
        except RecursionError:              # a page tree or outline built deep
            self.problems.append("objects refer to each other too deeply to check")

    def problem(self, message: str) -> None:
        self.problems.append(message)

    # -- navigation --------------------------------------------------------

    def get(self, value: object) -> object:
        """`value`, or the object it refers to; None for a missing object."""
        if isinstance(value, Ref):
            return self.objects.get(value.number)
        return value

    def dictionary(self, value: object) -> dict[str, object]:
        resolved = self.get(value)
        if isinstance(resolved, Stream):
            return resolved.entries
        return resolved if isinstance(resolved, dict) else {}

    @property
    def catalog(self) -> dict[str, object]:
        return self.dictionary(self.trailer.get("Root"))

    def page(self, index: int) -> dict[str, object]:
        return self.dictionary(self.page_refs[index])

    def content(self, index: int) -> bytes:
        """Page `index`'s content stream, decoded."""
        streams = self.get(self.page(index).get("Contents"))
        parts = streams if isinstance(streams, list) else [streams]
        return b"\n".join(self.decoded(part) for part in parts)

    def decoded(self, value: object) -> bytes:
        stream = self.get(value)
        if not isinstance(stream, Stream):
            return b""
        if stream.entries.get("Filter") == "FlateDecode":
            return zlib.decompress(stream.raw)
        return stream.raw

    def annotations(self, index: int) -> list[dict[str, object]]:
        annots = self.get(self.page(index).get("Annots"))
        return [self.dictionary(a) for a in annots] if isinstance(annots, list) else []

    def destination(self, value: object) -> tuple[int, float] | None:
        """(page index, top) of a named or explicit destination, if it exists."""
        target = self._target(value)
        if target is None:
            return None
        page = target[0]
        index = self._indexes.get(page) if isinstance(page, Ref) else None
        if index is None:
            return None
        top = target[3] if len(target) > 3 else None
        return index, float(top) if _number(top) else 0.0

    def _target(self, value: object) -> list[object] | None:
        """The explicit destination `value` is or names, if there is one."""
        target = self.get(value)
        if isinstance(target, bytes | Name):
            key = target if isinstance(target, bytes) else str(target).encode("latin-1")
            target = self.get(self.names.get(key))
        if isinstance(target, dict):
            target = self.get(target.get("D"))
        return target if isinstance(target, list) and target else None

    def outline(self) -> list[Outline]:
        root = self.dictionary(self.catalog.get("Outlines"))
        return list(self._children(root))

    def _children(self, parent: dict[str, object]) -> Iterator[Outline]:
        seen: set[int] = set()
        ref = parent.get("First")
        while isinstance(ref, Ref) and ref.number not in seen:
            seen.add(ref.number)
            item = self.dictionary(ref)
            title = item.get("Title")
            yield Outline(
                _text(title) if isinstance(title, bytes) else "",
                item.get("Dest", self.dictionary(item.get("A")).get("D")),
                list(self._children(item)),
            )
            ref = item.get("Next")

    # -- reading -----------------------------------------------------------

    def _read(self) -> None:
        entries = self._cross_references()
        if not entries:
            return
        for number, (offset, generation) in sorted(entries.items()):
            self._object(number, offset, generation)
        self._references(entries)
        self._info()
        root = self.trailer.get("Root")
        catalog = self.dictionary(root)
        if catalog.get("Type") != "Catalog":
            self.problem(f"trailer /Root {_show(root)} is not a /Catalog dictionary")
            return
        self._pages(catalog.get("Pages"))
        self._named(catalog)
        self._outlines(catalog.get("Outlines"))
        self._links()

    def _cross_references(self) -> dict[int, tuple[int, int]]:
        data = self.data
        header = _HEADER.match(data)
        if header is None:
            self.problem("missing %PDF-x.y header line and binary comment line")
        else:
            version = (int(header.group(1)), int(header.group(2)))
            if not ((1, 0) <= version <= (1, 7) or version == (2, 0)):
                self.problem(f"header names PDF {version[0]}.{version[1]}, which is "
                             "not a PDF version")
            if sum(byte >= 128 for byte in header.group(3)) < 4:
                self.problem("second line is not a binary comment of four bytes >= 128")
        tail = data[-1024:]
        if not tail.rstrip(b"\r\n").endswith(b"%%EOF"):
            self.problem("file does not end with %%EOF")
        marker = data.rfind(b"startxref")
        found = _STARTXREF.match(data, marker) if marker >= 0 else None
        if found is None:
            self.problem("no startxref offset before %%EOF")
            return {}
        start = int(found.group(1))
        if not data.startswith(b"xref", start):
            self.problem(f"startxref {start} does not point at an xref table")
            return {}
        position = start + 4
        position += 2 if data.startswith(b"\r\n", position) else 1
        entries: dict[int, tuple[int, int]] = {}
        while (section := _SUBSECTION.match(data, position)) is not None:
            first, count = int(section.group(1)), int(section.group(2))
            position = section.end()
            for number in range(first, first + count):
                entry = _ENTRY.fullmatch(data, position, position + 20)
                if entry is None:
                    self.problem(f"xref entry for object {number} at byte {position} "
                                 "is not a 20-byte entry")
                    return {}
                position += 20
                offset, generation = int(entry.group(1)), int(entry.group(2))
                if entry.group(3) == b"n":
                    entries[number] = (offset, generation)
                elif number == 0 and generation != 65535:
                    self.problem("xref entry 0 is not free with generation 65535")
        if 0 in entries:
            self.problem("xref entry 0 is in use")
        parser = _Parser(data, position)
        if not parser.at(b"trailer"):
            self.problem(f"no trailer after the xref table, at byte {position}")
            return {}
        parser.position += len(b"trailer")
        trailer = parser.value()
        if not isinstance(trailer, dict):
            self.problem("trailer is not a dictionary")
            return {}
        self.trailer = trailer
        self._trailer(entries)
        return entries

    def _trailer(self, entries: dict[int, tuple[int, int]]) -> None:
        trailer = self.trailer
        size = trailer.get("Size")
        expected = max(entries, default=0) + 1
        if size != expected:
            self.problem(f"trailer /Size is {size!r}, but the highest object is "
                         f"{expected - 1}")
        if "Prev" in trailer or "XRefStm" in trailer:
            self.problem("incremental updates and cross-reference streams "
                         "are not supported")
        if not isinstance(trailer.get("Root"), Ref):
            self.problem("trailer has no /Root reference")
        if "Info" in trailer and not isinstance(trailer["Info"], Ref):
            self.problem("trailer /Info is not a reference")
        identifier = trailer.get("ID")
        if identifier is not None and not (
            isinstance(identifier, list) and len(identifier) == 2
            and all(isinstance(part, bytes) for part in identifier)
        ):
            self.problem("trailer /ID is not an array of two strings")

    def _object(self, number: int, offset: int, generation: int) -> None:
        data = self.data
        header = _OBJECT.match(data, offset)
        if header is None or (int(header.group(1)), int(header.group(2))) != (
            number, generation
        ):
            found = data[offset:offset + 16].split(b"\n")[0]
            self.problem(f"xref entry for object {number} points at byte {offset}, "
                         f"which holds {found!r}, not '{number} {generation} obj'")
            return
        parser = _Parser(data, header.end())
        try:
            value = parser.value()
            if parser.at(b"stream"):
                value = self._stream(number, value, parser)
            if not parser.at(b"endobj"):
                self.problem(f"object {number} does not end with endobj")
        except Malformed as err:
            self.problem(f"object {number}: {err}")
            return
        self.objects[number] = value

    def _stream(self, number: int, value: object, parser: _Parser) -> Stream:
        data = self.data
        if not isinstance(value, dict):
            raise Malformed("stream without a dictionary")
        start = parser.position + len(b"stream")
        if data.startswith(b"\r\n", start):
            start += 2
        elif data.startswith(b"\n", start):
            start += 1
        else:
            raise Malformed("stream keyword not followed by an end of line")
        length = value.get("Length")
        if isinstance(length, Ref):
            length = _referenced_length(data, length)
        if not isinstance(length, int) or length < 0:
            raise Malformed(f"stream /Length {length!r} is not a length")
        end = start + length
        after = end + (2 if data.startswith(b"\r\n", end) else
                       1 if data[end:end + 1] in (b"\n", b"\r") else 0)
        if after == end or not data.startswith(b"endstream", after):
            actual = data.find(b"endstream", start)
            raise Malformed(f"stream /Length {length} does not end at an end of "
                            f"line and endstream (the data and its end of line run "
                            f"{max(actual - start, 0)} bytes)")
        raw = data[start:end]
        if value.get("Filter") == "FlateDecode":
            inflater = zlib.decompressobj()
            try:
                inflater.decompress(raw)
            except zlib.error as err:
                self.problem(f"object {number}: FlateDecode stream does not "
                             f"decompress: {err}")
            else:
                if not inflater.eof or inflater.unused_data:
                    self.problem(f"object {number}: FlateDecode stream is truncated "
                                 "or has trailing data")
        elif "Filter" in value:
            self.problem(f"object {number}: unexpected filter {value['Filter']!r}")
        parser.position = after + len(b"endstream")
        return Stream(value, raw)

    def _references(self, entries: dict[int, tuple[int, int]]) -> None:
        def visit(value: object, owner: str) -> None:
            if isinstance(value, Ref):
                if entries.get(value.number, (0, -1))[1] != value.generation:
                    self.problem(f"{owner} refers to {value}, which is not in the "
                                 "cross-reference table")
            elif isinstance(value, list):
                for item in value:
                    visit(item, owner)
            elif isinstance(value, dict):
                for item in value.values():
                    visit(item, owner)
            elif isinstance(value, Stream):
                visit(value.entries, owner)

        for number, value in sorted(self.objects.items()):
            visit(value, f"object {number}")
        visit(self.trailer, "the trailer")

    def _info(self) -> None:
        """The document information: text strings, dates and /Trapped."""
        if "Info" not in self.trailer:
            return
        info = self.get(self.trailer["Info"])
        if not isinstance(info, dict):
            self.problem(f"trailer /Info {_show(self.trailer['Info'])} is not a "
                         "dictionary")
            return
        for key, value in info.items():
            where = f"/Info /{key}"
            if key == "Trapped":
                if not (isinstance(value, bool) or (
                    isinstance(value, Name) and value in ("True", "False", "Unknown")
                )):
                    self.problem(f"{where} {_show(value)} is not true, false or "
                                 "unknown")
            elif not isinstance(value, bytes):
                self.problem(f"{where} {_show(value)} is not a string")
            elif key in ("CreationDate", "ModDate"):
                if _DATE.fullmatch(value) is None:
                    self.problem(f"{where} {value!r} is not a date")
            elif wrong := _text_problem(value):
                self.problem(f"{where} {wrong}")

    # -- structure ---------------------------------------------------------

    def _pages(self, root: object) -> None:
        if not isinstance(root, Ref):
            self.problem("catalog has no /Pages reference")
            return
        seen: set[int] = set()

        def node(ref: Ref, parent: Ref | None) -> int:
            if ref.number in seen:
                self.problem(f"page tree reaches {ref} twice")
                return 0
            seen.add(ref.number)
            entry = self.dictionary(ref)
            if parent is not None and entry.get("Parent") != parent:
                self.problem(f"page tree node {ref} has /Parent "
                             f"{_show(entry.get('Parent'))}, expected {parent}")
            kind = entry.get("Type")
            if kind == "Page":
                self._indexes[ref] = len(self.page_refs)
                self.page_refs.append(ref)
                self._page(ref, entry)
                return 1
            if kind != "Pages":
                self.problem(f"page tree node {ref} is neither /Pages nor /Page")
                return 0
            kids = entry.get("Kids")
            if not isinstance(kids, list) or not all(isinstance(k, Ref) for k in kids):
                self.problem(f"page tree node {ref} has no /Kids array of references")
                return 0
            leaves = sum(node(kid, ref) for kid in kids if isinstance(kid, Ref))
            if entry.get("Count") != leaves:
                self.problem(f"page tree node {ref} has /Count "
                             f"{_show(entry.get('Count'))}, but {leaves} pages")
            return leaves

        if self.dictionary(root).get("Parent") is not None:
            self.problem("the page tree root has a /Parent")
        node(root, None)
        if not self.page_refs:
            self.problem("the page tree has no pages")

    def _page(self, ref: Ref, entry: dict[str, object]) -> None:
        if not _box(self._inherited(entry, "MediaBox")):
            self.problem(f"page {ref} has no /MediaBox of four numbers")
        resources = self._inherited(entry, "Resources")
        if not isinstance(resources, dict):
            self.problem(f"page {ref} has no /Resources dictionary")
            resources = {}
        for name, font in self.dictionary(resources.get("Font")).items():
            self._font(font, f"font /{name} of page {ref}")
        contents = self.get(entry.get("Contents"))
        parts = contents if isinstance(contents, list) else [contents]
        streams = [part for part in map(self.get, parts) if isinstance(part, Stream)]
        if len(streams) != len(parts):
            self.problem(f"page {ref} /Contents is not a stream")
            return
        data = _joined(streams)
        if data is not None:
            named = {category: frozenset(self.dictionary(resources.get(category)))
                     for category in _CATEGORIES}
            wrong = _Content(named).check(data)
            if wrong:
                self.problem(f"page {ref} content: {wrong}")

    def _font(self, value: object, where: str) -> None:
        """A simple font the reader can draw without the writer's help."""
        if isinstance(value, Ref):
            if value.number in self._fonts:
                return
            self._fonts.add(value.number)
        font = self.get(value)
        if not isinstance(font, dict) or font.get("Type") != "Font":
            self.problem(f"{where} is not a /Font dictionary")
            return
        if font.get("Subtype") != "Type1":
            self.problem(f"{where} has /Subtype {_show(font.get('Subtype'))}; only "
                         "Type1 fonts are supported")
            return
        base = font.get("BaseFont")
        standard = isinstance(base, Name) and base in _STANDARD_FONTS
        if not standard and not all(key in font for key in _METRICS):
            self.problem(f"{where} has /BaseFont {_show(base)}, which is not one of "
                         "the 14 standard fonts, and no metrics of its own")
        encoding = font.get("Encoding")
        if encoding is not None and not (
            isinstance(encoding, Name) and encoding in _ENCODINGS
        ):
            self.problem(f"{where} has /Encoding {_show(encoding)}, which is not a "
                         "predefined encoding")

    def _inherited(self, entry: dict[str, object], key: str) -> object:
        seen: set[int] = set()
        while key not in entry:
            parent = entry.get("Parent")
            if not isinstance(parent, Ref) or parent.number in seen:
                return None
            seen.add(parent.number)
            entry = self.dictionary(parent)
        return self.get(entry[key])

    def _named(self, catalog: dict[str, object]) -> None:
        """Collect named destinations from /Names /Dests and /Dests, and check
        the tree's order and that every one lands on a page."""
        old = self.dictionary(catalog.get("Dests"))
        for key, value in old.items():
            self.names[key.encode("latin-1")] = value
        tree = self.dictionary(catalog.get("Names")).get("Dests")
        if tree is not None:
            keys: list[bytes] = []
            self._name_tree(tree, set(), keys, root=True)
            for earlier, later in pairwise(keys):
                if later == earlier:
                    self.problem(f"name tree lists {later!r} twice")
                elif later < earlier:
                    self.problem(f"name tree key {later!r} comes after {earlier!r}")
        for name, value in self.names.items():
            where = f"named destination {name!r}"
            target = self._target(value)
            if target is None or self.destination(target) is None:
                self.problem(f"{where} does not lead to a page")
            else:
                self._explicit(target, where)

    def _name_tree(
        self, value: object, seen: set[int], keys: list[bytes], *, root: bool
    ) -> None:
        """Read a name tree node; its keys, in tree order, go to `keys`."""
        if isinstance(value, Ref):
            if value.number in seen:
                self.problem(f"name tree reaches {value} twice")
                return
            seen.add(value.number)
        node = self.dictionary(value)
        names, kids, limits = node.get("Names"), node.get("Kids"), node.get("Limits")
        start = len(keys)
        if isinstance(names, list):
            if len(names) % 2:
                self.problem(f"name tree node {_show(value)} has an odd /Names array")
            pairs = list(zip(names[::2], names[1::2], strict=False))
            if not all(isinstance(key, bytes) for key, _ in pairs):
                self.problem(f"name tree node {_show(value)} has a key that is not "
                             "a string")
            for key, target in pairs:
                if isinstance(key, bytes):
                    keys.append(key)
                    self.names[key] = target
        elif isinstance(kids, list):
            for kid in kids:
                self._name_tree(kid, seen, keys, root=False)
        else:
            self.problem(f"name tree node {_show(value)} has neither /Names nor /Kids")
        below = keys[start:]
        if not root and below and limits != [min(below), max(below)]:
            self.problem(f"name tree node {_show(value)} has /Limits {limits!r}, "
                         f"expected [{min(below)!r} {max(below)!r}]")

    def _explicit(self, target: list[object], where: str) -> None:
        """An explicit destination's type, its operands, and its top."""
        kind = target[1] if len(target) > 1 else None
        if not isinstance(kind, Name) or kind not in _FITS:
            self.problem(f"{where}: {_show(kind)} is not a destination type")
            return
        operands = target[2:]
        if len(operands) != _FITS[kind]:
            self.problem(f"{where}: /{kind} takes {_FITS[kind]} operands, not "
                         f"{len(operands)}")
            return
        if not all(_number(v) or (v is None and kind != "FitR") for v in operands):
            self.problem(f"{where}: /{kind} has an operand that is not a number"
                         + ("" if kind == "FitR" else " or null"))
            return
        top = operands[_TOPS[kind]] if kind in _TOPS else None
        box = self._inherited(self.dictionary(target[0]), "MediaBox")
        if _number(top) and _box(box):
            low, high = sorted((box[1], box[3]))
            if not low <= top <= high:
                self.problem(f"{where}: top {top} is off the page, which runs from "
                             f"{low} to {high}")

    def _outlines(self, root: object) -> None:
        if root is None:
            return
        if not isinstance(root, Ref):
            self.problem("catalog /Outlines is not a reference")
            return
        seen: set[int] = set()
        visible = self._siblings(root, self.dictionary(root), seen)
        count = self.dictionary(root).get("Count")
        if count is not None and count != visible:
            self.problem(f"outline root has /Count {_show(count)}, but {visible} "
                         "entries are visible")

    def _siblings(self, parent: Ref, entry: dict[str, object], seen: set[int]) -> int:
        """Check `parent`'s children; return how many entries they show."""
        first, last = entry.get("First"), entry.get("Last")
        if first is None and last is None:
            return 0
        if not isinstance(first, Ref) or not isinstance(last, Ref):
            self.problem(f"outline node {parent} has /First without /Last, or "
                         "one of them is not a reference")
            return 0
        visible = 0
        previous: Ref | None = None
        current: object = first
        while isinstance(current, Ref):
            if current.number in seen:
                self.problem(f"outline reaches {current} twice")
                return visible
            seen.add(current.number)
            item = self.dictionary(current)
            if item.get("Parent") != parent:
                self.problem(f"outline item {current} has /Parent "
                             f"{_show(item.get('Parent'))}, expected {parent}")
            if item.get("Prev") != previous:
                self.problem(f"outline item {current} has /Prev "
                             f"{_show(item.get('Prev'))}, expected {_show(previous)}")
            title = item.get("Title")
            if not isinstance(title, bytes):
                self.problem(f"outline item {current} has no /Title string")
            elif wrong := _text_problem(title):
                self.problem(f"outline item {current} /Title {wrong}")
            self._leads(item.get("Dest"), item.get("A"), f"outline item {current}")
            below = self._siblings(current, item, seen)
            count = item.get("Count")
            if (below or count is not None) and (
                not isinstance(count, int) or abs(count) != below
            ):
                self.problem(f"outline item {current} has /Count {_show(count)}, "
                             f"but {below} descendants show when it is open")
            visible += 1 + (below if isinstance(count, int) and count > 0 else 0)
            previous, current = current, item.get("Next")
        if current is not None:
            self.problem(f"outline item {previous} has a /Next that is not a reference")
        if previous != last:
            self.problem(f"outline node {parent} has /Last {last}, but its chain "
                         f"ends at {previous}")
        return visible

    def _links(self) -> None:
        for index, ref in enumerate(self.page_refs):
            annots = self.get(self.page(index).get("Annots"))
            if annots is None:
                continue
            if not isinstance(annots, list):
                self.problem(f"page {ref} /Annots is not an array")
                continue
            for annot in annots:
                entry = self.dictionary(annot)
                if entry.get("Subtype") != "Link":
                    continue
                where = f"link {_show(annot)} on page {index + 1}"
                rect = entry.get("Rect")
                if not _box(rect):
                    self.problem(f"{where} has no /Rect of four numbers")
                if "P" in entry and entry["P"] != ref:
                    self.problem(f"{where} has /P {_show(entry['P'])}, expected {ref}")
                self._leads(entry.get("Dest"), entry.get("A"), where)

    def _leads(self, destination: object, action: object, where: str) -> None:
        """A /Dest or /A action, not both, must lead somewhere that exists."""
        if destination is not None and action is not None:
            self.problem(f"{where} has both /Dest and /A")
        if destination is None:
            entry = self.dictionary(action)
            kind = entry.get("S")
            if kind == "GoToR":
                self._remote(entry, where)
                return
            if kind == "URI":
                if not isinstance(entry.get("URI"), bytes):
                    self.problem(f"{where}: URI action has no URI string")
                return
            if kind != "GoTo":
                self.problem(f"{where} has neither a destination nor a known action")
                return
            destination = entry.get("D")
        target = self.get(destination)
        if self.destination(target) is None:
            self.problem(f"{where}: destination {_show(destination)} is not defined")
        elif isinstance(target, list):          # a named one is checked by name
            self._explicit(target, where)

    def _remote(self, action: dict[str, object], where: str) -> None:
        """A GoToR action: a file to open, its name in Unicode a text string,
        and a destination there."""
        spec = self.get(action.get("F"))
        fields = spec if isinstance(spec, dict) else {}
        name = fields.get("F") if isinstance(spec, dict) else spec
        if not isinstance(name, bytes) or not name:
            self.problem(f"{where}: GoToR action has no file")
        unicode = fields.get("UF")
        if unicode is not None and (
            not isinstance(unicode, bytes) or _text_problem(unicode)
        ):
            self.problem(f"{where}: GoToR file /UF {_show(unicode)} is not a text "
                         "string")
        if not isinstance(action.get("D"), bytes | Name | list):
            self.problem(f"{where}: GoToR action has no destination")


class _Content:
    """One page's content stream, read operator by operator against the
    graphics-object states, the font in use and the page's resources."""

    def __init__(self, resources: Mapping[str, Collection[str]]) -> None:
        self.resources = resources      # category: the names it holds
        self.state = "P"
        self.font: str | None = None
        self.saved: list[str | None] = []   # the font as each q saved it
        self.marked = 0
        self.compatible = 0             # BX ... EX sections open

    def check(self, data: bytes) -> str:
        """The first problem in `data`; "" when there is none."""
        operands: list[object] = []
        position = 0
        while True:
            token = _TOKEN.match(data, position)
            if token is None:           # a string with escapes, an array, ...
                parser = _Parser(data, position, references=False)
                try:
                    operands.append(parser.value())
                except Malformed as err:
                    return str(err)
                position = parser.position
                continue
            position = token.end()
            kind = token.lastgroup or "end"     # one named alternative matched
            if kind == "end":
                return self.finish(operands)
            text = token[kind]
            if kind == "number":
                operands.append(float(text) if b"." in text else int(text))
            elif kind == "name":
                operands.append(_name(text[1:]))
            elif kind == "string":
                operands.append(text[1:-1])
            elif text in _KEYWORDS:
                operands.append(_KEYWORDS[text])
            else:
                wrong = self.operator(text, operands)
                if wrong:
                    return f"{wrong}, at byte {token.start(kind)}"
                operands = []

    def operator(self, word: bytes, operands: list[object]) -> str:
        """Why `word` cannot apply to `operands` here; "" when it can."""
        name = word.decode("latin-1")
        if word == b"BI":
            return "inline images are not supported"
        known = _OPERATORS.get(word)
        if known is None:
            return "" if self.compatible else f"unknown operator {name}"
        signature, allowed, after = known
        if self.state not in allowed:
            return f"{name} is not allowed {_STATES[self.state]}"
        wrong = _operands(signature, operands) or self.resource(word, operands)
        if wrong:
            return f"{name} {wrong}"
        wrong = self.effect(word, operands)
        if wrong:
            return f"{name} {wrong}"
        if after:
            self.state = after
        return ""

    def resource(self, word: bytes, operands: list[object]) -> str:
        """What `word` names that the page's resources lack; "" if nothing."""
        category = _RESOURCES.get(word)
        if category is None:
            return ""
        name = operands[-1] if word in (b"DP", b"BDC") else operands[0]
        if not isinstance(name, Name) or name in self.resources.get(category, ()):
            return ""
        if category == "ColorSpace" and name in _DEVICE_SPACES:
            return ""
        return f"names /{name}, which the page's /Resources /{category} lacks"

    def effect(self, word: bytes, operands: list[object]) -> str:
        """Apply `word` to the font, q and marked-content state; why it
        cannot be applied, or ""."""
        if word == b"q":
            self.saved.append(self.font)
        elif word == b"Q":
            if not self.saved:
                return "without a q before it"
            self.font = self.saved.pop()
        elif word == b"Tf":
            self.font = str(operands[0])
        elif word in _SHOWS:
            if self.font is None:
                return "shows text before Tf sets a font"
            shown = operands[-1]
            strings = shown if isinstance(shown, list) else [shown]
            low = min((min(s) for s in strings if isinstance(s, bytes) and s),
                      default=32)
            if low < 32:
                return f"shows code {low:#04x}, which no simple-font encoding defines"
        elif word in (b"BMC", b"BDC"):
            self.marked += 1
        elif word == b"EMC":
            if not self.marked:
                return "without a BMC or BDC before it"
            self.marked -= 1
        elif word == b"BX":
            self.compatible += 1
        elif word == b"EX":
            if not self.compatible:
                return "without a BX before it"
            self.compatible -= 1
        return ""

    def finish(self, operands: list[object]) -> str:
        """What the stream leaves open at its end; "" if nothing."""
        if operands:
            return "operands at the end of the stream have no operator"
        if self.state in ("A", "C"):
            return "a path is never painted"
        if self.state == "T":
            return "a text object is never closed with ET"
        if self.saved:
            return "a q is never matched by Q"
        if self.marked:
            return "a marked-content sequence is never closed with EMC"
        if self.compatible:
            return "a BX is never closed with EX"
        return ""


def _operands(signature: str, operands: Sequence[object]) -> str:
    """How `operands` differ from what `signature` takes; "" if they agree."""
    if signature in ("+", "*"):
        numbers = list(operands)
        if signature == "*" and numbers and isinstance(numbers[-1], Name):
            numbers.pop()
        if operands and all(map(_number, numbers)):
            return ""
        return ("takes one or more numbers" if signature == "+"
                else "takes numbers, a pattern name or both")
    if len(operands) != len(signature):
        return f"takes {len(signature)} operands, not {len(operands)}"
    for position, (code, value) in enumerate(zip(signature, operands, strict=True), 1):
        if not _fits(code, value):
            return f"operand {position}, {_show(value)}, is not {_KINDS[code]}"
    return ""


def _fits(code: str, value: object) -> bool:
    """Whether `value` is an operand of the kind `code` stands for."""
    match code:
        case "n":
            return _number(value)
        case "i":
            return isinstance(value, int) and not isinstance(value, bool)
        case "N":
            return isinstance(value, Name)
        case "s":
            return isinstance(value, bytes)
        case "a":
            return isinstance(value, list) and all(map(_number, value))
        case "t":
            return isinstance(value, list) and all(
                isinstance(item, bytes) or _number(item) for item in value
            )
    return isinstance(value, dict | Name)


def _number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _box(value: object) -> TypeGuard[list[int | float]]:
    """Whether `value` is a rectangle, four numbers."""
    return isinstance(value, list) and len(value) == 4 and all(map(_number, value))


def _text_problem(value: bytes) -> str:
    """Why `value` is not a well-formed text string; "" when it is."""
    if value.startswith(b"\xfe\xff"):
        try:
            value[2:].decode("utf-16-be")
        except UnicodeDecodeError:
            return "is not well-formed UTF-16"
        return ""
    undefined = sorted(set(value) & _UNDEFINED)
    if undefined:
        codes = ", ".join(f"{code:#04x}" for code in undefined)
        return f"has codes PDFDocEncoding does not define: {codes}"
    return ""


def _joined(streams: Sequence[Stream]) -> bytes | None:
    """Content streams decoded and joined; None when one cannot be decoded,
    which reading it reported already."""
    parts: list[bytes] = []
    for stream in streams:
        kind = stream.entries.get("Filter")
        if kind not in (None, "FlateDecode"):
            return None
        try:
            parts.append(zlib.decompress(stream.raw) if kind else stream.raw)
        except zlib.error:
            return None
    return b"\n".join(parts)


def _referenced_length(data: bytes, ref: Ref) -> object:
    """A /Length stored as its own object, read straight from the file."""
    marker = re.search(rb"(?<![0-9])%d\s+%d\s+obj" % (ref.number, ref.generation), data)
    if marker is None:
        return None
    parser = _Parser(data, marker.end())
    return parser.value()


def _text(value: bytes) -> str:
    if value.startswith(b"\xfe\xff"):
        return value[2:].decode("utf-16-be", "replace")
    return value.decode("latin-1")


def _show(value: object) -> str:
    """`value` for a message: a name with its slash, a reference as `n g R`,
    anything else as its repr, shortened past 80 characters."""
    if isinstance(value, Name):
        return f"/{value}"
    text = str(value) if isinstance(value, Ref) else repr(value)
    return text if len(text) <= 80 else text[:77] + "..."
