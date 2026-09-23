"""SQLite cache for paper search results.

Paper discovery hits paid web search and several public APIs; the same query is often repeated
while a user compares results. Caching the merged payload keeps repeat searches free and instant.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SEARCH_CACHE_LOCK = threading.RLock()
DEFAULT_TTL_HOURS = 168


def now() -> datetime:
    return datetime.now(timezone.utc)


def cache_key(query: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", query).strip().casefold()
    return f"{normalized}|{int(limit)}"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS search_cache (
            cache_key TEXT PRIMARY KEY,
            query TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_hit_at TEXT NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    return connection


def load(path: Path, query: str, limit: int, ttl_hours: int = DEFAULT_TTL_HOURS) -> dict[str, Any] | None:
    if ttl_hours <= 0 or not path.exists():
        return None
    key = cache_key(query, limit)
    with SEARCH_CACHE_LOCK:
        with connect(path) as connection:
            row = connection.execute(
                "SELECT payload_json, created_at FROM search_cache WHERE cache_key = ?", (key,)
            ).fetchone()
            if not row:
                return None
            try:
                created_at = datetime.fromisoformat(row["created_at"])
            except ValueError:
                return None
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if now() - created_at > timedelta(hours=ttl_hours):
                connection.execute("DELETE FROM search_cache WHERE cache_key = ?", (key,))
                return None
            connection.execute(
                "UPDATE search_cache SET hits = hits + 1, last_hit_at = ? WHERE cache_key = ?",
                (now().isoformat(), key),
            )
    try:
        payload = json.loads(row["payload_json"])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def save(path: Path, query: str, limit: int, payload: dict[str, Any]) -> None:
    key = cache_key(query, limit)
    timestamp = now().isoformat()
    with SEARCH_CACHE_LOCK:
        with connect(path) as connection:
            connection.execute(
                """
                INSERT INTO search_cache(cache_key, query, payload_json, created_at, last_hit_at, hits)
                VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(cache_key) DO UPDATE SET
                    query=excluded.query,
                    payload_json=excluded.payload_json,
                    created_at=excluded.created_at
                """,
                (key, query, json.dumps(payload, ensure_ascii=False), timestamp, timestamp),
            )


def clear(path: Path) -> int:
    if not path.exists():
        return 0
    with SEARCH_CACHE_LOCK:
        with connect(path) as connection:
            cursor = connection.execute("DELETE FROM search_cache")
            return int(cursor.rowcount or 0)


def stats(path: Path, ttl_hours: int = DEFAULT_TTL_HOURS) -> dict[str, Any]:
    if not path.exists():
        return {"entries": 0, "hits": 0, "freshEntries": 0, "ttlHours": ttl_hours}
    cutoff = now() - timedelta(hours=max(0, ttl_hours))
    with SEARCH_CACHE_LOCK:
        with connect(path) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS entries, COALESCE(SUM(hits), 0) AS hits FROM search_cache"
            ).fetchone()
            fresh = connection.execute(
                "SELECT COUNT(*) AS fresh FROM search_cache WHERE created_at >= ?",
                (cutoff.isoformat(),),
            ).fetchone()
    return {
        "entries": int(row["entries"]),
        "hits": int(row["hits"]),
        "freshEntries": int(fresh["fresh"]),
        "ttlHours": ttl_hours,
    }
