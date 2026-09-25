"""Mermaid-subset diagrams are read back and laid out without a library."""

from __future__ import annotations

import unittest
from itertools import pairwise

from devdocs.compose import mermaid
from devdocs.render.diagram import (
    Box,
    Graph,
    Layout,
    describe,
    layout,
    parse_mermaid,
    text_width,
    to_svg,
)


def width(text: str) -> float:
    return 6.0 * len(text)


def crosses(start: tuple[float, float], end: tuple[float, float], box: Box) -> bool:
    """Whether the segment enters the box's interior (Liang-Barsky clipping
    against the box shrunk by half a unit, so touching its sides is fine)."""
    (x0, y0), (x1, y1) = start, end
    dx, dy = x1 - x0, y1 - y0
    low, high = 0.0, 1.0
    for p, q in ((-dx, x0 - box.x - 0.5), (dx, box.x + box.width - 0.5 - x0),
                 (-dy, y0 - box.y - 0.5), (dy, box.y + box.height - 0.5 - y0)):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            low = max(low, t)
        else:
            high = min(high, t)
        if low > high:
            return False
    return True


def on_side(point: tuple[float, float], box: Box) -> bool:
    x, y = point
    inside_x = box.x - 0.01 <= x <= box.x + box.width + 0.01
    inside_y = box.y - 0.01 <= y <= box.y + box.height + 0.01
    on_x = abs(x - box.x) < 0.01 or abs(x - box.x - box.width) < 0.01
    on_y = abs(y - box.y) < 0.01 or abs(y - box.y - box.height) < 0.01
    return inside_x and inside_y and (on_x or on_y)


def tangle(direction: str, count: int = 15) -> Graph:
    """A deterministic graph with long edges, a cycle and mutual pairs."""
    nodes = tuple((f"n{i}", f"node{i}.py") for i in range(count))
    edges = [(f"n{i}", f"n{(i * 7 + 3) % count}") for i in range(count)]
    edges += [(f"n{i}", f"n{(i * 5 + 1) % count}") for i in range(0, count, 2)]
    edges += [("n1", "n4"), ("n4", "n1")]
    return Graph(direction, nodes, tuple(dict.fromkeys(edges)))


class TestParse(unittest.TestCase):
    def test_round_trip_of_composer_output(self) -> None:
        text = mermaid(["a", "b"], [("a", "b")], labels={"a": 'say "hi" #1'})
        graph = parse_mermaid(text)
        self.assertEqual(graph, Graph("LR", (("n0", 'say "hi" #1'), ("n1", "b")),
                                      (("n0", "n1"),)))

    def test_direction_and_implicit_nodes(self) -> None:
        graph = parse_mermaid("flowchart TB\n  x --> y\n  x --> y\n")
        assert graph is not None
        self.assertEqual(graph.direction, "TD")
        self.assertEqual(graph.nodes, (("x", "x"), ("y", "y")))
        self.assertEqual(graph.edges, (("x", "y"),))       # duplicates collapse

    def test_anything_outside_the_subset_is_refused(self) -> None:
        for text in ("", "sequenceDiagram\n a->>b: hi", "graph LR\n a -.-> b",
                     "pie\n"):
            with self.subTest(text=text):
                self.assertIsNone(parse_mermaid(text))

    def test_entity_codes_beyond_unicode_stay_as_written(self) -> None:
        graph = parse_mermaid('graph LR\n  a["#1114112; #99999999999999999999;"]')
        assert graph is not None
        self.assertEqual(graph.nodes,
                         (("a", "#1114112; #99999999999999999999;"),))

    def test_describe_says_what_the_drawing_shows(self) -> None:
        graph = Graph("LR", (("a", "api.py"), ("b", "cart.py")), (("a", "b"),))
        self.assertEqual(describe(graph), "Arrows: api.py → cart.py.")
        self.assertEqual(describe(Graph("LR", (("a", "A"), ("b", "B")), ())),
                         "Boxes: A, B.")
        self.assertEqual(describe(Graph("LR", (), ())), "")


class TestLayout(unittest.TestCase):
    def test_layers_follow_edges(self) -> None:
        graph = Graph("LR", (("a", "a"), ("b", "b"), ("c", "c")),
                      (("a", "b"), ("b", "c"), ("a", "c")))
        boxes = {b.id: b for b in layout(graph, measure=width).boxes}
        self.assertLess(boxes["a"].x, boxes["b"].x)
        self.assertLess(boxes["b"].x, boxes["c"].x)        # longest path wins

    def test_cycles_are_drawn_not_rejected(self) -> None:
        graph = Graph("TD", (("a", "a"), ("b", "b")), (("a", "b"), ("b", "a")))
        drawing = layout(graph, measure=width)
        self.assertEqual(len(drawing.arrows), 2)
        forward, back = drawing.arrows
        self.assertNotEqual(forward.points[0][0], back.points[-1][0])  # offset

    def test_boxes_never_overlap_and_layout_is_deterministic(self) -> None:
        nodes = tuple((f"n{i}", f"node {i}") for i in range(12))
        edges = tuple((f"n{i}", f"n{(i * 7 + 3) % 12}") for i in range(12))
        first = layout(Graph("LR", nodes, edges), measure=width)
        self.assertEqual(first, layout(Graph("LR", nodes, edges), measure=width))
        boxes = first.boxes
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                apart = (a.x + a.width <= b.x or b.x + b.width <= a.x
                         or a.y + a.height <= b.y or b.y + b.height <= a.y)
                self.assertTrue(apart, (a, b))

    def test_a_layout_is_the_same_on_every_python(self) -> None:
        """From 3.12 `sum` rounds floats differently; with it, 3.11 drew this
        top box at 73.3 and later versions at 73.4, its exact place rounded."""
        kids = ("money.py", "util.py", "auth.py")
        edges = [("app.py", kid) for kid in kids]
        graph = parse_mermaid(mermaid(["app.py", *kids], edges, direction="TD"))
        assert graph is not None
        svg = to_svg(layout(graph, font_size=12.0), font_size=12.0)
        self.assertIn('<rect x="73.4" y="0" width="52"', svg)

    def test_empty_graph(self) -> None:
        drawing = layout(Graph("LR", (), ()), measure=width)
        self.assertEqual((drawing.width, drawing.boxes), (0.0, ()))

    def test_long_edges_run_in_lanes_and_never_through_a_box(self) -> None:
        for direction in ("LR", "TD"):
            drawing = layout(tangle(direction))
            with self.subTest(direction=direction):
                self.assertTrue(any(len(a.points) > 2 for a in drawing.arrows))
                for arrow in drawing.arrows:
                    for start, end in pairwise(arrow.points):
                        for box in drawing.boxes:
                            self.assertFalse(crosses(start, end, box), (arrow, box))

    def test_arrows_leave_and_reach_the_sides_of_their_boxes(self) -> None:
        for direction in ("LR", "TD"):
            drawing = layout(tangle(direction))
            boxes = {box.id: box for box in drawing.boxes}
            with self.subTest(direction=direction):
                for arrow in drawing.arrows:
                    self.assertTrue(on_side(arrow.points[0], boxes[arrow.source]))
                    self.assertTrue(on_side(arrow.points[-1], boxes[arrow.target]))
                    for x, y in arrow.points:
                        self.assertTrue(0 <= x <= drawing.width + 0.01)
                        self.assertTrue(0 <= y <= drawing.height + 0.01)

    def test_arrows_meet_their_boxes_square_on_a_run_of_fixed_length(self) -> None:
        for direction, axis in (("LR", 1), ("TD", 0)):
            drawing = layout(tangle(direction), layer_gap=40.0)
            with self.subTest(direction=direction):
                for arrow in drawing.arrows:
                    for start, end in (arrow.points[:2], arrow.points[-2:]):
                        self.assertEqual(start[axis], end[axis])     # square on
                        run = abs(end[1 - axis] - start[1 - axis])
                        self.assertGreaterEqual(run, 8.0)            # 40 / 4 is 10

    def test_arrowheads_into_one_box_never_share_a_point(self) -> None:
        graph = Graph("LR", tuple((n, n) for n in "abcde"),
                      (("a", "e"), ("b", "e"), ("c", "e"), ("d", "e"), ("a", "b")))
        heads = [arrow.points[-1] for arrow in layout(graph).arrows
                 if arrow.target == "e"]
        self.assertEqual(len(set(heads)), 4)
        tails = {arrow.points[0] for arrow in layout(graph).arrows
                 if arrow.source == "a"}
        self.assertEqual(len(tails), 1)                    # a fan from one point

    def test_long_labels_are_cut_in_the_middle_keeping_the_full_text(self) -> None:
        label = "src/main/java/com/example/billing/invoice/render"
        graph = Graph("LR", (("a", label), ("b", "short")), (("a", "b"),))
        long, short = layout(graph, max_label=120).boxes
        self.assertTrue(long.label.startswith("src/"))
        self.assertTrue(long.label.endswith("render"))
        self.assertIn("\u2026", long.label)
        self.assertLessEqual(text_width(long.label), 120)
        self.assertEqual(long.title, label)
        self.assertEqual((short.label, short.title), ("short", ""))
        self.assertGreaterEqual(long.width, text_width(long.label))

    def test_labels_are_measured_in_helvetica_by_default(self) -> None:
        self.assertAlmostEqual(text_width("Hello", 10), 22.78)
        self.assertAlmostEqual(text_width("\u4e2d", 10), 10.0)     # wide: one em
        self.assertAlmostEqual(text_width("e\u0301", 10), 5.56)    # combining: none
        box, = layout(Graph("LR", (("a", "Hello"),), ()), font_size=10).boxes
        self.assertAlmostEqual(box.width, 24.0 + 2 * 8)            # 24 minimum


class TestSvg(unittest.TestCase):
    def test_labels_are_escaped_and_markers_scoped(self) -> None:
        graph = Graph("LR", (("a", "<script>&"), ("b", "b")), (("a", "b"),))
        svg = to_svg(layout(graph, measure=width), ident="d7")
        self.assertIn("&lt;script&gt;&amp;", svg)
        self.assertNotIn("<script>", svg)
        self.assertIn('id="d7-arrow"', svg)
        self.assertIn("url(#d7-arrow)", svg)
        self.assertTrue(svg.startswith("<svg") and svg.endswith("</svg>"))

    def test_a_titled_drawing_is_a_named_image(self) -> None:
        graph = Graph("LR", (("a", "a & b"), ("b", "b")), (("a", "b"),))
        svg = to_svg(layout(graph), ident="g", title="Map <1>",
                     description=describe(graph))
        self.assertIn('role="img" aria-labelledby="g-title" '
                      'aria-describedby="g-desc"', svg)
        self.assertIn('<title id="g-title">Map &lt;1&gt;</title>', svg)
        self.assertIn('<desc id="g-desc">Arrows: a &amp; b → b.</desc>', svg)
        untitled = to_svg(layout(graph))
        self.assertIn('aria-hidden="true"', untitled)
        self.assertNotIn("role=", untitled)

    def test_plain_outlines_without_a_stylesheet_and_valid_xml_text(self) -> None:
        graph = Graph("TD", (("a", "bad\x01name"), ("b", "x" * 60)), (("a", "b"),))
        svg = to_svg(layout(graph, max_label=60))
        self.assertIn('fill="none" stroke="currentColor"', svg)
        self.assertIn('fill="currentColor"', svg)
        self.assertIn("bad\ufffdname", svg)
        self.assertNotIn("\x01", svg)
        self.assertIn("<title>" + "x" * 60 + "</title>", svg)   # tooltip, full text
        self.assertIn('orient="auto"', svg)
        self.assertNotIn("-0,", svg)

    def test_negative_zero_is_written_as_zero(self) -> None:
        drawing = Layout(1.0, 1.0, (Box("a", "a", -0.04, -0.0, 1.0, 1.0),), ())
        self.assertIn('x="0" y="0"', to_svg(drawing))


if __name__ == "__main__":
    unittest.main()
