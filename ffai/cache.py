import json
import os
import sqlite3
from datetime import datetime, timezone

from ffai.config import CACHE_DB_PATH


class Cache:
    """Generic SQLite-backed key-value cache. One table, any JSON-serializable
    payload, keyed by caller-chosen strings (e.g. "league:<id>"). Knows nothing
    about Sleeper -- reused as-is for rosters, users, matchups, etc. later."""

    def __init__(self, db_path: str = CACHE_DB_PATH):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                )
                """
            )

    def get(self, key: str):
        """Returns (payload, fetched_at_iso8601) or None if the key isn't cached."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload, fetched_at FROM cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        payload, fetched_at = row
        return json.loads(payload), fetched_at

    def set(self, key: str, payload) -> str:
        """Writes payload under key, stamped with the current UTC time. Returns that timestamp."""
        fetched_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cache (key, payload, fetched_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at
                """,
                (key, json.dumps(payload), fetched_at),
            )
        return fetched_at
