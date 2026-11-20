"""What a run set out to do, what it finished, and what it spent per day.

On a free tier a medium repository takes more requests than one day allows,
so a run *will* be interrupted — by quota, by a budget ceiling, or by Ctrl-C.
`RunLedger` records every planned item and its outcome as it happens, so the
next run resumes where the last one stopped instead of paying again for work
already done.

`DailyUsage` counts requests per model per day across runs. The rate limiter
only sees its own process; this is what lets a second run on the same day
know how much of the daily limit the first one already used.

A source is whatever path the scanner produced, which on POSIX may hold lone
surrogates (an undecodable file name). Such text is stored losslessly through
`encode_text`, so one oddly named file cannot stop a run. Reads and writes go
through `Database.query` and `Database.transaction`, so failures are
StorageError.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum

from devdocs.index.db import Database, decode_text, encode_text

__all__ = [
    "DailyUsage",
    "ItemStatus",
    "LedgerItem",
    "RunLedger",
    "RunTotals",
    "config_fingerprint",
    "quota_day",
]


class ItemStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class LedgerItem:
    source: str
    kind: str
    content_hash: str
    status: ItemStatus = ItemStatus.PENDING


@dataclass(frozen=True)
class RunTotals:
    cost_usd: float
    done: int
    failed: int
    pending: int
    skipped: int


def config_fingerprint(*parts: object) -> str:
    """Stable hash of whatever makes two runs the "same" run for resuming."""
    text = "\x1f".join(map(str, parts))
    # surrogatepass: a path from an undecodable name still hashes, and valid
    # text hashes as before, so existing runs stay resumable
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


class RunLedger:
    """Repository over the `run` and `run_item` tables."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def start(self, config_hash: str, *, resume: bool = True) -> tuple[str, bool]:
        """Open a run. Returns (run_id, resumed).

        With `resume`, the newest unfinished run with the same configuration
        is reopened rather than starting over; a changed configuration always
        starts fresh, because its items would not mean the same thing.
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

    def plan(self, run_id: str, items: list[LedgerItem]) -> int:
        """Record items to do. An item whose source changed is reset to pending.

        Returns how many items are new or changed, i.e. still need work.
        """
        changed = 0
        with self.db.transaction() as conn:
            for item in items:
                source, kind = encode_text(item.source), encode_text(item.kind)
                row = conn.execute(
                    "SELECT content_hash, status FROM run_item "
                    "WHERE run_id = ? AND source = ? AND kind = ?",
                    (run_id, source, kind),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO run_item (run_id, source, kind, content_hash) "
                        "VALUES (?, ?, ?, ?)",
                        (run_id, source, kind, item.content_hash),
                    )
                    changed += 1
                elif row["content_hash"] != item.content_hash:
                    conn.execute(
                        "UPDATE run_item SET content_hash = ?, status = 'pending', "
                        "error = NULL WHERE run_id = ? AND source = ? AND kind = ?",
                        (item.content_hash, run_id, source, kind),
                    )
                    changed += 1
                elif row["status"] != ItemStatus.DONE:
                    changed += 1
        return changed

    def pending(self, run_id: str) -> list[LedgerItem]:
        """Items still to do, including ones that failed last time."""
        rows = self.db.query(
            "SELECT source, kind, content_hash, status FROM run_item "
            "WHERE run_id = ? AND status IN ('pending', 'failed') "
            "ORDER BY source, kind",
            (run_id,),
        )
        return [
            LedgerItem(
                decode_text(r["source"]), decode_text(r["kind"]), r["content_hash"],
                ItemStatus(r["status"]),
            )
            for r in rows
        ]

    def mark(
        self,
        run_id: str,
        source: str,
        kind: str,
        status: ItemStatus,
        *,
        cost: float = 0.0,
        error: str | None = None,
    ) -> None:
        """Record one item's outcome and add its cost to the run."""
        detail = encode_text(error) if error is not None else None
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE run_item SET status = ?, cost_usd = cost_usd + ?, error = ? "
                "WHERE run_id = ? AND source = ? AND kind = ?",
                (status.value, cost, detail, run_id, encode_text(source),
                 encode_text(kind)),
            )
            conn.execute(
                "UPDATE run SET cost_usd = cost_usd + ?, "
                "requests = requests + ? WHERE id = ?",
                (cost, 1 if cost > 0 else 0, run_id),
            )

    def finish(self, run_id: str, status: str = "finished") -> None:
        """Close a run. Anything other than "finished" leaves it resumable."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE run SET status = ?, finished_at = ? WHERE id = ?",
                (status, time.time(), run_id),
            )

    def totals(self, run_id: str) -> RunTotals:
        rows = self.db.query(
            "SELECT status, count(*) AS n, coalesce(sum(cost_usd), 0) AS c "
            "FROM run_item WHERE run_id = ? GROUP BY status",
            (run_id,),
        )
        counts = {row["status"]: int(row["n"]) for row in rows}
        cost = sum(float(row["c"]) for row in rows)
        return RunTotals(
            cost_usd=cost,
            done=counts.get("done", 0),
            failed=counts.get("failed", 0),
            pending=counts.get("pending", 0),
            skipped=counts.get("skipped", 0),
        )


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
