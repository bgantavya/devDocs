"""Turning source into facts: symbols, imports, and exports.

`Parser` is the extension point you are most likely to use. It is a
`typing.Protocol`, so a new language parser needs no base class and no
registration boilerplate beyond one `register` call — see `generic.py` for the
smallest possible implementation.

Facts extracted here do two jobs: they let the planner score a file without
spending a token, and they let the renderer verify that every symbol a model
mentions actually exists. That second job is why `Symbol` carries line numbers
and signatures rather than free text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "Confidence",
    "FileFacts",
    "Parser",
    "Symbol",
    "SymbolKind",
    "get_parser",
    "register",
    "supported_languages",
]


class SymbolKind(StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    CONSTANT = "constant"
    TYPE = "type"
    INTERFACE = "interface"


class Confidence(StrEnum):
    """How far to trust the facts from this file.

    EXACT means a real parser produced them. DEGRADED means a fallback did, so
    the symbol table may be incomplete and the renderer must not claim
    completeness. FAILED means nothing usable came out.
    """

    EXACT = "exact"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True)
class Symbol:
    """One named thing a file defines.

    `signature` is rendered verbatim into docs, so it must come from the source
    rather than be reconstructed, or generated docs will describe an API that
    does not exist.
    """

    name: str
    kind: SymbolKind
    line: int = 0
    end_line: int = 0
    signature: str = ""
    docstring: str = ""
    decorators: tuple[str, ...] = ()
    bases: tuple[str, ...] = ()
    children: tuple[Symbol, ...] = ()
    is_public: bool = True
    is_async: bool = False

    @property
    def qualified_name(self) -> str:
        return self.name

    def walk(self) -> list[Symbol]:
        """This symbol and every descendant, depth first."""
        found = [self]
        for child in self.children:
            found.extend(child.walk())
        return found


@dataclass
class FileFacts:
    """Everything static analysis learned about one file.

    `confidence` and `notes` exist so a degraded parse is visible downstream
    instead of silently producing a thinner symbol table that looks complete.
    """

    path: Path
    language: str
    symbols: tuple[Symbol, ...] = ()
    imports: tuple[str, ...] = ()
    exports: tuple[str, ...] = ()
    module_docstring: str = ""
    loc: int = 0
    confidence: Confidence = Confidence.EXACT
    notes: list[str] = field(default_factory=list)

    @property
    def public_symbols(self) -> tuple[Symbol, ...]:
        """Top-level symbols forming the file's API.

        Honours `exports` (Python's __all__, JS export lists) when present,
        because an explicit export list is the author's own statement of what
        is public and beats any naming heuristic.
        """
        if self.exports:
            named = set(self.exports)
            return tuple(s for s in self.symbols if s.name in named)
        return tuple(s for s in self.symbols if s.is_public)

    @property
    def all_symbols(self) -> list[Symbol]:
        found: list[Symbol] = []
        for symbol in self.symbols:
            found.extend(symbol.walk())
        return found

    @property
    def symbol_names(self) -> frozenset[str]:
        """Every defined name, for verifying model output against reality."""
        return frozenset(symbol.name for symbol in self.all_symbols)

    @property
    def is_usable(self) -> bool:
        return self.confidence is not Confidence.FAILED


@runtime_checkable
class Parser(Protocol):
    """Extracts facts from one file's source.

    Implementations must not raise: a file that cannot be parsed returns
    FileFacts with a lower `confidence`, because one bad file must never end a
    run over thousands.
    """

    languages: tuple[str, ...]

    def parse(self, path: Path, source: str) -> FileFacts: ...


_REGISTRY: dict[str, Parser] = {}


def register(parser: Parser) -> Parser:
    """Register a parser for each language it declares. Returns it unchanged.

    Usable as a decorator on the class, since it hands the object back.
    """
    for language in parser.languages:
        _REGISTRY[language] = parser
    return parser


def get_parser(language: str) -> Parser | None:
    return _REGISTRY.get(language)


def supported_languages() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))
