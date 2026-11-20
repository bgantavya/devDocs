"""The document model: stable ids, a versioned JSON form, layout rules."""

from __future__ import annotations

import json
import unittest

from devdocs.render.model import (
    SCHEMA_VERSION,
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
    anchor,
)


def sample() -> DocSet:
    member = SymbolDoc(id="a.py#C.m", name="m", qualname="C.m", kind="method",
                       signature="def m(self)", is_async=True)
    cls = SymbolDoc(id="a.py#C", name="C", qualname="C", kind="class",
                    signature="class C(Base)", summary="Holds state.",
                    summary_source="docstring", bases=("Base",), members=(member,))
    file_doc = Document(
        id="file:a.py", kind=DocKind.FILE, title="a.py", path="a.py",
        summary="Holds state.",
        sections=(
            Section("purpose", "Purpose", text=("Holds state.",)),
            Section("public-surface", "Public surface", symbols=(cls,)),
            Section("dependencies", "Dependencies",
                    items=(Item("Internal", (Link("b.py", "file:b.py"),)),)),
            Section("known-issues", "Known issues", items=(Item("TODO: x", line=3),)),
        ),
        meta=DocMeta(mode=Mode.PROSE, source="a.py", source_hash="h",
                     language="python", model="m", tier="file",
                     input_tokens=10, output_tokens=2, notes=("n",)),
    )
    overview = Document(id="architecture", kind=DocKind.ARCHITECTURE,
                        title="Architecture", path="architecture",
                        sections=(Section("data-flow", "Data flow",
                                          items=(Item("Step."),), ordered=True),))
    return DocSet("shop", [file_doc, overview], description="A shop.",
                  run=RunInfo(generator="devdocs", requests=3, cost_usd=0.01))


def listed(*documents: tuple[DocKind, str, str]) -> DocSet:
    """A DocSet of bare documents, each (kind, source, path), added in the
    order given; a file's title is its source, or its path without one."""
    docset = DocSet("demo")
    for kind, source, path in documents:
        ident = {DocKind.ARCHITECTURE: "architecture",
                 DocKind.MODULE: f"module:{source}"}.get(kind, f"file:{source or path}")
        docset.add(Document(id=ident, kind=kind, title=source or path, path=path,
                            meta=DocMeta(source=source)))
    return docset


def tree() -> DocSet:
    """Paths that sort apart from the order a home page lists documents in:
    the root module's page, `index`, after `api/index`; a module's after its
    child's; and per-file pages, which drop the extension, apart from their
    sources (`a` before `a-b`, where `a-b.py` comes before `a.py`)."""
    per_file = Layout(per_file=True)
    return listed(
        (DocKind.FILE, "a.py", per_file.file("a.py")),
        (DocKind.FILE, "a-b.py", per_file.file("a-b.py")),
        (DocKind.MODULE, "api", Layout.module("api")),
        (DocKind.MODULE, "", Layout.module("")),
        (DocKind.MODULE, "api/auth", Layout.module("api/auth")),
        (DocKind.ARCHITECTURE, "", Layout.architecture()),
    )


class TestJson(unittest.TestCase):
    def test_round_trip_is_exact(self) -> None:
        docset = sample()
        back = DocSet.from_json(docset.to_json())
        self.assertEqual(back.documents, docset.sorted())
        self.assertEqual(back.run, docset.run)
        self.assertEqual(back.description, "A shop.")
        self.assertEqual(back.get("file:a.py").meta.mode, Mode.PROSE)  # type: ignore[union-attr]

    def test_documents_are_in_reading_order(self) -> None:
        data = json.loads(sample().to_json(include_run=False))
        self.assertEqual([d["id"] for d in data["documents"]],
                         ["architecture", "file:a.py"])
        self.assertNotIn("run", data)
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)

    def test_the_manifest_lists_documents_as_the_home_pages_do(self) -> None:
        # Regression: documents were listed by output path, so the root
        # module came after `api/` and a module after its own child.
        data = json.loads(tree().to_json(include_run=False))
        self.assertEqual([d["id"] for d in data["documents"]], [
            "architecture", "module:", "module:api", "module:api/auth",
            "file:a-b.py", "file:a.py",
        ])

    def test_a_newer_major_schema_is_refused(self) -> None:
        data = sample().to_dict()
        data["schema_version"] = "2.0.0"
        with self.assertRaises(ValueError):
            DocSet.from_dict(data)

    def test_duplicate_ids_are_refused(self) -> None:
        docset = sample()
        with self.assertRaises(ValueError):
            docset.add(docset.documents[0])


class TestReadingOrder(unittest.TestCase):
    def test_architecture_then_modules_by_directory_then_files_by_source(self) -> None:
        self.assertEqual([d.id for d in tree().sorted()], [
            "architecture", "module:", "module:api", "module:api/auth",
            "file:a-b.py", "file:a.py",
        ])

    def test_each_kind_is_listed_in_reading_order(self) -> None:
        docset = tree()
        self.assertEqual([d.id for d in docset.by_kind(DocKind.MODULE)],
                         ["module:", "module:api", "module:api/auth"])
        self.assertEqual([d.id for d in docset.by_kind(DocKind.FILE)],
                         ["file:a-b.py", "file:a.py"])
        self.assertEqual([d.id for d in docset.by_kind(DocKind.ARCHITECTURE)],
                         ["architecture"])

    def test_a_file_without_a_source_goes_by_its_title(self) -> None:
        # titles sort against paths; `listed` would make them one string
        docset = DocSet("demo", [
            Document("file:x", DocKind.FILE, title="b", path="z"),
            Document("file:m.py", DocKind.FILE, title="m.py", path="m.py",
                     meta=DocMeta(source="m.py")),
            Document("file:y", DocKind.FILE, title="z", path="b"),
        ])
        order = ["file:x", "file:m.py", "file:y"]
        self.assertEqual([d.id for d in docset.sorted()], order)
        self.assertEqual([d["id"] for d in docset.to_dict()["documents"]], order)

    def test_the_order_never_depends_on_the_order_of_adding(self) -> None:
        # Regression: documents that tied on kind and path kept the order
        # they were added in, so one DocSet could render two ways.
        same = [Document(id=f"file:{name}", kind=DocKind.FILE, title="x.py",
                         path="x.py", meta=DocMeta(source="x.py"))
                for name in ("b", "a", "c")]
        orders = {tuple(d.id for d in DocSet("demo", list(group)).sorted())
                  for group in (same, same[::-1])}
        self.assertEqual(orders, {("file:a", "file:b", "file:c")})
        shuffled = DocSet("demo", tree().documents[::-1])
        self.assertEqual(shuffled.sorted(), tree().sorted())


class TestIdentity(unittest.TestCase):
    def test_symbols_walk_and_anchor(self) -> None:
        doc = sample().get("file:a.py")
        assert doc is not None
        self.assertEqual([s.qualname for s in doc.symbols()], ["C", "C.m"])
        self.assertEqual(anchor("C.m"), "sym-C-m")
        self.assertEqual(anchor("Outer::<T>::run"), "sym-Outer-T-run")
        self.assertIsNone(doc.section("behaviour"))

    def test_layouts(self) -> None:
        self.assertEqual(Layout().file("a/b.py"), "a/b.py")
        self.assertEqual(Layout(per_file=True).file("a/b.py"), "a/b")
        self.assertEqual(Layout(per_file=True).file("a/b.py", collides=True), "a/b.py")
        self.assertEqual(Layout(per_file=True).file("Makefile"), "Makefile")
        self.assertEqual((Layout.module(""), Layout.module("a")), ("index", "a/index"))
        self.assertEqual((Layout.architecture(), Layout.home()),
                         ("architecture", "README"))


if __name__ == "__main__":
    unittest.main()
