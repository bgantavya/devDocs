"""Persistent state: the response cache, chunk store, and run ledger.

All three share one SQLite file and one connection, owned by `db.Database`.
Repositories receive that connection and never open, commit, or migrate on
their own, so there is exactly one schema and one transaction boundary.
"""

from devdocs.index.db import Database, MIGRATIONS, SCHEMA_VERSION

__all__ = ["Database", "MIGRATIONS", "SCHEMA_VERSION"]
