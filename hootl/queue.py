"""SQLite のタスクキュー。状態遷移はすべてこのモジュール経由で行う。"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL,
    acceptance  TEXT NOT NULL,          -- JSON array
    paths       TEXT NOT NULL,          -- JSON array of globs
    depends_on  TEXT NOT NULL DEFAULT '[]',
    epic        TEXT,
    priority    INTEGER NOT NULL DEFAULT 100,
    status      TEXT NOT NULL DEFAULT 'todo',   -- todo | running | done | blocked
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    feedback    TEXT,                   -- 次の試行に渡す差し戻し内容
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    task_id     TEXT NOT NULL,
    attempt     INTEGER NOT NULL,
    stage       TEXT NOT NULL,          -- agent | guard | gate | review | merge | done
    ok          INTEGER NOT NULL,
    detail      TEXT,
    cost_usd    REAL,
    turns       INTEGER,
    started_at  TEXT NOT NULL,
    ended_at    TEXT NOT NULL,
    PRIMARY KEY (task_id, attempt)
);
"""

REQUIRED = ("id", "title", "description", "acceptance", "paths")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Task:
    id: str
    title: str
    description: str
    acceptance: list[str]
    paths: list[str]
    depends_on: list[str]
    epic: str | None
    priority: int
    status: str
    attempts: int
    last_error: str | None
    feedback: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        return cls(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            acceptance=json.loads(row["acceptance"]),
            paths=json.loads(row["paths"]),
            depends_on=json.loads(row["depends_on"]),
            epic=row["epic"],
            priority=row["priority"],
            status=row["status"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            feedback=row["feedback"],
        )


class Queue:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- 取り込み ---------------------------------------------------------
    def import_jsonl(self, path: Path) -> int:
        count = 0
        with self._lock:
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                task = json.loads(line)
                missing = [k for k in REQUIRED if not task.get(k)]
                if missing:
                    raise ValueError(f"{path}:{line_no}: missing {missing}")
                self._db.execute(
                    """INSERT INTO tasks (id, title, description, acceptance, paths, depends_on,
                                          epic, priority, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                         title=excluded.title, description=excluded.description,
                         acceptance=excluded.acceptance, paths=excluded.paths,
                         depends_on=excluded.depends_on, epic=excluded.epic,
                         priority=excluded.priority, updated_at=excluded.updated_at
                       WHERE tasks.status != 'done'""",
                    (
                        task["id"], task["title"], task["description"],
                        json.dumps(task["acceptance"], ensure_ascii=False),
                        json.dumps(task["paths"]),
                        json.dumps(task.get("depends_on", [])),
                        task.get("epic"), int(task.get("priority", 100)), now(),
                    ),
                )
                count += 1
        return count

    # ---- 取り出し ---------------------------------------------------------
    def claim_next(self) -> Task | None:
        """依存がすべて done の todo を1件、running にして返す。"""
        with self._lock:
            done = {r["id"] for r in self._db.execute("SELECT id FROM tasks WHERE status='done'")}
            rows = self._db.execute(
                "SELECT * FROM tasks WHERE status='todo' ORDER BY priority, id"
            ).fetchall()
            for row in rows:
                task = Task.from_row(row)
                if all(dep in done for dep in task.depends_on):
                    self._db.execute(
                        "UPDATE tasks SET status='running', updated_at=? WHERE id=?",
                        (now(), task.id),
                    )
                    task.status = "running"
                    return task
            return None

    def recover_running(self) -> int:
        """前回の異常終了で running のまま残ったタスクを todo に戻す。"""
        with self._lock:
            cur = self._db.execute(
                "UPDATE tasks SET status='todo', updated_at=? WHERE status='running'", (now(),)
            )
            return cur.rowcount

    # ---- 状態更新 ---------------------------------------------------------
    def begin_attempt(self, task_id: str) -> int:
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET attempts = attempts + 1, updated_at=? WHERE id=?", (now(), task_id)
            )
            return self._db.execute("SELECT attempts FROM tasks WHERE id=?", (task_id,)).fetchone()[0]

    def record_attempt(self, task_id: str, attempt: int, stage: str, ok: bool, detail: str,
                       started_at: str, cost_usd: float | None = None, turns: int | None = None) -> None:
        with self._lock:
            self._db.execute(
                """INSERT OR REPLACE INTO attempts
                   (task_id, attempt, stage, ok, detail, cost_usd, turns, started_at, ended_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, attempt, stage, int(ok), detail[:2000], cost_usd, turns, started_at, now()),
            )

    def requeue(self, task_id: str, error: str, feedback: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET status='todo', last_error=?, feedback=?, updated_at=? WHERE id=?",
                (error, feedback, now(), task_id),
            )

    def mark(self, task_id: str, status: str, error: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET status=?, last_error=COALESCE(?, last_error), updated_at=? WHERE id=?",
                (status, error, now(), task_id),
            )

    def reset(self, task_id: str, only_blocked: bool = False) -> bool:
        """todo に戻す。only_blocked なら blocked のときだけ（Web から done や running を戻させない）。"""
        cond = " AND status='blocked'" if only_blocked else ""
        with self._lock:
            cur = self._db.execute(
                """UPDATE tasks SET status='todo', attempts=0, last_error=NULL, feedback=NULL,
                   updated_at=? WHERE id=?""" + cond,
                (now(), task_id),
            )
            return cur.rowcount == 1

    # ---- 参照 -------------------------------------------------------------
    def all(self) -> list[Task]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM tasks ORDER BY priority, id").fetchall()
        return [Task.from_row(r) for r in rows]

    def attempts_since(self, since_iso: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM attempts WHERE ended_at >= ? ORDER BY ended_at", (since_iso,)
            ).fetchall()

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return Task.from_row(row) if row else None

    def updated_at(self) -> dict[str, str]:
        with self._lock:
            rows = self._db.execute("SELECT id, updated_at FROM tasks").fetchall()
        return {r["id"]: r["updated_at"] for r in rows}

    def attempts_for(self, task_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM attempts WHERE task_id=? ORDER BY attempt", (task_id,)
            ).fetchall()

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) c FROM tasks GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}
