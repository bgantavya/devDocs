"""The one SQLite connection, its schema, and the migration chain.

Three things need to persist between runs: cached provider responses, embedded
chunks, and the ledger that lets an interrupted run resume. They live in one
file so a run has one transaction boundary and one place to migrate, rather
than three databases that can disagree about what completed.

Migrations are an ordered list applied by `PRAGMA user_version`. They only ever
move forward; a database from a newer devdocs is refused rather than guessed
at. Connections are per-thread because `sqlite3.threadsafety` is 1 on some
builds, where sharing one across threads raises.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from devdocs.errors import StorageError

__all__ = ["Database", "MIGRATIONS", "SCHEMA_VERSION"]

# Append only. Index + 1 is the user_version a migration produces.
MIGRATIONS: tuple[str, ...] = (
    # 1 — cached responses, chunk store, run ledger
    """
    CREATE TABLE response_cache (
        key         TEXT PRIMARY KEY,
        provider    TEXT NOT NULL,
        model       TEXT NOT NULL,
        content     TEXT NOT NULL,
        input_tokens  INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd    REAL NOT NULL DEFAULT 0.0,
        created_at  REAL NOT NULL
    );

    CREATE TABLE chunk (
        id          TEXT PRIMARY KEY,
        source      TEXT NOT NULL,
        symbol      TEXT NOT NULL DEFAULT '',
        start_line  INTEGER NOT NULL DEFAULT 0,
        end_line    INTEGER NOT NULL DEFAULT 0,
        text        TEXT NOT NULL,
        embedding   BLOB,
        dims        INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX chunk_source ON chunk(source);

    CREATE TABLE run (
        id          TEXT PRIMARY KEY,
        started_at  REAL NOT NULL,
        finished_at REAL,
        status      TEXT NOT NULL DEFAULT 'running',
        config_hash TEXT NOT NULL DEFAULT '',
        cost_usd    REAL NOT NULL DEFAULT 0.0,
        requests    INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE run_item (
        run_id      TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
        source      TEXT NOT NULL,
        kind        TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'pending',
        content_hash TEXT NOT NULL DEFAULT '',
        cost_usd    REAL NOT NULL DEFAULT 0.0,
        error       TEXT,
        PRIMARY KEY (run_id, source, kind)
    );
    CREATE INDEX run_item_status ON run_item(run_id, status);
    """,
)

SCHEMA_VERSION = len(MIGRATIONS)


class Database:
    """Owns the connection and the schema; repositories borrow both.

    One instance per run. `connection` is thread-local, so a thread pool gets a
    connection each while still sharing one file and one schema.
    """

    def __init__(self, path: Path | str = ".devdocs/devdocs.db") -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._memory = str(self.path) == ":memory:"
        if not self._memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._shared: sqlite3.Connection | None = None
        self.migrate()

    @property
    def connection(self) -> sqlite3.Connection:
        """This thread's connection, opened and configured on first use.

        An in-memory database cannot be reopened per thread without losing its
        contents, so that case keeps a single shared connection.
        """
        if self._memory:
            if self._shared is None:
                self._shared = self._connect()
            return self._shared

        existing = getattr(self._local, "conn", None)
        if existing is None:
            existing = self._connect()
            self._local.conn = existing
        return existing

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                self.path, timeout=30.0, check_same_thread=False
            )
        except sqlite3.Error as err:
            raise StorageError(f"cannot open {self.path}: {err}") from err

        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        if not self._memory:
            # WAL lets readers proceed during a write, which matters because
            # generation holds a transaction while waiting on the network.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @property
    def version(self) -> int:
        row = self.connection.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def migrate(self) -> int:
        """Apply every migration the file has not seen. Returns the new version.

        Refuses a database written by a newer devdocs: downgrading silently
        would corrupt data the running version cannot understand.
        """
        conn = self.connection
        current = self.version

        if current > SCHEMA_VERSION:
            raise StorageError(
                f"{self.path} is schema v{current}, this devdocs understands "
                f"v{SCHEMA_VERSION}",
                hint="upgrade devdocs, or delete .devdocs/ to rebuild the index",
            )

        for index in range(current, SCHEMA_VERSION):
            try:
                with conn:
                    conn.executescript(MIGRATIONS[index])
                    conn.execute(f"PRAGMA user_version = {index + 1}")
            except sqlite3.Error as err:
                raise StorageError(
                    f"migration to v{index + 1} failed: {err}"
                ) from err

        return self.version

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One unit of work. Commits on success, rolls back on any exception."""
        conn = self.connection
        try:
            with conn:
                yield conn
        except sqlite3.Error as err:
            raise StorageError(f"transaction failed: {err}") from err

    def close(self) -> None:
        for conn in (getattr(self._local, "conn", None), self._shared):
            if conn is not None:
                conn.close()
        self._local = threading.local()
        self._shared = None

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
