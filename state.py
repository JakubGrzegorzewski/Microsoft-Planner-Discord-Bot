"""SQLite persistence for completion notifications.

Two things are stored:

* ``tasks``: the last state the bot saw for every task of every watched plan. Comparing a
  new snapshot with it is how the *transition* to 100 % is detected, and because it lives
  on disk a restart does not make old completions look new.
* ``notifications``: an outbox. Detecting a completion and queueing its notification happen
  in one transaction; sending happens afterwards and is retried until it succeeds. The
  UNIQUE(task_id, event_key) constraint is the backstop against duplicates: one completion
  of one task can only ever be queued once.

All methods are synchronous and guarded by a lock; the async wrappers run them in a worker
thread so a slow disk (a NAS, for instance) never stalls the Discord connection.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id      TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    baseline_at  TEXT NOT NULL,   -- first full snapshot; nothing completed before it is announced
    last_poll_at TEXT NOT NULL    -- start time of the latest snapshot that was applied
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id          TEXT PRIMARY KEY,
    plan_id          TEXT NOT NULL,
    title            TEXT NOT NULL,
    bucket_id        TEXT,
    percent_complete INTEGER NOT NULL,
    completed_at     TEXT,
    etag             TEXT,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_by_plan ON tasks (plan_id);

CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    event_key       TEXT NOT NULL,   -- completedDateTime: identifies one completion of the task
    plan_id         TEXT NOT NULL,
    payload         TEXT NOT NULL,   -- JSON: what the message needs (title, bucket, who, when)
    discord_user_id INTEGER,         -- set when the task was completed with /task complete
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | expired
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    sent_at         TEXT,
    UNIQUE (task_id, event_key)
);
CREATE INDEX IF NOT EXISTS notifications_by_status ON notifications (status, id);
"""

# A task first seen already completed is announced only if it was completed after the
# previous poll. The slack absorbs clock differences between this machine and Microsoft's.
CLOCK_SLACK = timedelta(minutes=10)


def utc_iso(moment: datetime) -> str:
    """Canonical text form of a timestamp, used both for storage and as part of event keys."""
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def from_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


@dataclass(frozen=True)
class TaskState:
    """The slice of a Planner task that matters for completion tracking."""

    task_id: str
    plan_id: str
    title: str
    bucket_id: Optional[str]
    percent_complete: int
    completed_at: Optional[datetime]
    completed_by: Optional[str]
    etag: Optional[str]

    @property
    def is_complete(self) -> bool:
        return self.percent_complete >= 100

    @property
    def event_key(self) -> str:
        # Planner stamps completedDateTime when percentComplete becomes 100, so it differs
        # each time a task is completed again after being reopened.
        if self.completed_at is not None:
            return utc_iso(self.completed_at)
        return f"etag:{self.etag}"


@dataclass(frozen=True)
class SnapshotResult:
    baseline: bool  # True for the first snapshot of a plan (nothing is announced)
    seen: int
    changed: int
    removed: int
    completions: int  # notifications newly queued


@dataclass(frozen=True)
class Notification:
    id: int
    task_id: str
    plan_id: str
    payload: dict[str, Any]
    discord_user_id: Optional[int]
    attempts: int
    created_at: datetime


class StateStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle

    def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            conn.close()
            raise RuntimeError(
                f"{self._path} was written by a newer version of the bot (schema {version}, "
                f"this version understands {SCHEMA_VERSION})"
            )
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("StateStore.open() has not been called")
        return self._conn

    # ------------------------------------------------------------------ async facade

    async def aopen(self) -> None:
        await asyncio.to_thread(self.open)

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)

    async def apply_snapshot(
        self, plan_id: str, plan_title: str, tasks: Sequence[TaskState], *, started_at: datetime
    ) -> SnapshotResult:
        return await asyncio.to_thread(self.apply_snapshot_sync, plan_id, plan_title, tasks, started_at)

    async def record_completion(
        self, task: TaskState, plan_title: str, *, discord_user_id: Optional[int], now: datetime
    ) -> bool:
        return await asyncio.to_thread(self.record_completion_sync, task, plan_title, discord_user_id, now)

    async def pending(self, limit: int = 25) -> list[Notification]:
        return await asyncio.to_thread(self.pending_sync, limit)

    async def mark_sent(self, notification_id: int, now: datetime) -> None:
        await asyncio.to_thread(self.mark_sent_sync, notification_id, now)

    async def mark_failed(self, notification_id: int, error: str) -> None:
        await asyncio.to_thread(self.mark_failed_sync, notification_id, error)

    async def housekeeping(self, *, expire_before: datetime, prune_before: datetime) -> tuple[int, int]:
        return await asyncio.to_thread(self.housekeeping_sync, expire_before, prune_before)

    # ------------------------------------------------------------------ snapshots

    def apply_snapshot_sync(
        self, plan_id: str, plan_title: str, tasks: Sequence[TaskState], started_at: datetime
    ) -> SnapshotResult:
        """Store a complete listing of a plan's tasks and queue notifications for new completions."""
        now = utc_iso(started_at)
        with self._lock, self._db as db:
            plan_row = db.execute("SELECT last_poll_at FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
            baseline = plan_row is None
            previous_poll = None if baseline else from_iso(plan_row["last_poll_at"])
            known = {
                row["task_id"]: row
                for row in db.execute("SELECT task_id, percent_complete, etag FROM tasks WHERE plan_id = ?", (plan_id,))
            }

            changed = completions = 0
            for task in tasks:
                before = known.pop(task.task_id, None)
                if before is not None and before["etag"] is not None and before["etag"] == task.etag:
                    continue  # untouched since the last snapshot
                changed += 1
                self._upsert_task(db, task, now)
                if baseline or not task.is_complete:
                    continue
                if before is not None:
                    newly_completed = before["percent_complete"] < 100
                else:
                    # Never seen before and already complete: created and finished between
                    # two polls (announce), or an old completed task that was copied or moved
                    # into the plan (don't).
                    newly_completed = (
                        task.completed_at is not None
                        and previous_poll is not None
                        and task.completed_at >= previous_poll - CLOCK_SLACK
                    )
                if newly_completed and self._queue(db, task, plan_title, None, now):
                    completions += 1

            # Whatever is left was deleted or moved out of the plan.
            for task_id in known:
                db.execute("DELETE FROM tasks WHERE task_id = ? AND plan_id = ?", (task_id, plan_id))

            if baseline:
                db.execute(
                    "INSERT INTO plans (plan_id, title, baseline_at, last_poll_at) VALUES (?, ?, ?, ?)",
                    (plan_id, plan_title, now, now),
                )
            else:
                db.execute("UPDATE plans SET title = ?, last_poll_at = ? WHERE plan_id = ?", (plan_title, now, plan_id))
        return SnapshotResult(
            baseline=baseline, seen=len(tasks), changed=changed, removed=len(known), completions=completions
        )

    def record_completion_sync(
        self, task: TaskState, plan_title: str, discord_user_id: Optional[int], now: datetime
    ) -> bool:
        """Queue the notification for a task the bot itself just completed.

        Returns True when a notification is waiting to be sent for this completion. If the
        poller noticed the completion first, its queued notification is given the Discord
        user instead of a second one being added.
        """
        stamp = utc_iso(now)
        with self._lock, self._db as db:
            self._upsert_task(db, task, stamp)
            if not self._queue(db, task, plan_title, discord_user_id, stamp) and discord_user_id is not None:
                db.execute(
                    "UPDATE notifications SET discord_user_id = ? "
                    "WHERE task_id = ? AND event_key = ? AND status = 'pending'",
                    (discord_user_id, task.task_id, task.event_key),
                )
            row = db.execute(
                "SELECT status FROM notifications WHERE task_id = ? AND event_key = ?",
                (task.task_id, task.event_key),
            ).fetchone()
        return row is not None and row["status"] == "pending"

    @staticmethod
    def _upsert_task(db: sqlite3.Connection, task: TaskState, now: str) -> None:
        db.execute(
            """
            INSERT INTO tasks (task_id, plan_id, title, bucket_id, percent_complete, completed_at, etag, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (task_id) DO UPDATE SET
                plan_id = excluded.plan_id,
                title = excluded.title,
                bucket_id = excluded.bucket_id,
                percent_complete = excluded.percent_complete,
                completed_at = excluded.completed_at,
                etag = excluded.etag,
                updated_at = excluded.updated_at
            """,
            (
                task.task_id,
                task.plan_id,
                task.title,
                task.bucket_id,
                task.percent_complete,
                utc_iso(task.completed_at) if task.completed_at else None,
                task.etag,
                now,
            ),
        )

    @staticmethod
    def _queue(
        db: sqlite3.Connection, task: TaskState, plan_title: str, discord_user_id: Optional[int], now: str
    ) -> bool:
        payload = {
            "title": task.title,
            "plan_title": plan_title,
            "bucket_id": task.bucket_id,
            "completed_by": task.completed_by,
            "completed_at": utc_iso(task.completed_at) if task.completed_at else now,
        }
        cursor = db.execute(
            "INSERT OR IGNORE INTO notifications (task_id, event_key, plan_id, payload, discord_user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task.task_id, task.event_key, task.plan_id, json.dumps(payload), discord_user_id, now),
        )
        return cursor.rowcount == 1

    # ------------------------------------------------------------------ outbox

    def pending_sync(self, limit: int = 25) -> list[Notification]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, task_id, plan_id, payload, discord_user_id, attempts, created_at "
                "FROM notifications WHERE status = 'pending' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            Notification(
                id=row["id"],
                task_id=row["task_id"],
                plan_id=row["plan_id"],
                payload=json.loads(row["payload"]),
                discord_user_id=row["discord_user_id"],
                attempts=row["attempts"],
                created_at=from_iso(row["created_at"]),
            )
            for row in rows
        ]

    def mark_sent_sync(self, notification_id: int, now: datetime) -> None:
        with self._lock, self._db as db:
            db.execute(
                "UPDATE notifications SET status = 'sent', sent_at = ?, last_error = NULL WHERE id = ?",
                (utc_iso(now), notification_id),
            )

    def mark_failed_sync(self, notification_id: int, error: str) -> None:
        with self._lock, self._db as db:
            db.execute(
                "UPDATE notifications SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (error[:500], notification_id),
            )

    def housekeeping_sync(self, expire_before: datetime, prune_before: datetime) -> tuple[int, int]:
        """Give up on notifications that could not be sent for too long; drop old history."""
        with self._lock, self._db as db:
            expired = db.execute(
                "UPDATE notifications SET status = 'expired' WHERE status = 'pending' AND created_at < ?",
                (utc_iso(expire_before),),
            ).rowcount
            pruned = db.execute(
                "DELETE FROM notifications WHERE status != 'pending' AND created_at < ?",
                (utc_iso(prune_before),),
            ).rowcount
        return expired, pruned

    # ------------------------------------------------------------------ inspection (tests, diagnostics)

    def counts_sync(self) -> dict[str, int]:
        with self._lock:
            db = self._db
            result = {"tasks": db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]}
            for row in db.execute("SELECT status, COUNT(*) AS n FROM notifications GROUP BY status"):
                result[row["status"]] = row["n"]
        return result
