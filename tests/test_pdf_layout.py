"""PDF layout: line breaking, pagination, tables and diagrams, read back from
the content streams it writes."""

from __future__ import annotations

import re
import unittest
from dataclasses import dataclass
from itertools import pairwise

from devdocs.compose import mermaid
from devdocs.render import diagram
from devdocs.render.pdf import layout
from devdocs.render.pdf.fonts import FONTS

A4 = layout.PAGE_SIZES["A4"]
CHAIN = ["handlers.py", "routing.py", "middleware.py", "sessions.py", "auth.py",
         "permissions.py", "serializers.py", "validators.py", "models.py", "queries.py",
         "storage.py", "cache.py"]
MARGIN = 56.0
RESOURCES = {font.resource: font for font in FONTS}


@dataclass(frozen=True)
class Shown:
    """One text-showing operation read back from a content stream."""

    font: str
    size: float
    x: float
    y: float
    text: bytes

    @property
    def right(self) -> float:
        return self.x + RESOURCES[self.font].measure(self.text, self.size)


_OPERATION = re.compile(
    rb"/(F\d) ([\d.]+) Tf"
    rb"|1 0 0 1 (-?[\d.]+) (-?[\d.]+) Tm \(((?:\\[\s\S]|[^\\)])*)\) Tj"
)
_ESCAPE = re.compile(rb"\\([0-7]{1,3}|[\s\S])")
_SIMPLE = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}


def _unescape(text: bytes) -> bytes:
    def one(match: re.Match[bytes]) -> bytes:
        code: bytes = match.group(1)
        if code[:1].isdigit():
            return bytes([int(code, 8)])
        return _SIMPLE.get(code, code)

    return _ESCAPE.sub(one, text)


def shown(content: bytes) -> list[Shown]:
    """Every string a content stream shows, with its font, size and place."""
    found: list[Shown] = []
    font, size = "", 0.0
    for match in _OPERATION.finditer(content):
        if match.group(1):
            font, size = match.group(1).decode(), float(match.group(2))
        else:
            found.append(Shown(font, size, float(match.group(3)),
                               float(match.group(4)), _unescape(match.group(5))))
    return found


def foot(page: list[Shown]) -> list[bytes]:
    """The texts on a page's lowest line, left to right."""
    low = min(item.y for item in page)
    return [item.text for item in sorted(page, key=lambda item: item.x)
            if abs(item.y - low) < 0.01]


def typeset(*blocks: layout.Block, size: layout.PageSize = A4,
            missing: set[str] | None = None) -> tuple[layout.Flow, list[list[Shown]]]:
    flow = layout.typeset(blocks, size, missing=missing)
    return flow, [shown(sheet.content.finish()) for sheet in flow.sheets]


def words(text: str) -> tuple[layout.Span, ...]:
    return (layout.Span(text),)


class TestLayout(unittest.TestCase):
    def assert_inside(
        self, pages: list[list[Shown]], size: layout.PageSize = A4
    ) -> None:
        for number, page in enumerate(pages, 1):
            for item in page:
                with self.subTest(page=number, text=item.text[:30]):
                    self.assertGreaterEqual(item.x, MARGIN - 0.01)
                    self.assertLessEqual(item.right, size.width - MARGIN + 0.01)

    def test_lines_fill_but_never_pass_the_text_width(self) -> None:
        text = " ".join(f"word{index}" for index in range(400))
        _, pages = typeset(layout.Paragraph(words(text)))
        self.assert_inside(pages)
        lines = pages[0]
        self.assertGreater(len(lines), 10)
        for line in lines[:-1]:
            self.assertGreater(line.right, A4.width - MARGIN - 45)   # filled lines

    def test_long_words_break_after_separators(self) -> None:
        path = "/".join(f"segment{index}" for index in range(40)) + "/file_name.py"
        _, pages = typeset(layout.Paragraph(words(f"See {path} for details.")))
        self.assert_inside(pages)
        lines = [item.text for item in pages[0]]
        self.assertGreater(len(lines), 2)
        for line in lines[:-1]:
            self.assertIn(line[-1:], (b"/", b"_", b"."), line)

    def test_unbreakable_runs_are_cut_as_a_last_resort(self) -> None:
        _, pages = typeset(layout.Paragraph(words("x" * 500)))
        self.assert_inside(pages)
        self.assertEqual(b"".join(item.text for item in pages[0]), b"x" * 500)

    def test_code_wraps_with_a_continuation_indent(self) -> None:
        signature = "def handler(" + ", ".join(f"argument_{i}: int = {i}"
                                               for i in range(30)) + ") -> None"
        _, pages = typeset(layout.Code(signature))
        self.assert_inside(pages)
        lines = [item.text for item in pages[0]]
        self.assertGreater(len(lines), 3)
        self.assertTrue(lines[0].startswith(b"def handler("))
        for line in lines[1:]:
            self.assertTrue(line.startswith(b"    ") and not line.startswith(b"     "))
        self.assertEqual(b"".join(line.strip() for line in lines).replace(b" ", b""),
                         signature.replace(" ", "").encode())

    def test_long_code_continues_on_the_next_page(self) -> None:
        code = "\n".join(f"line_{index} = {index}" for index in range(200))
        _, pages = typeset(layout.Code(code))
        self.assertGreater(len(pages), 2)
        self.assertEqual(sum(len(page) for page in pages), 200)

    def test_heading_stays_with_what_follows(self) -> None:
        for count in range(30, 40):        # the heading lands at every foot position
            filler = [layout.Paragraph(words("filler text")) for _ in range(count)]
            flow, pages = typeset(*filler, layout.Heading(2, words("Section"), "s"),
                                  layout.Paragraph(words("First line of the section.")))
            index, _ = flow.destinations["s"]
            texts = [item.text for item in pages[index]]
            with self.subTest(count=count):
                self.assertIn(b"Section", texts)
                self.assertIn(b"First line of the section.", texts)

    def test_headings_keep_with_subheadings_tables_figures_and_code(self) -> None:
        graph = diagram.parse_mermaid(mermaid(["api.py", "cart.py"],
                                              [("api.py", "cart.py")]))
        assert graph is not None
        code = layout.Code("def first(\n    a: int,\n) -> int")
        wide = words("A summary long enough to wrap onto a second line in its column. "
                     * 2)
        table = layout.Table(("File", "Summary"), (layout.Row((words("a.py"), wide)),))
        cases: dict[str, tuple[layout.Block, ...]] = {
            "subheading": (layout.Heading(2, words("Public surface")),
                           layout.Heading(3, words("first")), code),
            "table": (layout.Heading(2, words("Components")), table),
            "figure": (layout.Heading(2, words("Relationships")), layout.figure(graph)),
            "code": (layout.Heading(3, words("first")), code),
            "contents": (layout.Entry("Files"),
                         layout.Entry("src/a.py", "9", layout.Target("a"), level=1)),
        }
        labels = {b"Public surface", b"first", b"Components", b"Relationships",
                  b"Files"}
        for name, blocks in cases.items():
            for tenths in range(5600, 6900, 5):     # the last block crosses the foot
                _, pages = typeset(layout.Paragraph(words("x")),
                                   layout.Space(tenths / 10), *blocks)
                with self.subTest(case=name, space=tenths / 10):
                    self.assertFalse(set(foot(pages[0])) <= labels, foot(pages[0]))

    def test_a_table_header_keeps_with_a_tall_first_row(self) -> None:
        tall = layout.Row((words("cart.py"),
                           words("Validates each request against the schema. " * 12)))
        table = layout.Table(("File", "Summary"), (tall,))
        for tenths in range(5600, 6400, 5):
            _, pages = typeset(layout.Paragraph(words("x")), layout.Space(tenths / 10),
                               table)
            with self.subTest(space=tenths / 10):
                self.assertNotEqual(foot(pages[0]), [b"File", b"Summary"])

    def test_short_path_columns_keep_their_natural_width(self) -> None:
        responsibility = words("Turning a DocSet into files on disk, and keeping them "
                               "in step with the sources.")
        depends = words("devdocs, devdocs/index, devdocs/llm, devdocs/parse, "
                        "devdocs/render")
        paths = ("devdocs/prompts", "devdocs/render", "devdocs/index")
        rows = tuple(layout.Row((words(path), responsibility, depends))
                     for path in paths)
        _, pages = typeset(layout.Table(("Component", "Responsibility", "Depends on"),
                                        rows))
        self.assert_inside(pages)
        texts = [item.text for item in pages[0]]
        for path in paths:
            self.assertIn(path.encode(), texts)

    def test_long_chains_are_drawn_down_the_page_at_a_legible_size(self) -> None:
        graph = diagram.parse_mermaid(mermaid(CHAIN, list(pairwise(CHAIN))))
        assert graph is not None and graph.direction == "LR"
        _, pages = typeset(layout.figure(graph))
        (labels,) = pages
        self.assertEqual(sorted(item.text for item in labels),
                         sorted(name.encode() for name in CHAIN))      # none cut
        self.assertEqual(len({item.y for item in labels}), len(CHAIN))  # one a row
        self.assertGreaterEqual(min(item.size for item in labels), 7.0)

    def test_diagram_labels_never_print_below_the_legible_size(self) -> None:
        names = [f"{index}/{name}" for index in range(4) for name in CHAIN]
        graph = diagram.parse_mermaid(mermaid(names, list(pairwise(names))))
        assert graph is not None
        _, pages = typeset(layout.figure(graph))       # too long either way
        (labels,) = pages
        self.assertEqual(len(labels), len(names))
        self.assertEqual({item.size for item in labels}, {4.5})
        self.assertTrue(all(item.text.endswith(b"\x85") for item in labels))

    def test_tables_repeat_their_header_on_every_page(self) -> None:
        rows = tuple(layout.Row((words(f"name{index}"), words("summary " * 12)))
                     for index in range(150))
        _, pages = typeset(layout.Table(("Name", "Summary"), rows))
        self.assertGreater(len(pages), 2)
        self.assert_inside(pages)
        for page in pages:
            self.assertEqual(page[0].text, b"Name")

    def test_diagrams_are_scaled_into_the_text_width(self) -> None:
        names = [f"a_rather_long_module_name_{index}" for index in range(14)]
        graph = diagram.parse_mermaid(
            mermaid(names, list(pairwise(names)))
        )
        assert graph is not None
        _, pages = typeset(layout.figure(graph))
        self.assertEqual(len(pages), 1)
        self.assert_inside(pages)
        self.assertEqual(len(pages[0]), 14)

    def test_missing_characters_are_collected_and_shown_as_question_marks(self) -> None:
        missing: set[str] = set()
        _, pages = typeset(layout.Paragraph(words("a\u4e2db")), missing=missing)
        self.assertEqual(missing, {"\u4e2d"})
        self.assertEqual(pages[0][0].text, b"a?b")


if __name__ == "__main__":
    unittest.main()
