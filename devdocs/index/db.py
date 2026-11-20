"""The one SQLite file, its schema, and the migration chain.

Three things need to persist between runs: cached provider responses, embedded
chunks, and the ledger that records each run. They live in one file so a run
has one transaction boundary and one place to migrate, rather than three
databases that can disagree about what completed.

Migrations are an ordered list applied by `PRAGMA user_version`. They only ever
move forward; a database from a newer devdocs is refused rather than guessed
at. Each one commits together with its version bump, in a transaction that
takes the write lock before the version is read: a failure, Ctrl-C or crash
leaves the file at the old version or the new one, never in between, and a
second process opening the file meanwhile waits and then finds the work done.
`executescript` would commit statement by statement, so it is not used.

Connections are per-thread because `sqlite3.threadsafety` is 1 on some
builds, where sharing one across threads is unsafe. For the same reason
":memory:" is a private temporary file, deleted by `close`: a true in-memory
database lives in a single connection, which a thread pool would share.
With `create=False` a file that is not there yet is not made: the database is
then an empty one in memory, which this instance's connections share through
SQLite's shared cache, gone at `close`. It suits a command that only reads, as
`estimate` does: in a shared cache a reader fails at once on a table another
connection is writing, rather than waiting.

No raw `sqlite3.Error` leaves this module. Opening, migrating, `query` and
`transaction` raise StorageError, with a hint when the fix is known, and
repositories read through `query` and write through `transaction`.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
import threading
import time
import weakref
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from devdocs.errors import StorageError

__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "Database", "decode_text", "encode_text"]

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
    # 2 — cache records why generation stopped; per-day request counts
    """
    ALTER TABLE response_cache ADD COLUMN finish TEXT NOT NULL DEFAULT 'stop';
    ALTER TABLE response_cache ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0;

    CREATE TABLE usage_day (
        model       TEXT NOT NULL,
        day         TEXT NOT NULL,
        requests    INTEGER NOT NULL DEFAULT 0,
        cost_usd    REAL NOT NULL DEFAULT 0.0,
        PRIMARY KEY (model, day)
    );
    """,
    # 3 — the embedding model behind each stored vector, which ChunkStore
    # needs to tell a stale vector from a current one of the same size
    """
    ALTER TABLE chunk ADD COLUMN model TEXT NOT NULL DEFAULT '';
    """,
    # 4 — what nothing reads: the ledger's items, as the cache is what resumes a
    # run; its cost and request columns, which nothing ever filled; when a run
    # finished; and each chunk's text, symbol and end line, which a run has from
    # its scan. Rebuilt, as DROP COLUMN needs SQLite 3.35.
    """
    DROP TABLE run_item;

    CREATE TABLE run_v4 (
        id          TEXT PRIMARY KEY,
        started_at  REAL NOT NULL,
        status      TEXT NOT NULL DEFAULT 'running',
        config_hash TEXT NOT NULL DEFAULT ''
    );
    INSERT INTO run_v4 (id, started_at, status, config_hash)
        SELECT id, started_at, status, config_hash FROM run;
    DROP TABLE run;
    ALTER TABLE run_v4 RENAME TO run;

    CREATE TABLE chunk_v4 (
        id          TEXT PRIMARY KEY,
        source      TEXT NOT NULL,
        start_line  INTEGER NOT NULL DEFAULT 0,
        embedding   BLOB,
        dims        INTEGER NOT NULL DEFAULT 0,
        model       TEXT NOT NULL DEFAULT ''
    );
    INSERT INTO chunk_v4 (id, source, start_line, embedding, dims, model)
        SELECT id, source, start_line, embedding, dims, model FROM chunk;
    DROP TABLE chunk;
    ALTER TABLE chunk_v4 RENAME TO chunk;
    CREATE INDEX chunk_source ON chunk(source);
    """,
)

SCHEMA_VERSION = len(MIGRATIONS)

# sqlite3's failures, plus what it raises for a value it cannot bind: text
# holding a lone surrogate, or an int wider than 64 bits.
_FAILURES: tuple[type[Exception], ...] = (
    sqlite3.Error, UnicodeEncodeError, OverflowError,
)
_BUSY = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
_DAMAGED = frozenset({sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB})
_DENIED = frozenset(
    {sqlite3.SQLITE_PERM, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CANTOPEN}
)


def encode_text(text: str) -> str | bytes:
    """`text` in a form sqlite3 can bind.

    sqlite3 binds str as strict UTF-8, which refuses a lone surrogate: what an
    undecodable POSIX file name becomes in Python. Such text is stored as its
    surrogatepass bytes, a BLOB that `decode_text` turns back into the same
    str. Any other text is stored unchanged, so existing rows still match.
    """
    if text.isascii():
        return text
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogatepass")
    return text


def decode_text(value: str | bytes) -> str:
    """A column written through `encode_text`, as the original str."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "surrogatepass")
    return value


def _statements(script: str) -> list[str]:
    """`script` split into the single statements `execute` accepts."""
    statements: list[str] = []
    pending = ""
    for piece in script.split(";"):
        pending += piece + ";"
        # a ';' inside a literal, comment or trigger body does not end it
        if sqlite3.complete_statement(pending):
            if pending.strip(" \t\r\n;"):
                statements.append(pending.strip())
            pending = ""
    return statements


def _code(err: BaseException) -> int:
    """SQLite's primary result code behind `err`, 0 when it has none."""
    return int(getattr(err, "sqlite_errorcode", 0) or 0) & 0xFF


def _nothing_at(path: Path) -> bool:
    """Whether no file is at `path` yet. One that cannot be looked at is there:
    opening it says what is wrong, as it would without `create=False`."""
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


class Database:
    """Owns the connections and the schema; repositories borrow both.

    One instance per run. `connection` is thread-local, so a thread pool gets
    a connection each while still sharing one file and one schema. Reads go
    through `query` / `query_one` and writes through `transaction`; all three
    raise StorageError, never a raw sqlite3 error. With `create=False`, a file
    not there yet is not made, and the database is an empty one in memory.
    """

    def __init__(
        self,
        path: Path | str = ".devdocs/devdocs.db",
        *,
        timeout: float = 30.0,
        create: bool = True,
    ) -> None:
        self.path = Path(path)
        self.timeout = timeout          # seconds to wait for another writer
        self._memory = str(self.path) == ":memory:"
        self._local = threading.local()
        self._lock = threading.Lock()
        self._opened: list[
            tuple[weakref.ref[threading.Thread], sqlite3.Connection]
        ] = []
        self._scratch: tempfile.TemporaryDirectory[str] | None = None
        self._file: Path | str = self.path   # a str is a URI
        try:
            if self._memory:
                self._scratch = tempfile.TemporaryDirectory(
                    prefix="devdocs-", ignore_cleanup_errors=True
                )
                self._file = Path(self._scratch.name) / "devdocs.db"
            elif not create and _nothing_at(self.path):
                self._memory = True
                name = f"devdocs-{os.urandom(8).hex()}"   # one per instance
                self._file = f"file:{name}?mode=memory&cache=shared"
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            self.migrate()
        except OSError as err:
            self.close()
            raise StorageError(
                f"cannot create {self.path}: {err}", hint=self._hint(err)
            ) from err
        except BaseException:
            self.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        """This thread's connection, opened and configured on first use.

        Raises StorageError when the file cannot be opened.
        """
        existing: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if existing is None:
            existing = self._connect()
            self._local.conn = existing
        return existing

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                self._file, timeout=self.timeout, check_same_thread=False,
                uri=isinstance(self._file, str),
            )
        except sqlite3.Error as err:
            raise self._error(f"cannot open {self.path}", err) from err

        try:
            conn.row_factory = sqlite3.Row
            # WAL lets the pool's threads read the cache while one writes.
            self._enable_wal(conn)
            conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.Error as err:
            conn.close()
            raise self._error(f"cannot open {self.path}", err) from err
        except BaseException:
            conn.close()
            raise
        self._adopt(conn)
        return conn

    def _enable_wal(self, conn: sqlite3.Connection) -> None:
        """Switch to WAL, giving a process that opens the file too a fair wait.

        On a new file the switch upgrades a read lock to a write lock, and
        SQLite fails a contended upgrade at once instead of calling the busy
        handler, since waiting there could deadlock. Retrying the statement,
        which drops the read lock in between, waits the way `timeout` would.
        """
        deadline = time.monotonic() + self.timeout
        pause = 0.001
        while True:
            try:
                conn.execute("PRAGMA journal_mode = WAL").fetchall()
                return
            except sqlite3.OperationalError as err:
                if _code(err) not in _BUSY or time.monotonic() >= deadline:
                    raise
            time.sleep(pause)
            pause = min(pause * 2, 0.05)

    def _adopt(self, conn: sqlite3.Connection) -> None:
        """Track `conn` for `close`, closing those whose thread has ended."""
        owner = weakref.ref(threading.current_thread())
        with self._lock:
            kept = []
            for thread_ref, other in self._opened:
                thread = thread_ref()
                if thread is not None and thread.is_alive():
                    kept.append((thread_ref, other))
                else:
                    other.close()
            kept.append((owner, conn))
            self._opened = kept

    @property
    def version(self) -> int:
        """The schema version the file is at: its `PRAGMA user_version`."""
        row = self.query_one("PRAGMA user_version")
        return int(row[0]) if row is not None else 0

    def migrate(self) -> int:
        """Apply every migration the file has not seen. Returns the new version.

        Refuses a database written by a newer devdocs: downgrading silently
        would corrupt data the running version cannot understand.
        """
        current = self.version
        while current < SCHEMA_VERSION:
            current = self._step()
        if current > SCHEMA_VERSION:
            raise StorageError(
                f"{self.path} is schema v{current}, this devdocs understands "
                f"v{SCHEMA_VERSION}",
                hint=f"upgrade devdocs, or {self._rebuild()}",
            )
        return current

    def _step(self) -> int:
        """Apply the next migration and its version bump as one transaction.

        BEGIN IMMEDIATE takes the write lock before the version is read, so a
        process migrating the same file meanwhile is waited for, and its work
        is seen rather than repeated. Returns the version the file is now at.
        """
        conn = self.connection
        target = SCHEMA_VERSION
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if current < SCHEMA_VERSION:
                    target = current + 1
                    for statement in _statements(MIGRATIONS[current]):
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {target}")
                    current = target
                conn.commit()
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.rollback()
                raise
        except sqlite3.Error as err:
            raise self._error(f"migration to v{target} failed", err) from err
        return current

    def query(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        """Run one read and fetch every row. Failures raise StorageError."""
        try:
            rows: list[sqlite3.Row] = self.connection.execute(sql, params).fetchall()
        except _FAILURES as err:
            raise self._error(f"cannot read {self.path}", err) from err
        return rows

    def query_one(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Row | None:
        """The first row of a read, or None. Failures raise StorageError."""
        try:
            row: sqlite3.Row | None = self.connection.execute(sql, params).fetchone()
        except _FAILURES as err:
            raise self._error(f"cannot read {self.path}", err) from err
        return row

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One unit of work. Commits on success, rolls back on any exception.

        A failed statement or commit raises StorageError, as does the
        UnicodeEncodeError or OverflowError with which sqlite3 refuses a value
        it cannot bind. Anything else the body raises passes through.
        """
        conn = self.connection
        try:
            with conn:
                yield conn
        except _FAILURES as err:
            raise self._error("transaction failed", err) from err

    def _error(self, message: str, err: BaseException) -> StorageError:
        return StorageError(f"{message}: {err}", hint=self._hint(err))

    def _hint(self, err: BaseException) -> str | None:
        """What the user can do about `err`, when that is known."""
        if self._memory:
            return None
        code = _code(err)
        if code in _BUSY:
            return "another devdocs process is using the index; run again once it ends"
        if code in _DAMAGED:
            return f"the index is damaged: {self._rebuild()}"
        if code in _DENIED or isinstance(err, PermissionError):
            return f"devdocs needs write access to {self.path.parent}"
        if code == sqlite3.SQLITE_FULL:
            return "the disk is full; free some space and run again"
        return None

    def _rebuild(self) -> str:
        """How to start the index afresh: the WAL files must go with the file."""
        if self.path.parent.name == ".devdocs":
            return f"delete {self.path.parent}{os.sep} to rebuild the index"
        return f"delete {self.path} and its -wal and -shm files to rebuild the index"

    def close(self) -> None:
        """Close every connection this instance opened, in any thread.

        A ":memory:" database is deleted with them, and one in memory is gone.
        """
        with self._lock:
            opened, self._opened = self._opened, []
        for _, conn in opened:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        self._local = threading.local()
        if self._scratch is not None:
            self._scratch.cleanup()
            self._scratch = None

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
