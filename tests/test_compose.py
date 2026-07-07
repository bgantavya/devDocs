"""Documents are assembled from facts, with prose only where it was planned."""

from __future__ import annotations

import unittest
from pathlib import Path

from devdocs.compose import (
    Composer,
    Prose,
    docstring_summary,
    first_sentence,
    mermaid,
)
from devdocs.graph import FileNode, ModuleNode, ProjectGraph
from devdocs.issues import Issue
from devdocs.parse import Confidence, FileFacts, Symbol, SymbolKind
from devdocs.plan import FilePlan, ModulePlan
from devdocs.render.model import DocKind, Layout, Mode
from devdocs.render.style import Reply
from devdocs.scan import Role, SourceFile


def source(relpath: str, language: str = "python", lines: int = 40) -> SourceFile:
    return SourceFile(Path(relpath), relpath, language, Role.SOURCE, 100, lines)


CLIENT = FileFacts(
    path=Path("app/client.py"),
    language="python",
    module_docstring="Talks to the API.\n\nKeeps one connection per host.",
    loc=120,
    symbols=(
        Symbol("Client", SymbolKind.CLASS, 10, 60, "class Client(Base)",
               "A pooled client.", bases=("Base",), children=(
                   Symbol("__init__", SymbolKind.METHOD, 12, 14,
                          "def __init__(self, host: str)"),
                   Symbol("send", SymbolKind.METHOD, 16, 30,
                          "async def send(self, body: bytes) -> int", is_async=True),
                   Symbol("_retry", SymbolKind.METHOD, 32, 40,
                          "def _retry(self)", is_public=False),
                   Symbol("__repr__", SymbolKind.METHOD, 42, 44, "def __repr__(self)"),
               )),
        Symbol("_helper", SymbolKind.FUNCTION, 70, 72, "def _helper()",
               is_public=False),
        Symbol("TIMEOUT", SymbolKind.CONSTANT, 5, 5, "TIMEOUT = 30"),
    ),
    imports=("app.store", "json", "requests"),
)

STORE = FileFacts(path=Path("app/store.py"), language="python", loc=30,
                  symbols=(Symbol("Store", SymbolKind.CLASS, 1, 20, "class Store"),))


def graph() -> ProjectGraph:
    return ProjectGraph(
        files={
            "app/client.py": FileNode(
                "app/client.py", "python", "app",
                internal=("app/store.py",), external=("json", "requests"),
                dependents=("cli.py",), tested_by=("tests/test_client.py",),
            ),
            "app/store.py": FileNode("app/store.py", "python", "app",
                                     dependents=("app/client.py",)),
            "cli.py": FileNode("cli.py", "python", "", internal=("app/client.py",),
                               entry_point="console script `shop`"),
        },
        modules={
            "": ModuleNode("", ("cli.py",), depends_on=("app",)),
            "app": ModuleNode("app", ("app/client.py", "app/store.py"),
                              used_by=("",)),
        },
    )


def composer(**kwargs: object) -> Composer:
    return Composer(
        project="shop",
        files={rel: source(rel) for rel in ("app/client.py", "app/store.py", "cli.py")},
        facts={"app/client.py": CLIENT, "app/store.py": STORE},
        graph=graph(),
        hashes={"app/client.py": "h1", "app/store.py": "h2", "cli.py": "h3"},
        modules=["", "app"],
        **kwargs,  # type: ignore[arg-type]
    )


def plan(relpath: str, mode: Mode = Mode.PROSE) -> FilePlan:
    return FilePlan(relpath, 5.0, 0.5, mode, "test", describe=("Client.send",))


class TestFileDocuments(unittest.TestCase):
    def test_prose_document(self) -> None:
        reply = Reply(
            purpose="Sends requests to the shop API over pooled connections.",
            symbols=(("Client.send", "Sends one body and returns the status."),),
            notes=("Retries once on a reset connection.",),
        )
        prose = Prose(reply, "gemini-2.5-flash-lite", "file", 900, 120)
        doc = composer().file(plan("app/client.py"), prose,
                              [Issue(41, "TODO", "drop the retry")])

        self.assertEqual((doc.id, doc.kind, doc.path), (
            "file:app/client.py", DocKind.FILE, "app/client.py"))
        self.assertEqual(doc.summary,
                         "Sends requests to the shop API over pooled connections.")
        self.assertEqual([s.key for s in doc.sections], [
            "purpose", "facts", "public-surface", "behaviour", "dependencies",
            "known-issues",
        ])
        self.assertEqual(doc.meta.mode, Mode.PROSE)
        self.assertEqual((doc.meta.model, doc.meta.input_tokens), (
            "gemini-2.5-flash-lite", 900))
        self.assertEqual(doc.meta.source_hash, "h1")

        surface = doc.section("public-surface")
        assert surface is not None
        names = [s.qualname for s in surface.symbols]
        self.assertEqual(names, ["Client", "TIMEOUT"])        # private dropped
        client = surface.symbols[0]
        self.assertEqual((client.summary, client.summary_source),
                         ("A pooled client.", "docstring"))
        members = {m.qualname: m for m in client.members}
        self.assertEqual(sorted(members), ["Client.__init__", "Client.send"])
        self.assertEqual(members["Client.send"].summary_source, "model")
        self.assertTrue(members["Client.send"].is_async)
        self.assertEqual(members["Client.send"].id, "app/client.py#Client.send")

        facts = doc.section("facts")
        assert facts is not None
        texts = [item.text for item in facts.items]
        self.assertEqual(texts, ["Module", "Python", "120 lines", "Used by",
                                 "Tested by"])
        self.assertEqual(facts.items[0].links[0].target, "module:app")
        self.assertEqual(facts.items[3].links[0].target, "file:cli.py")

        deps = doc.section("dependencies")
        assert deps is not None
        listed = [(i.text, [link.text for link in i.links]) for i in deps.items]
        self.assertEqual(listed,
                         [("Internal", ["app/store.py"]), ("External", ["requests"]),
                          ("Standard library", ["json"])])
        issues = doc.section("known-issues")
        assert issues is not None
        self.assertEqual((issues.items[0].text, issues.items[0].line),
                         ("TODO: drop the retry", 41))

    def test_stub_uses_the_module_docstring(self) -> None:
        doc = composer().file(plan("app/client.py", Mode.STUB), None)
        self.assertEqual(doc.meta.mode, Mode.STUB)
        purpose = doc.section("purpose")
        assert purpose is not None
        self.assertEqual(purpose.text, ("Talks to the API.",))
        self.assertIsNone(doc.section("behaviour"))
        surface = doc.section("public-surface")
        assert surface is not None
        send = next(m for m in surface.symbols[0].members if m.name == "send")
        self.assertEqual(send.summary, "")     # nothing invented without prose

    def test_file_without_a_parser(self) -> None:
        doc = composer().file(plan("cli.py", Mode.STUB), None)
        self.assertIsNone(doc.section("public-surface"))
        self.assertEqual(doc.meta.confidence, "failed")
        self.assertEqual(doc.meta.notes, ("no parser for python",))

    def test_degraded_parse_is_flagged(self) -> None:
        facts = FileFacts(Path("app/store.py"), "python", loc=3,
                          confidence=Confidence.DEGRADED, notes=["ast failed"])
        c = composer()
        c.facts = {**c.facts, "app/store.py": facts}
        doc = c.file(plan("app/store.py", Mode.STUB), None)
        facts_section = doc.section("facts")
        assert facts_section is not None
        self.assertIn("Symbol table may be incomplete",
                      [item.text for item in facts_section.items])
        self.assertEqual(doc.meta.confidence, "degraded")

    def test_per_file_layout_keeps_extensions_only_on_collision(self) -> None:
        c = Composer(
            project="p",
            files={"a/x.py": source("a/x.py"), "a/x.ts": source("a/x.ts", "typescript"),
                   "a/y.py": source("a/y.py")},
            facts={}, graph=ProjectGraph(), hashes={}, modules=[],
            layout=Layout(per_file=True),
        )
        paths = {rel: c.file(plan(rel, Mode.STUB), None).path
                 for rel in ("a/x.py", "a/x.ts", "a/y.py")}
        self.assertEqual(
            paths, {"a/x.py": "a/x.py", "a/x.ts": "a/x.ts", "a/y.py": "a/y"}
        )


class TestModuleAndArchitecture(unittest.TestCase):
    def test_module_document(self) -> None:
        reply = Reply(purpose="Holds the API client and its storage.",
                      relations=("The client persists replies through the store.",))
        doc = composer().module(
            ModulePlan("app", ("app/client.py", "app/store.py"), 9.0, Mode.PROSE, "t"),
            Prose(reply, "gemini-2.5-flash", "module"),
            {"app/client.py": "Talks to the API.", "app/store.py": ""},
        )
        self.assertEqual((doc.id, doc.title, doc.path),
                         ("module:app", "app/", "app/index"))
        components = doc.section("components")
        assert components is not None
        self.assertEqual([(i.text, i.links[0].target) for i in components.items], [
            ("Talks to the API.", "file:app/client.py"), ("", "file:app/store.py"),
        ])
        relations = doc.section("relationships")
        assert relations is not None
        self.assertIn('n0["client.py"]', relations.diagram)
        self.assertIn("n0 --> n1", relations.diagram)
        facts = doc.section("facts")
        assert facts is not None
        self.assertEqual([i.text for i in facts.items],
                         ["Parent", "2 files", "Used by"])

    def test_root_module_title_is_the_project(self) -> None:
        doc = composer().module(
            ModulePlan("", ("cli.py",), 1.0, Mode.STUB, "t"), None, {}
        )
        self.assertEqual((doc.title, doc.path), ("shop", "index"))
        entries = doc.section("entry-points")
        assert entries is not None
        self.assertEqual(entries.items[0].text, "console script `shop`")

    def test_architecture_document(self) -> None:
        reply = Reply(purpose="A shop backend.", flow=("A request arrives.",
                                                      "The client answers."),
                      extend=("Add a store backend.",), notes=("Needs a key.",))
        doc = composer().architecture(
            Prose(reply, "gemini-2.5-pro", "architecture"),
            {"app": "Talks to the API."}, {"python": 3},
        )
        self.assertEqual([s.key for s in doc.sections], [
            "overview", "component-map", "data-flow", "entry-points",
            "extension-points", "operational-notes",
        ])
        flow = doc.section("data-flow")
        assert flow is not None and flow.ordered
        component_map = doc.section("component-map")
        assert component_map is not None
        self.assertIn("graph TD", component_map.diagram)
        rows = {i.links[0].text: (i.text, [link.text for link in i.links[1:]])
                for i in component_map.items}
        self.assertEqual(rows["(root)"], ("", ["app"]))
        self.assertEqual(rows["app"], ("Talks to the API.", []))

    def test_architecture_stub_has_a_factual_overview(self) -> None:
        doc = composer().architecture(None, {}, {"python": 2, "go": 1})
        overview = doc.section("overview")
        assert overview is not None
        self.assertEqual(overview.text, (
            "shop has 3 documented source files in 2 directories: "
            "Python (2), Go (1).",
        ))
        self.assertEqual(doc.meta.mode, Mode.STUB)


class TestHelpers(unittest.TestCase):
    def test_first_sentence(self) -> None:
        self.assertEqual(first_sentence("Parses x. Then y."), "Parses x.")
        self.assertEqual(first_sentence("Uses e.g. pools. Then y."),
                         "Uses e.g. pools.")
        self.assertEqual(first_sentence("Calls `a.b()`. Then y."), "Calls `a.b()`.")
        self.assertEqual(first_sentence(""), "")
        self.assertTrue(first_sentence("word " * 100, limit=40).endswith("…"))

    def test_docstring_summary_takes_the_first_paragraph(self) -> None:
        self.assertEqual(docstring_summary("  One\n  line.\n\nMore."), "One line.")

    def test_mermaid_escapes_labels(self) -> None:
        text = mermaid(["a", "b"], [("a", "b"), ("a", "missing")],
                       labels={"a": 'x"#y'})
        self.assertEqual(text.splitlines(), [
            "graph LR", '  n0["x#quot;#35;y"]', '  n1["b"]', "  n0 --> n1",
        ])


if __name__ == "__main__":
    unittest.main()
