"""Typesetting for the built-in PDF engine: blocks in, laid-out pages out.

The renderer describes a document as a flat sequence of blocks (headings,
paragraphs, lists, code, tables, diagrams, contents entries) whose text is
spans in the standard fonts, some of them links. `typeset` flows the blocks
onto pages of one size and returns each page's content stream, its link
areas, and the position of every named destination; `decorate` adds the
running head and the folio afterwards, because "Page N of M" needs M.

Lines break greedily on the fonts' real advance widths, ragged right. A word
wider than its line, such as a path, a dotted name or a long identifier,
breaks after a slash, dot, underscore or other separator, or between the
words of a camelCase name, and only as a last resort between two letters.
Code keeps its own lines and wraps long ones at a space or punctuation, with
a continuation indent.

Pagination keeps a heading with the first lines of what follows it (with a
subheading, the subheading and what follows that), keeps a contents group
label with its first entry, never leaves a paragraph's first line alone at
the foot of a page, moves a table row whole to the next page and repeats the
header there, splits code between lines with its background continued, and
never splits a diagram: diagrams are scaled down to the text width, or to
most of a page's height, instead. What a block keeps with is measured from
the block's real opening, the space above it included, so the reservation
matches what the block then needs.

A diagram is laid out both ways, left to right and top to bottom, and drawn
the way its graph says unless that would print its labels below the
smallest legible size and the other way prints them larger: a long import
chain is unreadable across the page but fits down it. Labels never print
below that size; one still too wide for its box there is cut short.

Table columns keep their natural widths when the table fits. When it does
not, a column whose natural width is within an equal share of the width
keeps it, so short paths do not wrap, and the other columns share the rest,
each its longest unbreakable piece plus a part of the spare width by need.

Text is encoded to WinAnsi as it is set. Characters without a code there
print as "?" and are added to the caller's `missing` set, so the renderer can
report each one; running heads only repeat text reported elsewhere, so they
report nothing.
"""

from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from devdocs.render import diagram
from devdocs.render.pdf.fonts import BOLD, CODE, REGULAR, Font, encode
from devdocs.render.pdf.writer import Color, Content

__all__ = [
    "ACCENT",
    "BODY",
    "LEAD",
    "PAGE_SIZES",
    "SMALL",
    "TITLE",
    "Block",
    "Code",
    "Entry",
    "Figure",
    "Flow",
    "Heading",
    "ListBlock",
    "ListItem",
    "PageSize",
    "Paragraph",
    "Row",
    "Rule",
    "Sheet",
    "Space",
    "Span",
    "Style",
    "Table",
    "Target",
    "decorate",
    "figure",
    "typeset",
]


@dataclass(frozen=True)
class PageSize:
    name: str
    width: float      # points
    height: float


PAGE_SIZES = {
    "A4": PageSize("A4", 595.28, 841.89),
    "Letter": PageSize("Letter", 612.0, 792.0),
}

_INK: Color = (0.12, 0.13, 0.15)
_MUTED: Color = (0.42, 0.45, 0.49)
ACCENT: Color = (0.05, 0.35, 0.72)
_LINK: Color = ACCENT
_RULE: Color = (0.82, 0.84, 0.87)
_SHADE: Color = (0.955, 0.96, 0.97)
_PANEL: Color = (0.975, 0.98, 0.985)
_BOX: Color = (0.91, 0.94, 0.98)
_BOX_LINE: Color = (0.45, 0.52, 0.62)
_ARROW: Color = (0.36, 0.40, 0.46)

_SIDE = 56.0            # left and right margins
_TOP = 68.0             # page top to the text block
_BOTTOM = 60.0          # page bottom to the text block
_HEAD = 40.0            # page top to the running head's baseline
_FOLIO = 34.0           # page bottom to the folio's baseline
_MARK_ABOVE = 6.0       # a destination opens this far above its block
_EPSILON = 0.01


@dataclass(frozen=True)
class Style:
    font: Font
    size: float
    leading: float
    color: Color = _INK
    before: float = 0.0
    after: float = 0.0
    code: float = 0.9       # inline Courier's size against the text's


TITLE = Style(BOLD, 26.0, 31.0, after=10.0)
LEAD = Style(REGULAR, 12.0, 17.0, after=8.0)
BODY = Style(REGULAR, 9.5, 13.5, after=6.0)
SMALL = Style(REGULAR, 8.0, 11.0, _MUTED, before=6.0, after=4.0)
_HEADINGS = {
    1: Style(BOLD, 17.0, 22.0, after=12.0, code=1.0),
    2: Style(BOLD, 12.5, 16.5, before=16.0, after=6.0, code=1.0),
    3: Style(BOLD, 10.5, 14.0, before=12.0, after=4.0, code=1.0),
}
_CELL = Style(REGULAR, 9.0, 12.0)
_CELL_HEAD = Style(BOLD, 9.0, 12.0)
_CODE = Style(CODE, 8.0, 10.5)
_RUNNING = Style(REGULAR, 7.5, 10.0, _MUTED)
_NUMBERS = BODY.font.measure(b"0000", BODY.size)   # the contents' page column

_CODE_PAD = 5.0
_CODE_GAP = 6.0         # space around a code block
_CODE_INDENT = 4        # continuation indent, in characters
_CELL_PAD = 4.0
_TABLE_GAP = 8.0
_INDENT = 14.0          # per list depth or contents level
_BULLET_BOX = 11.0
_BULLETS = (b"\x95", b"\x96", b"\xb7")   # bullet, en dash, middle dot
_ITEM_GAP = 2.0
_H1_RULE = 8.0          # gap plus hairline under a first-level heading
_LEADER_GAP = 6.0
_GROUP_GAP = 10.0       # before a contents group label
_ENTRY_GAP = 1.5        # before any other contents entry
_LOOKAHEAD = 3          # blocks a heading keeps with: two subheadings and one more
_FIGURE_PAD = 8.0
_FIGURE_GAP = 8.0
_FIGURE_HEIGHT = 0.8    # most of a page a diagram may take
_DIAGRAM_SIZE = 9.0     # diagrams are laid out at this size, then scaled
_DIAGRAM_PADDING = 6.5  # around a label inside its box
_LABEL_MIN = 4.5
_ELLIPSIS = b"\x85"

# Break opportunities inside a word too long for its line.
_SPLIT = re.compile(
    rb"(?<=[/\\.,;:_|&=+>\])}-])(?=[^/\\.,;:_|&=+>\])}-])"
    rb"|(?<=[a-z])(?=[A-Z])"
    rb"|(?<=[^\s(\[{<])(?=[(\[{<])"
)
_TOKENS = re.compile(r"[^\S\xa0]+|[\S\xa0]+")   # no-break spaces stay in words
_CODE_BREAK = frozenset(" ,;([{")


# -- blocks --------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """Where a link goes: a named destination, in this file or another.

    `file` names the output file holding the destination ("" for the same
    file); `fallback` is used when `name` is never placed, such as a symbol
    the target page shows without an anchor.
    """

    name: str
    file: str = ""
    fallback: str = ""


@dataclass(frozen=True)
class Span:
    text: str
    font: Font | None = None        # None: the block's own font
    target: Target | None = None


Spans = tuple[Span, ...]


@dataclass(frozen=True)
class Heading:
    level: int                      # 1 to 3
    spans: Spans
    name: str = ""                  # destination placed at the heading


@dataclass(frozen=True)
class Paragraph:
    spans: Spans
    style: Style = BODY


@dataclass(frozen=True)
class ListItem:
    spans: Spans
    depth: int = 0
    name: str = ""


@dataclass(frozen=True)
class ListBlock:
    items: tuple[ListItem, ...]
    ordered: bool = False


@dataclass(frozen=True)
class Code:
    text: str


@dataclass(frozen=True)
class Row:
    cells: tuple[Spans, ...]
    name: str = ""


@dataclass(frozen=True)
class Table:
    header: tuple[str, ...]
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class Figure:
    drawing: diagram.Layout                 # in its graph's own direction
    turned: diagram.Layout | None = None    # the other way round


def figure(graph: diagram.Graph) -> Figure:
    """`graph` laid out for the page in its own direction and turned: labels
    measured in the PDF's own font, spacing in proportion to the type size."""
    turned = dataclasses.replace(
        graph, direction="TD" if graph.direction == "LR" else "LR"
    )
    return Figure(_drawing(graph), _drawing(turned))


def _drawing(graph: diagram.Graph) -> diagram.Layout:
    return diagram.layout(
        graph,
        measure=lambda label: REGULAR.measure(encode(label)[0], _DIAGRAM_SIZE),
        font_size=_DIAGRAM_SIZE,
        padding=_DIAGRAM_PADDING,
        layer_gap=36.0,
        node_gap=11.5,
    )


@dataclass(frozen=True)
class Entry:
    """A contents line: a title, its page number and leaders, all one link.
    Without a page number it is a group label."""

    title: str
    page: str = ""
    target: Target | None = None
    level: int = 0

    @property
    def group(self) -> bool:
        return not self.page and self.target is None


@dataclass(frozen=True)
class Rule:
    width: float = 0.0              # 0: the full text width
    thickness: float = 0.5
    color: Color = _RULE


@dataclass(frozen=True)
class Space:
    height: float


Block = (
    Heading | Paragraph | ListBlock | Code | Table | Figure | Entry | Rule | Space
)
Rect = tuple[float, float, float, float]


# -- pages ---------------------------------------------------------------------


@dataclass
class Sheet:
    """One laid-out page: its content and the link areas on it."""

    content: Content = field(default_factory=Content)
    links: list[tuple[Rect, Target]] = field(default_factory=list)

    def link(self, rect: Rect, target: Target) -> None:
        """Add a link area, joined to the previous one when they adjoin."""
        if self.links:
            (x0, y0, x1, y1), last = self.links[-1]
            if last == target and (y0, y1) == rect[1::2] and abs(x1 - rect[0]) < 0.5:
                self.links[-1] = ((x0, y0, rect[2], y1), target)
                return
        self.links.append((rect, target))


@dataclass
class Flow:
    """Typeset pages, and each destination as (page index, top y)."""

    sheets: list[Sheet]
    destinations: dict[str, tuple[int, float]]


def typeset(
    blocks: Sequence[Block], size: PageSize, *, missing: set[str] | None = None
) -> Flow:
    """Lay `blocks` out from the top of a fresh page."""
    setter = _Typesetter(size, missing)
    for index, block in enumerate(blocks):
        setter.place(block, blocks[index + 1:index + 1 + _LOOKAHEAD])
    return Flow(setter.sheets, setter.destinations)


def decorate(
    sheet: Sheet, size: PageSize, *, left: str, right: str, folio: str
) -> None:
    """The running head, `left` and `right` above a hairline, and the folio."""
    content, style = sheet.content, _RUNNING
    font = style.font
    y = size.height - _HEAD
    width = size.width - 2 * _SIDE
    head = _clip(encode(left)[0], font, style.size, width / 2)
    content.text(_SIDE, y, font.resource, style.size, head, style.color)
    room = width - font.measure(head, style.size) - 3 * _INDENT
    title = _clip(encode(right)[0], font, style.size, room)
    width = font.measure(title, style.size)
    content.text(size.width - _SIDE - width, y, font.resource, style.size, title,
                 style.color)
    content.line(_SIDE, y - 6, size.width - _SIDE, y - 6, color=_RULE)
    number, _ = encode(folio)
    width = font.measure(number, style.size)
    content.text((size.width - width) / 2, _FOLIO, font.resource, style.size, number,
                 style.color)


# -- lines ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Run:
    """Encoded text in one font, size and link: a word, part of one, or a space."""

    data: bytes
    font: Font
    size: float
    target: Target | None
    width: float

    def cut(self, start: int, end: int) -> _Run:
        data = self.data[start:end]
        return _Run(data, self.font, self.size, self.target,
                    self.font.measure(data, self.size))


@dataclass(frozen=True)
class _Piece:
    x: float            # from the start of the line
    run: _Run


@dataclass(frozen=True)
class _Line:
    pieces: tuple[_Piece, ...]
    width: float


_Word = tuple[_Run | None, list[_Run]]   # the space before it, and its runs


def _baseline(style: Style) -> float:
    """From the top of a line box to its baseline, centring the font's extent."""
    font = style.font
    extent = (font.ascent + font.descent) * style.size
    return (style.leading - extent) / 2 + font.ascent * style.size


def _atoms(runs: Sequence[_Run]) -> Iterator[_Run]:
    """A word's runs cut at every break opportunity inside them."""
    for run in runs:
        start = 0
        for part in _SPLIT.split(run.data):
            if part:
                yield run.cut(start, start + len(part))
                start += len(part)


def _chop(run: _Run, width: float) -> list[_Run]:
    """`run` cut into pieces no wider than `width`, at least a character each."""
    pieces: list[_Run] = []
    start, used = 0, 0.0
    for index in range(len(run.data)):
        advance = run.font.measure(run.data[index:index + 1], run.size)
        if index > start and used + advance > width + _EPSILON:
            pieces.append(run.cut(start, index))
            start, used = index, 0.0
        used += advance
    pieces.append(run.cut(start, len(run.data)))
    return pieces


def _assemble(runs: Sequence[_Run]) -> _Line:
    """A line from its runs, neighbours of one font, size and link merged."""
    merged: list[_Run] = []
    for run in runs:
        last = merged[-1] if merged else None
        if last is not None and (last.font, last.size, last.target) == (
            run.font, run.size, run.target
        ):
            merged[-1] = _Run(last.data + run.data, run.font, run.size, run.target,
                              last.width + run.width)
        else:
            merged.append(run)
    pieces: list[_Piece] = []
    x = 0.0
    for run in merged:
        pieces.append(_Piece(x, run))
        x += run.width
    return _Line(tuple(pieces), x)


def _fill(words: Sequence[_Word], width: float) -> list[_Line]:
    """Greedy line breaking: each word on the line it fits, long words split."""
    lines: list[list[_Run]] = [[]]
    used = 0.0
    for space, runs in words:
        gap = space.width if space is not None and lines[-1] else 0.0
        size = sum(run.width for run in runs)
        if used + gap + size <= width + _EPSILON:
            if gap and space is not None:
                lines[-1].append(space)
            lines[-1].extend(runs)
            used += gap + size
            continue
        if size <= width + _EPSILON:
            lines.append(list(runs))
            used = size
            continue
        for atom in _atoms(runs):
            if used + gap + atom.width > width + _EPSILON and lines[-1]:
                lines.append([])
                used, gap = 0.0, 0.0
            if gap and space is not None:
                lines[-1].append(space)
                used += gap
                gap = 0.0
            if used + atom.width <= width + _EPSILON:
                lines[-1].append(atom)
                used += atom.width
                continue
            *whole, last = _chop(atom, width)
            for chunk in whole:
                lines[-1].append(chunk)
                lines.append([])
            lines[-1].append(last)
            used = last.width
    return [_assemble(runs) for runs in lines if runs]


def _wrap_code(text: str, columns: int) -> list[str]:
    """Code lines no longer than `columns`, long ones wrapped at a space or
    punctuation in their second half where there is one, continuations
    indented past the original line."""
    out: list[str] = []
    for raw in text.expandtabs(4).splitlines() or [""]:
        line = raw.rstrip()
        indent = " " * min(len(line) - len(line.lstrip()) + _CODE_INDENT, columns // 2)
        while len(line) > columns:
            cut = next(
                (i for i in range(columns, columns // 2, -1)
                 if line[i - 1] in _CODE_BREAK or line[i] == " "),
                columns,
            )
            out.append(line[:cut].rstrip())
            line = indent + line[cut:].lstrip()
        out.append(line)
    return out


def _clip(data: bytes, font: Font, size: float, room: float) -> bytes:
    """`data`, or as much of both its ends as fits `room` around an ellipsis,
    which keeps a path's first directory and its file name."""
    if font.measure(data, size) <= room:
        return data
    for keep in range(len(data) - 1, 0, -1):
        head = (keep + 1) // 2
        clipped = data[:head] + _ELLIPSIS + data[len(data) - (keep - head):]
        if font.measure(clipped, size) <= room:
            return clipped
    return _ELLIPSIS


def _height(cells: Sequence[Sequence[_Line]], style: Style) -> float:
    """A row's text height: its tallest cell's, one line when all are empty."""
    return max(max((len(lines) for lines in cells), default=0), 1) * style.leading


# -- the typesetter ------------------------------------------------------------


class _Typesetter:
    """Places blocks one after another, breaking pages as they fill."""

    def __init__(self, size: PageSize, missing: set[str] | None) -> None:
        self.size = size
        self.left = _SIDE
        self.right = size.width - _SIDE
        self.width = self.right - self.left
        self.top = size.height - _TOP
        self.bottom = _BOTTOM
        self.missing = missing
        self.sheets: list[Sheet] = []
        self.destinations: dict[str, tuple[int, float]] = {}
        self.y = self.top
        self.gap = 0.0              # space owed below the last block
        self.new_page()

    # -- cursor ------------------------------------------------------------

    def new_page(self) -> None:
        self.sheets.append(Sheet())
        self.y = self.top
        self.gap = 0.0

    @property
    def fresh(self) -> bool:
        return self.y >= self.top

    @property
    def content(self) -> Content:
        return self.sheets[-1].content

    def room(self) -> float:
        return self.y - self.bottom

    def open(self, before: float) -> None:
        """Start a block below the larger of the owed and wanted space; at the
        top of a page, with none."""
        if not self.fresh:
            self.y -= max(self.gap, before)
        self.gap = 0.0

    def fit(self, height: float) -> None:
        """Turn the page unless `height` fits below the cursor."""
        if height > self.room() + _EPSILON and not self.fresh:
            self.new_page()

    def mark(self, name: str) -> None:
        if name and name not in self.destinations:
            top = min(self.y + _MARK_ABOVE, self.size.height)
            self.destinations[name] = (len(self.sheets) - 1, top)

    # -- text --------------------------------------------------------------

    def encode(self, text: str) -> bytes:
        data, lost = encode(text)
        if lost and self.missing is not None:
            self.missing.update(lost)
        return data

    def words(self, spans: Spans, style: Style) -> list[_Word]:
        """`spans` as words, each with the space before it. A word runs on
        across spans until whitespace, so `Decimal`, stays one word."""
        words: list[_Word] = []
        current: list[_Run] = []
        space: _Run | None = None
        pending: _Run | None = None     # whitespace since the last word
        for span in spans:
            font = span.font or style.font
            scaled = font.monospaced and not style.font.monospaced
            size = style.size * style.code if scaled else style.size
            for token in _TOKENS.findall(span.text):
                if token[0] != "\xa0" and token.isspace():
                    if current:
                        words.append((space, current))
                        current = []
                    pending = _Run(b" ", font, size, span.target,
                                   font.measure(b" ", size))
                    continue
                if not current:
                    space = pending if words else None
                    pending = None
                data = self.encode(token)
                current.append(_Run(data, font, size, span.target,
                                    font.measure(data, size)))
        if current:
            words.append((space, current))
        return words

    def lines(self, spans: Spans, style: Style, width: float) -> list[_Line]:
        return _fill(self.words(spans, style), width)

    def put(self, line: _Line, x: float, top: float, style: Style) -> None:
        """Draw `line` in the line box whose top is `top`."""
        baseline = top - _baseline(style)
        sheet = self.sheets[-1]
        for piece in line.pieces:
            run = piece.run
            color = style.color if run.target is None else _LINK
            sheet.content.text(x + piece.x, baseline, run.font.resource, run.size,
                               run.data, color)
            if run.target is not None:
                left = x + piece.x
                sheet.link((left, top - style.leading, left + run.width, top),
                           run.target)

    def flow(self, lines: Sequence[_Line], x: float, style: Style) -> None:
        """Draw `lines` down from the cursor, turning pages as needed."""
        for line in lines:
            self.fit(style.leading)
            self.put(line, x, self.y, style)
            self.y -= style.leading

    # -- blocks ------------------------------------------------------------

    def place(self, block: Block, following: Sequence[Block]) -> None:
        """Set `block`; `following` are the next few blocks, which a heading
        or contents group label must not be parted from."""
        match block:
            case Heading():
                self.heading(block, following)
            case Paragraph():
                self.paragraph(block)
            case ListBlock():
                self.listing(block)
            case Code():
                self.code(block)
            case Table():
                self.table(block)
            case Figure():
                self.figure(block)
            case Entry():
                self.entry(block, following)
            case Rule():
                self.rule(block)
            case Space():
                self.y = max(self.y - block.height, self.bottom)

    def heading(self, block: Heading, following: Sequence[Block]) -> None:
        style = _HEADINGS[block.level]
        lines = self.lines(block.spans, style, self.width)
        rule = _underline(block.level)
        self.open(style.before)
        self.fit(len(lines) * style.leading + rule
                 + self.keep(style.after, following, block.level))
        self.mark(block.name)
        self.flow(lines, self.left, style)
        if rule:
            y = self.y - rule / 2
            self.content.line(self.left, y, self.right, y, color=_RULE, width=0.75)
            self.y -= rule
        self.gap = style.after

    def keep(self, after: float, following: Sequence[Block], level: int = 3) -> float:
        """How far below a block, which leaves `after` of space under it, its
        page must reach for `following` not to be parted from it: the space
        between them and the first part of the next block, and for a
        heading deeper than `level`, what that heading keeps with in turn."""
        if not following:
            return 0.0
        block = following[0]
        gap, height = self.opening(block)
        if isinstance(block, Heading) and block.level > level:
            height += self.keep(_HEADINGS[block.level].after, following[1:],
                                block.level)
        return max(after, gap) + height

    def opening(self, block: Block) -> tuple[float, float]:
        """The space `block` opens with, and the height of the part of it that
        must share a page with whatever comes before it."""
        match block:
            case Heading(level=level):
                style = _HEADINGS[level]
                lines = self.lines(block.spans, style, self.width)
                return style.before, len(lines) * style.leading + _underline(level)
            case Paragraph(style=style):
                return style.before, 2 * style.leading
            case ListBlock():
                return BODY.before, 2 * BODY.leading
            case Code():
                return _CODE_GAP, 2 * _CODE.leading + 2 * _CODE_PAD
            case Table():
                return _TABLE_GAP, self.table_start(block)
            case Figure():
                return _FIGURE_GAP, self.figure_height(self.drawing(block))
            case Entry():
                gap = _GROUP_GAP if block.group else _ENTRY_GAP
                return gap, len(self.entry_lines(block)) * BODY.leading
            case Rule():
                return 0.0, block.thickness
        return 0.0, 0.0

    def paragraph(self, block: Paragraph) -> None:
        style = block.style
        lines = self.lines(block.spans, style, self.width)
        if not lines:
            return
        self.open(style.before)
        self.fit(min(len(lines), 2) * style.leading)
        self.flow(lines, self.left, style)
        self.gap = style.after

    def listing(self, block: ListBlock) -> None:
        style = BODY
        last = f"{len(block.items)}.".encode("ascii")
        marker = (style.font.measure(last, style.size) + 5.0 if block.ordered
                  else _BULLET_BOX)
        before = style.before
        for number, item in enumerate(block.items, 1):
            indent = self.left + item.depth * _INDENT
            lines = self.lines(item.spans, style, self.right - indent - marker)
            if not lines:
                continue
            self.open(before)
            before = _ITEM_GAP
            self.fit(min(len(lines), 2) * style.leading)
            self.mark(item.name)
            sign = (f"{number}.".encode("ascii") if block.ordered
                    else _BULLETS[min(item.depth, len(_BULLETS) - 1)])
            self.content.text(indent, self.y - _baseline(style), style.font.resource,
                              style.size, sign, _MUTED)
            self.flow(lines, indent + marker, style)
        self.gap = style.after

    def code(self, block: Code) -> None:
        style = _CODE
        columns = max(int((self.width - 2 * _CODE_PAD) / (0.6 * style.size)), 16)
        lines = _wrap_code(block.text, columns)
        self.open(_CODE_GAP)
        self.fit(min(len(lines), 2) * style.leading + 2 * _CODE_PAD)
        done = 0
        while done < len(lines):
            fits = int((self.room() - 2 * _CODE_PAD + _EPSILON) // style.leading)
            if fits < 1 and not self.fresh:
                self.new_page()
                continue
            count = min(len(lines) - done, max(fits, 1))
            height = count * style.leading + 2 * _CODE_PAD
            self.content.rect(self.left, self.y - height, self.width, height,
                              fill=_SHADE, radius=2.0)
            self.y -= _CODE_PAD
            for text in lines[done:done + count]:
                if text:
                    self.content.text(self.left + _CODE_PAD, self.y - _baseline(style),
                                      CODE.resource, style.size, self.encode(text),
                                      style.color)
                self.y -= style.leading
            self.y -= _CODE_PAD
            done += count
            if done < len(lines):
                self.new_page()
        self.gap = _CODE_GAP

    def table(self, block: Table) -> None:
        widths = self.columns(block)
        header, head = self.header(block, widths)
        rows = [self.cells(row.cells, widths, _CELL) for row in block.rows]
        capacity = self.top - self.bottom - head
        self.open(_TABLE_GAP)
        self.fit(head + _first_part(rows[0] if rows else [], capacity))
        self.row(header, widths, _CELL_HEAD, None)
        for row, cells in zip(block.rows, rows, strict=True):
            height = _height(cells, _CELL) + 2 * _CELL_PAD
            if height > self.room() + _EPSILON and height <= capacity:
                self.new_page()
                self.row(header, widths, _CELL_HEAD, None)
            self.mark(row.name)
            self.row(cells, widths, _CELL, header)
        self.gap = _TABLE_GAP

    def table_start(self, block: Table) -> float:
        """The height a table must start with on its page: its header row
        and the first part of its first row."""
        widths = self.columns(block)
        _, head = self.header(block, widths)
        first = self.cells(block.rows[0].cells, widths, _CELL) if block.rows else []
        return head + _first_part(first, self.top - self.bottom - head)

    def header(
        self, block: Table, widths: Sequence[float]
    ) -> tuple[list[list[_Line]], float]:
        """A table's header row, as each column's lines, and its height."""
        cells = self.cells(tuple((Span(text),) for text in block.header), widths,
                           _CELL_HEAD)
        return cells, _height(cells, _CELL_HEAD) + 2 * _CELL_PAD

    def cells(
        self, cells: Sequence[Spans], widths: Sequence[float], style: Style
    ) -> list[list[_Line]]:
        return [self.lines(spans, style, width - 2 * _CELL_PAD)
                for spans, width in zip(cells, widths, strict=True)]

    def row(
        self,
        cells: Sequence[Sequence[_Line]],
        widths: Sequence[float],
        style: Style,
        header: Sequence[Sequence[_Line]] | None,
    ) -> None:
        """One table row, shaded when it is the header row (`header` None);
        a row taller than the page continues under a repeated header."""
        total = max((len(lines) for lines in cells), default=0) or 1
        done = 0
        while True:
            fits = int((self.room() - 2 * _CELL_PAD + _EPSILON) // style.leading)
            if fits < 1 and not self.fresh:
                self.new_page()
                if header is not None:
                    self.row(header, widths, _CELL_HEAD, None)
                continue
            count = min(total - done, max(fits, 1))
            height = count * style.leading + 2 * _CELL_PAD
            if header is None:
                self.content.rect(self.left, self.y - height, self.width, height,
                                  fill=_SHADE)
            x = self.left
            for lines, width in zip(cells, widths, strict=True):
                top = self.y - _CELL_PAD
                for line in lines[done:done + count]:
                    self.put(line, x + _CELL_PAD, top, style)
                    top -= style.leading
                x += width
            self.y -= height
            self.content.line(self.left, self.y, self.right, self.y, color=_RULE,
                              width=0.75 if header is None else 0.4)
            done += count
            if done >= total:
                return
            self.new_page()
            if header is not None:
                self.row(header, widths, _CELL_HEAD, None)

    def columns(self, block: Table) -> list[float]:
        """Column widths: natural widths when they fit; else the narrow
        columns' natural widths and, for the others, each one's longest
        unbreakable piece plus a share of the rest by need."""
        count = len(block.header)
        natural = [0.0] * count
        minimum = [0.0] * count
        heads = tuple((Span(text, BOLD),) for text in block.header)
        for cells in (heads, *(row.cells for row in block.rows)):
            for index, spans in enumerate(cells):
                words = self.words(spans, _CELL)
                width = sum(
                    (space.width if space is not None and position else 0.0)
                    + sum(run.width for run in runs)
                    for position, (space, runs) in enumerate(words)
                )
                widest = max((atom.width for _, runs in words for atom in _atoms(runs)),
                             default=0.0)
                natural[index] = max(natural[index], width)
                minimum[index] = max(minimum[index], widest)
        pad = 2 * _CELL_PAD + _EPSILON
        natural = [width + pad for width in natural]
        if sum(natural) <= self.width:
            natural[-1] += self.width - sum(natural)
            return natural
        fixed: dict[int, float] = {}            # narrow columns, at natural width
        while True:
            rest = [index for index in range(count) if index not in fixed]
            share = (self.width - sum(fixed.values())) / len(rest)
            narrow = {i: natural[i] for i in rest if natural[i] <= share}
            if not narrow or len(narrow) == len(rest):
                break
            fixed.update(narrow)
        low = {i: min(minimum[i] + pad, share) for i in rest}
        spare = self.width - sum(fixed.values()) - sum(low.values())
        wants = {i: max(natural[i] - low[i], 0.0) for i in rest}
        total = sum(wants.values()) or 1.0
        return [
            fixed[i] if i in fixed else low[i] + spare * wants[i] / total
            for i in range(count)
        ]

    def figure_height(self, drawing: diagram.Layout) -> float:
        return drawing.height * self.scale(drawing) + 2 * _FIGURE_PAD

    def scale(self, drawing: diagram.Layout) -> float:
        tall = (self.top - self.bottom) * _FIGURE_HEIGHT - 2 * _FIGURE_PAD
        wide = self.width - 2 * _FIGURE_PAD
        return min(1.0, wide / max(drawing.width, 1.0), tall / max(drawing.height, 1.0))

    def drawing(self, block: Figure) -> diagram.Layout:
        """The way to draw `block`: its graph's own, unless that would print
        the labels below the legible size and turned prints them larger."""
        own, turned = block.drawing, block.turned
        if turned is None or _DIAGRAM_SIZE * self.scale(own) >= _LABEL_MIN:
            return own
        return turned if self.scale(turned) > self.scale(own) else own

    def figure(self, block: Figure) -> None:
        drawing = self.drawing(block)
        scale = self.scale(drawing)
        height = self.figure_height(drawing)
        self.open(_FIGURE_GAP)
        self.fit(height)
        content = self.content
        content.rect(self.left, self.y - height, self.width, height, fill=_PANEL,
                     radius=3.0)
        x0 = self.left + (self.width - drawing.width * scale) / 2
        y0 = self.y - _FIGURE_PAD

        def point(x: float, y: float) -> tuple[float, float]:
            return x0 + x * scale, y0 - y * scale

        size = _DIAGRAM_SIZE * scale
        for box in drawing.boxes:
            x, y = point(box.x, box.y + box.height)
            width, tall = box.width * scale, box.height * scale
            content.rect(x, y, width, tall, fill=_BOX, stroke=_BOX_LINE, line=0.6,
                         radius=3.0 * scale)
            label, label_size = _label(self.encode(box.label), size,
                                       width - 2 * _DIAGRAM_PADDING * scale)
            label_width = REGULAR.measure(label, label_size)
            centre = (REGULAR.ascent - REGULAR.descent) / 2 * label_size
            baseline = y + tall / 2 - centre
            content.text(x + (width - label_width) / 2, baseline, REGULAR.resource,
                         label_size, label, _INK)
        for arrow in drawing.arrows:
            _arrow(content, [point(x, y) for x, y in arrow.points], scale)
        self.y -= height
        self.gap = _FIGURE_GAP

    def entry(self, block: Entry, following: Sequence[Block]) -> None:
        style = BODY
        indent = self.left + block.level * _INDENT
        lines = self.entry_lines(block)
        self.open(_GROUP_GAP if block.group else _ENTRY_GAP)
        keep = self.keep(0.0, following) if block.group else 0.0
        self.fit(len(lines) * style.leading + keep)
        top = self.y
        self.flow(lines, indent, style)
        if block.page:
            baseline = self.y + style.leading - _baseline(style)
            page = self.encode(block.page)
            width = style.font.measure(page, style.size)
            self.content.text(self.right - width, baseline, style.font.resource,
                              style.size, page, style.color)
            start = indent + (lines[-1].width if lines else 0.0) + _LEADER_GAP
            end = self.right - _NUMBERS - _LEADER_GAP
            step = style.font.measure(b" .", style.size)
            dots = int((end - start) // step)
            if dots > 0:
                self.content.text(end - dots * step, baseline, style.font.resource,
                                  style.size, b" ." * dots, _MUTED)
        if block.target is not None:
            self.sheets[-1].link((indent, self.y, self.right, top), block.target)

    def entry_lines(self, block: Entry) -> list[_Line]:
        """A contents entry's title as lines left of the page-number column."""
        indent = self.left + block.level * _INDENT
        span = Span(block.title, BOLD if block.group else None)
        return self.lines((span,), BODY, self.right - _NUMBERS - _LEADER_GAP - indent)

    def rule(self, block: Rule) -> None:
        self.open(0.0)
        self.fit(block.thickness)
        y = self.y - block.thickness / 2
        self.content.line(self.left, y, self.left + (block.width or self.width), y,
                          color=block.color, width=block.thickness)
        self.y -= block.thickness


def _underline(level: int) -> float:
    """The gap and hairline under a heading of `level`."""
    return _H1_RULE if level == 1 else 0.0


def _first_part(cells: Sequence[Sequence[_Line]], capacity: float) -> float:
    """How much of a table's first row must share a page with the header:
    all of it when it fits in `capacity`, a page below the header, as the
    row would otherwise move to the next page whole; else three lines."""
    if not cells:
        return 0.0
    height = _height(cells, _CELL) + 2 * _CELL_PAD
    return height if height <= capacity else 3 * _CELL.leading + 2 * _CELL_PAD


def _label(data: bytes, size: float, room: float) -> tuple[bytes, float]:
    """A diagram label and the size it is drawn at: shrunk to fit its box,
    but never below the smallest legible size, where a label still too wide
    is cut short with an ellipsis."""
    width = REGULAR.measure(data, size)
    if width > room + _EPSILON:
        size *= room / width
    if size >= _LABEL_MIN:
        return data, size
    if REGULAR.measure(data, _LABEL_MIN) > room + _EPSILON:
        while (len(data) > 1
               and REGULAR.measure(data + _ELLIPSIS, _LABEL_MIN) > room + _EPSILON):
            data = data[:-1]
        data += _ELLIPSIS
    return data, _LABEL_MIN


def _arrow(
    content: Content, points: Sequence[tuple[float, float]], scale: float
) -> None:
    """A connector through `points` with a filled head at the last one."""
    (x1, y1), (x2, y2) = points[-2], points[-1]
    length = math.hypot(x2 - x1, y2 - y1)
    if length == 0:
        return
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    head = min(max(5.0 * scale, 2.5), length)
    half = head * 0.42
    bx, by = x2 - ux * head, y2 - uy * head
    path = [*points[:-1], (bx, by)]
    for (ax, ay), (cx, cy) in pairwise(path):
        content.line(ax, ay, cx, cy, color=_ARROW, width=0.7)
    content.polygon(
        [(x2, y2), (bx - uy * half, by + ux * half), (bx + uy * half, by - ux * half)],
        fill=_ARROW,
    )
