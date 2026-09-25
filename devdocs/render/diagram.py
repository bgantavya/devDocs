"""Diagrams laid out without a diagram library.

The composer describes graphs in a small subset of Mermaid — `graph LR` or
`graph TD`, quoted node labels, `-->` edges — which Markdown hosts draw
natively. HTML and PDF have no Mermaid runtime, so this module reads that
subset back and lays the graph out itself, in the layered style: nodes are
put in layers by longest path; an edge that spans several layers gets a
waypoint in each layer it crosses, so it runs in a lane of its own instead of
through the boxes in between; each layer is ordered by the barycentre of its
neighbours to cut crossings; then everything is placed on a grid, each box
centred in its layer. The result is plain geometry, boxes and polyline
arrows, which the HTML renderer turns into inline SVG and the PDF renderer
into vector paths, so diagrams work offline and look the same in both.

Labels are measured with Helvetica's widths, which Arial and Liberation Sans
share: the SVG asks for those fonts and the PDF's built-in font is Helvetica,
so a label fits its box in either. A label wider than the limit is shortened
in the middle, which keeps both ends of a path, and its full text is kept for
a tooltip.

Cycles are drawn, not rejected: for layering only, edges that close a cycle
are reversed, then drawn in their real direction.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from itertools import pairwise

__all__ = [
    "Arrow",
    "Box",
    "Graph",
    "Layout",
    "describe",
    "layout",
    "parse_mermaid",
    "text_width",
    "to_svg",
]

_HEADER = re.compile(r"^\s*(?:graph|flowchart)\s+(LR|RL|TD|TB|BT)\s*$")
_NODE = re.compile(r'^\s*(?P<id>[A-Za-z_][\w-]*)\["(?P<label>[^"]*)"\]\s*$')
_EDGE = re.compile(r"^\s*(?P<a>[A-Za-z_][\w-]*)\s*-->\s*(?P<b>[A-Za-z_][\w-]*)\s*$")
_ENTITY = re.compile(r"#(\d+|quot);")
_NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")

_FONTS = "Helvetica, Arial, Liberation Sans, sans-serif"
_ELLIPSIS = "…"
# Helvetica advance widths in thousandths of an em, ASCII 32 to 126 (Adobe AFM).
_WIDTHS = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)


@dataclass(frozen=True)
class Graph:
    direction: str                          # "LR" or "TD"
    nodes: tuple[tuple[str, str], ...]      # (id, label), in declaration order
    edges: tuple[tuple[str, str], ...]


def _unescape(label: str) -> str:
    def char(match: re.Match[str]) -> str:
        code = match.group(1)
        if code == "quot":
            return '"'
        try:
            return chr(int(code))
        except (ValueError, OverflowError):   # beyond Unicode: keep it as written
            return match.group()

    return _ENTITY.sub(char, label)


def parse_mermaid(text: str) -> Graph | None:
    """Read the subset `compose.mermaid` writes; None for anything else."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    header = _HEADER.match(lines[0])
    if header is None:
        return None
    direction = "LR" if header.group(1) in ("LR", "RL") else "TD"
    nodes: dict[str, str] = {}
    edges: list[tuple[str, str]] = []
    for line in lines[1:]:
        node = _NODE.match(line)
        if node is not None:
            nodes.setdefault(node.group("id"), _unescape(node.group("label")))
            continue
        edge = _EDGE.match(line)
        if edge is None:
            return None
        edges.append((edge.group("a"), edge.group("b")))
    for a, b in edges:
        nodes.setdefault(a, a)
        nodes.setdefault(b, b)
    return Graph(direction, tuple(nodes.items()), tuple(dict.fromkeys(edges)))


def describe(graph: Graph) -> str:
    """The graph in words, for readers who cannot see the drawing."""
    labels = dict(graph.nodes)
    arrows = [
        f"{labels.get(a, a)} → {labels.get(b, b)}" for a, b in graph.edges if a != b
    ]
    if arrows:
        return "Arrows: " + "; ".join(arrows) + "."
    return "Boxes: " + ", ".join(labels.values()) + "." if labels else ""


def text_width(text: str, font_size: float = 11.0) -> float:
    """`text`'s advance width in Helvetica at `font_size`.

    Outside ASCII a character takes an average Latin width, a wide East Asian
    one a full em and a combining mark nothing; an estimate, but a generous one.
    """
    total = 0
    for char in text:
        code = ord(char)
        if 32 <= code < 127:
            total += _WIDTHS[code - 32]
        elif char == _ELLIPSIS or unicodedata.east_asian_width(char) in ("W", "F"):
            total += 1000
        elif not unicodedata.combining(char):
            total += 556
    return total * font_size / 1000


@dataclass(frozen=True)
class Box:
    id: str
    label: str            # as drawn: shortened in the middle when too wide
    x: float
    y: float
    width: float
    height: float
    title: str = ""       # the full label, when `label` had to be shortened

    @property
    def centre(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2


@dataclass(frozen=True)
class Arrow:
    """An edge as a polyline from its source box to its target box: two points,
    or more where it runs through the lanes of the layers it crosses."""

    source: str
    target: str
    points: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class Layout:
    width: float
    height: float
    boxes: tuple[Box, ...]
    arrows: tuple[Arrow, ...]
    direction: str = "LR"


def _layers(ids: list[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    """Longest-path layering over the graph with cycle-closing edges reversed."""
    outgoing: dict[str, list[str]] = {node: [] for node in ids}
    for a, b in edges:
        if a != b:
            outgoing[a].append(b)

    acyclic: list[tuple[str, str]] = []
    state: dict[str, int] = {}          # 1 on the DFS stack, 2 finished
    for start in ids:
        if start in state:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        state[start] = 1
        while stack:
            node, index = stack[-1]
            if index < len(outgoing[node]):
                stack[-1] = (node, index + 1)
                child = outgoing[node][index]
                if state.get(child) == 1:
                    acyclic.append((child, node))     # back edge, reversed
                else:
                    acyclic.append((node, child))
                    if child not in state:
                        state[child] = 1
                        stack.append((child, 0))
            else:
                state[node] = 2
                stack.pop()

    layer = dict.fromkeys(ids, 0)
    incoming: dict[str, list[str]] = {node: [] for node in ids}
    for a, b in acyclic:
        incoming[b].append(a)
    order = _topological(ids, acyclic)
    for node in order:
        for parent in incoming[node]:
            layer[node] = max(layer[node], layer[parent] + 1)
    return layer


def _topological(ids: list[str], edges: list[tuple[str, str]]) -> list[str]:
    indegree = dict.fromkeys(ids, 0)
    children: dict[str, list[str]] = {node: [] for node in ids}
    for a, b in edges:
        indegree[b] += 1
        children[a].append(b)
    ready = [node for node in ids if indegree[node] == 0]
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for child in children[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    return order + [node for node in ids if node not in order]


def _lanes(
    edges: list[tuple[str, str]], layer: dict[str, int]
) -> tuple[list[tuple[str, ...]], list[tuple[str, str]]]:
    """A waypoint in every layer each edge crosses, adding it to `layer`; and
    the short edges that chain each edge through its waypoints.

    Waypoint ids start with NUL, which no node id read from Mermaid can.
    """
    chains: list[tuple[str, ...]] = []
    segments: list[tuple[str, str]] = []
    for index, (a, b) in enumerate(edges):
        step = 1 if layer[b] > layer[a] else -1
        crossed = range(layer[a] + step, layer[b], step)
        chain = tuple(f"\x00{index}.{level}" for level in crossed)
        layer.update(zip(chain, crossed, strict=True))
        segments.extend(pairwise((a, *chain, b)))
        chains.append(chain)
    return chains, segments


def _order(
    ranks: list[list[str]], edges: list[tuple[str, str]], sweeps: int = 4
) -> list[list[str]]:
    """Barycentre ordering: each layer sorted by its neighbours' mean position."""
    neighbours: dict[str, list[str]] = {}
    for a, b in edges:
        neighbours.setdefault(a, []).append(b)
        neighbours.setdefault(b, []).append(a)
    for sweep in range(sweeps):
        downward = sweep % 2 == 0
        indices = range(1, len(ranks)) if downward else range(len(ranks) - 2, -1, -1)
        for index in indices:
            reference = ranks[index - 1] if downward else ranks[index + 1]
            position = {node: i for i, node in enumerate(reference)}
            current = {node: i for i, node in enumerate(ranks[index])}

            def key(node: str, pos: dict[str, int] = position,
                    cur: dict[str, int] = current) -> tuple[float, int]:
                linked = [pos[n] for n in neighbours.get(node, []) if n in pos]
                centre = sum(linked) / len(linked) if linked else float(cur[node])
                return centre, cur[node]

            ranks[index] = sorted(ranks[index], key=key)
    return ranks


def _fit(label: str, limit: float, measure: Callable[[str], float]) -> str:
    """`label`, or as much of both its ends as fits `limit` around an ellipsis."""
    if measure(label) <= limit:
        return label
    low, high = 0, len(label) - 1       # characters kept; `low` always fits
    while low < high:
        middle = (low + high + 1) // 2
        if measure(_cut(label, middle)) <= limit:
            low = middle
        else:
            high = middle - 1
    return _cut(label, low)


def _cut(label: str, keep: int) -> str:
    head = (keep + 1) // 2
    return label[:head] + _ELLIPSIS + label[len(label) - (keep - head):]


def layout(
    graph: Graph,
    *,
    measure: Callable[[str], float] | None = None,
    font_size: float = 11.0,
    padding: float = 8.0,
    layer_gap: float = 44.0,
    node_gap: float = 14.0,
    max_label: float = 180.0,
) -> Layout:
    """Place every node and route every edge.

    `measure(label)` is a label's width at `font_size`, Helvetica's when not
    given. A label wider than `max_label` is shortened to fit; its box keeps
    the full text as `title`. Self-loops are not drawn.
    """
    labels: dict[str, str] = {}
    for node, label in graph.nodes:
        labels.setdefault(node, label)
    ids = list(labels)
    if not ids:
        return Layout(0.0, 0.0, (), (), graph.direction)
    width_of = measure or (lambda text: text_width(text, font_size))
    shown = {node: _fit(labels[node], max_label, width_of) for node in ids}
    edges = [(a, b) for a, b in graph.edges if a in labels and b in labels and a != b]

    layer = _layers(ids, edges)
    ranks: list[list[str]] = [[] for _ in range(max(layer.values()) + 1)]
    chains, segments = _lanes(edges, layer)
    for node in ids:
        ranks[layer[node]].append(node)
    for chain in chains:
        for waypoint in chain:
            ranks[layer[waypoint]].append(waypoint)
    ranks = _order(ranks, segments)

    height = font_size + 2 * padding
    sizes = {
        node: (max(width_of(shown[node]), 24.0) + 2 * padding, height) for node in ids
    }
    grid = _Grid(ranks, sizes, layer, horizontal=graph.direction == "LR",
                 layer_gap=layer_gap, node_gap=node_gap)
    boxes = tuple(
        grid.box(node, shown[node], labels[node] if shown[node] != labels[node] else "")
        for node in ids
    )
    ports = grid.ports(edges, chains)
    arrows = tuple(
        Arrow(a, b, grid.route(a, b, chain, ends))
        for (a, b), chain, ends in zip(edges, chains, ports, strict=True)
    )
    width, total = grid.extent
    return Layout(width, total, boxes, arrows, graph.direction)


class _Grid:
    """Layer bands along the main axis (x for LR, y for TD), and where each
    node starts across it: what boxes sit on and arrows run through.

    Every band holds at least one box, since longest-path layering leaves no
    layer empty; a waypoint takes no room across the axis but the gap after it.
    Sizes are added with `math.fsum`, which rounds alike on every Python:
    `sum` rounds floats differently from 3.12 on, enough to move a box.
    """

    def __init__(
        self,
        ranks: list[list[str]],
        sizes: dict[str, tuple[float, float]],
        layer: dict[str, int],
        *,
        horizontal: bool,
        layer_gap: float,
        node_gap: float,
    ) -> None:
        self.horizontal = horizontal
        self.sizes = sizes
        self.layer = layer
        self.lead = min(8.0, layer_gap / 4)   # straight run into each gap
        self.bands: list[tuple[float, float]] = []
        offset = 0.0
        for rank in ranks:
            size = max(self.along(node) for node in rank if node in sizes)
            self.bands.append((offset, offset + size))
            offset += size + layer_gap
        spans = [
            math.fsum(self.across(node) for node in rank) + node_gap * (len(rank) - 1)
            for rank in ranks
        ]
        self.breadth = max(spans)
        self.cross: dict[str, float] = {}
        for rank, span in zip(ranks, spans, strict=True):
            position = (self.breadth - span) / 2      # each rank centred
            for node in rank:
                self.cross[node] = position
                position += self.across(node) + node_gap

    def along(self, node: str) -> float:
        width, height = self.sizes[node]
        return width if self.horizontal else height

    def across(self, node: str) -> float:
        if node not in self.sizes:
            return 0.0                  # a waypoint
        width, height = self.sizes[node]
        return height if self.horizontal else width

    def start(self, node: str) -> float:
        """Where a box starts along the main axis: centred in its band."""
        low, high = self.bands[self.layer[node]]
        return low + (high - low - self.along(node)) / 2

    def point(self, main: float, side: float) -> tuple[float, float]:
        return (main, side) if self.horizontal else (side, main)

    def box(self, node: str, label: str, title: str) -> Box:
        x, y = self.point(self.start(node), self.cross[node])
        width, height = self.sizes[node]
        return Box(node, label, x, y, width, height, title)

    def centre(self, node: str) -> float:
        return self.cross[node] + self.across(node) / 2

    def ports(
        self, edges: list[tuple[str, str]], chains: list[tuple[str, ...]]
    ) -> list[tuple[float, float]]:
        """Where each edge leaves its source and reaches its target, across
        the main axis.

        Ends that share a side of a box where an arrow arrives are spread
        evenly along it, in the order of where they head, so arrowheads never
        pile up and a mutual pair's two arrows run apart. A side that arrows
        only leave keeps them on its centre, fanning out from one point.
        """
        sides: dict[tuple[str, bool], list[tuple[float, int, int]]] = {}
        for index, ((a, b), chain) in enumerate(zip(edges, chains, strict=True)):
            forward = self.layer[b] > self.layer[a]
            toward_b = self.cross[chain[0]] if chain else self.centre(b)
            toward_a = self.cross[chain[-1]] if chain else self.centre(a)
            sides.setdefault((a, forward), []).append((toward_b, index, 0))
            sides.setdefault((b, not forward), []).append((toward_a, index, 1))
        ends = [[0.0, 0.0] for _ in edges]
        for (node, _), group in sides.items():
            group.sort()
            fan = all(end == 0 for _, _, end in group)
            for place, (_, index, end) in enumerate(group, 1):
                share = 0.5 if fan else place / (len(group) + 1)
                ends[index][end] = self.cross[node] + self.across(node) * share
        return [(source, target) for source, target in ends]

    def route(
        self, a: str, b: str, chain: tuple[str, ...], ends: tuple[float, float]
    ) -> tuple[tuple[float, float], ...]:
        """From `a`'s side facing `b`, straight out of its band, through the
        lane of each waypoint in `chain`, then straight into `b`'s band to
        `b`'s facing side; `ends` are the two ports.

        Only stretches between bands are slanted, and no box stands there. The
        straight runs reach a little into the gaps, so every arrow leaves and
        meets its boxes square, its head on a run of a fixed least length.
        """
        forward = self.layer[b] > self.layer[a]
        step = self.lead if forward else -self.lead
        side_a, side_b = ends

        def edge(node: str, entering: bool) -> float:
            low, high = self.bands[self.layer[node]]
            return low if entering == forward else high

        leave = self.start(a) + (self.along(a) if forward else 0.0)
        arrive = self.start(b) + (0.0 if forward else self.along(b))
        points = [self.point(leave, side_a), self.point(edge(a, False) + step, side_a)]
        for waypoint in chain:
            lane = self.cross[waypoint]
            points.append(self.point(edge(waypoint, True), lane))
            points.append(self.point(edge(waypoint, False), lane))
        points.append(self.point(edge(b, True) - step, side_b))
        points.append(self.point(arrive, side_b))
        return tuple(p for i, p in enumerate(points) if i == 0 or p != points[i - 1])

    @property
    def extent(self) -> tuple[float, float]:
        """The drawing's width and height."""
        return self.point(self.bands[-1][1], self.breadth)


def _f(value: float) -> str:
    text = f"{round(value, 1) + 0.0:.1f}"     # + 0.0 turns -0.0 into 0.0
    return text[:-2] if text.endswith(".0") else text


def _xml(text: str) -> str:
    """`text` escaped, with characters XML cannot hold made U+FFFD."""
    return escape(_NOT_XML.sub("\ufffd", text))


def to_svg(
    drawing: Layout,
    *,
    font_size: float = 11.0,
    margin: float = 4.0,
    ident: str = "d",
    title: str = "",
    description: str = "",
) -> str:
    """Inline SVG for `drawing`.

    Colours come from CSS (`currentColor` and the `.diagram` classes), so one
    drawing suits light and dark themes; without a stylesheet it falls back to
    plain outlines. `ident` prefixes the ids inside, so a page can hold several
    diagrams. With a `title` the drawing is an image named by it and described
    by `description`; without one it is hidden from assistive technology.
    """
    width = drawing.width + 2 * margin
    height = drawing.height + 2 * margin
    ref = _xml(ident)
    if title:
        role = f' role="img" aria-labelledby="{ref}-title"'
        if description:
            role += f' aria-describedby="{ref}-desc"'
    else:
        role = ' aria-hidden="true"'
    parts = [
        f'<svg class="diagram" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {_f(width)} {_f(height)}" width="{_f(width)}" '
        f'height="{_f(height)}" font-family="{_FONTS}"{role}>',
    ]
    if title:
        parts.append(f'<title id="{ref}-title">{_xml(title)}</title>')
        if description:
            parts.append(f'<desc id="{ref}-desc">{_xml(description)}</desc>')
    parts.append(
        f'<defs><marker id="{ref}-arrow" viewBox="0 0 8 8" refX="7" refY="4" '
        'markerWidth="7" markerHeight="7" orient="auto">'
        '<path d="M0,0 L8,4 L0,8 z" fill="currentColor"/></marker></defs>'
    )
    parts.append(f'<g transform="translate({_f(margin)},{_f(margin)})">')
    for arrow in drawing.arrows:
        path = " ".join(
            f"{'M' if i == 0 else 'L'}{_f(x)},{_f(y)}"
            for i, (x, y) in enumerate(arrow.points)
        )
        parts.append(f'<path class="edge" d="{path}" fill="none" '
                     f'stroke="currentColor" stroke-linejoin="round" '
                     f'marker-end="url(#{ref}-arrow)"/>')
    for box in drawing.boxes:
        cx, cy = box.centre
        tooltip = f"<title>{_xml(box.title)}</title>" if box.title else ""
        parts.append(
            f'<g class="node">{tooltip}<rect x="{_f(box.x)}" y="{_f(box.y)}" '
            f'width="{_f(box.width)}" height="{_f(box.height)}" rx="4" '
            f'fill="none" stroke="currentColor"/>'
            f'<text x="{_f(cx)}" y="{_f(cy)}" font-size="{_f(font_size)}" '
            f'text-anchor="middle" dominant-baseline="central" '
            f'fill="currentColor">{_xml(box.label)}</text></g>'
        )
    parts.append("</g></svg>")
    return "".join(parts)
