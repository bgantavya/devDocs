"""Source split into retrievable pieces at symbol boundaries.

A chunk is one top-level declaration (a long class splits into its methods),
plus one header chunk per file for its docstring and imports. Symbol
boundaries keep each piece meaningful on its own, which is what retrieval
returns and what an embedding summarises.

Chunk ids are content addresses — a hash of the file, the symbol and the text
— so an unchanged declaration keeps its id across runs, and with it the
embedding already paid for. `ChunkStore` replaces a file's chunks wholesale
but keeps every row whose id survives, which is what makes re-indexing an
edited repository cost only the edits. A file left out of one run keeps its
vectors too, as they stay valid and a later run may select it again.

A chunk's text may hold lone surrogates, as a module docstring that spells one
as an escape parses to, and its source too, as an undecodable POSIX file name
reads. So the id hashes them losslessly, and a source is stored through
`encode_text`; valid text hashes and stores as ever, so existing rows match.
"""

from __future__ import annotations

import hashlib
from array import array
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass

from devdocs.index.db import Database, decode_text, encode_text
from devdocs.parse import FileFacts, Symbol, SymbolKind

__all__ = ["Chunk", "ChunkStore", "chunk_file"]

_MAX_LINES = 60
_MAX_CHARS = 2400


@dataclass(frozen=True)
class Chunk:
    id: str
    source: str        # relpath
    symbol: str        # qualified name; "" for the file header
    start_line: int
    end_line: int
    text: str


def _chunk(source: str, symbol: str, start: int, end: int, body: str) -> Chunk:
    label = f"{source} :: {symbol}" if symbol else source
    text = f"{label}\n{body}"
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS].rsplit("\n", 1)[0]
    identity = f"{source}\0{symbol}\0{text}".encode("utf-8", "surrogatepass")
    digest = hashlib.sha256(identity).hexdigest()
    return Chunk(digest[:32], source, symbol, start, end, text)


def _span(symbol: Symbol, following: int, total: int) -> tuple[int, int]:
    """Lines a symbol covers; an unknown end runs to the next declaration."""
    end = symbol.end_line or (following - 1 if following else total)
    return symbol.line, max(symbol.line, min(end, total))


def chunk_file(relpath: str, source: str, facts: FileFacts | None) -> list[Chunk]:
    """The chunks of one file, in source order."""
    lines = source.splitlines()
    if not lines:
        return []
    chunks: list[Chunk] = []
    symbols = sorted(
        (s for s in (facts.symbols if facts else ()) if s.line),
        key=lambda s: s.line,
    )

    first = symbols[0].line if symbols else len(lines) + 1
    header = "\n".join(lines[: min(first - 1, _MAX_LINES)]).strip()
    docstring = facts.module_docstring.strip() if facts is not None else ""
    if docstring and docstring not in header:
        header = f"{docstring}\n{header}".strip()
    if header:
        chunks.append(_chunk(relpath, "", 1, min(first - 1, _MAX_LINES) or 1, header))

    for index, symbol in enumerate(symbols):
        following = symbols[index + 1].line if index + 1 < len(symbols) else 0
        start, end = _span(symbol, following, len(lines))
        if symbol.kind is SymbolKind.CONSTANT:
            continue                      # constants travel with the header
        long_class = (
            symbol.kind in (SymbolKind.CLASS, SymbolKind.INTERFACE)
            and end - start + 1 > _MAX_LINES
            and symbol.children
        )
        if not long_class:
            body = "\n".join(lines[start - 1 : min(end, start + _MAX_LINES - 1)])
            chunks.append(_chunk(relpath, symbol.name, start, end, body))
            continue
        # A long class: its header and docstring, then one chunk per member.
        members = sorted((c for c in symbol.children if c.line), key=lambda c: c.line)
        head_end = (members[0].line - 1) if members else start + _MAX_LINES - 1
        head = "\n".join(lines[start - 1 : min(head_end, start + _MAX_LINES - 1)])
        chunks.append(_chunk(relpath, symbol.name, start, head_end, head))
        for position, member in enumerate(members):
            last = position + 1 == len(members)
            after = end + 1 if last else members[position + 1].line
            m_start, m_end = _span(member, after, end)
            body = "\n".join(lines[m_start - 1 : min(m_end, m_start + _MAX_LINES - 1)])
            chunks.append(_chunk(
                relpath, f"{symbol.name}.{member.name}", m_start, m_end, body
            ))
    return chunks


class ChunkStore:
    """Repository over the `chunk` table: which chunks are known, and the
    vectors bought for them.

    A chunk's text comes from a run's scan, not from here: the store also
    holds the ids of files the run left out, so what it is asked about a run,
    it is asked about that run's chunks.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def replace(self, source: str, chunks: Sequence[Chunk]) -> tuple[int, int]:
        """Make `chunks` the stored chunks of `source`. Rows whose id survives
        keep their vectors. Returns (added, removed)."""
        ids = {chunk.id for chunk in chunks}
        with self.db.transaction() as conn:
            existing = {
                row["id"] for row in conn.execute(
                    "SELECT id FROM chunk WHERE source = ?", (encode_text(source),)
                )
            }
            stale = existing - ids
            conn.executemany("DELETE FROM chunk WHERE id = ?", [(i,) for i in stale])
            fresh = [c for c in chunks if c.id not in existing]
            conn.executemany(
                "INSERT OR IGNORE INTO chunk (id, source, start_line) "
                "VALUES (?, ?, ?)",
                [(c.id, encode_text(c.source), c.start_line) for c in fresh],
            )
        return len(fresh), len(stale)

    def prune(self, exists: Callable[[str], bool]) -> int:
        """Drop the chunks of files no longer in the project, which `exists`
        tells of each source stored. Returns how many."""
        with self.db.transaction() as conn:
            gone = [
                row["source"]
                for row in conn.execute("SELECT DISTINCT source FROM chunk")
                if not exists(decode_text(row["source"]))
            ]
            removed = 0
            for source in gone:
                removed += conn.execute(
                    "DELETE FROM chunk WHERE source = ?", (source,)
                ).rowcount
        return removed

    def missing(self, chunks: Iterable[Chunk], model: str, dims: int) -> list[Chunk]:
        """Those of `chunks` with no vector from this model at this size, each
        once: two identical declarations in a file are one chunk."""
        rows = self.db.query(
            "SELECT id FROM chunk "
            "WHERE embedding IS NOT NULL AND model = ? AND dims = ?",
            (model, dims),
        )
        have = {row["id"] for row in rows}
        return list({c.id: c for c in chunks if c.id not in have}.values())

    def store(self, vectors: Sequence[tuple[str, Sequence[float]]], model: str) -> None:
        with self.db.transaction() as conn:
            conn.executemany(
                "UPDATE chunk SET embedding = ?, dims = ?, model = ? WHERE id = ?",
                [(array("f", vector).tobytes(), len(vector), model, chunk_id)
                 for chunk_id, vector in vectors],
            )

    def vectors(
        self, model: str, dims: int, among: Collection[str]
    ) -> tuple[list[str], list[array[float]]]:
        """Ids and vectors stored for `model` at `dims`, of the chunks `among`
        these ids, in a stable order."""
        rows = self.db.query(
            "SELECT id, embedding FROM chunk WHERE model = ? AND dims = ? "
            "AND embedding IS NOT NULL ORDER BY source, start_line, id",
            (model, dims),
        )
        ids: list[str] = []
        vectors: list[array[float]] = []
        for row in rows:
            if row["id"] not in among:
                continue
            vector: array[float] = array("f")
            vector.frombytes(row["embedding"])
            ids.append(row["id"])
            vectors.append(vector)
        return ids, vectors
