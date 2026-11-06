"""Paid responses, kept so an unchanged input is never paid for twice.

The key is a hash of everything that shapes the output: provider, model,
system prompt, prompt, output cap, temperature and effort. Change any of them
and the key changes, so a stale answer cannot be served for a new question.
The prompt already embeds the source it documents, which is what makes an
unchanged file a cache hit and an edited one a miss. An answer a model gave
on loan to a higher tier is keyed by that tier too, apart from its own.

Only usable completions are stored. A blocked or empty response is not an
answer worth replaying, and caching it would stop the next run retrying.

Each write is its own transaction: a response is persisted the moment it has
been paid for, so a crash loses nothing that cost money.

Text is keyed and stored exactly, lone surrogates included (an undecodable
file name in a prompt): the key hashes it with surrogatepass, which encodes
valid text as before, so no existing key changes. Reads and writes go through
`Database.query_one` and `Database.transaction`, so a damaged or locked index
is a StorageError, never a raw sqlite3 error.
"""

from __future__ import annotations

import hashlib
import json
import time

from devdocs.index.db import Database, decode_text, encode_text
from devdocs.llm import Completion, Finish, Request, Usage

__all__ = ["ResponseCache"]

# Bump to invalidate every cached response, e.g. after a prompt-format change
# that the key would not otherwise notice.
CACHE_VERSION = 1


class ResponseCache:
    """Repository over the `response_cache` table."""

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def key(request: Request, *, provider: str, lent: str = "") -> str:
        """`lent` is the tier the request's model answers on loan, if any."""
        parts = [
            CACHE_VERSION,
            provider,
            request.model,
            request.system,
            request.prompt,
            request.max_output_tokens,
            request.temperature,
            request.effort,
        ]
        if lent:
            parts.append(lent)      # last, so every other key stays as it was
        encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8", "surrogatepass")).hexdigest()

    def get(self, key: str) -> Completion | None:
        row = self.db.query_one(
            "SELECT provider, model, content, input_tokens, output_tokens, "
            "cached_tokens, finish FROM response_cache WHERE key = ?",
            (key,),
        )
        if row is None:
            return None
        try:
            finish = Finish(row["finish"])
        except ValueError:
            finish = Finish.STOP
        return Completion(
            text=decode_text(row["content"]),
            model=row["model"],
            provider=row["provider"],
            finish=finish,
            usage=Usage(
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                cached_input_tokens=row["cached_tokens"],
            ),
            cost_usd=0.0,
            cached=True,
        )

    def put(self, key: str, completion: Completion) -> bool:
        """Store a usable completion. Returns False when it was not worth keeping."""
        if not completion.ok or completion.text is None:
            return False
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO response_cache "
                "(key, provider, model, content, input_tokens, output_tokens, "
                " cached_tokens, cost_usd, finish, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    completion.provider,
                    completion.model,
                    encode_text(completion.text),
                    completion.usage.input_tokens,
                    completion.usage.output_tokens,
                    completion.usage.cached_input_tokens,
                    completion.cost_usd,
                    completion.finish.value,
                    time.time(),
                ),
            )
        return True

    def stats(self) -> tuple[int, float]:
        """Entries held, and what they cost when first bought."""
        row = self.db.query_one(
            "SELECT count(*), coalesce(sum(cost_usd), 0) FROM response_cache"
        )
        return (int(row[0]), float(row[1])) if row is not None else (0, 0.0)

    def clear(self) -> int:
        with self.db.transaction() as conn:
            return conn.execute("DELETE FROM response_cache").rowcount
