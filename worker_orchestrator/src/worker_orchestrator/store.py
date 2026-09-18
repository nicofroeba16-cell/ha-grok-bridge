from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from .models import Goal, LifecycleState


SCHEMA_VERSION = 1


class Registry:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        with self.conn:
            yield self.conn

    def _migrate(self) -> None:
        with self.tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS workers (
                    worker_key TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    chat TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    workstream_issue INTEGER,
                    goal_hash TEXT NOT NULL,
                    goal_version TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    done_criteria TEXT NOT NULL,
                    files TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    approved_actions TEXT NOT NULL,
                    state TEXT NOT NULL,
                    last_head TEXT NOT NULL DEFAULT '',
                    ci_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                    blockers TEXT NOT NULL DEFAULT '[]',
                    user_gate TEXT NOT NULL DEFAULT '[]',
                    last_progress TEXT NOT NULL DEFAULT '',
                    completion_evidence TEXT NOT NULL DEFAULT '{}',
                    verified_criteria TEXT NOT NULL DEFAULT '[]',
                    error_signature TEXT NOT NULL DEFAULT '',
                    unchanged_runs INTEGER NOT NULL DEFAULT 0,
                    execution_count INTEGER NOT NULL DEFAULT 0,
                    recovery_count INTEGER NOT NULL DEFAULT 0,
                    last_report_fingerprint TEXT NOT NULL DEFAULT '',
                    source_comment_id INTEGER,
                    session_state TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    worker_key TEXT NOT NULL,
                    goal_version TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS locks (
                    worker_key TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    files TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    acquired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))

    def recover_interrupted(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT worker_key,session_state FROM workers WHERE state=?",
            (LifecycleState.RUNNING,),
        ).fetchall()
        keys: list[str] = []
        with self.tx() as c:
            for row in rows:
                key = str(row[0])
                try:
                    session_state = json.loads(row[1] or "{}")
                except json.JSONDecodeError:
                    session_state = {}
                if isinstance(session_state, dict) and session_state.get("verified_wake_delivery"):
                    # Browser-verified Auto Chat work executes outside this process.
                    # A daemon restart must not invent a redispatch or erase RUNNING.
                    continue
                keys.append(key)
                c.execute(
                    "UPDATE workers SET state=?, recovery_count=recovery_count+1, last_progress=?, updated_at=CURRENT_TIMESTAMP WHERE worker_key=?",
                    (LifecycleState.ASSIGNED, "Recovered after orchestrator restart; safe re-dispatch required.", key),
                )
                c.execute("DELETE FROM locks WHERE worker_key=?", (key,))
        return keys

    def upsert_goal(self, goal: Goal):
        current = self.get(goal.key)
        if current is not None:
            current_source = -1 if current["source_comment_id"] is None else current["source_comment_id"]
            incoming_source = -1 if goal.source_comment_id is None else goal.source_comment_id
            if incoming_source < current_source or current["goal_hash"] == goal.hash:
                return False, current
        is_new_version = True
        with self.tx() as c:
            c.execute("""
                INSERT INTO workers(
                    worker_key, project, chat, repository, branch, workstream_issue,
                    goal_hash, goal_version, prompt, done_criteria, files, scope,
                    approved_actions, state, source_comment_id, last_progress
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(worker_key) DO UPDATE SET
                    project=excluded.project, chat=excluded.chat,
                    repository=excluded.repository, branch=excluded.branch,
                    workstream_issue=excluded.workstream_issue,
                    goal_hash=excluded.goal_hash, goal_version=excluded.goal_version,
                    prompt=excluded.prompt, done_criteria=excluded.done_criteria,
                    files=excluded.files, scope=excluded.scope,
                    approved_actions=excluded.approved_actions,
                    state=excluded.state, source_comment_id=excluded.source_comment_id,
                    last_head='', ci_status='UNKNOWN', blockers='[]', user_gate='[]',
                    last_progress=excluded.last_progress, completion_evidence='{}',
                    verified_criteria='[]', error_signature='', unchanged_runs=0,
                    execution_count=0, last_report_fingerprint='', session_state='{}',
                    updated_at=CURRENT_TIMESTAMP
            """, (
                goal.key, goal.project, goal.chat, goal.repository, goal.branch,
                goal.workstream_issue, goal.hash, goal.version, goal.prompt,
                json.dumps(goal.done_criteria), json.dumps(goal.files), goal.scope,
                json.dumps(goal.approved_actions), LifecycleState.ASSIGNED,
                goal.source_comment_id, "New or materially changed GOAL assigned.",
            ))
            c.execute("DELETE FROM locks WHERE worker_key=?", (goal.key,))
        return True, self.get(goal.key)

    def get(self, worker_key: str):
        return self.conn.execute("SELECT * FROM workers WHERE worker_key=?", (worker_key,)).fetchone()

    def list_dispatchable(self):
        # Only newly/materially assigned goals may spawn workers. READY is a
        # stable handoff state and remains dormant until Master changes the
        # goal. BLOCKED is reconciled separately without worker respawn.
        return self.conn.execute(
            "SELECT * FROM workers WHERE state=? ORDER BY updated_at",
            (LifecycleState.ASSIGNED,),
        ).fetchall()

    def list_all(self):
        return self.conn.execute("SELECT * FROM workers ORDER BY worker_key").fetchall()

    def requeue_executor_failures(self) -> int:
        """Clear executor-only failures when local worker execution is disabled."""
        changed = 0
        for row in self.list_all():
            if row["state"] != LifecycleState.BLOCKED:
                continue
            blockers = list(json.loads(row["blockers"] or "[]"))
            if "WORKER_EXECUTION_FAILED" not in blockers:
                continue
            self.set_state(
                row["worker_key"],
                LifecycleState.ASSIGNED,
                blockers=[],
                user_gate=[],
                error_signature="",
                unchanged_runs=0,
                last_progress="Local worker executor disabled; awaiting external workstream execution.",
            )
            self.record_event(
                row["worker_key"],
                row["goal_version"],
                "EXECUTOR_DISABLED_REQUEUE",
                {"previous_blockers": blockers},
            )
            changed += 1
        return changed

    def set_state(self, worker_key: str, state: LifecycleState, **fields) -> None:
        allowed = {
            "last_head", "ci_status", "blockers", "user_gate", "last_progress",
            "completion_evidence", "verified_criteria", "error_signature",
            "unchanged_runs", "execution_count", "last_report_fingerprint",
            "session_state",
        }
        parts = ["state=?", "updated_at=CURRENT_TIMESTAMP"]
        values: list[object] = [state]
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"unsupported worker field: {k}")
            if isinstance(v, (dict, list, tuple)):
                v = json.dumps(v, sort_keys=True)
            parts.append(f"{k}=?")
            values.append(v)
        values.append(worker_key)
        with self.tx() as c:
            c.execute(f"UPDATE workers SET {', '.join(parts)} WHERE worker_key=?", values)

    def record_event(self, worker_key: str, goal_version: str, event_type: str, payload: dict) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO events(worker_key, goal_version, event_type, payload) VALUES(?,?,?,?)",
                (worker_key, goal_version, event_type, json.dumps(payload, sort_keys=True)),
            )

    def events(self, worker_key: str):
        return self.conn.execute("SELECT * FROM events WHERE worker_key=? ORDER BY id", (worker_key,)).fetchall()

    def try_acquire_lock(self, worker_key: str, repository: str, branch: str, files: Iterable[str], scope: str):
        files_set = {x for x in files if x}
        rows = self.conn.execute("SELECT * FROM locks WHERE worker_key<>?", (worker_key,)).fetchall()
        for row in rows:
            if row["repository"] != repository:
                continue
            other_files = set(json.loads(row["files"]))
            same_branch = row["branch"] == branch
            file_overlap = bool(files_set and other_files and files_set.intersection(other_files))
            scope_overlap = bool(scope and row["scope"] and scope == row["scope"])
            if same_branch or file_overlap or scope_overlap:
                return False, row["worker_key"]
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO locks(worker_key, repository, branch, files, scope) VALUES(?,?,?,?,?)",
                (worker_key, repository, branch, json.dumps(sorted(files_set)), scope),
            )
        return True, ""

    def release_lock(self, worker_key: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM locks WHERE worker_key=?", (worker_key,))
