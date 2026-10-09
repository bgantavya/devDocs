"""The documentation model every output format is rendered from.

Markdown, HTML and PDF are projections of one `DocSet`; none of them is the
source of truth. Building the model once per run and rendering it several ways
keeps the formats consistent, and lets tests assert on structure instead of on
rendered text.

Identity is stable by construction. A document's id comes from its kind and
source path, a symbol's from its file and qualified name, never from order or
position, so links survive a regeneration that rewrites surrounding content
and an edit to one file changes only that file's document.

The JSON form (`DocSet.to_json`) is a public interface. `SCHEMA_VERSION`
follows semver: additive changes bump the minor version, and anything that
would break a reader bumps the major.
"""

from __future__ import annotations

import json
import re
import types
import typing
from collections.abc import Iterator
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum, StrEnum
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "SECTION_ORDER",
    "DocKind",
    "DocMeta",
    "DocSet",
    "Document",
    "Item",
    "Layout",
    "Link",
    "Mode",
    "RunInfo",
    "Section",
    "SymbolDoc",
    "anchor",
]

SCHEMA_VERSION = "1.0.0"


class DocKind(StrEnum):
    FILE = "file"
    MODULE = "module"
    ARCHITECTURE = "architecture"


class Mode(StrEnum):
    """How a document's prose was produced."""

    PROSE = "prose"   # carries model-written sections
    STUB = "stub"     # deterministic only: zero tokens spent


# Section order is fixed per kind so regenerations diff cleanly. Presence is
# conditional: a section with nothing to say is left out, never padded.
SECTION_ORDER: dict[DocKind, tuple[str, ...]] = {
    DocKind.FILE: (
        "purpose", "public-surface", "behaviour", "dependencies", "known-issues",
    ),
    DocKind.MODULE: (
        "purpose", "components", "relationships", "entry-points", "notes",
    ),
    DocKind.ARCHITECTURE: (
        "overview", "component-map", "data-flow", "entry-points",
        "extension-points", "operational-notes",
    ),
}


def anchor(qualname: str) -> str:
    """The in-page anchor for a symbol, e.g. `Client.send` -> `sym-Client-send`."""
    return "sym-" + re.sub(r"[^A-Za-z0-9_]+", "-", qualname).strip("-")


@dataclass(frozen=True)
class Link:
    """A reference to another document, optionally to a symbol inside it."""

    text: str
    target: str = ""      # document id; "" renders as plain text
    anchor: str = ""


@dataclass(frozen=True)
class Item:
    """One bullet or table row: text, the documents it points at, a line ref."""

    text: str = ""
    links: tuple[Link, ...] = ()
    line: int = 0


@dataclass(frozen=True)
class SymbolDoc:
    """One documented symbol. `signature` is verbatim source, never rebuilt."""

    id: str               # "<source path>#<qualname>"
    name: str
    qualname: str
    kind: str
    signature: str = ""
    summary: str = ""
    summary_source: str = ""   # "docstring" | "model" | ""
    line: int = 0
    end_line: int = 0
    is_async: bool = False
    decorators: tuple[str, ...] = ()
    bases: tuple[str, ...] = ()
    members: tuple[SymbolDoc, ...] = ()

    @property
    def anchor(self) -> str:
        return anchor(self.qualname)

    def walk(self) -> Iterator[SymbolDoc]:
        yield self
        for member in self.members:
            yield from member.walk()


@dataclass(frozen=True)
class Section:
    """A titled block. Which fields a renderer uses depends on `key`."""

    key: str
    title: str
    text: tuple[str, ...] = ()            # paragraphs
    items: tuple[Item, ...] = ()          # bullets, or table rows
    symbols: tuple[SymbolDoc, ...] = ()
    diagram: str = ""                     # Mermaid source
    ordered: bool = False                 # numbered steps rather than bullets


@dataclass(frozen=True)
class DocMeta:
    """Where a document came from and what it cost to write.

    Deliberately carries no timestamp: an unchanged source must render to
    byte-identical output, or every regeneration becomes a whole-tree diff.
    The run's timestamp lives once, in `RunInfo`.
    """

    mode: Mode = Mode.STUB
    source: str = ""            # file relpath, module dir, or "" for the project
    source_hash: str = ""
    language: str = ""
    model: str = ""             # model that wrote the prose; "" for stubs
    tier: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    confidence: str = "exact"   # parse confidence of the source
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Document:
    id: str                    # "file:<relpath>", "module:<dir>", "architecture"
    kind: DocKind
    title: str
    path: str                  # output path without extension, POSIX
    summary: str = ""          # one sentence, for index tables
    sections: tuple[Section, ...] = ()
    meta: DocMeta = field(default_factory=DocMeta)

    def section(self, key: str) -> Section | None:
        return next((s for s in self.sections if s.key == key), None)

    def symbols(self) -> Iterator[SymbolDoc]:
        for section in self.sections:
            for symbol in section.symbols:
                yield from symbol.walk()


@dataclass(frozen=True)
class RunInfo:
    """The run that produced a DocSet. The only place a timestamp appears."""

    generator: str = ""
    generated_at: str = ""
    git_commit: str = ""
    provider: str = ""
    requests: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class DocSet:
    """Every document of one project, plus the run that produced them."""

    project: str
    documents: list[Document] = field(default_factory=list)
    run: RunInfo | None = None
    description: str = ""
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self._by_id: dict[str, Document] = {d.id: d for d in self.documents}

    def add(self, document: Document) -> None:
        if document.id in self._by_id:
            raise ValueError(f"duplicate document id {document.id!r}")
        self.documents.append(document)
        self._by_id[document.id] = document

    def get(self, doc_id: str) -> Document | None:
        return self._by_id.get(doc_id)

    def by_kind(self, kind: DocKind) -> list[Document]:
        """The documents of `kind`, in reading order."""
        return [d for d in self.sorted() if d.kind is kind]

    def sorted(self) -> list[Document]:
        """Documents in reading order: the architecture, the modules by
        directory, then the files by source path (by title without one).

        Every home page, book, contents and bookmark list follows it, and so
        does the manifest. A tie falls to the id, so the order never depends
        on the order documents were added in.
        """
        return sorted(self.documents, key=_reading_key)

    def to_dict(self, *, include_run: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "project": self.project,
            "description": self.description,
            "documents": [_encode(d) for d in self.sorted()],
        }
        if include_run and self.run is not None:
            data["run"] = _encode(self.run)
        return data

    def to_json(self, *, include_run: bool = True) -> str:
        return json.dumps(
            self.to_dict(include_run=include_run), indent=2, ensure_ascii=False
        ) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DocSet:
        version = str(data.get("schema_version", ""))
        major = version.split(".", 1)[0]
        if major != SCHEMA_VERSION.split(".", 1)[0]:
            raise ValueError(
                f"document schema {version or '?'} is not readable by "
                f"schema {SCHEMA_VERSION}"
            )
        run = data.get("run")
        return cls(
            project=str(data.get("project", "")),
            description=str(data.get("description", "")),
            documents=[_decode(Document, d) for d in data.get("documents", [])],
            run=_decode(RunInfo, run) if run else None,
            schema_version=version,
        )

    @classmethod
    def from_json(cls, text: str) -> DocSet:
        return cls.from_dict(json.loads(text))


@dataclass(frozen=True)
class Layout:
    """Where each document lands, relative to the output directory.

    The default mirrors the source tree and keeps the extension, so
    `a/b.py` and `a/b.ts` cannot collide. `per_file` reproduces the v3 layout
    (`a/b.md`), keeping the extension where the shorter path is another
    file's, a module page's or one a renderer takes, as `Composer` finds.
    """

    per_file: bool = False

    def file(self, relpath: str, *, collides: bool = False) -> str:
        if self.per_file and not collides:
            stem, dot, _ = relpath.rpartition(".")
            return stem if dot else relpath
        return relpath

    @staticmethod
    def module(directory: str) -> str:
        return f"{directory}/index" if directory else "index"

    @staticmethod
    def architecture() -> str:
        return "architecture"

    @staticmethod
    def home() -> str:
        return "README"


# -- reading order ---------------------------------------------------------------

_RANK = {DocKind.ARCHITECTURE: 0, DocKind.MODULE: 1, DocKind.FILE: 2}


def _reading_key(document: Document) -> tuple[int, str, str, str]:
    """`DocSet.sorted`'s key: kind, the name the home page lists the
    document by, then path and id. A module's directory sorts its parent
    first, which its page path (`<dir>/index`) would not."""
    if document.kind is DocKind.MODULE:
        name = document.meta.source
    elif document.kind is DocKind.FILE:
        name = document.meta.source or document.title
    else:
        name = ""
    return (_RANK[document.kind], name, document.path, document.id)


# -- JSON encoding ---------------------------------------------------------------


def _encode(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple | list):
        return [_encode(v) for v in value]
    return value


def _decode(tp: Any, value: Any) -> Any:
    """Rebuild `tp` from its JSON form, strictly for the types used here."""
    if isinstance(tp, type) and is_dataclass(tp):
        if not isinstance(value, dict):
            raise ValueError(f"expected an object for {tp.__name__}")
        hints = typing.get_type_hints(tp)
        known = {f.name for f in fields(tp) if f.init}
        kwargs = {
            name: _decode(hints[name], item)
            for name, item in value.items()
            if name in known
        }
        return tp(**kwargs)

    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is tuple:
        return tuple(_decode(args[0], v) for v in value or ())
    if origin is list:
        return [_decode(args[0], v) for v in value or ()]
    if origin in (typing.Union, types.UnionType):
        if value is None:
            return None
        concrete = [a for a in args if a is not type(None)]
        return _decode(concrete[0], value)
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(value)
    if tp is float:
        return float(value)
    if tp in (int, str, bool):
        return tp(value)
    return value
