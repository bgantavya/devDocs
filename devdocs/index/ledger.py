"""How each run ended, and what is spent per day.

On a free tier a medium repository takes more requests than one day allows,
so a run *will* be interrupted — by quota, by a budget ceiling, or by Ctrl-C.
`RunLedger` records each run and how it ended, and the next run with the same
configuration reopens an unfinished one, as a resumption. What spares that run
paying again is the response cache, which holds every answer.

`DailyUsage` counts requests per model per day across runs. The rate limiter
only sees its own process; this is what lets a second run on the same day
know how much of the daily limit the first one already used.

Reads and writes go through `Database.query` and `Database.transaction`, so
failures are StorageError.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import UTC, datetime, timedelta, timezone

from devdocs.index.db import Database

__all__ = ["DailyUsage", "RunLedger", "config_fingerprint", "quota_day"]


def config_fingerprint(*parts: object) -> str:
    """Stable hash of whatever makes two runs the "same" run for resuming."""
    text = "\x1f".join(map(str, parts))
    # surrogatepass: a path from an undecodable name still hashes, and valid
    # text hashes as before, so existing runs stay resumable
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


class RunLedger:
    """Repository over the `run` table."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def start(self, config_hash: str, *, resume: bool = True) -> tuple[str, bool]:
        """Open a run. Returns (run_id, resumed).

        With `resume`, the newest unfinished run with the same configuration
        is reopened rather than starting over; a changed configuration always
        starts fresh, being another run.
        """
        if resume:
            row = self.db.query_one(
                "SELECT id FROM run WHERE config_hash = ? AND status != 'finished' "
                "ORDER BY started_at DESC LIMIT 1",
                (config_hash,),
            )
            if row is not None:
                with self.db.transaction() as conn:
                    conn.execute(
                        "UPDATE run SET status = 'running' WHERE id = ?", (row["id"],)
                    )
                return str(row["id"]), True

        run_id = uuid.uuid4().hex[:12]
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO run (id, started_at, status, config_hash) "
                "VALUES (?, ?, 'running', ?)",
                (run_id, time.time(), config_hash),
            )
        return run_id, False

    def finish(self, run_id: str, status: str = "finished") -> None:
        """Close a run. Anything other than "finished" leaves it resumable."""
        with self.db.transaction() as conn:
            conn.execute("UPDATE run SET status = ? WHERE id = ?", (status, run_id))


def quota_day(now: datetime | None = None) -> str:
    """The provider's quota day, which resets at midnight US Pacific time.

    Uses the real zone when the tz database is present, else a fixed UTC-8.
    Being an hour out around DST only matters within an hour of the reset.
    """
    moment = now or datetime.now(UTC)
    try:
        from zoneinfo import ZoneInfo

        local = moment.astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:   # no tzdata (e.g. bare Windows): fixed offset
        local = moment.astimezone(timezone(timedelta(hours=-8)))
    return local.date().isoformat()


class DailyUsage:
    """Repository over `usage_day`: requests and spend per model per day."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, model: str, *, cost: float = 0.0, day: str | None = None) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO usage_day (model, day, requests, cost_usd) "
                "VALUES (?, ?, 1, ?) ON CONFLICT(model, day) DO UPDATE SET "
                "requests = requests + 1, cost_usd = cost_usd + excluded.cost_usd",
                (model, day or quota_day(), cost),
            )

    def requests(self, model: str, *, day: str | None = None) -> int:
        row = self.db.query_one(
            "SELECT requests FROM usage_day WHERE model = ? AND day = ?",
            (model, day or quota_day()),
        )
        return int(row["requests"]) if row is not None else 0
