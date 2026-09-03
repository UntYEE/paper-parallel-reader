"""SQLite persistence for generation task status."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TASK_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS generation_tasks (
            task_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    return connection


def save_task(path: Path, task: dict[str, Any]) -> dict[str, Any]:
    task_id = str(task["taskId"])
    updated_at = str(task.get("updatedAt") or now_iso())
    task["updatedAt"] = updated_at
    with TASK_LOCK:
        with connect(path) as connection:
            connection.execute(
                """
                INSERT INTO generation_tasks(task_id, payload_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (task_id, json.dumps(task, ensure_ascii=False), updated_at),
            )
    return dict(task)


def get_task(path: Path, task_id: str) -> dict[str, Any] | None:
    with TASK_LOCK:
        with connect(path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM generation_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def update_task(path: Path, task_id: str, **fields: Any) -> dict[str, Any] | None:
    with TASK_LOCK:
        task = get_task(path, task_id)
        if not task:
            return None
        task.update(fields)
        task["revision"] = int(task.get("revision", 0)) + 1
        task["updatedAt"] = now_iso()
        return save_task(path, task)


def mark_unfinished_tasks_interrupted(path: Path) -> int:
    changed = 0
    with TASK_LOCK:
        with connect(path) as connection:
            rows = connection.execute("SELECT task_id, payload_json FROM generation_tasks").fetchall()
            for row in rows:
                task = json.loads(row["payload_json"])
                if task.get("status") not in {"queued", "running"}:
                    continue
                task["status"] = "interrupted"
                task["error"] = "The previous process stopped. Start generation again to resume from checkpoints."
                progress = dict(task.get("progress") or {})
                progress["status"] = "interrupted"
                task["progress"] = progress
                task["revision"] = int(task.get("revision", 0)) + 1
                task["updatedAt"] = now_iso()
                connection.execute(
                    "UPDATE generation_tasks SET payload_json = ?, updated_at = ? WHERE task_id = ?",
                    (json.dumps(task, ensure_ascii=False), task["updatedAt"], row["task_id"]),
                )
                changed += 1
    return changed
