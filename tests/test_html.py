"""HTML rendering of the documentation model: page structure and parity with
Markdown, escaping of hostile text, link integrity over a whole site, the root
module's page, the book, the security policy and determinism."""

from __future__ import annotations

import json
import posixpath
import re
import tempfile
import unicodedata
import unittest
from dataclasses import replace
from html.parser import HTMLParser
from itertools import pairwise
from pathlib import Path
from urllib.parse import unquote, urlsplit

from devdocs.compose import mermaid
from devdocs.render import Renderer
from devdocs.render.html import HtmlRenderer
from devdocs.render.markdown import MarkdownRenderer
from devdocs.render.model import (
    DocKind,
    DocMeta,
    DocSet,
    Document,
    Item,
    Layout,
    Link,
    Mode,
    RunInfo,
    Section,
    SymbolDoc,
)
from devdocs.render.pdf import PdfRenderer
from devdocs.render.pdf.check import PdfFile
from devdocs.scan import Role, Scanner
from tests.test_markdown import file_doc, shop, sym
from tests.test_pdf_layout import shown

HOSTILE = "</script><img src=x onerror=alert(1)>"
SITE_POLICY = ("default-src 'none'; style-src 'self'; script-src 'self'; "
               "base-uri 'none'; form-action 'none'")
VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                  "link", "meta", "source", "track", "wbr"})
HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


class Page(HTMLParser):
    """A parsed page: each element with its attributes and the elements
    around it, ids, text, headings, and any tag that does not balance."""

    def __init__(self, data: bytes) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str], tuple[str, ...]]] = []
        self.ids: list[str] = []
        self.text: list[str] = []
        self.headings: list[tuple[str, str]] = []
        self.problems: list[str] = []
        self.stack: list[str] = []
        self.classes: list[str] = []      # class of each open element
        self._heading: list[str] | None = None
        self.feed(data.decode("utf-8"))
        self.close()
        if self.stack:
            self.problems.append(f"unclosed {self.stack}")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs)
        if tag not in VOID:
            self.stack.append(tag)
            self.classes.append(dict(attrs).get("class") or "")
        if tag in HEADINGS:
            self._heading = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs)

    def _open(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        context = tuple(self.stack) + tuple(f".{c}" for c in self.classes if c)
        self.elements.append((tag, attributes, context))
        if "id" in attributes:
            self.ids.append(attributes["id"])

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"</{tag}> while {self.stack[-1:]} is open")
            return
        self.stack.pop()
        self.classes.pop()
        if tag in HEADINGS and self._heading is not None:
            self.headings.append((tag, "".join(self._heading).strip()))
            self._heading = None

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._heading is not None:
            self._heading.append(data)

    def find(self, tag: str, **wanted: str) -> list[dict[str, str]]:
        """Attributes of every `tag` element whose attributes include `wanted`;
        `class_` names `class` and `aria_current` names `aria-current`."""
        wanted = {k.rstrip("_").replace("_", "-"): v for k, v in wanted.items()}
        return [
            attributes for name, attributes, _ in self.elements
            if name == tag and all(attributes.get(k) == v for k, v in wanted.items())
        ]

    def links(self, *, inside: str = "", outside: str = "") -> list[str]:
        """Every link's href, optionally only those within an element or class
        named `inside` and not within one named `outside`."""
        return [
            attributes["href"] for name, attributes, context in self.elements
            if name == "a" and "href" in attributes
            and (not inside or inside in context)
            and (not outside or outside not in context)
        ]

    def heading_texts(self, tag: str) -> list[str]:
        return [text for name, text in self.headings if name == tag]


def check_links(files: dict[str, bytes]) -> list[str]:
    """Every problem with the links of a rendered site: a reference that would
    leave it, or that names a file or an id it does not have; and every page
    whose tags do not balance or whose ids repeat."""
    pages = {path: Page(data) for path, data in files.items() if path.endswith(".html")}
    problems: list[str] = []
    for path, page in pages.items():
        problems.extend(f"{path}: {problem}" for problem in page.problems)
        repeated = sorted({i for i in page.ids if page.ids.count(i) > 1})
        if repeated:
            problems.append(f"{path}: repeated ids {repeated}")
        ids = set(page.ids)
        for tag, attributes, _ in page.elements:
            for name in ("href", "src"):
                if name in attributes:
                    problem = _resolve(path, attributes[name], files, pages)
                    if problem:
                        problems.append(f"{path}: <{tag} {name}> {problem}")
            for name in ("aria-labelledby", "aria-describedby", "aria-controls", "for"):
                for ident in attributes.get(name, "").split():
                    if ident not in ids:
                        problems.append(f"{path}: {name} names missing id {ident!r}")
            for ident in re.findall(r"url\(#([^)]*)\)", " ".join(attributes.values())):
                if ident not in ids:
                    problems.append(f"{path}: url() names missing id {ident!r}")
    return problems


def _resolve(
    path: str, reference: str, files: dict[str, bytes], pages: dict[str, Page]
) -> str:
    """"" when `reference`, on the page at `path`, names a generated file and,
    for a fragment, an id on it; else what is wrong."""
    parts = urlsplit(reference)
    if parts.scheme or parts.netloc or reference.startswith("/"):
        return f"leaves the site: {reference!r}"
    if parts.query:
        return f"carries a query: {reference!r}"
    target = path
    if parts.path:
        joined = posixpath.join(posixpath.dirname(path),
                                unquote(parts.path, errors="surrogateescape"))
        target = posixpath.normpath(joined)
        if target == ".." or target.startswith("../"):
            return f"climbs out of the site: {reference!r}"
    if target not in files:
        return f"names a missing file {target!r}"
    if parts.fragment:
        page = pages.get(target)
        if page is None or unquote(parts.fragment) not in page.ids:
            return f"names a missing id: {reference!r}"
    return ""


def render(*documents: Document, project: str = "demo", book: bool = False,
           description: str = "", run: RunInfo | None = None) -> dict[str, bytes]:
    docset = DocSet(project=project, description=description, run=run)
    for document in documents:
        docset.add(document)
    return HtmlRenderer(book=book).render(docset)


def page_of(files: dict[str, bytes], path: str) -> Page:
    return Page(files[path])


def text_of(files: dict[str, bytes], path: str) -> str:
    return files[path].decode("utf-8")


def stylesheet(css: str = "") -> dict[tuple[str, str], dict[str, str]]:
    """A stylesheet's declarations, the shipped one's by default, by (the
    block around them, or "" at the top level, and selector), each selector
    of a list on its own and later declarations winning. An @media block is
    named by its condition, any other by its at-rule, as `@page book` holds
    `@bottom-center`: enough CSS for blocks of plain rules."""
    css = css or text_of(HtmlRenderer().render(shop()), "assets/style.css")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules: dict[tuple[str, str], dict[str, str]] = {}
    blocks: list[str] = []
    for match in re.finditer(r"([^{}]*)([{}])", css):
        prelude, brace = " ".join(match.group(1).split()), match.group(2)
        if brace == "{":
            blocks.append(prelude)
            continue
        if not blocks:
            continue
        selectors = re.split(r"\s*,\s*(?![^()]*\))", blocks.pop())
        parts = (part.partition(":") for part in prelude.split(";"))
        declared = {
            name.strip(): value.strip() for name, _, value in parts if name.strip()
        }
        if not declared:
            continue
        around = blocks[-1].removeprefix("@media ") if blocks else ""
        for selector in selectors:
            rules.setdefault((around, selector), {}).update(declared)
    return rules


def colour(value: str, tokens: dict[str, str]) -> str:
    """The `#rrggbb` colour a declared value names, through its token."""
    token = re.search(r"var\((--[\w-]+)\)", value)
    return tokens[token.group(1)] if token else value


def contrast(first: str, second: str) -> float:
    """The WCAG 2 contrast ratio of two `#rrggbb` colours."""

    def luminance(hexa: str) -> float:
        r, g, b = (
            c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
            for c in (int(hexa[i:i + 2], 16) / 255 for i in (1, 3, 5))
        )
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class TestShopSite(unittest.TestCase):
    """The site for a small realistic project, page by page."""

    files: dict[str, bytes]

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = HtmlRenderer().render(shop())

    def test_one_page_per_document_a_home_page_and_shared_assets(self) -> None:
        self.assertEqual(list(self.files), [
            ".nojekyll",
            "architecture.html",
            "assets/filter.js",
            "assets/pages.js",
            "assets/style.css",
            "index.html",
            "root.html",
            "src/shop/api.py.html",
            "src/shop/cart.py.html",
            "src/shop/index.html",
            "src/shop/util.py.html",
        ])
        self.assertEqual(self.files[".nojekyll"], b"")

    def test_renderer_is_a_renderer(self) -> None:
        self.assertIsInstance(HtmlRenderer(), Renderer)
        self.assertEqual(HtmlRenderer.format, "html")

    def test_every_link_resolves(self) -> None:
        self.assertEqual(check_links(self.files), [])

    def test_a_file_page_has_landmarks_one_h1_and_ordered_headings(self) -> None:
        page = page_of(self.files, "src/shop/cart.py.html")
        self.assertEqual(page.find("html"), [{"lang": "en"}])
        self.assertEqual(page.heading_texts("h1"), ["src/shop/cart.py"])
        self.assertEqual(page.heading_texts("h2"), [
            "Public surface", "Behaviour", "Dependencies", "Known issues",
        ])
        self.assertEqual(page.heading_texts("h3"), ["Cart", "checkout (async)"])
        levels = [int(tag[1]) for tag, _ in page.headings]
        self.assertEqual(levels[0], 1)
        for previous, level in pairwise(levels):
            self.assertLessEqual(level, previous + 1)
        self.assertEqual(len(page.find("header", class_="masthead")), 1)
        self.assertEqual(len(page.find("main", id="content")), 1)
        labels = [a.get("aria-label") or a.get("aria-labelledby")
                  for a in page.find("nav")]
        self.assertEqual(labels, ["Breadcrumb", "toc-title", "Documentation"])
        first = page.elements[[t for t, _, _ in page.elements].index("body") + 1]
        self.assertEqual(first[:2], ("a", {"class": "skip-link", "href": "#content"}))

    def test_symbols_carry_markdown_anchors_listed_on_this_page(self) -> None:
        page = page_of(self.files, "src/shop/cart.py.html")
        self.assertEqual([i for i in page.ids if i.startswith("sym-")], [
            "sym-Cart", "sym-Cart-add", "sym-Cart-total", "sym-Cart-Line",
            "sym-Cart-Line-price", "sym-checkout", "sym-MAX_ITEMS", "sym-SEP",
        ])
        self.assertEqual(page.links(inside=".toc"), [
            "#public-surface", "#sym-Cart", "#sym-checkout", "#behaviour",
            "#dependencies", "#known-issues",
        ])

    def test_breadcrumbs_and_sidebar_place_the_page(self) -> None:
        page = page_of(self.files, "src/shop/cart.py.html")
        self.assertEqual(page.links(inside=".crumbs"),
                         ["../../index.html", "../../root.html", "index.html"])
        crumbs = "".join(page.text).split("Skip to content")[1]
        self.assertIn("shop(root)src/shopcart.py", crumbs)
        self.assertEqual(page.links(inside=".sidebar"), [
            "../../index.html", "../../architecture.html", "../../root.html",
            "index.html", "api.py.html", "cart.py.html", "util.py.html",
        ])
        current = page.find("a", aria_current="page")
        self.assertEqual([a["href"] for a in current], ["cart.py.html"])

    def test_the_sidebar_opens_only_the_current_module(self) -> None:
        home = page_of(self.files, "index.html")
        self.assertEqual(home.links(inside=".sidebar"), [
            "index.html", "architecture.html", "root.html", "src/shop/index.html",
        ])
        module = page_of(self.files, "src/shop/index.html")
        self.assertEqual(module.find("a", aria_current="page"),
                         [{"href": "index.html", "aria-current": "page"}])
        self.assertIn("api.py.html", module.links(inside=".sidebar"))

    def test_metadata_and_provenance(self) -> None:
        cart = page_of(self.files, "src/shop/cart.py.html")
        meta = {a["name"]: a["content"] for a in cart.find("meta") if "name" in a}
        self.assertEqual(meta, {
            "viewport": "width=device-width, initial-scale=1",
            "color-scheme": "light dark",
            "description": "Keeps a shopping cart and turns it into an order.",
            "devdocs:id": "file:src/shop/cart.py",
            "devdocs:kind": "file",
            "devdocs:source": "src/shop/cart.py",
            "devdocs:source_hash": "c4a7",
            "devdocs:language": "python",
            "devdocs:mode": "prose",
            "devdocs:model": "gemini-2.5-flash-lite",
            "devdocs:tier": "file",
            "devdocs:input_tokens": "900",
            "devdocs:output_tokens": "120",
            "devdocs:schema": "1.0.0",
        })
        self.assertIn("Written from source by devdocs; prose by gemini-2.5-flash-lite.",
                      cart.text)
        util = page_of(self.files, "src/shop/util.py.html")
        names = [a["name"] for a in util.find("meta") if "name" in a]
        self.assertIn("devdocs:confidence", names)
        self.assertNotIn("devdocs:model", names)
        self.assertIn("Written from source by devdocs; no model prose.", util.text)

    def test_home_page_lists_architecture_modules_and_files(self) -> None:
        home = page_of(self.files, "index.html")
        self.assertEqual(home.heading_texts("h1"), ["shop"])
        self.assertEqual(home.heading_texts("h2"), ["Modules", "Files"])
        self.assertEqual(home.links(inside="main", outside=".toc"), [
            "architecture.html", "root.html", "src/shop/index.html",
            "src/shop/api.py.html", "src/shop/cart.py.html", "src/shop/util.py.html",
        ])
        text = "".join(home.text)
        self.assertIn("A demo shop.", text)
        self.assertIn("Architecture — A small shop: an HTTP layer over a cart.", text)
        self.assertIn("Generated by devdocs; edit the source, not these pages.", text)
        self.assertEqual([a["name"] for a in home.find("meta")
                          if a.get("name", "").startswith("devdocs:")],
                         ["devdocs:schema"])

    def test_diagrams_are_inline_svg_named_for_assistive_technology(self) -> None:
        module = page_of(self.files, "src/shop/index.html")
        (svg,) = module.find("svg")
        self.assertEqual((svg["role"], svg["aria-labelledby"], svg["aria-describedby"]),
                         ("img", "diagram-1-title", "diagram-1-desc"))
        text = "".join(module.text)
        self.assertIn("Relationships diagram", text)
        self.assertIn("Arrows: api.py → cart.py.", text)
        architecture = page_of(self.files, "architecture.html")
        self.assertEqual(len(architecture.find("svg")), 1)


class TestSameContentAsMarkdown(unittest.TestCase):
    """Each page shows the Markdown page's sections, symbols and links."""

    def test_every_page_matches_its_markdown_page(self) -> None:
        docset = shop()
        markdown = {path: data.decode("utf-8")
                    for path, data in MarkdownRenderer().render(docset).items()}
        files = HtmlRenderer().render(docset)
        place = {d.id: "root.html" if d.path == Layout.module("") else f"{d.path}.html"
                 for d in docset.documents}
        by_md = {f"{d.path}.md": d for d in docset.documents}
        for document in docset.documents:
            with self.subTest(document=document.id):
                md = markdown[f"{document.path}.md"]
                page = page_of(files, place[document.id])
                self.assertEqual(
                    page.heading_texts("h2"),
                    [line[3:] for line in md.splitlines() if line.startswith("## ")],
                )
                self.assertEqual(
                    [i for i in page.ids if i.startswith("sym-")],
                    re.findall(r'<a id="(sym-[^"]*)"></a>', md),
                )
                directory = posixpath.dirname(document.path)
                md_targets = [
                    by_md[posixpath.normpath(posixpath.join(directory, href))].id
                    for href in re.findall(r"\]\(([^)#]+\.md)(?:#[^)]*)?\)", md)
                ]
                html_targets = [
                    next(d.id for d in docset.documents if place[d.id] == target)
                    for target in (
                        posixpath.normpath(posixpath.join(
                            posixpath.dirname(place[document.id]), href.split("#")[0]
                        ))
                        for href in page.links(inside=".body")
                        + page.links(inside=".intro", outside=".crumbs")
                        if not href.startswith("#")
                    )
                ]
                self.assertEqual(sorted(html_targets), sorted(md_targets))


class TestRootModule(unittest.TestCase):
    def test_the_root_module_is_root_html_and_the_home_page_keeps_index(self) -> None:
        files = HtmlRenderer().render(shop())
        root = page_of(files, "root.html")
        self.assertEqual(root.find("meta", name="devdocs:id"),
                         [{"name": "devdocs:id", "content": "module:"}])
        self.assertEqual(root.heading_texts("h1"), ["shop"])
        home = page_of(files, "index.html")
        self.assertEqual(home.find("meta", name="devdocs:id"), [])
        self.assertEqual(home.heading_texts("h2"), ["Modules", "Files"])
        package = page_of(files, "src/shop/index.html")
        self.assertIn("../../root.html", package.links(inside=".facts"))
        architecture = page_of(files, "architecture.html")
        self.assertIn("root.html", architecture.links(inside=".body"))

    def test_any_other_claim_on_a_taken_name_gets_a_numbered_page(self) -> None:
        root = Document(id="module:", kind=DocKind.MODULE, title="demo", path="index")
        index = file_doc("index")              # e.g. index.ts, per-file layout
        named_root = file_doc("root")          # e.g. root.go, per-file layout
        home_link = file_doc("main.py", Section("facts", "Facts", items=(
            Item("Links", (Link("i", "file:index"), Link("r", "file:root"),
                           Link("m", "module:"))),
        )))
        files = render(root, index, named_root, home_link)
        self.assertIn("root.html", files)
        self.assertIn("index-2.html", files)
        self.assertIn("root-2.html", files)
        self.assertEqual(Page(files["main.py.html"]).links(inside=".facts"),
                         ["index-2.html", "root-2.html", "root.html"])
        self.assertEqual(check_links(files), [])

    def test_names_differing_only_in_case_or_normalization_are_one_name(self) -> None:
        """macOS and Windows store `Root.html` and `root.html` as one file, so
        one page overwrote the other and its links opened the wrong document;
        macOS also takes the two encodings of `café` for one name."""
        root = Document(id="module:", kind=DocKind.MODULE, title="demo", path="index")
        per_file = [replace(file_doc(f"{stem}.py"), path=stem)
                    for stem in ("Root", "Index")]
        twins = [file_doc(name) for name in ("Cart.py", "cart.py",
                                             "café.py", "café.py")]
        main = file_doc("main.py", Section("facts", "Facts", items=(
            Item("Links", (Link("r", "file:Root.py"), Link("i", "file:Index.py"),
                           Link("m", "module:"), Link("c", "file:cart.py"))),
        )))
        files = render(root, *per_file, *twins, main)
        pages = sorted(path for path in files if path.endswith(".html"))
        self.assertEqual(pages, [
            "Cart.py.html", "Index-2.html", "Root-2.html", "café.py.html",
            "café.py-2.html", "cart.py-2.html", "index.html", "main.py.html",
            "root.html",
        ])
        folded = {unicodedata.normalize("NFC", page).casefold() for page in pages}
        self.assertEqual(len(folded), len(pages))
        self.assertEqual(Page(files["main.py.html"]).links(inside=".facts"),
                         ["Root-2.html", "Index-2.html", "root.html", "cart.py-2.html"])
        self.assertEqual(check_links(files), [])


class TestEscaping(unittest.TestCase):
    """Text from source or a model never becomes markup, anywhere."""

    def hostile_site(self, *, book: bool = False) -> dict[str, bytes]:
        symbol = SymbolDoc(
            id="h.py#x", name=HOSTILE, qualname=f"{HOSTILE}.x", kind="class",
            signature=f"class X({HOSTILE})", summary=f"Does `{HOSTILE}` {HOSTILE}",
            decorators=(HOSTILE,), bases=(HOSTILE,),
            members=(SymbolDoc(id="h.py#x.y", name="y", qualname="x.y",
                               kind="method", summary=HOSTILE),),
        )
        document = Document(
            id="file:h.py", kind=DocKind.FILE, title=f"h.py {HOSTILE}", path="h.py",
            summary=HOSTILE,
            sections=(
                Section("purpose", "Purpose", text=(HOSTILE,)),
                Section("facts", "Facts", items=(
                    Item(HOSTILE, (Link(HOSTILE, "file:h.py", HOSTILE),)),
                    Item("Elsewhere", (Link("x", "https://evil.example/"),
                                       Link("y", "javascript:alert(1)"))),
                )),
                Section("public-surface", "Public surface", symbols=(symbol,)),
                Section(f"{HOSTILE}-key", HOSTILE, text=(HOSTILE,),
                        diagram=mermaid(["a", "b"], [("a", "b")],
                                        labels={"a": HOSTILE, "b": '"b" #1'})),
            ),
            meta=DocMeta(mode=Mode.PROSE, source=f"h.py{HOSTILE}", model=HOSTILE,
                         language=HOSTILE, notes=(HOSTILE,)),
        )
        scheme = file_doc("javascript:alert(1).py")
        return render(document, scheme, project=HOSTILE, description=HOSTILE,
                      book=book, run=RunInfo(generator=HOSTILE, git_commit=HOSTILE))

    def test_hostile_text_stays_text_on_every_page(self) -> None:
        for book in (False, True):
            files = self.hostile_site(book=book)
            for path, data in files.items():
                if not path.endswith(".html"):
                    continue
                with self.subTest(book=book, path=path):
                    page = Page(data)
                    self.assertEqual(page.problems, [])
                    self.assertEqual(page.find("img"), [])
                    for tag, attributes, _ in page.elements:
                        self.assertFalse([a for a in attributes if a.startswith("on")])
                        self.assertNotIn("style", attributes)
                        if tag == "script":
                            self.assertTrue(attributes.get("src", "").endswith(".js"))
                    self.assertNotIn("<img", data.decode("utf-8"))
            home = Page(files["book.html" if book else "index.html"])
            self.assertIn(HOSTILE, "".join(home.text))

    def test_hostile_text_round_trips_through_attributes(self) -> None:
        files = self.hostile_site()
        page = Page(files["h.py.html"])
        meta = {a["name"]: a["content"] for a in page.find("meta") if "name" in a}
        self.assertEqual(meta["devdocs:note"], HOSTILE)
        self.assertEqual(meta["devdocs:model"], HOSTILE)
        self.assertEqual(meta["description"], HOSTILE)
        home = Page(files["index.html"])
        run = {a["name"]: a["content"] for a in home.find("meta") if "name" in a}
        self.assertEqual(run["devdocs:generator"], HOSTILE)
        self.assertEqual(run["devdocs:git_commit"], HOSTILE)
        text = "".join(page.text)
        self.assertIn(f"Written from source by devdocs; prose by {HOSTILE}.", text)
        self.assertIn(f"class X({HOSTILE})", text)
        self.assertEqual(page.heading_texts("h2")[-1], HOSTILE)
        self.assertIn(f"Arrows: {HOSTILE} → \"b\" #1.", text)

    def test_links_go_only_to_generated_pages(self) -> None:
        files = self.hostile_site()
        self.assertEqual(check_links(files), [])
        page = Page(files["h.py.html"])
        self.assertEqual(page.links(inside=".facts"), ["h.py.html"])  # no anchor
        home = Page(files["index.html"])
        hrefs = home.links(inside=".body")
        self.assertIn("javascript%3Aalert%281%29.py.html", hrefs)
        for href in hrefs:
            self.assertEqual(urlsplit(href).scheme, "")


class TestLinks(unittest.TestCase):
    def facts(self, *links: Link) -> list[str]:
        helper = SymbolDoc(id="lib.py#helper", name="helper", qualname="helper",
                           kind="function", signature="def helper()")
        hidden = SymbolDoc(id="lib.py#C.h", name="", qualname="", kind="method")
        holder = SymbolDoc(id="lib.py#C", name="C", qualname="C", kind="class",
                           members=(hidden,))
        library = file_doc("src/lib.py",
                           Section("api", "API", symbols=(helper, holder)))
        main = file_doc("src/app/main.py",
                        Section("facts", "Facts", items=(Item("Links", links),)),
                        Section("api", "API", symbols=(
                            SymbolDoc(id="m#run", name="run", qualname="run",
                                      kind="function"),)))
        files = render(main, library)
        self.assertEqual(check_links(files), [])
        return Page(files["src/app/main.py.html"]).links(inside=".facts")

    def test_targets_resolve_relative_to_the_page(self) -> None:
        self.assertEqual(self.facts(
            Link("lib", "file:src/lib.py"),
            Link("self", "file:src/app/main.py"),
            Link("gone", "file:missing.py"),
        ), ["../lib.py.html", "main.py.html"])

    def test_anchors_are_kept_only_where_the_target_page_has_them(self) -> None:
        self.assertEqual(self.facts(
            Link("helper", "file:src/lib.py", "sym-helper"),
            Link("run", "file:src/app/main.py", "sym-run"),
            Link("missing", "file:src/lib.py", "sym-missing"),
            Link("hidden", "file:src/lib.py", "sym-"),
        ), ["../lib.py.html#sym-helper", "#sym-run", "../lib.py.html",
            "../lib.py.html"])

    def test_odd_file_names_are_encoded_and_still_resolve(self) -> None:
        names = ["docs/my file (1)#a.py", "a|b.py", "café.py", "q?x.py",
                 "pct%41.py", "c:d.py", "bad\udcff.py"]
        files = render(*(file_doc(name) for name in names))
        self.assertEqual(check_links(files), [])
        home = Page(files["index.html"])
        self.assertEqual(home.links(inside=".body"), [
            "a%7Cb.py.html", "bad%FF.py.html", "c%3Ad.py.html", "café.py.html",
            "docs/my%20file%20%281%29%23a.py.html", "pct%2541.py.html",
            "q%3Fx.py.html",
        ])
        self.assertIn("bad\ufffd.py", "".join(home.text))

    def test_a_name_no_file_system_can_hold_still_renders(self) -> None:
        """A lone surrogate names no byte, so the writer refuses the path; the
        page still renders, linked the way Markdown links it."""
        files = render(file_doc("odd\ud800.py"))
        self.assertIn("odd\ud800.py.html", files)
        self.assertEqual(Page(files["index.html"]).links(inside=".body"),
                         ["odd%EF%BF%BD.py.html"])


class TestBook(unittest.TestCase):
    files: dict[str, bytes]
    page: Page

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = HtmlRenderer(book=True).render(shop())
        cls.page = Page(cls.files["book.html"])

    def test_one_self_contained_file(self) -> None:
        self.assertEqual(list(self.files), ["book.html"])
        self.assertEqual(self.page.problems, [])
        self.assertEqual(self.page.find("link"), [])
        self.assertEqual(self.page.find("script"), [])
        self.assertFalse([a for _, a, _ in self.page.elements if "src" in a])
        (policy,) = self.page.find("meta", http_equiv="Content-Security-Policy")
        self.assertEqual(policy["content"], "default-src 'none'; style-src "
                         "'unsafe-inline'; base-uri 'none'; form-action 'none'")

    def test_every_document_in_reading_order_after_the_overview(self) -> None:
        self.assertEqual(self.page.heading_texts("h1"), ["shop"])
        self.assertEqual(
            [a["id"] for a in self.page.find("article", class_="document")],
            ["architecture", "module", "module-src-shop", "file-src-shop-api-py",
             "file-src-shop-cart-py", "file-src-shop-util-py"],
        )
        self.assertEqual(self.page.heading_texts("h2"), [
            "Modules", "Files", "Architecture", "shop", "src/shop/",
            "src/shop/api.py", "src/shop/cart.py", "src/shop/util.py",
        ])
        self.assertIn("Public surface", self.page.heading_texts("h3"))
        self.assertIn("Cart", self.page.heading_texts("h4"))

    def test_links_are_in_page_anchors_that_exist(self) -> None:
        self.assertEqual(check_links(self.files), [])
        hrefs = self.page.links()
        self.assertTrue(hrefs)
        self.assertTrue(all(href.startswith("#") for href in hrefs))
        self.assertIn("#module-src-shop", hrefs)
        self.assertIn("file-src-shop-cart-py--sym-Cart-add", self.page.ids)
        self.assertEqual(len(self.page.find("svg")), 2)

    def test_print_rules_are_inlined(self) -> None:
        book = text_of(self.files, "book.html")
        style = book.split("<style>")[1].split("</style>")[0]
        self.assertIn("@page", style)
        self.assertIn("break-before: page", style)
        self.assertIn("@media print", style)

    def test_every_page_carries_the_folio_the_builtin_engine_prints(self) -> None:
        """Chrome and WeasyPrint printed the book without page numbers. The
        book is a named page whose foot reads "Page N of M", in the size and
        grey of the built-in engine's folio; a site page printed alone, not
        being one, has none."""
        book = text_of(self.files, "book.html")
        rules = stylesheet(book.split("<style>")[1].split("</style>")[0])
        folio = rules.get(("@page book", "@bottom-center"), {})
        self.assertEqual(folio.get("content"),
                         '"Page " counter(page) " of " counter(pages)')
        content = PdfFile(PdfRenderer().render(shop())["shop.pdf"]).content(1)
        (builtin,) = [item for item in shown(content) if item.text.startswith(b"Page ")]
        *_, grey = re.findall(rb"([\d.]+) ([\d.]+) ([\d.]+) rg",
                              content[:content.index(b"(Page ")])
        self.assertEqual(folio["font-size"], f"{builtin.size:g}pt")
        self.assertEqual(folio["color"],
                         "#" + "".join(f"{round(float(c) * 255):02x}" for c in grey))
        self.assertEqual(rules[("", ".book")]["page"], "book")
        self.assertEqual(self.page.find("body"), [{"class": "book"}])
        site = HtmlRenderer().render(shop())
        self.assertEqual(page_of(site, "index.html").find("body"), [{}])


class TestSecurityPolicy(unittest.TestCase):
    def test_every_site_page_declares_the_policy_before_loading_anything(self) -> None:
        files = HtmlRenderer().render(shop())
        for path, data in files.items():
            if not path.endswith(".html"):
                continue
            with self.subTest(path=path):
                page = Page(data)
                tags = [tag for tag, _, _ in page.elements]
                (policy,) = page.find("meta", http_equiv="Content-Security-Policy")
                self.assertEqual(policy["content"], SITE_POLICY)
                position = page.elements.index(
                    ("meta", policy, ("html", "head"))
                )
                self.assertLess(position, tags.index("link"))
                self.assertLess(position, tags.index("script"))
                self.assertNotIn("style", tags)
                for _, attributes, _ in page.elements:
                    self.assertNotIn("style", attributes)
                    self.assertFalse([a for a in attributes if a.startswith("on")])
                self.assertNotRegex(data.decode("utf-8"), r"<script(?![^>]*\bsrc=)")


class TestDeterminism(unittest.TestCase):
    def test_the_same_docset_renders_to_the_same_bytes(self) -> None:
        for book in (False, True):
            with self.subTest(book=book):
                first = HtmlRenderer(book=book).render(shop())
                self.assertEqual(HtmlRenderer(book=book).render(shop()), first)
                reversed_set = DocSet(project="shop", description="A demo shop.")
                for document in reversed(shop().documents):
                    reversed_set.add(document)
                self.assertEqual(HtmlRenderer(book=book).render(reversed_set), first)
                self.assertEqual(list(first), sorted(first))

    def test_documents_that_tie_are_named_and_listed_by_id(self) -> None:
        # Regression: documents alike in kind, source and path kept the order
        # they were added in, which chose the one that got `x.py-2.html`.
        twins = [Document(f"file:{name}", DocKind.FILE, "x.py", "x.py",
                          summary=f"Twin {name}.", meta=DocMeta(source="x.py"))
                 for name in ("b", "a")]
        for book in (False, True):
            with self.subTest(book=book):
                self.assertEqual(render(*twins[::-1], book=book),
                                 render(*twins, book=book))
        site = render(*twins)
        self.assertIn('content="file:a"', text_of(site, "x.py.html"))
        self.assertIn('content="file:b"', text_of(site, "x.py-2.html"))

    def test_a_new_run_with_no_source_change_rewrites_only_the_home_page(self) -> None:
        """The run's generator and commit are named once: on every page, each
        regeneration at a new commit rewrote the whole site."""
        sites = []
        runs = (("devdocs 4.0.0", "a" * 40), ("devdocs 4.0.1", "b" * 40))
        for generator, commit in runs:
            docset = shop()
            docset.run = RunInfo(generator=generator, git_commit=commit)
            sites.append(HtmlRenderer().render(docset))
        first, second = sites
        self.assertEqual([path for path in first if first[path] != second[path]],
                         ["index.html"])
        home = page_of(second, "index.html")
        self.assertEqual(
            [(a["name"], a["content"]) for a in home.find("meta")
             if a.get("name", "").startswith("devdocs:")],
            [("devdocs:schema", "1.0.0"), ("devdocs:generator", "devdocs 4.0.1"),
             ("devdocs:git_commit", "b" * 40)],
        )


class TestSearchIndex(unittest.TestCase):
    def test_pages_js_lists_every_document_by_a_path_that_resolves(self) -> None:
        files = HtmlRenderer().render(shop())
        head, _, text = text_of(files, "assets/pages.js").partition("\n")
        self.assertEqual(
            head, "// @generated by devdocs: each page's title, path and summary.")
        self.assertTrue(text.startswith("window.devdocsPages = [\n"))
        self.assertTrue(text.endswith("\n];\n"))
        entries = json.loads(text.removeprefix("window.devdocsPages = ").rstrip(";\n"))
        self.assertEqual(len(entries), len(shop().documents))
        self.assertEqual(entries[0], [
            "Architecture", "architecture.html",
            "A small shop: an HTTP layer over a cart.",
        ])
        for _, path, _ in entries:
            self.assertIn(unquote(path, errors="surrogateescape"), files)
        self.assertTrue(text.isascii())

    def test_a_site_without_its_manifest_is_not_scanned_as_source(self) -> None:
        """A run passes over the files a site's manifest names; with the
        manifest lost, the site's scripts were scanned as the project's
        JavaScript. Both now open with the marker the scan reads."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for path, data in HtmlRenderer().render(shop()).items():
                target = root / "docs" / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            result = Scanner(root).scan()
        self.assertEqual({f.relpath: f.role for f in result.files}, {
            "docs/assets/filter.js": Role.GENERATED,
            "docs/assets/pages.js": Role.GENERATED,
        })
        self.assertEqual(result.documentable, [])


class TestContent(unittest.TestCase):
    def body(self, *sections: Section) -> Page:
        return Page(render(file_doc("a.py", *sections))["a.py.html"])

    def test_code_spans_paragraphs_and_unsafe_characters(self) -> None:
        purpose = Section("purpose", "Purpose", text=(
            "Uses `a < b` and ``x ` y``.\n\nSecond\u2028line \x00 \ud800 end.",
        ))
        data = render(file_doc("a.py", purpose))["a.py.html"]
        source = data.decode("utf-8")
        self.assertIn("<p>Uses <code>a &lt; b</code> and <code>x ` y</code>.</p>",
                      source)
        self.assertIn("<p>Second line \ufffd \ufffd end.</p>", source)
        self.assertEqual(Page(data).problems, [])

    def test_a_code_span_shows_what_commonmark_shows(self) -> None:
        # Regression: a line break before the closing backticks was dropped
        # before the padding was judged, so `` f<LF>`` showed " f".
        purpose = Section("purpose", "Purpose", text=("Call `` f\n`` or `` `x` ``.",))
        source = render(file_doc("a.py", purpose))["a.py.html"].decode("utf-8")
        self.assertIn("<p>Call <code>f</code> or <code>`x`</code>.</p>", source)

    def test_sections_follow_section_order_and_empty_ones_are_left_out(self) -> None:
        page = self.body(
            Section("known-issues", "Known issues", items=(Item("TODO: x", line=3),)),
            Section("zeta", "", text=("Unknown.",)),
            Section("behaviour", "Behaviour", items=(Item(""),)),
            Section("dependencies", "Dependencies", items=(
                Item("External", (Link("numpy"),)),)),
            Section("notes", "Notes", diagram="sequenceDiagram\n a->>b: hi"),
        )
        self.assertEqual(page.heading_texts("h2"),
                         ["Dependencies", "Known issues", "Zeta", "Notes"])
        self.assertIn("L3: TODO: x", page.text)
        (code,) = page.find("code", class_="language-mermaid")
        self.assertEqual(code, {"class": "language-mermaid"})

    def test_ordered_sections_are_numbered_lists(self) -> None:
        page = self.body(
            Section("steps", "Steps", ordered=True, items=(Item("One."), Item("Two."))),
            Section("data-flow", "Data flow", items=(Item("In."),)),
            Section("notes", "Notes", items=(Item("Plain."),)),
        )
        lists = [tag for tag, _, context in page.elements
                 if tag in ("ol", "ul") and ".body" in context]
        self.assertEqual(lists, ["ol", "ol", "ul"])

    def test_an_anchor_is_emitted_once_and_hidden_members_lift_their_own(self) -> None:
        overloads = tuple(
            SymbolDoc(id="a.py#parse", name="parse", qualname="parse",
                      kind="function", signature=f"def parse(x: {kind})")
            for kind in ("int", "str")
        )
        nested = SymbolDoc(id="a.py#C.x", name="x", qualname="C.x", kind="method")
        hidden = SymbolDoc(id="a.py#", name="", qualname="", kind="method",
                           members=(nested,))
        holder = sym("C", "class", "class C", members=(hidden,))
        page = self.body(Section("public-surface", "API",
                                 symbols=(*overloads, holder)))
        self.assertEqual([i for i in page.ids if i.startswith("sym-")],
                         ["sym-parse", "sym-C", "sym-C-x"])
        self.assertEqual(page.heading_texts("h3"), ["parse", "parse", "C"])

    def test_a_diagram_wider_than_a_phone_column_takes_focus(self) -> None:
        """A drawing never scales on screen; any drawing wider than a 320px
        phone's 288px column may scroll there, so a keyboard must reach it,
        including one that fits a desktop's 736px column."""
        chain = [f"stage_{i}.py" for i in range(12)]
        wide = mermaid(chain, list(pairwise(chain)))
        middle = mermaid(chain[:4], list(pairwise(chain[:4])))
        narrow = mermaid(chain[:2], [(chain[0], chain[1])])
        files = render(file_doc("a.py", Section("one", "One", diagram=wide),
                                Section("two", "Two", diagram=middle),
                                Section("three", "Three", diagram=narrow)))
        page = Page(files["a.py.html"])
        widths = [float(svg["width"]) for svg in page.find("svg")]
        self.assertGreater(widths[0], 736)
        self.assertTrue(288 < widths[1] < 736, widths[1])
        self.assertLessEqual(widths[2], 288)
        self.assertEqual(page.find("figure"), [
            {"class": "figure", "tabindex": "0", "aria-labelledby": "diagram-1-title"},
            {"class": "figure", "tabindex": "0", "aria-labelledby": "diagram-2-title"},
            {"class": "figure"},
        ])
        self.assertEqual(check_links(files), [])

    def test_two_diagrams_on_one_page_keep_their_ids_apart(self) -> None:
        diagram = 'graph LR\n  a["A"]\n  b["B"]\n  a --> b'
        page = self.body(Section("one", "One", diagram=diagram),
                         Section("two", "Two", diagram=diagram))
        self.assertEqual([s["aria-labelledby"] for s in page.find("svg")],
                         ["diagram-1-title", "diagram-2-title"])
        self.assertEqual(page.problems, [])


class TestStylesheet(unittest.TestCase):
    """Layout and contrast that a browser check measured, held here by the
    rules that give them, as no browser runs in these tests."""

    rules: dict[tuple[str, str], dict[str, str]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = stylesheet()

    def test_long_names_wrap_rather_than_widen_the_page(self) -> None:
        """An identifier in prose, a fact, a breadcrumb or the project name
        widened pages on a phone, and tables did so up to 1100px. A word
        breaks only when it cannot fit its line, set once for the page; flex
        items holding text may shrink below their longest word; and columns
        after a table's first, which hold prose, may break a word anywhere."""
        wrapping = {key: rule["overflow-wrap"] for key, rule in self.rules.items()
                    if "overflow-wrap" in rule}
        self.assertEqual(wrapping, {("", "body"): "break-word",
                                    ("", "td + td"): "anywhere"})
        for item in (".brand", ".crumbs li", ".facts li"):
            self.assertEqual(self.rules[("", item)].get("min-width"), "0", item)

    def test_a_fact_that_fills_its_line_leaves_no_dot_alone(self) -> None:
        """A dot drawn after every fact but the last went onto a line of its
        own after a fact that filled its line, as on a module page at
        1000px; drawn before every fact but the first, it leads the line the
        next fact starts on."""
        dot = self.rules.get(("", ".facts li + li::before"), {})
        self.assertEqual((dot.get("content"), dot.get("background")),
                         ('""', "currentColor"))
        self.assertEqual([key for key in self.rules
                          if key[1].startswith(".facts") and "::after" in key[1]], [])

    def test_a_diagram_keeps_its_size_on_screen_and_fits_a_printed_page(self) -> None:
        """Scaled to a phone's column, a diagram's labels drew at 6px; on
        screen a figure scrolls instead, and only print scales a drawing."""
        self.assertNotIn("max-width", self.rules[("", "svg.diagram")])
        self.assertEqual(self.rules[("", ".figure")].get("overflow-x"), "auto")
        self.assertEqual(self.rules[("print", "svg.diagram")].get("max-width"), "100%")
        self.assertEqual(self.rules[("print", ".figure")].get("overflow"), "visible")

    def test_the_filter_box_stands_out_from_the_masthead_in_both_themes(self) -> None:
        """WCAG asks 3:1 of a control's boundary against the colours on
        either side of it, and 4.5:1 of text, the placeholder included."""
        field = self.rules[("", ".filter input")]
        masthead = self.rules[("", ".masthead")]["background"]
        light = self.rules[("", ":root")]
        dark = {**light, **self.rules[("(prefers-color-scheme: dark)", ":root")]}
        for theme, tokens in (("light", light), ("dark", dark)):
            with self.subTest(theme=theme):
                inside = colour(field["background"], tokens)
                border = colour(field["border"], tokens)
                self.assertGreaterEqual(contrast(border, colour(masthead, tokens)), 3)
                self.assertGreaterEqual(contrast(border, inside), 3)
                hint = self.rules[("", ".filter input::placeholder")]
                placeholder = colour(hint["color"], tokens)
                self.assertGreaterEqual(contrast(placeholder, inside), 4.5)
                self.assertEqual(hint["opacity"], "1")    # Firefox dims it by default


if __name__ == "__main__":
    unittest.main()
