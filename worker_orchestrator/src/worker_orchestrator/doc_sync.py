from __future__ import annotations

import json
import sqlite3
import time
from hashlib import sha256
from typing import Callable, Mapping

from .security import sanitize

DOC_SYNC_PENDING = "DOC_SYNC_PENDING"
DOC_SYNC_WORKSTREAM_VERIFIED = "DOC_SYNC_WORKSTREAM_VERIFIED"
DOC_SYNC_MASTER_PENDING = "DOC_SYNC_MASTER_PENDING"
DOC_SYNC_VERIFIED = "DOC_SYNC_VERIFIED"
DOC_SYNC_BLOCKED = "DOC_SYNC_BLOCKED"
DOC_SYNC_REQUIRED = "DOC_SYNC_REQUIRED"


def _comment_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in str(body).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().upper()] = value.strip()
    return fields


def _has_fingerprint(items: list[dict], worker_key: str, goal_version: str, fingerprint: str) -> int | None:
    expected_project, expected_chat = "", ""
    if worker_key.startswith("Projekt: ") and " → Chat: " in worker_key:
        expected_project, expected_chat = worker_key[len("Projekt: "):].split(" → Chat: ", 1)
    for item in reversed(items):
        body = str(item.get("body", ""))
        if not body.lstrip().startswith(("WORKER_STATUS", "WORKER_DONE", "WORKER_STALLED", "INTEGRATION_CONFLICT")):
            continue
        fields = _comment_fields(body)
        if (
            fields.get("PROJECT") == expected_project
            and fields.get("CHAT") == expected_chat
            and fields.get("GOAL_VERSION") == goal_version
            and fields.get("FINGERPRINT") == fingerprint
        ):
            try:
                return int(item.get("id"))
            except (TypeError, ValueError):
                return 0
    return None


class DocumentationSyncOutbox:
    """Durable two-phase canonical status publisher.

    Phase A verifies the canonical workstream status. Phase B verifies the same
    semantic status in Master. A write attempt alone never advances a phase.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        post_comment: Callable[[str, int, str], None],
        read_items: Callable[[str, int], list[dict]],
        master_repo: str,
        master_issue: int,
        now: Callable[[], float] = time.time,
    ):
        self.connection = connection
        self.post_comment = post_comment
        self.read_items = read_items
        self.master_repo = master_repo
        self.master_issue = int(master_issue)
        self.now = now
        self._migrate()

    def _migrate(self) -> None:
        with self.connection:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS doc_sync_outbox (
                    sync_key TEXT PRIMARY KEY,
                    worker_key TEXT NOT NULL,
                    goal_version TEXT NOT NULL,
                    desired_state TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    body TEXT NOT NULL,
                    workstream_repo TEXT NOT NULL,
                    workstream_issue INTEGER NOT NULL,
                    master_repo TEXT NOT NULL,
                    master_issue INTEGER NOT NULL,
                    workstream_verified INTEGER NOT NULL DEFAULT 0,
                    workstream_comment_id INTEGER,
                    master_verified INTEGER NOT NULL DEFAULT 0,
                    master_comment_id INTEGER,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_doc_sync_worker_goal
                ON doc_sync_outbox(worker_key,goal_version,updated_at);
            """)

    @staticmethod
    def key(worker_key: str, goal_version: str, desired_state: str, fingerprint: str) -> str:
        raw = "|".join((worker_key, goal_version, desired_state, fingerprint))
        return sha256(raw.encode()).hexdigest()

    def queue(
        self,
        *,
        worker_key: str,
        goal_version: str,
        desired_state: str,
        kind: str,
        fingerprint: str,
        body: str,
        workstream_repo: str,
        workstream_issue: int,
    ) -> str:
        sync_key = self.key(worker_key, goal_version, desired_state, fingerprint)
        with self.connection:
            self.connection.execute(
                """INSERT OR IGNORE INTO doc_sync_outbox(
                       sync_key,worker_key,goal_version,desired_state,kind,fingerprint,body,
                       workstream_repo,workstream_issue,master_repo,master_issue,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sync_key, worker_key, goal_version, desired_state, kind, fingerprint,
                    body, workstream_repo, int(workstream_issue), self.master_repo,
                    self.master_issue, self.now(),
                ),
            )
        return sync_key

    def get(self, sync_key: str):
        return self.connection.execute(
            "SELECT * FROM doc_sync_outbox WHERE sync_key=?", (sync_key,)
        ).fetchone()

    def latest(self, worker_key: str, goal_version: str):
        return self.connection.execute(
            """SELECT * FROM doc_sync_outbox
               WHERE worker_key=? AND goal_version=?
               ORDER BY updated_at DESC,rowid DESC LIMIT 1""",
            (worker_key, goal_version),
        ).fetchone()

    @staticmethod
    def state(row) -> str:
        if row is None:
            return DOC_SYNC_REQUIRED
        if row["last_error"]:
            return DOC_SYNC_BLOCKED
        if row["master_verified"]:
            return DOC_SYNC_VERIFIED
        if row["workstream_verified"]:
            return DOC_SYNC_MASTER_PENDING
        return DOC_SYNC_PENDING

    def _update(self, sync_key: str, **fields) -> None:
        allowed = {
            "workstream_verified", "workstream_comment_id", "master_verified",
            "master_comment_id", "last_error",
        }
        parts = ["updated_at=?"]
        values: list[object] = [self.now()]
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(key)
            parts.append(f"{key}=?")
            values.append(value)
        values.append(sync_key)
        with self.connection:
            self.connection.execute(
                f"UPDATE doc_sync_outbox SET {', '.join(parts)} WHERE sync_key=?", values
            )

    def process(self, sync_key: str) -> str:
        row = self.get(sync_key)
        if row is None:
            return DOC_SYNC_REQUIRED
        # A previous failure is retryable from the last verified phase.
        if row["last_error"]:
            self._update(sync_key, last_error="")
            row = self.get(sync_key)

        workstream = (str(row["workstream_repo"]), int(row["workstream_issue"]))
        master = (str(row["master_repo"]), int(row["master_issue"]))
        fingerprint = str(row["fingerprint"])
        worker_key = str(row["worker_key"])
        goal_version = str(row["goal_version"])
        body = str(row["body"])

        if not row["workstream_verified"]:
            try:
                items = self.read_items(*workstream)
                comment_id = _has_fingerprint(items, worker_key, goal_version, fingerprint)
                if comment_id is None:
                    self.post_comment(workstream[0], workstream[1], body)
                    items = self.read_items(*workstream)
                    comment_id = _has_fingerprint(items, worker_key, goal_version, fingerprint)
                if comment_id is None:
                    raise RuntimeError("workstream status persistence could not be verified")
                self._update(
                    sync_key,
                    workstream_verified=1,
                    workstream_comment_id=comment_id,
                    last_error="",
                )
            except Exception as exc:
                self._update(sync_key, last_error=str(sanitize(str(exc)))[:240])
                return DOC_SYNC_BLOCKED
            row = self.get(sync_key)

        if workstream == master:
            if not row["master_verified"]:
                self._update(
                    sync_key,
                    master_verified=1,
                    master_comment_id=row["workstream_comment_id"],
                    last_error="",
                )
            return DOC_SYNC_VERIFIED

        if not row["master_verified"]:
            try:
                items = self.read_items(*master)
                comment_id = _has_fingerprint(items, worker_key, goal_version, fingerprint)
                if comment_id is None:
                    self.post_comment(master[0], master[1], body)
                    items = self.read_items(*master)
                    comment_id = _has_fingerprint(items, worker_key, goal_version, fingerprint)
                if comment_id is None:
                    raise RuntimeError("master mirror persistence could not be verified")
                self._update(
                    sync_key,
                    master_verified=1,
                    master_comment_id=comment_id,
                    last_error="",
                )
            except Exception as exc:
                self._update(sync_key, last_error=str(sanitize(str(exc)))[:240])
                return DOC_SYNC_BLOCKED
        return DOC_SYNC_VERIFIED

    def process_pending(self) -> dict[str, int]:
        counts = {"verified": 0, "blocked": 0, "pending": 0}
        rows = self.connection.execute(
            """SELECT sync_key FROM doc_sync_outbox
               WHERE master_verified=0 OR last_error<>''
               ORDER BY updated_at,sync_key"""
        ).fetchall()
        for row in rows:
            state = self.process(str(row[0]))
            if state == DOC_SYNC_VERIFIED:
                counts["verified"] += 1
            elif state == DOC_SYNC_BLOCKED:
                counts["blocked"] += 1
            else:
                counts["pending"] += 1
        return counts

    def force_workstream_verified(self, sync_key: str, comment_id: int | None = None) -> None:
        self._update(
            sync_key,
            workstream_verified=1,
            workstream_comment_id=comment_id,
            last_error="",
        )

    def summary(self) -> dict[str, int]:
        result = {
            DOC_SYNC_PENDING: 0,
            DOC_SYNC_WORKSTREAM_VERIFIED: 0,
            DOC_SYNC_MASTER_PENDING: 0,
            DOC_SYNC_VERIFIED: 0,
            DOC_SYNC_BLOCKED: 0,
        }
        for row in self.connection.execute("SELECT * FROM doc_sync_outbox"):
            state = self.state(row)
            if state == DOC_SYNC_MASTER_PENDING:
                result[DOC_SYNC_MASTER_PENDING] += 1
            elif state in result:
                result[state] += 1
        return result
