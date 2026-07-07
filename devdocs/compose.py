"""Assembling documents from parsed facts and, where planned, model prose.

Every document is mostly facts. Signatures, members, dependencies, entry
points and known issues come straight from the parser and the import graph,
so they are exact and cost nothing. Model prose fills only what code cannot
state — purpose, behaviour, how the parts relate — and arrives already
cleaned and grounded by `render.style`. A file without prose still gets a
complete, accurate reference page; it is only terser.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from devdocs.graph import FileNode, ProjectGraph, is_stdlib
from devdocs.issues import Issue
from devdocs.parse import Confidence, FileFacts, Symbol, SymbolKind
from devdocs.plan import FilePlan, ModulePlan
from devdocs.render.model import (
    DocKind,
    DocMeta,
    Document,
    Item,
    Layout,
    Link,
    Mode,
    Section,
    SymbolDoc,
)
from devdocs.render.style import Reply
from devdocs.scan import SourceFile

__all__ = [
    "ARCHITECTURE_ID",
    "Composer",
    "Prose",
    "docstring_summary",
    "file_id",
    "first_sentence",
    "mermaid",
    "module_id",
]

ARCHITECTURE_ID = "architecture"

LANGUAGE_NAMES = {
    "python": "Python", "javascript": "JavaScript", "typescript": "TypeScript",
    "go": "Go", "rust": "Rust", "java": "Java", "kotlin": "Kotlin",
    "csharp": "C#", "cpp": "C++", "c": "C", "ruby": "Ruby", "php": "PHP",
    "swift": "Swift", "scala": "Scala", "shell": "Shell", "sql": "SQL",
    "elixir": "Elixir", "lua": "Lua", "dart": "Dart", "vue": "Vue",
    "svelte": "Svelte",
}

_MAX_LINKS = 8
_SENTENCE_END = re.compile(
    r"(?<![A-Z])(?<!\be\.g)(?<!\bi\.e)(?<!\betc)(?<!\bvs)[.!?](?=\s|$)"
)


def file_id(relpath: str) -> str:
    return f"file:{relpath}"


def module_id(path: str) -> str:
    return f"module:{path}"


def first_sentence(text: str, limit: int = 220) -> str:
    """The first sentence of `text`, for index tables and summaries."""
    text = " ".join(text.split())
    if not text:
        return ""
    depth = 0
    for index, char in enumerate(text):
        if char == "`":
            depth ^= 1
        elif depth == 0 and _SENTENCE_END.match(text, index):
            text = text[: index + 1]
            break
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


def docstring_summary(docstring: str, limit: int = 320) -> str:
    """A docstring's first paragraph, flattened, for summaries and leads."""
    lead = docstring.strip().split("\n\n", 1)[0]
    lead = " ".join(lead.split())
    if len(lead) > limit:
        lead = first_sentence(lead, limit)
    return lead


def mermaid(
    nodes: Sequence[str],
    edges: Sequence[tuple[str, str]],
    *,
    direction: str = "LR",
    labels: Mapping[str, str] | None = None,
) -> str:
    """A Mermaid flowchart. Node ids are synthetic so labels need no escaping
    beyond quotes and `#`, which Mermaid reads as entity codes."""
    ids = {node: f"n{index}" for index, node in enumerate(nodes)}
    lines = [f"graph {direction}"]
    for node in nodes:
        label = (labels or {}).get(node, node) or "(root)"
        text = label.replace("#", "#35;").replace('"', "#quot;")
        lines.append(f'  {ids[node]}["{text}"]')
    for source, target in edges:
        if source in ids and target in ids:
            lines.append(f"  {ids[source]} --> {ids[target]}")
    return "\n".join(lines)


@dataclass(frozen=True)
class Prose:
    """A cleaned model reply and what it cost to get."""

    reply: Reply
    model: str
    tier: str
    input_tokens: int = 0
    output_tokens: int = 0


def _public_member(symbol: Symbol) -> bool:
    if symbol.name in ("__init__", "__call__"):
        return True
    return symbol.is_public and not symbol.name.startswith("_")


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


class Composer:
    """Builds every `Document` of a run from the same shared facts.

    `hashes` maps each file to the sha256 of its source; module and
    architecture hashes are derived from them, so a document's hash changes
    exactly when something it describes does.
    """

    def __init__(
        self,
        *,
        project: str,
        files: Mapping[str, SourceFile],
        facts: Mapping[str, FileFacts],
        graph: ProjectGraph,
        hashes: Mapping[str, str],
        modules: Collection[str],
        layout: Layout | None = None,
    ) -> None:
        self.project = project
        self.files = files
        self.facts = facts
        self.graph = graph
        self.hashes = hashes
        self.modules = frozenset(modules)
        self.layout = layout or Layout()
        self._collisions = self._colliding_stems()

    def _colliding_stems(self) -> frozenset[str]:
        seen: dict[str, list[str]] = {}
        for relpath in self.files:
            stem = relpath.rpartition(".")[0] or relpath
            seen.setdefault(stem, []).append(relpath)
        return frozenset(p for group in seen.values() if len(group) > 1 for p in group)

    # -- links -------------------------------------------------------------

    def _file_link(self, relpath: str, text: str | None = None) -> Link:
        target = file_id(relpath) if relpath in self.files else ""
        return Link(text or relpath, target)

    def _module_link(self, path: str, text: str | None = None) -> Link:
        target = module_id(path) if path in self.modules else ""
        return Link(text or path or "(root)", target)

    def _links(self, paths: Sequence[str], make: str = "file") -> tuple[Link, ...]:
        shown = [
            self._file_link(p) if make == "file" else self._module_link(p)
            for p in paths[:_MAX_LINKS]
        ]
        if len(paths) > _MAX_LINKS:
            shown.append(Link(f"{len(paths) - _MAX_LINKS} more"))
        return tuple(shown)

    # -- file documents ----------------------------------------------------

    def file(
        self, plan: FilePlan, prose: Prose | None, issues: Sequence[Issue] = ()
    ) -> Document:
        relpath = plan.relpath
        source = self.files[relpath]
        facts = self.facts.get(relpath)
        node = self.graph.file(relpath)
        reply = prose.reply if prose else None

        purpose = (reply.purpose if reply else "") or docstring_summary(
            facts.module_docstring if facts else ""
        )
        described = dict(reply.symbols) if reply else {}

        sections: list[Section] = []
        if purpose:
            sections.append(Section("purpose", "Purpose", text=(purpose,)))
        sections.append(self._file_facts(source, facts, node))
        surface = self._surface(relpath, facts, described)
        if surface:
            sections.append(
                Section("public-surface", "Public surface", symbols=surface)
            )
        if reply and reply.notes:
            items = tuple(Item(note) for note in reply.notes)
            sections.append(Section("behaviour", "Behaviour", items=items))
        dependencies = self._dependencies(node)
        if dependencies:
            sections.append(Section("dependencies", "Dependencies", items=dependencies))
        if issues:
            sections.append(Section(
                "known-issues",
                "Known issues",
                items=tuple(Item(f"{i.tag}: {i.text}", line=i.line) for i in issues),
            ))

        confidence = facts.confidence.value if facts else Confidence.FAILED.value
        notes = tuple(facts.notes) if facts else (f"no parser for {source.language}",)
        return Document(
            id=file_id(relpath),
            kind=DocKind.FILE,
            title=relpath,
            path=self.layout.file(relpath, collides=relpath in self._collisions),
            summary=first_sentence(purpose),
            sections=tuple(sections),
            meta=DocMeta(
                mode=Mode.PROSE if prose else Mode.STUB,
                source=relpath,
                source_hash=self.hashes.get(relpath, ""),
                language=source.language,
                model=prose.model if prose else "",
                tier=prose.tier if prose else "",
                input_tokens=prose.input_tokens if prose else 0,
                output_tokens=prose.output_tokens if prose else 0,
                confidence=confidence,
                notes=notes,
            ),
        )

    def _file_facts(
        self, source: SourceFile, facts: FileFacts | None, node: FileNode | None
    ) -> Section:
        items: list[Item] = []
        if node is not None and node.module in self.modules:
            items.append(Item("Module", (self._module_link(node.module),)))
        items.append(Item(LANGUAGE_NAMES.get(source.language, source.language.title())))
        lines = (facts.loc if facts else 0) or source.lines
        items.append(Item(f"{lines:,} lines"))
        if node is not None and node.entry_point:
            items.append(Item(f"Entry point: {node.entry_point}"))
        if node is not None and node.dependents:
            items.append(Item("Used by", self._links(node.dependents)))
        if node is not None and node.tested_by:
            items.append(Item("Tested by", self._links(node.tested_by)))
        if facts is not None and facts.confidence in (
            Confidence.DEGRADED, Confidence.FAILED
        ):
            items.append(Item("Symbol table may be incomplete"))
        return Section("facts", "Facts", items=tuple(items))

    def _surface(
        self, relpath: str, facts: FileFacts | None, described: Mapping[str, str]
    ) -> tuple[SymbolDoc, ...]:
        if facts is None:
            return ()
        return tuple(
            self._symbol(relpath, symbol, symbol.name, described)
            for symbol in facts.public_symbols
        )

    def _symbol(
        self,
        relpath: str,
        symbol: Symbol,
        qualname: str,
        described: Mapping[str, str],
        depth: int = 0,
    ) -> SymbolDoc:
        summary = docstring_summary(symbol.docstring)
        origin = "docstring" if summary else ""
        if not summary and qualname in described:
            summary, origin = described[qualname], "model"

        members: tuple[SymbolDoc, ...] = ()
        if depth < 2 and symbol.kind in (SymbolKind.CLASS, SymbolKind.INTERFACE):
            members = tuple(
                self._symbol(relpath, child, f"{qualname}.{child.name}",
                             described, depth + 1)
                for child in symbol.children
                if _public_member(child)
            )
        return SymbolDoc(
            id=f"{relpath}#{qualname}",
            name=symbol.name,
            qualname=qualname,
            kind=symbol.kind.value,
            signature=symbol.signature,
            summary=summary,
            summary_source=origin,
            line=symbol.line,
            end_line=symbol.end_line,
            is_async=symbol.is_async,
            decorators=symbol.decorators,
            bases=symbol.bases,
            members=members,
        )

    def _dependencies(self, node: FileNode | None) -> tuple[Item, ...]:
        if node is None:
            return ()
        items: list[Item] = []
        if node.internal:
            internal = tuple(self._file_link(p) for p in node.internal)
            items.append(Item("Internal", internal))
        third = [n for n in node.external if not is_stdlib(node.language, n)]
        stdlib = [n for n in node.external if is_stdlib(node.language, n)]
        if third:
            items.append(Item("External", tuple(Link(n) for n in third)))
        if stdlib:
            items.append(Item("Standard library", tuple(Link(n) for n in stdlib)))
        return tuple(items)

    # -- module documents --------------------------------------------------

    def module(
        self,
        plan: ModulePlan,
        prose: Prose | None,
        summaries: Mapping[str, str],
    ) -> Document:
        path = plan.path
        reply = prose.reply if prose else None
        files = sorted(plan.files)

        purpose = (reply.purpose if reply else "") or self._package_docstring(path)
        sections: list[Section] = []
        if purpose:
            sections.append(Section("purpose", "Purpose", text=(purpose,)))
        sections.append(self._module_facts(path, files))
        sections.append(Section(
            "components",
            "Components",
            items=tuple(
                Item(
                    summaries.get(rel, ""),
                    (self._file_link(rel, posixpath.basename(rel)),),
                )
                for rel in files
            ),
        ))

        relations = tuple(Item(text) for text in reply.relations) if reply else ()
        diagram = self._module_diagram(files)
        if relations or diagram:
            sections.append(Section(
                "relationships", "Relationships", items=relations, diagram=diagram
            ))
        entries = [
            Item(
                n.entry_point,
                (self._file_link(n.relpath, posixpath.basename(n.relpath)),),
            )
            for n in self.graph.entry_points()
            if n.relpath in plan.files
        ]
        if entries:
            sections.append(
                Section("entry-points", "Entry points", items=tuple(entries))
            )
        if reply and reply.notes:
            sections.append(Section(
                "notes", "Notes", items=tuple(Item(n) for n in reply.notes)
            ))

        return Document(
            id=module_id(path),
            kind=DocKind.MODULE,
            title=f"{path}/" if path else self.project,
            path=self.layout.module(path),
            summary=first_sentence(purpose),
            sections=tuple(sections),
            meta=DocMeta(
                mode=Mode.PROSE if prose else Mode.STUB,
                source=path,
                source_hash=_hash(*(f"{f}:{self.hashes.get(f, '')}" for f in files)),
                model=prose.model if prose else "",
                tier=prose.tier if prose else "",
                input_tokens=prose.input_tokens if prose else 0,
                output_tokens=prose.output_tokens if prose else 0,
            ),
        )

    def _package_docstring(self, path: str) -> str:
        for name in ("__init__.py", "mod.rs", "lib.rs", "index.ts", "index.js"):
            facts = self.facts.get(posixpath.join(path, name) if path else name)
            if facts is not None and facts.module_docstring:
                return docstring_summary(facts.module_docstring)
        return ""

    def _module_facts(self, path: str, files: Sequence[str]) -> Section:
        items: list[Item] = [Item(f"{len(files)} files")]
        parent = posixpath.dirname(path) if path else None
        while parent is not None and parent not in self.modules and parent:
            parent = posixpath.dirname(parent)
        if parent is not None and parent in self.modules and parent != path:
            items.insert(0, Item("Parent", (self._module_link(parent),)))
        children = sorted(
            m for m in self.modules
            if m != path and posixpath.dirname(m) == path and m
        )
        if children:
            items.append(Item("Submodules", self._links(children, "module")))
        module = self.graph.modules.get(path)
        if module is not None and module.depends_on:
            items.append(Item("Depends on", self._links(module.depends_on, "module")))
        if module is not None and module.used_by:
            items.append(Item("Used by", self._links(module.used_by, "module")))
        return Section("facts", "Facts", items=tuple(items))

    def _module_diagram(self, files: Sequence[str]) -> str:
        inside = set(files)
        edges = sorted(
            (rel, target)
            for rel in files
            if (node := self.graph.file(rel)) is not None
            for target in node.internal
            if target in inside and target != rel
        )
        if not edges or len(files) > 20:
            return ""
        nodes = sorted({n for edge in edges for n in edge})
        labels = {n: posixpath.basename(n) for n in nodes}
        return mermaid(nodes, edges, labels=labels)

    # -- architecture ------------------------------------------------------

    def architecture(
        self,
        prose: Prose | None,
        summaries: Mapping[str, str],
        languages: Mapping[str, int],
    ) -> Document:
        """The project overview. `summaries` maps modules to one sentence."""
        reply = prose.reply if prose else None
        overview = (reply.purpose if reply else "") or self._default_overview(languages)

        components = self.graph.components(12)
        names = sorted(set(components.values()))
        weights: dict[tuple[str, str], int] = {}
        for (source, target), count in self.graph.module_edges().items():
            a, b = components.get(source, source), components.get(target, target)
            if a != b:
                weights[(a, b)] = weights.get((a, b), 0) + count
        edges = sorted(weights)
        depends: dict[str, list[str]] = {}
        for a, b in edges:
            depends.setdefault(a, []).append(b)

        sections: list[Section] = [Section("overview", "Overview", text=(overview,))]
        if names:
            rows = tuple(
                Item(
                    summaries.get(name, ""),
                    (self._module_link(name), *(
                        self._module_link(dep) for dep in depends.get(name, [])
                    )),
                )
                for name in names
            )
            sections.append(Section(
                "component-map",
                "Component map",
                items=rows,
                diagram=mermaid(names, edges, direction="TD") if len(names) > 1 else "",
            ))
        if reply and reply.flow:
            sections.append(Section(
                "data-flow", "Data flow",
                items=tuple(Item(step) for step in reply.flow), ordered=True,
            ))
        entries = tuple(
            Item(n.entry_point, (self._file_link(n.relpath),))
            for n in self.graph.entry_points()
        )
        if entries:
            sections.append(Section("entry-points", "Entry points", items=entries))
        if reply and reply.extend:
            sections.append(Section(
                "extension-points", "Extension points",
                items=tuple(Item(text) for text in reply.extend),
            ))
        if reply and reply.notes:
            sections.append(Section(
                "operational-notes", "Operational notes",
                items=tuple(Item(text) for text in reply.notes),
            ))

        everything = sorted(self.hashes.items())
        return Document(
            id=ARCHITECTURE_ID,
            kind=DocKind.ARCHITECTURE,
            title="Architecture",
            path=self.layout.architecture(),
            summary=first_sentence(overview),
            sections=tuple(sections),
            meta=DocMeta(
                mode=Mode.PROSE if prose else Mode.STUB,
                source_hash=_hash(*(f"{k}:{v}" for k, v in everything)),
                model=prose.model if prose else "",
                tier=prose.tier if prose else "",
                input_tokens=prose.input_tokens if prose else 0,
                output_tokens=prose.output_tokens if prose else 0,
            ),
        )

    def _default_overview(self, languages: Mapping[str, int]) -> str:
        total = sum(languages.values())
        if not total:
            return f"{self.project} has no documentable source files."
        ranked = sorted(languages.items(), key=lambda kv: (-kv[1], kv[0]))
        spoken = ", ".join(
            f"{LANGUAGE_NAMES.get(name, name.title())} ({count})"
            for name, count in ranked
        )
        modules = len(self.modules)
        noun = "directory" if modules == 1 else "directories"
        return (
            f"{self.project} has {total} documented source files in {modules} "
            f"{noun}: {spoken}."
        )
