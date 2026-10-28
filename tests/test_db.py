"""Migrations, transaction boundaries, and thread-local connections."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from devdocs.errors import StorageError
from devdocs.index import MIGRATIONS, SCHEMA_VERSION, Database

_connect: Callable[..., sqlite3.Connection] = sqlite3.connect
_POSIX_PERMISSIONS = os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0


def scratch(case: unittest.TestCase) -> Path:
    """A directory removed when `case` ends."""
    return Path(case.enterContext(tempfile.TemporaryDirectory()))


def build(path: Path, upto: int) -> None:
    """A file migrated the way earlier releases did it, with executescript."""
    conn = _connect(path)
    for index in range(upto):
        conn.executescript(MIGRATIONS[index])
        conn.execute(f"PRAGMA user_version = {index + 1}")
        conn.commit()
    conn.close()


def inspect(path: Path, sql: str) -> list[Any]:
    conn = _connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def version_of(path: Path) -> int:
    return int(inspect(path, "PRAGMA user_version")[0][0])


def columns(path: Path, table: str) -> list[str]:
    return [row[1] for row in inspect(path, f"PRAGMA table_info({table})")]


def schema(path: Path) -> list[Any]:
    return inspect(
        path,
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY name",
    )


@contextlib.contextmanager
def connections(adjust: Callable[[sqlite3.Connection], None] | None = None,
                **extra: Any) -> Iterator[None]:
    """Have every connection Database opens made with `extra`, then `adjust`ed."""

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = _connect(*args, **kwargs, **extra)
        if adjust is not None:
            adjust(conn)
        return conn

    with mock.patch.object(sqlite3, "connect", side_effect=connect):
        yield


class InterruptBeforeBump(sqlite3.Connection):
    """Ctrl-C lands after a migration's statements, before its version bump."""

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql.startswith("PRAGMA user_version =") and not sql.endswith("= 1"):
            raise KeyboardInterrupt
        return super().execute(sql, *args)


def deny_version_bump(action: int, arg1: str | None, arg2: str | None,
                      _db: str | None, _source: str | None) -> int:
    """Authorizer that fails the bump itself, as a full disk or I/O error would."""
    if action == sqlite3.SQLITE_PRAGMA and arg1 == "user_version" and arg2 not in (
        None, "1",
    ):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class TestMigrations(unittest.TestCase):
    def setUp(self) -> None:
        self.path = scratch(self) / "devdocs.db"

    def test_fresh_database_is_at_head(self) -> None:
        with Database(self.path) as db:
            self.assertEqual(db.version, SCHEMA_VERSION)

    def test_expected_tables_exist(self) -> None:
        with Database(self.path) as db:
            rows = db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            names = {row[0] for row in rows}
        self.assertLessEqual({"response_cache", "chunk", "run", "run_item"}, names)

    def test_migrate_is_idempotent(self) -> None:
        with Database(self.path) as db:
            self.assertEqual(db.migrate(), db.migrate())

    def test_reopening_does_not_remigrate(self) -> None:
        Database(self.path).close()
        with Database(self.path) as db:
            self.assertEqual(db.version, SCHEMA_VERSION)

    def test_newer_schema_is_refused(self) -> None:
        Database(self.path).close()
        conn = sqlite3.connect(self.path)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 50}")
        conn.commit()
        conn.close()
        with self.assertRaises(StorageError) as caught:
            Database(self.path)
        self.assertIn("upgrade devdocs", str(caught.exception))

    def test_parent_directory_is_created(self) -> None:
        nested = scratch(self) / "a" / "b" / "devdocs.db"
        with Database(nested) as db:
            self.assertTrue(db.path.exists())

    def test_chunk_vectors_record_their_model(self) -> None:
        # ChunkStore keys a vector by model and size; the column must exist.
        with Database(self.path):
            pass
        self.assertIn("model", columns(self.path, "chunk"))


class TestAtomicMigrations(unittest.TestCase):
    """A migration and its version bump commit together or not at all."""

    def setUp(self) -> None:
        self.path = scratch(self) / "devdocs.db"
        build(self.path, 1)   # an index from the first release, due migration 2

    def assert_clean_v1_then_upgrades(self) -> None:
        self.assertEqual(version_of(self.path), 1)
        self.assertNotIn("finish", columns(self.path, "response_cache"))
        with Database(self.path) as db:
            self.assertEqual(db.version, SCHEMA_VERSION)
        self.assertIn("finish", columns(self.path, "response_cache"))

    def test_failed_version_bump_rolls_the_migration_back(self) -> None:
        def adjust(conn: sqlite3.Connection) -> None:
            conn.set_authorizer(deny_version_bump)

        with connections(adjust), self.assertRaises(StorageError) as caught:
            Database(self.path)
        self.assertIn("migration to v2 failed", str(caught.exception))
        self.assert_clean_v1_then_upgrades()

    def test_interrupt_mid_migration_leaves_the_old_version(self) -> None:
        with connections(factory=InterruptBeforeBump), \
                self.assertRaises(KeyboardInterrupt):
            Database(self.path)
        self.assert_clean_v1_then_upgrades()

    def test_a_migration_applied_meanwhile_is_not_applied_again(self) -> None:
        # Another process migrated between our version check and our lock.
        other = scratch(self) / "devdocs.db"
        Database(other).close()

        class Raced(Database):
            stale = True

            @property
            def version(self) -> int:
                if Raced.stale:
                    Raced.stale = False
                    return 0
                return super().version

        with Raced(other) as db:
            self.assertEqual(db.version, SCHEMA_VERSION)

    def test_upgrade_matches_a_fresh_database(self) -> None:
        old = scratch(self) / "devdocs.db"
        build(old, 2)   # what existing users have on disk
        fresh = scratch(self) / "devdocs.db"
        Database(old).close()
        Database(fresh).close()
        self.assertEqual(schema(old), schema(fresh))

    def test_statements_have_the_effect_of_the_whole_script(self) -> None:
        scripted = scratch(self) / "devdocs.db"
        build(scripted, SCHEMA_VERSION)
        fresh = scratch(self) / "devdocs.db"
        Database(fresh).close()
        self.assertEqual(schema(scripted), schema(fresh))

    def test_rows_survive_the_upgrade(self) -> None:
        conn = _connect(self.path)
        conn.execute(
            "INSERT INTO response_cache (key, provider, model, content, created_at) "
            "VALUES ('k', 'gemini', 'm', 'paid for', 1.0)"
        )
        conn.commit()
        conn.close()
        with Database(self.path) as db:
            row = db.query_one("SELECT content, finish FROM response_cache")
        assert row is not None
        self.assertEqual((row[0], row[1]), ("paid for", "stop"))


class TestOpenFailures(unittest.TestCase):
    """Whatever is wrong with the file, opening it raises StorageError."""

    def setUp(self) -> None:
        self.good = scratch(self) / "devdocs.db"
        Database(self.good).close()
        self.state = scratch(self) / ".devdocs"
        self.state.mkdir()
        self.path = self.state / "devdocs.db"

    def assert_storage_error(self, *hint: str) -> StorageError:
        with self.assertRaises(StorageError) as caught:
            Database(self.path)
        for words in hint:
            self.assertIn(words, caught.exception.hint or "")
        return caught.exception

    def test_truncated_index(self) -> None:
        self.path.write_bytes(self.good.read_bytes()[:200])
        error = self.assert_storage_error("rebuild the index")
        self.assertIn(str(self.state), error.hint or "")

    def test_file_that_is_not_a_database(self) -> None:
        self.path.write_text("version https://git-lfs.github.com/spec/v1\n")
        self.assert_storage_error("rebuild the index")

    def test_path_that_is_a_directory(self) -> None:
        self.path.mkdir()
        self.assert_storage_error()

    @unittest.skipUnless(_POSIX_PERMISSIONS, "needs POSIX permissions, not root")
    def test_unwritable_state_directory(self) -> None:
        self.path.write_bytes(self.good.read_bytes())
        os.chmod(self.state, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, self.state, stat.S_IRWXU)
        self.assert_storage_error("write access")

    @unittest.skipUnless(_POSIX_PERMISSIONS, "needs POSIX permissions, not root")
    def test_state_directory_that_cannot_be_created(self) -> None:
        root = scratch(self)
        os.chmod(root, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, root, stat.S_IRWXU)
        with self.assertRaises(StorageError) as caught:
            Database(root / ".devdocs" / "devdocs.db")
        self.assertIn("write access", caught.exception.hint or "")


class TestConcurrentOpen(unittest.TestCase):
    """A second process creating the same index at the same moment."""

    def setUp(self) -> None:
        self.path = scratch(self) / "devdocs.db"
        # Holds the write lock on the new file, as the other process would.
        self.holder = _connect(self.path, isolation_level=None)
        self.addCleanup(self.holder.close)
        self.holder.execute("BEGIN IMMEDIATE")

    def test_open_waits_for_the_lock_instead_of_failing(self) -> None:
        def other_process_finishes(_seconds: float) -> None:
            if self.holder.in_transaction:
                self.holder.execute("ROLLBACK")

        with mock.patch("devdocs.index.db.time.sleep",
                        side_effect=other_process_finishes) as waited, \
                Database(self.path) as db:
            row = db.query_one("PRAGMA journal_mode")
        self.assertTrue(waited.called)
        assert row is not None
        self.assertEqual(row[0], "wal")

    def test_lock_held_past_the_timeout_is_a_storage_error(self) -> None:
        with self.assertRaises(StorageError) as caught:
            Database(self.path, timeout=0)
        self.assertIn("another devdocs process", caught.exception.hint or "")


class TestBehaviour(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(scratch(self) / "devdocs.db")
        self.addCleanup(self.db.close)

    def test_wal_is_enabled(self) -> None:
        mode = self.db.connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_foreign_keys_are_enforced(self) -> None:
        with self.assertRaises(StorageError), self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO run_item(run_id, source, kind) VALUES('ghost','a','file')"
            )

    def test_transaction_rolls_back_on_error(self) -> None:
        with self.assertRaises(RuntimeError), self.db.transaction() as conn:
            conn.execute("INSERT INTO run(id, started_at) VALUES('r1', 1.0)")
            raise RuntimeError("boom")
        count = self.db.connection.execute("SELECT count(*) FROM run").fetchone()[0]
        self.assertEqual(count, 0)

    def test_unbindable_value_is_a_storage_error(self) -> None:
        with self.assertRaises(StorageError), self.db.transaction() as conn:
            conn.execute("INSERT INTO run(id, started_at) VALUES(?, 1.0)", ("\udce9",))
        with self.assertRaises(StorageError):
            self.db.query("SELECT ?", (2**70,))

    def test_failed_reads_are_storage_errors(self) -> None:
        with self.assertRaises(StorageError):
            self.db.query("SELECT * FROM no_such_table")
        with self.assertRaises(StorageError):
            self.db.query_one("SELECT * FROM no_such_table")

    def test_query_returns_rows_by_name(self) -> None:
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO run(id, started_at) VALUES('r1', 1.0)")
        rows = self.db.query("SELECT id FROM run WHERE started_at = ?", (1.0,))
        self.assertEqual([row["id"] for row in rows], ["r1"])
        self.assertIsNone(self.db.query_one("SELECT id FROM run WHERE id = 'none'"))

    def test_delete_cascades_to_items(self) -> None:
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO run(id, started_at) VALUES('r1', 1.0)")
            conn.execute(
                "INSERT INTO run_item(run_id, source, kind) VALUES('r1','a.py','file')"
            )
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM run WHERE id='r1'")
        rows = self.db.connection.execute("SELECT count(*) FROM run_item")
        self.assertEqual(rows.fetchone()[0], 0)

    def test_concurrent_writers_all_commit(self) -> None:
        self.assertEqual(writers(self.db), ([], 160))

    def test_close_reaches_every_thread_connection(self) -> None:
        opened: list[sqlite3.Connection] = []
        worker = threading.Thread(target=lambda: opened.append(self.db.connection))
        worker.start()
        worker.join()
        self.db.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")


def writers(db: Database) -> tuple[list[str], int]:
    """Eight threads inserting twenty rows each; returns (errors, rows)."""
    errors: list[str] = []

    def write(worker: int) -> None:
        try:
            for index in range(20):
                with db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO run(id, started_at) VALUES(?, ?)",
                        (f"r{worker}-{index}", 1.0),
                    )
        except Exception as err:  # surfaced via the assertion below
            errors.append(repr(err))

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    row = db.query_one("SELECT count(*) FROM run")
    return errors, int(row[0]) if row is not None else 0


class TestInMemory(unittest.TestCase):
    def test_in_memory_keeps_its_contents(self) -> None:
        with Database(":memory:") as db:
            with db.transaction() as conn:
                conn.execute("INSERT INTO run(id, started_at) VALUES('r', 1.0)")
            count = db.connection.execute("SELECT count(*) FROM run").fetchone()[0]
        self.assertEqual(count, 1)

    def test_threads_get_their_own_connection_to_the_same_data(self) -> None:
        with Database(":memory:") as db:
            with db.transaction() as conn:
                conn.execute("INSERT INTO run(id, started_at) VALUES('r', 1.0)")
            seen: dict[str, Any] = {}

            def look() -> None:
                seen["conn"] = db.connection
                seen["rows"] = db.query("SELECT id FROM run")

            worker = threading.Thread(target=look)
            worker.start()
            worker.join()
            self.assertIsNot(seen["conn"], db.connection)
            self.assertEqual([row["id"] for row in seen["rows"]], ["r"])

    def test_concurrent_writers_all_commit(self) -> None:
        with Database(":memory:") as db:
            self.assertEqual(writers(db), ([], 160))

    def test_close_deletes_the_backing_file(self) -> None:
        db = Database(":memory:")
        row = db.query_one("PRAGMA database_list")
        assert row is not None
        backing = Path(row["file"])
        self.assertTrue(backing.exists())
        db.close()
        self.assertFalse(backing.exists())


class TestNotCreated(unittest.TestCase):
    """`create=False`: a file not there yet is not made; the database is an
    empty one in memory instead, for a command that only reads."""

    def setUp(self) -> None:
        self.state = scratch(self) / ".devdocs"
        self.path = self.state / "devdocs.db"

    def test_nothing_is_made_on_disk(self) -> None:
        with Database(self.path, create=False) as db:
            self.assertEqual(db.version, SCHEMA_VERSION)
            self.assertEqual(db.query("SELECT key FROM response_cache"), [])
            row = db.query_one("PRAGMA database_list")
            assert row is not None
            self.assertEqual(row["file"], "")   # SQLite's word for in memory
        self.assertFalse(self.state.exists())

    def test_threads_share_it(self) -> None:
        with Database(self.path, create=False) as db:
            with db.transaction() as conn:
                conn.execute("INSERT INTO run(id, started_at) VALUES('r', 1.0)")
            seen: dict[str, Any] = {}

            def look() -> None:
                seen["conn"] = db.connection
                seen["rows"] = db.query("SELECT id FROM run")

            worker = threading.Thread(target=look)
            worker.start()
            worker.join()
            self.assertIsNot(seen["conn"], db.connection)
            self.assertEqual([row["id"] for row in seen["rows"]], ["r"])

    def test_each_is_its_own(self) -> None:
        with Database(self.path, create=False) as first:
            with first.transaction() as conn:
                conn.execute("INSERT INTO run(id, started_at) VALUES('r', 1.0)")
            with Database(self.path, create=False) as second:
                self.assertEqual(second.query("SELECT id FROM run"), [])
        self.assertFalse(self.state.exists())

    def test_it_is_gone_at_close(self) -> None:
        # Under one name, the next one would see this row had it outlived close.
        with mock.patch("devdocs.index.db.os.urandom", return_value=bytes(8)):
            with Database(self.path, create=False) as first, \
                    first.transaction() as conn:
                conn.execute("INSERT INTO run(id, started_at) VALUES('r', 1.0)")
            with Database(self.path, create=False) as again:
                self.assertEqual(again.query("SELECT id FROM run"), [])
        self.assertFalse(self.state.exists())

    def test_a_state_folder_that_is_a_file_is_left_as_it_is(self) -> None:
        # Nothing can be in it: the database is an empty one, as with no folder.
        self.state.write_text("not a folder\n", "utf-8")
        with Database(self.path, create=False) as db:
            self.assertEqual(db.query("SELECT key FROM response_cache"), [])
        self.assertEqual(self.state.read_text("utf-8"), "not a folder\n")

    def test_its_errors_give_no_hint_meant_for_a_file(self) -> None:
        # A reader fails at once on a table being written, in a shared cache:
        # no other process is in the way, as the hint for a lock would say.
        with Database(self.path, create=False) as db:
            holding, release = threading.Event(), threading.Event()

            def write() -> None:
                with db.transaction() as conn:
                    conn.execute("INSERT INTO run(id, started_at) VALUES('w', 1.0)")
                    holding.set()
                    release.wait(10)

            writer = threading.Thread(target=write)
            writer.start()
            try:
                self.assertTrue(holding.wait(10))
                with self.assertRaises(StorageError) as caught:
                    db.query("SELECT id FROM run")
            finally:
                release.set()
                writer.join()
        self.assertIn("locked", caught.exception.message)
        self.assertIsNone(caught.exception.hint)

    def test_a_file_that_is_there_is_opened(self) -> None:
        with Database(self.path) as made, made.transaction() as conn:
            conn.execute("INSERT INTO run(id, started_at) VALUES('r', 1.0)")
        with Database(self.path, create=False) as db:
            self.assertEqual([row["id"] for row in db.query("SELECT id FROM run")],
                             ["r"])
            row = db.query_one("PRAGMA database_list")
            assert row is not None
            self.assertEqual(Path(row["file"]).resolve(), self.path.resolve())

    @unittest.skipUnless(_POSIX_PERMISSIONS, "needs POSIX permissions, not root")
    def test_a_file_that_cannot_be_looked_at_is_not_taken_for_none(self) -> None:
        # It may hold the answers paid for: say so, as opening it always has.
        self.state.mkdir()
        os.chmod(self.state, 0)
        self.addCleanup(os.chmod, self.state, stat.S_IRWXU)
        with self.assertRaises(StorageError) as caught:
            Database(self.path, create=False)
        self.assertIn("write access", caught.exception.hint or "")


if __name__ == "__main__":
    unittest.main()
