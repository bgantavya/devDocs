"""PDF objects, their serialization, and the assembly of a whole file.

This module is PDF syntax and nothing else. The layout decides what goes on a
page; this decides how pages, links, bookmarks and named destinations are
written down: PDF 1.7 with a classic cross-reference table of 20-byte entries
and FlateDecode streams. Object and cross-reference streams would make files
smaller, but they hide the byte offsets `check.py` verifies one by one, and
the content streams, which hold most of the bytes, are compressed anyway.

Object numbers are handed out in two phases. `Objects.reserve` returns a
reference before its object exists and `Objects.define` supplies the object
later, because the page tree and its pages, and pages and their annotations,
refer to each other: whichever is written first needs the other's number.
Object n always sits at list position n - 1, with no sentinel at position 0;
a sentinel there once desynchronised numbers from positions in a prototype,
and only a strict re-parse noticed. Serializing refuses an object that was
reserved but never defined and a reference to an object that does not exist,
so the writer cannot produce either.

Output is deterministic: dictionaries keep insertion order, reals are written
in fixed point with at most three decimals, streams are compressed at a fixed
level, and the file identifier is a hash of the content, so the same pages
always serialize to the same bytes.

Names that come from the file system are written so every one survives. A
named destination's key is its name in UTF-8, a lone surrogate (what
`os.walk` makes of an undecodable POSIX file name) kept as its own three
bytes, so each name has exactly one key, the same in every file. A link to
another PDF names the file twice, because readers disagree: /UF in Unicode,
which Acrobat, pdf.js and poppler read, and /F in Mac OS Roman, the only
form PDFKit (Preview, Safari) resolves. A path Mac OS Roman cannot spell,
such as one in Chinese, has its UTF-8 bytes in /F instead, which PDFKit
cannot follow; an undecodable name keeps its bytes from the file system
there, and has no /UF, as Unicode cannot spell it either.
"""

from __future__ import annotations

import hashlib
import math
import re
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "Bookmark",
    "Color",
    "Content",
    "LinkArea",
    "Name",
    "Objects",
    "Page",
    "Ref",
    "Stream",
    "build",
    "serialize",
]

Color = tuple[float, float, float]

_HEADER = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"   # the binary comment marks the file binary
_DELIMITERS = frozenset(b"()<>[]{}/% \t\r\n\f\x00")
_LITERAL_SPECIAL = re.compile(rb"[\\()\x00-\x1f\x7f-\xff]")
_SHORT_ESCAPES = {
    0x0A: b"\\n", 0x0D: b"\\r", 0x09: b"\\t", 0x08: b"\\b", 0x0C: b"\\f",
    0x5C: b"\\\\", 0x28: b"\\(", 0x29: b"\\)",
}
_NAME_SPECIAL = re.compile(rb"[^!-~]|[#()<>\[\]{}/%]")
_SURROGATE = re.compile("[\ud800-\udfff]")
_NAME_TREE_LEAF = 64   # destinations per name-tree leaf
_KAPPA = 0.5523        # Bezier handle length for a quarter circle, per radius


class Name(str):
    """A PDF name, such as `/Type`; a plain `str` is a text string instead."""

    __slots__ = ()


@dataclass(frozen=True)
class Ref:
    """An indirect reference, `n 0 R`. Generations are always 0 here."""

    number: int


@dataclass(frozen=True, eq=False)
class Stream:
    """A stream object. Serializing compresses `data` and adds /Length and
    /Filter to `entries`."""

    data: bytes
    entries: Mapping[str, object] = field(default_factory=dict)


# -- serialization -------------------------------------------------------------


def serialize(value: object, *, objects: int | None = None) -> bytes:
    """The PDF syntax for `value`, with whitespace only where tokens need it.

    With `objects`, a reference outside 1..objects raises ValueError.
    """
    tokens: list[bytes] = []
    _tokens(value, tokens, objects)
    out = bytearray()
    for token in tokens:
        if out and out[-1] not in _DELIMITERS and token[0] not in _DELIMITERS:
            out += b" "
        out += token
    return bytes(out)


def _tokens(value: object, out: list[bytes], objects: int | None) -> None:
    if value is None:
        out.append(b"null")
    elif isinstance(value, bool):
        out.append(b"true" if value else b"false")
    elif isinstance(value, int):
        out.append(b"%d" % value)
    elif isinstance(value, float):
        out.append(_real(value))
    elif isinstance(value, Name):
        out.append(b"/" + _NAME_SPECIAL.sub(_hex_escape, value.encode("utf-8")))
    elif isinstance(value, str):
        out.append(_text_string(value))
    elif isinstance(value, bytes):
        out.append(_literal(value))
    elif isinstance(value, Ref):
        if objects is not None and not 1 <= value.number <= objects:
            raise ValueError(f"reference to missing object {value.number}")
        out.append(b"%d 0 R" % value.number)
    elif isinstance(value, list | tuple):
        out.append(b"[")
        for item in value:
            _tokens(item, out, objects)
        out.append(b"]")
    elif isinstance(value, Mapping):
        out.append(b"<<")
        for key, item in value.items():
            _tokens(Name(key), out, objects)
            _tokens(item, out, objects)
        out.append(b">>")
    else:
        raise TypeError(f"cannot write {type(value).__name__} into a PDF")


def _real(value: float) -> bytes:
    """A number in fixed point, trimmed: 12.5, 3, -0.25; never an exponent."""
    if not math.isfinite(value):
        raise ValueError(f"PDF numbers must be finite, not {value!r}")
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return b"0" if text in ("-0", "") else text.encode("ascii")


def _literal(data: bytes) -> bytes:
    """A literal string: backslash, parentheses and non-printables escaped."""
    return b"(" + _LITERAL_SPECIAL.sub(_escape, data) + b")"


def _escape(match: re.Match[bytes]) -> bytes:
    code = match.group()[0]
    return _SHORT_ESCAPES.get(code) or b"\\%03o" % code


def _hex_escape(match: re.Match[bytes]) -> bytes:
    return b"#%02X" % match.group()[0]


def _text_string(value: str) -> bytes:
    """A text string: printable ASCII as is, since PDFDocEncoding agrees with
    ASCII there; anything else, control characters included, as UTF-16BE
    with a BOM, where every character keeps its meaning."""
    if value.isascii() and value.isprintable():
        return _literal(value.encode("ascii"))
    return _literal(b"\xfe\xff" + value.encode("utf-16-be", "replace"))


def _key(name: str) -> bytes:
    """A named destination's key: UTF-8, a lone surrogate as its own bytes."""
    return name.encode("utf-8", "surrogatepass")


def _file_spec(path: str) -> dict[str, object]:
    """A file specification of the relative POSIX `path` (see the module
    docstring for why it names the file twice)."""
    spec: dict[str, object] = {"Type": Name("Filespec"), "F": _file_name(path)}
    if not _SURROGATE.search(path):
        spec["UF"] = path
    return spec


def _file_name(path: str) -> bytes:
    """`path` in Mac OS Roman; else in the bytes `os.fsencode` gives on
    POSIX, which restores an undecodable name's own bytes, or for a lone
    surrogate that stands for no byte, the bytes it gives on Windows."""
    try:
        return path.encode("mac_roman")
    except UnicodeEncodeError:
        pass
    try:
        return path.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return path.encode("utf-8", "surrogatepass")


class Objects:
    """The numbered objects of one file, allocated in two phases."""

    def __init__(self) -> None:
        self._values: list[object] = []
        self._defined: list[bool] = []

    def reserve(self) -> Ref:
        """A reference for an object to be defined later."""
        self._values.append(None)
        self._defined.append(False)
        return Ref(len(self._values))   # object n sits at position n - 1

    def define(self, ref: Ref, value: object) -> None:
        if not 1 <= ref.number <= len(self._values):
            raise ValueError(f"object {ref.number} was never reserved")
        if self._defined[ref.number - 1]:
            raise ValueError(f"object {ref.number} is already defined")
        self._values[ref.number - 1] = value
        self._defined[ref.number - 1] = True

    def add(self, value: object) -> Ref:
        ref = self.reserve()
        self.define(ref, value)
        return ref

    def serialize(self, *, root: Ref, info: Ref | None = None) -> bytes:
        """The whole file: header, objects, cross-reference table, trailer."""
        undefined = [n for n, done in enumerate(self._defined, 1) if not done]
        if undefined:
            raise ValueError(f"objects reserved but never defined: {undefined}")
        count = len(self._values)
        out = bytearray(_HEADER)
        offsets: list[int] = []
        for number, value in enumerate(self._values, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % number
            out += _body(value, count)
            out += b"\nendobj\n"
        identifier = hashlib.sha256(out).digest()[:16]
        trailer: dict[str, object] = {"Size": count + 1, "Root": root}
        if info is not None:
            trailer["Info"] = info
        trailer["ID"] = [identifier, identifier]
        start = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f\r\n" % (count + 1)
        for offset in offsets:
            out += b"%010d 00000 n\r\n" % offset
        out += b"trailer\n" + serialize(trailer, objects=count)
        out += b"\nstartxref\n%d\n%%%%EOF\n" % start
        return bytes(out)


def _body(value: object, count: int) -> bytes:
    if not isinstance(value, Stream):
        return serialize(value, objects=count)
    data = zlib.compress(value.data, 9)
    entries = {**value.entries, "Length": len(data), "Filter": Name("FlateDecode")}
    return serialize(entries, objects=count) + b"\nstream\n" + data + b"\nendstream"


# -- page content --------------------------------------------------------------


class Content:
    """One page's content stream: every graphic first, then one text object.

    With all glyphs in a single BT/ET block after the graphics, backgrounds
    and rules can be drawn at any point of the layout and still sit beneath
    the text, and font and colour changes are written only when they change.
    """

    def __init__(self) -> None:
        self._graphics: list[bytes] = []
        self._text: list[bytes] = []
        self._font: tuple[str, float] | None = None
        self._color: Color | None = None

    def text(
        self, x: float, y: float, font: str, size: float, data: bytes, color: Color
    ) -> None:
        """Show WinAnsi `data` with its baseline starting at (x, y)."""
        if self._font != (font, size):
            self._text.append(b"/%s %s Tf" % (font.encode("ascii"), _real(size)))
            self._font = (font, size)
        if self._color != color:
            self._text.append(_rgb(color) + b" rg")
            self._color = color
        self._text.append(
            b"1 0 0 1 %s %s Tm %s Tj" % (_real(x), _real(y), _literal(data))
        )

    def rect(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        *,
        fill: Color | None = None,
        stroke: Color | None = None,
        line: float = 0.5,
        radius: float = 0.0,
    ) -> None:
        """A rectangle from its lower-left corner; `radius` rounds the corners."""
        corner = min(radius, width / 2, height / 2)
        if corner > 0:
            path = _rounded(x, y, x + width, y + height, corner)
        else:
            path = b"%s %s %s %s re" % (_real(x), _real(y), _real(width), _real(height))
        self._paint(path, fill=fill, stroke=stroke, line=line)

    def line(
        self, x0: float, y0: float, x1: float, y1: float, *, color: Color,
        width: float = 0.5,
    ) -> None:
        path = b"%s %s m %s %s l" % (_real(x0), _real(y0), _real(x1), _real(y1))
        self._paint(path, stroke=color, line=width)

    def polygon(self, points: Sequence[tuple[float, float]], *, fill: Color) -> None:
        """A closed, filled polygon, such as an arrowhead."""
        (x, y), *rest = points
        moves = [b"%s %s m" % (_real(x), _real(y))]
        moves += [b"%s %s l" % (_real(px), _real(py)) for px, py in rest]
        self._paint(b" ".join(moves) + b" h", fill=fill)

    def _paint(
        self, path: bytes, *, fill: Color | None = None, stroke: Color | None = None,
        line: float = 0.5,
    ) -> None:
        state: list[bytes] = []
        if fill is not None:
            state.append(_rgb(fill) + b" rg")
        if stroke is not None:
            state.append(_rgb(stroke) + b" RG %s w" % _real(line))
        operator = b"S" if fill is None else b"f" if stroke is None else b"B"
        self._graphics.append(b" ".join([*state, path, operator]))

    def finish(self) -> bytes:
        parts = list(self._graphics)
        if self._text:
            parts += [b"BT", *self._text, b"ET"]
        return b"\n".join(parts)


def _rgb(color: Color) -> bytes:
    return b" ".join(_real(channel) for channel in color)


def _rounded(x0: float, y0: float, x1: float, y1: float, r: float) -> bytes:
    k = r * _KAPPA
    points = (
        (b"m", (x0 + r, y0)),
        (b"l", (x1 - r, y0)),
        (b"c", (x1 - r + k, y0, x1, y0 + r - k, x1, y0 + r)),
        (b"l", (x1, y1 - r)),
        (b"c", (x1, y1 - r + k, x1 - r + k, y1, x1 - r, y1)),
        (b"l", (x0 + r, y1)),
        (b"c", (x0 + r - k, y1, x0, y1 - r + k, x0, y1 - r)),
        (b"l", (x0, y0 + r)),
        (b"c", (x0, y0 + r - k, x0 + r - k, y0, x0 + r, y0)),
    )
    return b" ".join(
        b" ".join(_real(value) for value in values) + b" " + operator
        for operator, values in points
    ) + b" h"


# -- document assembly ---------------------------------------------------------


@dataclass(frozen=True)
class LinkArea:
    """A clickable rectangle and the named destination it opens."""

    rect: tuple[float, float, float, float]   # x0, y0, x1, y1
    destination: str
    file: str = ""      # another PDF, relative to this one; "" for this file


@dataclass(frozen=True)
class Page:
    width: float
    height: float
    content: bytes
    links: tuple[LinkArea, ...] = ()


@dataclass(frozen=True)
class Bookmark:
    """An outline entry; closed entries show their children on demand."""

    title: str
    destination: str
    children: tuple[Bookmark, ...] = ()
    open: bool = False


def build(
    pages: Sequence[Page],
    *,
    fonts: Mapping[str, str],
    destinations: Mapping[str, tuple[int, float]],
    outline: Sequence[Bookmark] = (),
    info: Mapping[str, str] | None = None,
) -> bytes:
    """A complete PDF file.

    `fonts` maps resource names to standard base fonts, all WinAnsi encoded.
    `destinations` maps each named destination to (page index, top y); links
    to this file and bookmarks must name one of them. `info` becomes the
    document information dictionary.
    """
    if not pages:
        raise ValueError("a PDF needs at least one page")
    for page in pages:
        for link in page.links:
            if not link.file and link.destination not in destinations:
                raise ValueError(f"link to undefined destination {link.destination!r}")

    objects = Objects()
    catalog = objects.reserve()
    tree = objects.reserve()
    page_refs = [objects.reserve() for _ in pages]
    font_refs = {
        resource: objects.add({
            "Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name(base),
            "Encoding": Name("WinAnsiEncoding"),
        })
        for resource, base in fonts.items()
    }
    resources = objects.add({"Font": font_refs})
    for page, ref in zip(pages, page_refs, strict=True):
        entry: dict[str, object] = {
            "Type": Name("Page"),
            "Parent": tree,
            "MediaBox": [0, 0, page.width, page.height],
            "Resources": resources,
            "Contents": objects.add(Stream(page.content)),
        }
        if page.links:
            entry["Annots"] = [
                objects.add(_annotation(link, ref)) for link in page.links
            ]
        objects.define(ref, entry)
    objects.define(tree, {
        "Type": Name("Pages"), "Kids": page_refs, "Count": len(pages),
    })

    root: dict[str, object] = {"Type": Name("Catalog"), "Pages": tree}
    if destinations:
        values = {
            _key(name): [page_refs[index], Name("XYZ"), 0, top, None]
            for name, (index, top) in destinations.items()
        }
        root["Names"] = {"Dests": _name_tree(objects, values)}
    if outline:
        root["Outlines"] = _outlines(objects, outline, destinations)
        root["PageMode"] = Name("UseOutlines")
    root["ViewerPreferences"] = {"DisplayDocTitle": True}
    objects.define(catalog, root)
    info_ref = objects.add(dict(info)) if info else None
    return objects.serialize(root=catalog, info=info_ref)


def _annotation(link: LinkArea, page: Ref) -> dict[str, object]:
    annotation: dict[str, object] = {
        "Type": Name("Annot"),
        "Subtype": Name("Link"),
        "Rect": list(link.rect),
        "Border": [0, 0, 0],
        "P": page,
    }
    destination = _key(link.destination)
    if link.file:
        annotation["A"] = {
            "S": Name("GoToR"), "F": _file_spec(link.file), "D": destination,
        }
    else:
        annotation["Dest"] = destination
    return annotation


def _name_tree(objects: Objects, values: Mapping[bytes, object]) -> Ref:
    """A name tree of sorted leaves under one root, so no array grows long."""
    keys = sorted(values)
    leaves = [keys[start:start + _NAME_TREE_LEAF]
              for start in range(0, len(keys), _NAME_TREE_LEAF)]
    if len(leaves) == 1:
        return objects.add({"Names": _pairs(keys, values)})
    kids = [
        objects.add({"Limits": [leaf[0], leaf[-1]], "Names": _pairs(leaf, values)})
        for leaf in leaves
    ]
    return objects.add({"Kids": kids})


def _pairs(keys: Iterable[bytes], values: Mapping[bytes, object]) -> list[object]:
    return [part for key in keys for part in (key, values[key])]


def _outlines(
    objects: Objects, items: Sequence[Bookmark], destinations: Mapping[str, object]
) -> Ref:
    root = objects.reserve()
    first, last, visible = _entries(objects, items, root, destinations)
    objects.define(root, {
        "Type": Name("Outlines"), "First": first, "Last": last, "Count": visible,
    })
    return root


def _entries(
    objects: Objects,
    items: Sequence[Bookmark],
    parent: Ref,
    destinations: Mapping[str, object],
) -> tuple[Ref, Ref, int]:
    """Write `items` as siblings under `parent`: their first and last
    references, and how many entries they show, open descendants included."""
    refs = [objects.reserve() for _ in items]
    visible = 0
    for index, (item, ref) in enumerate(zip(items, refs, strict=True)):
        if item.destination not in destinations:
            raise ValueError(f"bookmark to undefined destination {item.destination!r}")
        entry: dict[str, object] = {
            "Title": item.title, "Parent": parent, "Dest": _key(item.destination),
        }
        if index:
            entry["Prev"] = refs[index - 1]
        if index + 1 < len(refs):
            entry["Next"] = refs[index + 1]
        shown = 0
        if item.children:
            first, last, below = _entries(objects, item.children, ref, destinations)
            entry.update(First=first, Last=last, Count=below if item.open else -below)
            shown = below if item.open else 0
        objects.define(ref, entry)
        visible += 1 + shown
    return refs[0], refs[-1], visible
