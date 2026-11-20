"""Migrations, transaction boundaries, and thread-local connections."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from devdocs.errors import StorageError
from devdocs.index import SCHEMA_VERSION, Database


class TestMigrations(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "devdocs.db"

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
        nested = Path(tempfile.mkdtemp()) / "a" / "b" / "devdocs.db"
        with Database(nested) as db:
            self.assertTrue(db.path.exists())


class TestBehaviour(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(Path(tempfile.mkdtemp()) / "devdocs.db")
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
        errors: list[str] = []

        def write(worker: int) -> None:
            try:
                for index in range(20):
                    with self.db.transaction() as conn:
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

        self.assertEqual(errors, [])
        count = self.db.connection.execute("SELECT count(*) FROM run").fetchone()[0]
        self.assertEqual(count, 160)


class TestInMemory(unittest.TestCase):
    def test_in_memory_keeps_one_connection(self) -> None:
        with Database(":memory:") as db:
            with db.transaction() as conn:
                conn.execute("INSERT INTO run(id, started_at) VALUES('r', 1.0)")
            count = db.connection.execute("SELECT count(*) FROM run").fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
