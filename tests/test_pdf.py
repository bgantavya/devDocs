"""PDF output: the bound and split renderers on small and large projects,
the strict checker against the rendered book broken on purpose, and the
renderer printing the HTML book with an external engine."""

from __future__ import annotations

import random
import re
import sys
import time
import types
import unittest
from collections.abc import Callable
from typing import Any
from unittest import mock

from devdocs.compose import mermaid
from devdocs.errors import ConfigError
from devdocs.render import Renderer
from devdocs.render.model import (
    DocKind,
    DocMeta,
    DocSet,
    Document,
    Item,
    Layout,
    Link,
    RunInfo,
    Section,
    SymbolDoc,
)
from devdocs.render.pdf import PdfRenderer, layout
from devdocs.render.pdf.check import PdfFile, check
from devdocs.render.pdf.fonts import CODE
from devdocs.render.pdf.writer import Name, Objects, Stream
from tests.test_markdown import shop
from tests.test_pdf_check import rewrite
from tests.test_pdf_external import printing
from tests.test_pdf_layout import A4, MARGIN, shown
from tests.test_pdf_writer import small_pdf

LETTER = layout.PAGE_SIZES["Letter"]


# -- helpers -------------------------------------------------------------------


def page_text(pdf: PdfFile, index: int) -> bytes:
    return b" ".join(item.text for item in shown(pdf.content(index)))


def text(value: object) -> str:
    """A PDF text string as Python text."""
    assert isinstance(value, bytes)
    if value.startswith(b"\xfe\xff"):
        return value[2:].decode("utf-16-be")
    return value.decode("latin-1")


def remote(pdf: PdfFile, index: int = 0) -> dict[bytes, dict[str, object]]:
    """Page `index`'s links to other files: destination to file spec."""
    actions = [pdf.dictionary(annot["A"]) for annot in pdf.annotations(index)
               if "A" in annot]
    found: dict[bytes, dict[str, object]] = {}
    for action in actions:
        destination = action["D"]
        assert isinstance(destination, bytes)
        found[destination] = pdf.dictionary(action["F"])
    return found


def rect(annot: dict[str, object]) -> tuple[float, float, float, float]:
    values = annot["Rect"]
    assert isinstance(values, list) and len(values) == 4
    x0, y0, x1, y1 = (float(v) for v in values if isinstance(v, int | float))
    return x0, y0, x1, y1


def assert_valid(case: unittest.TestCase, files: dict[str, bytes]) -> None:
    for path, data in files.items():
        with case.subTest(path=path):
            case.assertEqual(check(data), [])


def sym(path: str, qualname: str, kind: str, signature: str = "", summary: str = "",
        **kwargs: Any) -> SymbolDoc:
    return SymbolDoc(id=f"{path}#{qualname}", name=qualname.rsplit(".", 1)[-1],
                     qualname=qualname, kind=kind, signature=signature,
                     summary=summary, **kwargs)


def large(modules: int = 10, files: int = 14) -> DocSet:
    """About 150 documents: every section kind, diagrams, long code, long
    paths, cross links to pages and to symbols, and one character the
    standard fonts lack."""
    docset = DocSet(
        project="Large Project",
        description="A synthetic project big enough to need many pages.",
        run=RunInfo(generator="devdocs 4.0.0", generated_at="2026-11-02T09:30:00+01:00",
                    git_commit="0123456789abcdef0123"),
    )
    names = [f"src/pkg{index:02d}" for index in range(modules)]
    for number, module in enumerate(names):
        paths = [f"{module}/file_{index:02d}{'_part' * (index % 4)}.py"
                 for index in range(files)]
        for index, path in enumerate(paths):
            following = paths[(index + 1) % files]
            members = tuple(
                sym(path, f"Widget.method_{k}", "method",
                    f"def method_{k}(self, first_argument: int, second_argument: str"
                    f" = 'default', *, flag: bool = False) -> dict[str, list[int]]",
                    "Does one thing, well." if k % 2 else "")
                for k in range(6)
            )
            symbols: tuple[SymbolDoc, ...] = (
                sym(path, "Widget", "class", "class Widget(Base, metaclass=Registry)",
                    "Holds `state` and hands it to the `Engine`.", bases=("Base",),
                    decorators=("dataclass(frozen=True, slots=True)",),
                    members=members),
                sym(path, "run", "function",
                    "async def run(\n    config: Config,\n    *,\n    retries: int = 3,"
                    "\n) -> Result", "Runs the widget once.", is_async=True),
                sym(path, "LIMIT", "constant", "LIMIT = 100", "Upper bound."),
            )
            purpose = (
                "Keeps the widget registry for this package and answers lookups by "
                "name. Every entry is validated on registration, so a lookup never "
                "sees a half-built widget, and the registry is safe to share."
            )
            if number == 0 and index == 0:
                purpose += (" It reads " + "/".join(["deeply"] * 40) + "/config.toml"
                            " and logs 中 when it starts.")
                symbols += (sym(path, "pattern", "function",
                                "def pattern() -> str: return '" + "ab" * 200 + "'"),)
            if number == 0 and index == 1:
                purpose = "\n\n".join([purpose] * 40)
            sections = (
                Section("purpose", "Purpose", text=(purpose,)),
                Section("facts", "Facts", items=(
                    Item("Module", (Link(module, f"module:{module}"),)),
                    Item("Python"),
                    Item("120 lines"),
                    Item("Used by", tuple(Link(p, f"file:{p}") for p in paths[:5])),
                )),
                Section("public-surface", "Public surface", symbols=symbols),
                Section("dependencies", "Dependencies", items=(
                    Item("Internal", (
                        Link(following, f"file:{following}", "sym-Widget-method_1"),
                        Link(following, f"file:{following}", "sym-no-such-anchor"),
                    )),
                    Item("Standard library", (Link("json"), Link("re"))),
                )),
                Section("known-issues", "Known issues", items=(
                    Item("TODO: handle the empty registry", line=12),
                )),
            )
            docset.add(Document(
                id=f"file:{path}", kind=DocKind.FILE, title=path, path=path,
                summary="Keeps the widget registry for this package.",
                sections=sections,
                meta=DocMeta(source=path, language="python"),
            ))
        edges = [(paths[i], paths[(i * 3 + 1) % files]) for i in range(files)]
        docset.add(Document(
            id=f"module:{module}", kind=DocKind.MODULE, title=f"{module}/",
            path=f"{module}/index", summary="One package of widgets.",
            sections=(
                Section("purpose", "Purpose", text=("One package of widgets.",)),
                Section("facts", "Facts", items=(Item(f"{files} files"),)),
                Section("components", "Components", items=tuple(
                    Item("Keeps the widget registry.", (Link(p.rsplit("/", 1)[1],
                                                             f"file:{p}"),))
                    for p in paths
                )),
                Section("relationships", "Relationships", diagram=mermaid(
                    paths, edges, labels={p: p.rsplit("/", 1)[1] for p in paths}
                )),
            ),
            meta=DocMeta(source=module),
        ))
    docset.add(Document(
        id="architecture", kind=DocKind.ARCHITECTURE, title="Architecture",
        path="architecture", summary="Widgets in packages.",
        sections=(
            Section("overview", "Overview", text=("Widgets in packages.",)),
            Section("component-map", "Component map", items=tuple(
                Item("A package.", (Link(name, f"module:{name}"),
                                    Link(names[0], f"module:{names[0]}")))
                for name in names
            ), diagram=mermaid(names, [(n, names[0]) for n in names[1:]],
                               direction="TD")),
            Section("data-flow", "Data flow", ordered=True,
                    items=tuple(Item(f"Step {k}.") for k in range(12))),
        ),
    ))
    return docset


# -- check ---------------------------------------------------------------------


def neighbour(number: bytes) -> bytes:
    """Another object number of the same width."""
    value = int(number)
    other = value + 1 if len(str(value + 1)) == len(number) else value - 1
    return str(other).encode()


def corrupt(
    data: bytes, pattern: bytes, change: Callable[[re.Match[bytes]], bytes]
) -> bytes:
    """`data` with the first match of `pattern` rewritten to the same length."""
    match = re.search(pattern, data)
    assert match is not None, pattern
    replacement = change(match)
    assert len(replacement) == len(match.group()), (match.group(), replacement)
    return data[:match.start()] + replacement + data[match.end():]


def deep_pages(depth: int) -> bytes:
    """A valid page tree of `depth` nested /Pages nodes over one page."""
    objects = Objects()
    catalog = objects.reserve()
    nodes = [objects.reserve() for _ in range(depth)]
    page = objects.reserve()
    for index, ref in enumerate(nodes):
        node: dict[str, object] = {
            "Type": Name("Pages"),
            "Kids": [nodes[index + 1] if index + 1 < depth else page], "Count": 1,
        }
        if index:
            node["Parent"] = nodes[index - 1]
        objects.define(ref, node)
    objects.define(page, {
        "Type": Name("Page"), "Parent": nodes[-1], "MediaBox": [0, 0, 10, 10],
        "Resources": {}, "Contents": objects.add(Stream(b"")),
    })
    objects.define(catalog, {"Type": Name("Catalog"), "Pages": nodes[0]})
    return objects.serialize(root=catalog)


class TestCheckBook(unittest.TestCase):
    """The checker on the rendered shop book, whole and broken on purpose.
    The book needs the renderer, so these are here, not in test_pdf_check."""

    data: bytes

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = next(iter(PdfRenderer().render(shop()).values()))

    def test_valid_output_has_no_problems(self) -> None:
        self.assertEqual(check(self.data), [])
        self.assertEqual(check(small_pdf()), [])

    def test_shifted_xref_offset(self) -> None:
        broken = corrupt(self.data, rb"(\d{10}) 00000 n\r\n(?=\d{10} 00000 n)",
                         lambda m: b"%010d 00000 n\r\n" % (int(m.group(1)) + 1))
        problems = check(broken)
        self.assertTrue(any("xref entry for object 1 points at byte" in p
                            for p in problems), problems)

    def test_wrong_stream_length(self) -> None:
        broken = corrupt(self.data, rb"/Length (\d*[0-8])/",
                         lambda m: b"/Length %d/" % (int(m.group(1)) + 1))
        problems = check(broken)
        self.assertTrue(any("does not end at an end of line and endstream" in p
                            for p in problems), problems)

    def test_dangling_reference(self) -> None:
        broken = corrupt(self.data, rb"/Resources (\d+) 0 R",       # object 0 is free
                         lambda m: b"/Resources %s 0 R" % (b"0" * len(m.group(1))))
        problems = check(broken)
        self.assertTrue(any("refers to 0 0 R, which is not in the cross-reference "
                            "table" in p for p in problems), problems)

    def test_broken_outline_chain(self) -> None:
        broken = corrupt(self.data, rb"/Next (\d+) 0 R",
                         lambda m: b"/Next %s 0 R" % neighbour(m.group(1)))
        problems = check(broken)
        self.assertTrue(any(p.startswith("outline") for p in problems), problems)

    def test_page_count_that_does_not_match_the_pages(self) -> None:
        broken = corrupt(self.data, rb"/Type/Pages/Kids\[[^\]]*\]/Count (\d)",
                         lambda m: m.group()[:-1] + neighbour(m.group(1)))
        problems = check(broken)
        self.assertTrue(any("/Count" in p and "pages" in p for p in problems), problems)

    def test_link_to_a_destination_that_does_not_exist(self) -> None:
        broken = corrupt(self.data, rb"/Dest\(file:src/shop/api\.py\)",
                         lambda m: m.group().replace(b"api", b"apo"))
        problems = check(broken)
        self.assertTrue(
            any("destination" in p and "not defined" in p for p in problems), problems
        )

    def test_garbage_and_truncation_are_reported_not_raised(self) -> None:
        self.assertTrue(check(b"not a pdf at all"))
        self.assertTrue(check(b""))
        chance = random.Random(7)
        for _ in range(150):
            cut = chance.randrange(len(self.data))
            self.assertTrue(check(self.data[:cut]), cut)        # truncated
            noisy = bytearray(self.data)
            for _ in range(3):
                noisy[chance.randrange(len(noisy))] = chance.randrange(256)
            check(bytes(noisy))                                 # never raises

    def test_input_built_to_exhaust_a_parser_is_reported_not_raised(self) -> None:
        data = self.data
        end = data.rindex(b"startxref")
        hostile = {      # the trailer follows the xref table: edits keep offsets
            "nesting": data.replace(b"trailer\n<<",
                                    b"trailer\n<</X" + b"[" * 3000 + b"]" * 3000, 1),
            "integer": data.replace(b"trailer\n<<", b"trailer\n<</X " + b"9" * 5000, 1),
            "startxref": data[:end] + b"startxref\n" + b"9" * 5000 + b"\n%%EOF\n",
            "subsection": data.replace(b"xref\n0 ", b"xref\n0 " + b"9" * 4990, 1),
            "page tree": deep_pages(1500),
        }
        for name, broken in hostile.items():
            with self.subTest(name=name):
                self.assertTrue(check(broken))
        self.assertEqual(check(deep_pages(20)), [])

    def test_rewriting_alone_breaks_nothing(self) -> None:
        def nothing(objects: dict[int, Any], trailer: dict[str, Any]) -> None:
            pass

        self.assertEqual(check(rewrite(self.data, nothing)), [])


# -- renderer ------------------------------------------------------------------


def toc_pages(pdf: PdfFile) -> range:
    contents = pdf.destination(b"#contents")
    overview = pdf.destination(b"#overview")
    assert contents is not None and overview is not None
    return range(contents[0], overview[0])


class TestBound(unittest.TestCase):
    def test_shop_book(self) -> None:
        for size, box in (("A4", [0, 0, 595.28, 841.89]), ("Letter", [0, 0, 612, 792])):
            with self.subTest(size=size):
                files = PdfRenderer(page_size=size).render(shop())
                self.assertEqual(list(files), ["shop.pdf"])
                assert_valid(self, files)
                pdf = PdfFile(files["shop.pdf"])
                self.assertEqual(pdf.page(0)["MediaBox"], box)
                self.assertEqual(len(pdf.page_refs), 9)

    def test_every_document_has_a_destination_bookmark_and_contents_link(self) -> None:
        docset = shop()
        pdf = PdfFile(PdfRenderer().render(docset)["shop.pdf"])
        bookmarks = {item.destination for item in pdf.outline()}
        linked = {annot.get("Dest") for page in toc_pages(pdf)
                  for annot in pdf.annotations(page)}
        for document in docset.documents:
            name = document.id.encode()
            with self.subTest(document=document.id):
                self.assertIsNotNone(pdf.destination(name))
                self.assertIn(name, bookmarks)
                self.assertIn(name, linked)
        self.assertEqual([item.title for item in pdf.outline()][:3],
                         ["Contents", "Overview", "Architecture"])
        cart = next(item for item in pdf.outline() if item.title == "src/shop/cart.py")
        self.assertEqual(
            [child.title for child in cart.children],
            ["Public surface", "Behaviour", "Dependencies", "Known issues"],
        )

    def test_documents_come_in_the_order_the_overview_lists_them(self) -> None:
        # Regression: chapters followed the paths, so `app/index` put `app/`
        # before the root module's `index`, and `src/a/b/` before `src/a/`.
        docset = DocSet("demo")
        for source in ("app", "", "src/a/b", "src/a"):
            docset.add(Document(f"module:{source}", DocKind.MODULE,
                                f"{source}/" if source else "(root)",
                                Layout.module(source), meta=DocMeta(source=source)))
        pdf = PdfFile(PdfRenderer().render(docset)["demo.pdf"])
        self.assertEqual([item.title for item in pdf.outline()][2:],
                         ["(root)", "app/", "src/a/", "src/a/b/"])

    def test_a_code_span_shows_what_commonmark_shows(self) -> None:
        # Regression: a line break before the closing backticks was dropped
        # before the padding was judged, so `` f<LF>`` showed " f".
        purpose = Section("purpose", "Purpose", text=("Call `` f\n`` now.",))
        docset = DocSet("demo", [Document("file:a.py", DocKind.FILE, "a.py", "a.py",
                                          sections=(purpose,))])
        pdf = PdfFile(PdfRenderer().render(docset)["demo.pdf"])
        code = [item.text for index in range(len(pdf.page_refs))
                for item in shown(pdf.content(index)) if item.font == CODE.resource]
        self.assertEqual(code, [b"f"])

    def test_contents_numbers_are_where_the_documents_start(self) -> None:
        for docset in (shop(), large()):
            pdf = PdfFile(next(iter(PdfRenderer().render(docset).values())))
            checked = 0
            for index in toc_pages(pdf):
                items = shown(pdf.content(index))
                for annot in pdf.annotations(index):
                    _, y0, x1, y1 = rect(annot)
                    numbers = [item.text for item in items
                               if y0 <= item.y <= y1 and item.x > x1 - 40
                               and item.text.isdigit()]
                    target = pdf.destination(annot["Dest"])
                    assert target is not None
                    self.assertEqual(numbers, [b"%d" % (target[0] + 1)])
                    checked += 1
            self.assertEqual(checked, len(docset.documents) + 1)

    def test_running_heads_and_folios(self) -> None:
        pdf = PdfFile(PdfRenderer().render(shop())["shop.pdf"])
        self.assertNotIn(b"Page 1 of", page_text(pdf, 0))          # the cover
        last = page_text(pdf, len(pdf.page_refs) - 1)
        self.assertIn(b"Page 9 of 9", last)
        self.assertIn(b"src/shop/util.py", last)

    def test_running_heads_shorten_long_names_in_the_middle(self) -> None:
        name = "A Project Whose Name Goes On " * 8 + "End"
        docset = DocSet(name)
        title = "src/" + "very/deep/" * 12 + "module.py"
        docset.add(Document(f"file:{title}", DocKind.FILE, title, title))
        pdf = PdfFile(next(iter(PdfRenderer().render(docset).values())))
        head = [item for item in shown(pdf.content(3)) if item.y > A4.height - 50]
        self.assertEqual(len(head), 2)
        project, page = head
        self.assertTrue(project.text.startswith(b"A Project"))
        self.assertIn(b"\x85", project.text)                 # an ellipsis
        self.assertTrue(page.text.startswith(b"src/"))
        self.assertTrue(page.text.endswith(b"module.py"))
        self.assertLess(project.right, page.x)
        self.assertLessEqual(page.right, A4.width - MARGIN + 0.01)

    def test_links_to_symbols_land_on_the_symbol(self) -> None:
        docset = shop()
        cart = docset.get("file:src/shop/cart.py")
        assert cart is not None
        note = Section("notes", "Notes", items=(Item("See", (
            Link("add", "file:src/shop/cart.py", "sym-Cart-add"),
            Link("gone", "file:src/shop/cart.py", "sym-Gone"),
        )),))
        docset = DocSet("shop", [*(d for d in docset.documents if d.id != cart.id)])
        docset.add(Document(cart.id, cart.kind, cart.title, cart.path, cart.summary,
                            (*cart.sections, note), cart.meta))
        pdf = PdfFile(PdfRenderer().render(docset)["shop.pdf"])
        self.assertEqual(check(pdf.data), [])
        dests = [annot["Dest"] for page in range(len(pdf.page_refs))
                 for annot in pdf.annotations(page) if annot.get("Dest")]
        self.assertIn(b"file:src/shop/cart.py#sym-Cart-add", dests)
        self.assertNotIn(b"file:src/shop/cart.py#sym-Gone", dests)    # fell back
        add = pdf.destination(b"file:src/shop/cart.py#sym-Cart-add")
        page = pdf.destination(b"file:src/shop/cart.py")
        assert add is not None and page is not None
        self.assertEqual(add[0], page[0])
        self.assertLess(add[1], page[1])

    def test_information_dictionary(self) -> None:
        docset = shop()
        docset.run = RunInfo(generator="devdocs 4.0.0",
                             generated_at="2026-11-02T12:00:00+00:00", git_commit="abc")
        pdf = PdfFile(PdfRenderer().render(docset)["shop.pdf"])
        info = pdf.dictionary(pdf.trailer["Info"])
        self.assertEqual(info["Title"], b"shop")
        self.assertEqual(info["Subject"], b"A demo shop.")
        creator = info["Creator"]
        assert isinstance(creator, bytes)
        self.assertRegex(creator, rb"^devdocs \S+$")
        self.assertNotIn("CreationDate", info)
        cover = page_text(pdf, 0)
        self.assertIn(b"devdocs 4.0.0", cover)
        self.assertIn(b"abc", cover)
        self.assertNotIn(b"2026", cover)

    def test_regenerating_an_unchanged_project_rewrites_no_pdf(self) -> None:
        def run(stamp: str) -> DocSet:
            docset = shop()
            docset.run = RunInfo(generator="devdocs 4.0.0", generated_at=stamp,
                                 git_commit="abc123")
            return docset

        first = run("2026-11-02T15:42:03+00:00")
        again = run("2026-11-02T15:42:05+00:00")         # the same, seconds later
        for split in (False, True):
            with self.subTest(split=split):
                renderer = PdfRenderer(split=split)
                self.assertEqual(renderer.render(first), renderer.render(again))

    def test_bookmarks_and_information_show_titles_as_their_pages_do(self) -> None:
        docset = DocSet("proj\x01name", description="line one\x0cpage\x1b[0m")
        for path in ("src/evil\x1b[31m.py", "src/new\nline.py"):
            docset.add(Document(f"file:{path}", DocKind.FILE, path, path, sections=(
                Section("purpose", "Purpose", text=("Hi.",)),
                Section("behaviour", "Behaviour\x00\x18", text=("x",)),
            )))
        renderer = PdfRenderer()
        files = renderer.render(docset)
        assert_valid(self, files)
        pdf = PdfFile(files["proj-name.pdf"])
        info = pdf.dictionary(pdf.trailer["Info"])
        self.assertEqual((info["Title"], info["Subject"]),
                         (b"proj?name", b"line one page?[0m"))
        titles = {item.title: [child.title for child in item.children]
                  for item in pdf.outline()}
        self.assertEqual(titles["src/evil?[31m.py"], ["Behaviour??"])
        self.assertEqual(titles["src/new line.py"], ["Behaviour??"])
        self.assertIn("U+001B <control> in src/evil?[31m.py", renderer.warnings)

    def test_unreadable_diagram_is_shown_as_source(self) -> None:
        docset = DocSet("demo")
        source = "sequenceDiagram\n a->>b: hi"
        docset.add(Document(
            "architecture", DocKind.ARCHITECTURE, "Architecture", "architecture",
            sections=(Section("overview", "Overview", diagram=source),),
        ))
        pdf = PdfFile(PdfRenderer().render(docset)["demo.pdf"])
        self.assertIn(b"sequenceDiagram", page_text(pdf, 3))

    def test_book_names(self) -> None:
        names = (
            ("My Project!", "my-project.pdf"), ("", "documentation.pdf"),
            ("\u4e2d\u6587", "documentation.pdf"), ("Caf\u00e9 API", "cafe-api.pdf"),
        )
        for project, name in names:
            with self.subTest(project=project):
                self.assertEqual(list(PdfRenderer().render(DocSet(project))), [name])


class TestSplit(unittest.TestCase):
    def test_one_file_per_document_plus_the_overview(self) -> None:
        files = PdfRenderer(split=True).render(shop())
        self.assertEqual(list(files), [
            "pdf/architecture.pdf",
            "pdf/index.pdf",
            "pdf/root.pdf",                  # the root module, off index.pdf
            "pdf/src/shop/api.py.pdf",
            "pdf/src/shop/cart.py.pdf",
            "pdf/src/shop/index.pdf",
            "pdf/src/shop/util.py.pdf",
        ])
        assert_valid(self, files)

    def test_documents_claiming_one_file_are_numbered_in_reading_order(self) -> None:
        docset = DocSet("demo")
        for kind, path in ((DocKind.MODULE, "index"), (DocKind.FILE, "root"),
                           (DocKind.FILE, "index")):
            docset.add(Document(f"{kind}:{path}", kind, path, path))
        files = PdfRenderer(split=True).render(docset)
        self.assertEqual(list(files), ["pdf/index-2.pdf", "pdf/index.pdf",
                                       "pdf/root-2.pdf", "pdf/root.pdf"])
        assert_valid(self, files)

    def test_links_between_files_open_the_sibling_file(self) -> None:
        files = PdfRenderer(split=True).render(shop())
        pdf = PdfFile(files["pdf/src/shop/cart.py.pdf"])
        actions = [pdf.dictionary(annot["A"]) for annot in pdf.annotations(0)
                   if "A" in annot]
        remote = {(pdf.dictionary(a["F"])["F"], a["D"]) for a in actions}
        self.assertIn((b"index.pdf", b"module:src/shop"), remote)
        self.assertIn((b"api.py.pdf", b"file:src/shop/api.py"), remote)
        overview = PdfFile(files["pdf/index.pdf"])
        targets = {overview.dictionary(overview.dictionary(a["A"])["F"])["F"]
                   for a in overview.annotations(0)}
        self.assertIn(b"root.pdf", targets)
        self.assertIn(b"src/shop/cart.py.pdf", targets)
        module = PdfFile(files["pdf/src/shop/index.pdf"])
        parents = [module.dictionary(module.dictionary(a["A"])["F"])["F"]
                   for a in module.annotations(0) if "A" in a]
        self.assertIn(b"../../root.pdf", parents)

    def test_documents_that_tie_are_numbered_by_id(self) -> None:
        # Regression: documents alike in kind, source and path kept the order
        # they were added in, which chose the one written to `x.py-2.pdf`.
        twins = [Document(f"file:{name}", DocKind.FILE, "x.py", "x.py",
                          summary=f"Twin {name}.", meta=DocMeta(source="x.py"))
                 for name in ("b", "a")]
        for split in (False, True):
            with self.subTest(split=split):
                renderer = PdfRenderer(split=split)
                self.assertEqual(renderer.render(DocSet("demo", twins[::-1])),
                                 renderer.render(DocSet("demo", list(twins))))
        files = PdfRenderer(split=True).render(DocSet("demo", list(twins)))
        subjects = {}
        for path in ("pdf/x.py.pdf", "pdf/x.py-2.pdf"):
            pdf = PdfFile(files[path])
            subjects[path] = pdf.dictionary(pdf.trailer["Info"])["Subject"]
        self.assertEqual(subjects, {"pdf/x.py.pdf": b"Twin a.",
                                    "pdf/x.py-2.pdf": b"Twin b."})

    def test_names_differing_only_in_case_or_normalization_get_own_files(self) -> None:
        docset = DocSet("demo")
        for source in ("Index.js", "café.py", "café.py"):
            path = Layout(per_file=True).file(source)
            docset.add(Document(f"file:{source}", DocKind.FILE, source, path))
        files = PdfRenderer(split=True).render(docset)
        self.assertEqual(list(files), ["pdf/Index-2.pdf", "pdf/café.pdf",
                                       "pdf/café-2.pdf", "pdf/index.pdf"])
        assert_valid(self, files)

    def test_links_name_the_other_file_for_every_reader(self) -> None:
        # /F as PDFKit reads it, in Mac OS Roman where that can spell the
        # name; /UF in Unicode, as Acrobat, pdf.js and poppler read it
        expected = {
            "src/café.py": b"caf\x8e.py.pdf",
            "src/中文.py": "中文.py.pdf".encode(),
            "src/a b#c.py": b"a b#c.py.pdf",
        }
        docset = DocSet("demo")
        for path in expected:
            docset.add(Document(f"file:{path}", DocKind.FILE, path, path))
        docset.add(Document("file:src/main.py", DocKind.FILE, "src/main.py",
                            "src/main.py", sections=(Section(
                                "dependencies", "Dependencies",
                                items=(Item("Internal", tuple(
                                    Link(path, f"file:{path}") for path in expected
                                )),),
                            ),)))
        files = PdfRenderer(split=True).render(docset)
        assert_valid(self, files)
        specs = remote(PdfFile(files["pdf/src/main.py.pdf"]))
        for path, name in expected.items():
            with self.subTest(path=path):
                spec = specs[f"file:{path}".encode()]
                self.assertEqual(spec["F"], name)
                self.assertEqual(text(spec["UF"]), path.removeprefix("src/") + ".pdf")

    def test_undecodable_file_names_render_in_both_forms(self) -> None:
        name = b"caf\xe9.py".decode("utf-8", "surrogateescape")    # as os.walk has it
        key = f"file:{name}".encode("utf-8", "surrogatepass")
        docset = DocSet("demo")
        docset.add(Document(f"file:{name}", DocKind.FILE, name, name,
                            sections=(Section("purpose", "Purpose", text=("Hi.",)),)))
        docset.add(Document("file:main.py", DocKind.FILE, "main.py", "main.py",
                            sections=(Section("dependencies", "Dependencies", items=(
                                Item("Internal", (Link(name, f"file:{name}"),)),
                            )),)))
        bound = PdfRenderer().render(docset)
        assert_valid(self, bound)
        book = PdfFile(bound["demo.pdf"])
        self.assertIsNotNone(book.destination(key))
        self.assertIn(key, {item.destination for item in book.outline()})
        split = PdfRenderer(split=True).render(docset)
        assert_valid(self, split)
        self.assertIsNotNone(PdfFile(split[f"pdf/{name}.pdf"]).destination(key))
        spec = remote(PdfFile(split["pdf/main.py.pdf"]))[key]
        self.assertEqual(spec["F"], b"caf\xe9.py.pdf")      # the bytes on disk
        self.assertNotIn("UF", spec)                         # Unicode cannot spell it

    def test_each_file_names_its_own_document(self) -> None:
        files = PdfRenderer(split=True).render(shop())
        pdf = PdfFile(files["pdf/src/shop/cart.py.pdf"])
        info = pdf.dictionary(pdf.trailer["Info"])
        self.assertEqual(info["Title"], b"src/shop/cart.py")
        (top,) = pdf.outline()
        self.assertEqual((top.title, top.destination), ("src/shop/cart.py",
                                                        b"file:src/shop/cart.py"))
        self.assertIn(b"Page 1 of 1", page_text(pdf, 0))


class TestScale(unittest.TestCase):
    docset: DocSet

    @classmethod
    def setUpClass(cls) -> None:
        cls.docset = large()

    def test_large_project_renders_valid_compact_files_quickly(self) -> None:
        self.assertEqual(len(self.docset.documents), 151)
        for split in (False, True):
            for size in ("A4", "Letter"):
                with self.subTest(split=split, size=size):
                    started = time.monotonic()
                    files = PdfRenderer(split=split, page_size=size).render(self.docset)
                    self.assertLess(time.monotonic() - started, 30.0)
                    assert_valid(self, files)
                    self.assertLess(sum(map(len, files.values())), 3_000_000)
        self.assertEqual(len(files), 152)

    def test_every_page_keeps_its_text_inside_the_margins(self) -> None:
        for size in (A4, LETTER):
            files = PdfRenderer(page_size=size.name).render(self.docset)
            pdf = PdfFile(next(iter(files.values())))
            outside = [
                (index + 1, item.text[:30])
                for index in range(len(pdf.page_refs))
                for item in shown(pdf.content(index))
                if item.x < MARGIN - 0.01 or item.right > size.width - MARGIN + 0.01
                or not 20 < item.y < size.height - 20
            ]
            with self.subTest(size=size.name):
                self.assertEqual(outside, [])

    def test_rendering_is_deterministic(self) -> None:
        for split in (False, True):
            with self.subTest(split=split):
                first = PdfRenderer(split=split).render(self.docset)
                self.assertEqual(first, PdfRenderer(split=split).render(self.docset))

    def test_warnings_name_each_missing_character_and_its_document(self) -> None:
        renderer = PdfRenderer()
        renderer.render(self.docset)
        expected = ["U+4E2D CJK UNIFIED IDEOGRAPH-4E2D in src/pkg00/file_00.py"]
        self.assertEqual(renderer.warnings, expected)
        renderer.render(self.docset)
        self.assertEqual(renderer.warnings, expected)       # refilled, not appended
        renderer.render(shop())
        self.assertEqual(renderer.warnings, [])


class TestRenderer(unittest.TestCase):
    def test_renderer_contract(self) -> None:
        self.assertIsInstance(PdfRenderer(), Renderer)
        self.assertEqual(PdfRenderer.format, "pdf")
        self.assertEqual(PdfRenderer(page_size="letter").page_size, "Letter")

    def test_bad_options_are_configuration_errors(self) -> None:
        with self.assertRaises(ConfigError):
            PdfRenderer(page_size="A5")
        with self.assertRaises(ConfigError):
            PdfRenderer(engine="pandoc")
        with self.assertRaises(ConfigError):
            PdfRenderer(split=True, engine="chrome")

    def test_renderer_prints_the_html_book(self) -> None:
        rendered: list[tuple[bool, DocSet]] = []

        class Book:
            def __init__(self, *, book: bool = False) -> None:
                self.book = book

            def render(self, docset: DocSet) -> dict[str, bytes]:
                rendered.append((self.book, docset))
                return {"book.html": b"<html><head></head><body>book</body></html>"}

        stub = types.ModuleType("devdocs.render.html")
        stub.HtmlRenderer = Book  # type: ignore[attr-defined]
        docset = shop()
        renderer = PdfRenderer(engine="chrome")
        renderer.warnings.append("stale")
        with (
            mock.patch.dict(sys.modules, {"devdocs.render.html": stub}),
            printing(self, "chrome"),
        ):
            files = renderer.render(docset)
        self.assertEqual(files, {"shop.pdf": b"%PDF-1.4 printed"})
        self.assertEqual(rendered, [(True, docset)])
        self.assertEqual(renderer.warnings, [])


if __name__ == "__main__":
    unittest.main()
