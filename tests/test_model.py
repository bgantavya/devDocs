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

    def test_a_newer_major_schema_is_refused(self) -> None:
        data = sample().to_dict()
        data["schema_version"] = "2.0.0"
        with self.assertRaises(ValueError):
            DocSet.from_dict(data)

    def test_duplicate_ids_are_refused(self) -> None:
        docset = sample()
        with self.assertRaises(ValueError):
            docset.add(docset.documents[0])


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
