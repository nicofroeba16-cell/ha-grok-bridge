from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.parse import urlparse

from .control_plane import MasterRequestError, parse_master_request

MASTER_ROUTE_KEY = "__master__"
MASTER_RELEVANT_STATES = frozenset({"DONE", "BLOCKED", "WAITING_FOR_USER", "READY", "STALLED"})
MASTER_RELEVANT_KINDS = frozenset({"WORKER_DONE", "WORKER_STALLED", "INTEGRATION_CONFLICT"})
IGNORED_KINDS = frozenset({
    "MASTER_REQUEST", "MASTER_STATUS", "MASTER_DONE", "MASTER_BLOCKED",
    "BROWSER_WAKE_STATUS", "BROWSER_WAKE_DELIVERY", "BROWSER_WAKE_BOOTSTRAP",
})


class BrowserWakeError(RuntimeError):
    pass


class BrowserWakePreSendError(BrowserWakeError):
    pass


class BrowserWakeUncertainError(BrowserWakeError):
    pass


@dataclass(frozen=True, slots=True)
class BrowserRoute:
    key: str
    url: str


class BrowserRouteRegistry:
    def __init__(self, routes: Mapping[str, BrowserRoute]):
        self.routes = dict(routes)

    @staticmethod
    def _validate_url(raw: str) -> str:
        value = raw.strip()
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname != "chatgpt.com":
            raise BrowserWakeError("browser route must use https://chatgpt.com")
        path = parsed.path.rstrip("/")
        if not re.fullmatch(r"/(?:g/g-p-[A-Za-z0-9_-]+/)?c/[A-Za-z0-9-]+", path):
            raise BrowserWakeError("browser route must target one concrete ChatGPT conversation")
        if parsed.username or parsed.password or parsed.port:
            raise BrowserWakeError("browser route must not contain credentials or a custom port")
        return value

    @classmethod
    def from_json(cls, raw: str) -> "BrowserRouteRegistry":
        value = json.loads(raw)
        if not isinstance(value, Mapping):
            raise BrowserWakeError("BROWSER_CHAT_ROUTES_JSON must be an object")
        routes: dict[str, BrowserRoute] = {}
        for key, config in value.items():
            if not isinstance(config, Mapping):
                raise BrowserWakeError(f"browser route {key} must be an object")
            routes[str(key)] = BrowserRoute(str(key), cls._validate_url(str(config.get("url", ""))))
        return cls(routes)

    @classmethod
    def from_file(cls, path: str | Path) -> "BrowserRouteRegistry":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def get(self, key: str) -> BrowserRoute | None:
        return self.routes.get(key)


def _hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _fields(body: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in body.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip().upper()] = value.strip()
    return result


def _kind(body: str) -> str:
    return next((line.strip() for line in body.splitlines() if line.strip()), "")


def _event_id(item: Mapping, fallback: int) -> int:
    try:
        return int(item.get("id") or fallback)
    except (TypeError, ValueError):
        return fallback


def master_event_fingerprint(item: Mapping) -> str | None:
    body = str(item.get("body", ""))
    kind = _kind(body)
    if not kind or kind in IGNORED_KINDS or kind.startswith("BROWSER_WAKE_"):
        return None
    fields = _fields(body)
    if kind not in MASTER_RELEVANT_KINDS:
        if kind != "WORKER_STATUS":
            return None
        state = fields.get("STATE", "").upper()
        ci = fields.get("CI", "").upper()
        user_action = fields.get("USER_ACTION_REQUIRED", "").strip().lower()
        if state not in MASTER_RELEVANT_STATES and ci not in {"RED", "FAIL", "FAILED", "FAILURE"}:
            if user_action in {"", "[]", "none", "null"}:
                return None
    explicit = fields.get("FINGERPRINT", "")
    if explicit:
        return "canonical:" + explicit
    semantic = {
        "kind": kind,
        "project": fields.get("PROJECT", ""),
        "chat": fields.get("CHAT", ""),
        "goal": fields.get("GOAL_VERSION", ""),
        "state": fields.get("STATE", "DONE" if kind == "WORKER_DONE" else ""),
        "head": fields.get("HEAD", fields.get("FINAL_HEAD", "")),
        "ci": fields.get("CI", ""),
        "blockers": fields.get("BLOCKERS", ""),
        "user_action": fields.get("USER_ACTION_REQUIRED", ""),
    }
    return "semantic:" + _hash(semantic)


class WakeLedger:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self._migrate()

    def _migrate(self) -> None:
        with self.connection:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS browser_wake_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_wake_delivery (
                    message_id TEXT PRIMARY KEY, route_key TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_wake_seen_status (
                    fingerprint TEXT PRIMARY KEY, event_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_wake_pending_worker (
                    message_id TEXT PRIMARY KEY, route_key TEXT NOT NULL,
                    destination TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_wake_pending_master (
                    id INTEGER PRIMARY KEY CHECK(id=1), event_ids TEXT NOT NULL,
                    first_seen REAL NOT NULL, max_event_id INTEGER NOT NULL
                );
            """)

    def get_int(self, key: str, default: int = 0) -> int:
        row = self.connection.execute("SELECT value FROM browser_wake_meta WHERE key=?", (key,)).fetchone()
        try:
            return int(row[0]) if row else default
        except (TypeError, ValueError):
            return default

    def set_int(self, key: str, value: int) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO browser_wake_meta(key,value) VALUES(?,?)", (key, str(int(value)))
            )

    def has_seen_status(self, fingerprint: str) -> bool:
        return bool(self.connection.execute(
            "SELECT 1 FROM browser_wake_seen_status WHERE fingerprint=?", (fingerprint,)
        ).fetchone())

    def mark_seen_status(self, fingerprint: str, event_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO browser_wake_seen_status(fingerprint,event_id) VALUES(?,?)",
                (fingerprint, event_id),
            )

    def delivery(self, message_id: str):
        return self.connection.execute(
            "SELECT status,attempts,last_error FROM browser_wake_delivery WHERE message_id=?", (message_id,)
        ).fetchone()

    def claim(self, message_id: str, route_key: str, max_attempts: int = 3) -> bool:
        row = self.delivery(message_id)
        if row and row[0] in {"DELIVERED", "IN_FLIGHT", "UNCERTAIN", "BLOCKED"}:
            return False
        attempts = int(row[1]) if row else 0
        if attempts >= max_attempts:
            with self.connection:
                self.connection.execute(
                    "UPDATE browser_wake_delivery SET status='BLOCKED',updated_at=? WHERE message_id=?",
                    (time.time(), message_id),
                )
            return False
        with self.connection:
            self.connection.execute(
                """INSERT INTO browser_wake_delivery(message_id,route_key,status,attempts,last_error,updated_at)
                   VALUES(?,?,'IN_FLIGHT',1,'',?) ON CONFLICT(message_id) DO UPDATE SET
                   route_key=excluded.route_key,status='IN_FLIGHT',attempts=browser_wake_delivery.attempts+1,
                   last_error='',updated_at=excluded.updated_at""",
                (message_id, route_key, time.time()),
            )
        return True

    def finish(self, message_id: str, status: str, error: str = "") -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE browser_wake_delivery SET status=?,last_error=?,updated_at=? WHERE message_id=?",
                (status, error[:240], time.time(), message_id),
            )

    def recover_interrupted(self) -> None:
        # At-most-once safety: a crashed process may already have clicked Send.
        # Never retry such a wake automatically.
        with self.connection:
            self.connection.execute(
                "UPDATE browser_wake_delivery SET status='UNCERTAIN',last_error='INTERRUPTED_AFTER_CLAIM',updated_at=? WHERE status='IN_FLIGHT'",
                (time.time(),),
            )

    def queue_worker(self, message_id: str, route_key: str, destination: str, payload: str) -> None:
        row = self.delivery(message_id)
        if row and row[0] in {"DELIVERED", "UNCERTAIN", "BLOCKED"}:
            return
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO browser_wake_pending_worker(message_id,route_key,destination,payload) VALUES(?,?,?,?)",
                (message_id, route_key, destination, payload),
            )

    def pending_workers(self) -> list[tuple[str, str, str, str]]:
        return [tuple(row) for row in self.connection.execute(
            "SELECT message_id,route_key,destination,payload FROM browser_wake_pending_worker ORDER BY message_id"
        )]

    def remove_pending_worker(self, message_id: str) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM browser_wake_pending_worker WHERE message_id=?", (message_id,))

    def pending_master(self) -> tuple[list[int], float, int] | None:
        row = self.connection.execute(
            "SELECT event_ids,first_seen,max_event_id FROM browser_wake_pending_master WHERE id=1"
        ).fetchone()
        if not row:
            return None
        return list(json.loads(row[0])), float(row[1]), int(row[2])

    def add_pending_master(self, event_ids: Iterable[int], max_event_id: int, now: float) -> None:
        current = self.pending_master()
        merged = sorted(set((current[0] if current else []) + [int(x) for x in event_ids]))
        first_seen = current[1] if current else now
        max_seen = max(max_event_id, current[2] if current else 0)
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO browser_wake_pending_master(id,event_ids,first_seen,max_event_id) VALUES(1,?,?,?)",
                (json.dumps(merged), first_seen, max_seen),
            )

    def clear_pending_master(self) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM browser_wake_pending_master WHERE id=1")


class CommandBrowserSender:
    """Input-only sender. The helper returns delivery metadata, never ChatGPT output."""

    def __init__(self, command: str, *, timeout: int = 330):
        parts = shlex.split(command)
        if not parts:
            raise BrowserWakeError("BROWSER_WAKE_COMMAND is required")
        self.command = parts
        self.timeout = max(5, timeout)

    def __call__(self, message_id: str, destination: str, payload: str) -> None:
        request = json.dumps({"message_id": message_id, "destination": destination, "payload": payload}, ensure_ascii=False)
        try:
            proc = subprocess.run(
                self.command, input=request + "\n", text=True, capture_output=True,
                timeout=self.timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # The child may already have clicked Send; an outer timeout is never retry-safe.
            raise BrowserWakeUncertainError(str(exc)) from exc
        except OSError as exc:
            raise BrowserWakePreSendError(str(exc)) from exc
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        result = {}
        if lines:
            try:
                result = json.loads(lines[-1])
            except json.JSONDecodeError:
                result = {}
        if proc.returncode == 0 and result.get("status") == "sent":
            return
        reason = str(result.get("error") or proc.stderr.strip() or "browser sender failed")[:240]
        if result.get("safe_to_retry") is True:
            raise BrowserWakePreSendError(reason)
        raise BrowserWakeUncertainError(reason)


class WakeCoordinator:
    def __init__(
        self,
        connection: sqlite3.Connection,
        routes: BrowserRouteRegistry,
        sender: Callable[[str, str, str], None],
        *,
        master_repo: str,
        master_issue: int,
        debounce_seconds: float = 10.0,
        now: Callable[[], float] = time.time,
    ):
        self.ledger = WakeLedger(connection)
        self.routes = routes
        self.sender = sender
        self.master_repo = master_repo
        self.master_issue = int(master_issue)
        self.debounce_seconds = max(0.0, float(debounce_seconds))
        self.now = now
        self.ledger.recover_interrupted()

    def bootstrap(self, items: list[Mapping]) -> int:
        if self.ledger.get_int("scan_cursor", 0):
            return self.ledger.get_int("scan_cursor", 0)
        maximum = max((_event_id(item, index + 1) for index, item in enumerate(items)), default=0)
        self.ledger.set_int("scan_cursor", maximum)
        return maximum

    def reconcile(self, items: list[Mapping], *, replay_existing: bool = False) -> dict[str, int | str]:
        if not replay_existing and self.ledger.get_int("scan_cursor", 0) == 0:
            cursor = self.bootstrap(items)
            return {"state": "BOOTSTRAPPED", "cursor": cursor, "worker_wakes": 0, "master_wakes": 0}

        cursor = self.ledger.get_int("scan_cursor", 0)
        ordered = sorted(
            [(_event_id(item, index + 1), item) for index, item in enumerate(items)],
            key=lambda pair: pair[0],
        )
        new_items = [(event_id, item) for event_id, item in ordered if event_id > cursor]
        relevant_master_ids: list[int] = []

        for event_id, item in new_items:
            body = str(item.get("body", ""))
            if _kind(body) == "MASTER_REQUEST":
                self._queue_request(body, event_id)
            fingerprint = master_event_fingerprint(item)
            if fingerprint and not self.ledger.has_seen_status(fingerprint):
                self.ledger.mark_seen_status(fingerprint, event_id)
                relevant_master_ids.append(event_id)

        max_seen = max([cursor, *[event_id for event_id, _ in new_items]])
        if new_items:
            self.ledger.set_int("scan_cursor", max_seen)
        if relevant_master_ids:
            self.ledger.add_pending_master(relevant_master_ids, max_seen, self.now())

        worker_wakes = self._flush_worker_pending()
        master_wakes = self._flush_master_pending()
        return {
            "state": "OK", "cursor": max_seen,
            "worker_wakes": worker_wakes, "master_wakes": master_wakes,
        }

    def _queue_request(self, body: str, event_id: int) -> None:
        try:
            request = parse_master_request(body, event_id)
        except MasterRequestError:
            return
        if request is None:
            return
        for child in request.children:
            route = self.routes.get(child.worker_key)
            if route is None:
                continue
            message_id = f"worker-wake:{request.request_id}:{request.version}:{child.child_id}"
            payload = "\n".join([
                "WORKER_WAKE",
                f"WAKE_ID: {message_id}",
                f"PROJECT: {child.project}",
                f"CHAT: {child.chat}",
                f"GOAL_VERSION: {request.version}-{child.child_id}",
                f"MASTER: {self.master_repo}#{self.master_issue}",
                f"WORKSTREAM_ISSUE: {child.workstream_issue or ''}",
                "ACTION: New GitHub work is available. Read your assigned workstream issue and Master state, then continue only your owned scope.",
                "LOOP_GUARD: Do not answer this wake through the browser relay. Report substantive status only through the canonical GitHub workstream log.",
                "LIVE_GATE: Every live-system mutation still requires separate explicit user approval.",
            ])
            self.ledger.queue_worker(message_id, child.worker_key, route.url, payload)

    def _flush_worker_pending(self) -> int:
        sent = 0
        for message_id, route_key, destination, payload in self.ledger.pending_workers():
            delivered = self._send_once(message_id, route_key, destination, payload)
            row = self.ledger.delivery(message_id)
            if delivered:
                sent += 1
            if row and row[0] in {"DELIVERED", "UNCERTAIN", "BLOCKED"}:
                self.ledger.remove_pending_worker(message_id)
        return sent

    def _flush_master_pending(self) -> int:
        pending = self.ledger.pending_master()
        if not pending:
            return 0
        event_ids, first_seen, max_event_id = pending
        if self.now() - first_seen < self.debounce_seconds:
            return 0
        route = self.routes.get(MASTER_ROUTE_KEY)
        if route is None:
            return 0
        message_id = f"master-wake:{_hash(event_ids)[:20]}"
        payload = "\n".join([
            "MASTER_WAKE",
            f"WAKE_ID: {message_id}",
            f"MASTER: {self.master_repo}#{self.master_issue}",
            f"NEW_RELEVANT_EVENTS: {len(event_ids)}",
            f"THROUGH_EVENT_ID: {max_event_id}",
            "ACTION: New worker-relevant status is available. Read Master Issue #3 from the last processed status and coordinate only genuinely new work.",
            "LOOP_GUARD: Do not create a wake/status echo. Master-originated and browser-relay-originated messages never trigger Master again.",
            "LIVE_GATE: Every live-system mutation still requires separate explicit user approval.",
        ])
        delivered = self._send_once(message_id, MASTER_ROUTE_KEY, route.url, payload)
        row = self.ledger.delivery(message_id)
        if delivered or (row and row[0] in {"DELIVERED", "UNCERTAIN", "BLOCKED"}):
            self.ledger.clear_pending_master()
        return int(delivered)

    def _send_once(self, message_id: str, route_key: str, destination: str, payload: str) -> bool:
        row = self.ledger.delivery(message_id)
        if row and row[0] == "DELIVERED":
            return False
        if not self.ledger.claim(message_id, route_key):
            return False
        try:
            self.sender(message_id, destination, payload)
        except BrowserWakePreSendError as exc:
            self.ledger.finish(message_id, "FAILED_PRE_SEND", str(exc))
            return False
        except BrowserWakeUncertainError as exc:
            self.ledger.finish(message_id, "UNCERTAIN", str(exc))
            return False
        except Exception as exc:
            self.ledger.finish(message_id, "UNCERTAIN", f"{type(exc).__name__}: {exc}")
            return False
        self.ledger.finish(message_id, "DELIVERED")
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="browser-wake")
    parser.add_argument("--db", default=os.environ.get("BROWSER_WAKE_DB", "./browser-wake.sqlite3"))
    parser.add_argument("--routes-file", default=os.environ.get("BROWSER_CHAT_ROUTES_FILE", ""))
    parser.add_argument("--master-repo", default=os.environ.get("MASTER_REPO", "nicofroeba16-cell/ha-grok-bridge"))
    parser.add_argument("--master-issue", type=int, default=int(os.environ.get("MASTER_ISSUE", "3")))
    parser.add_argument("--poll-seconds", type=int, default=int(os.environ.get("BROWSER_WAKE_POLL_SECONDS", "15")))
    parser.add_argument("--debounce-seconds", type=float, default=float(os.environ.get("MASTER_WAKE_DEBOUNCE_SECONDS", "10")))
    parser.add_argument("--browser-command", default=os.environ.get("BROWSER_WAKE_COMMAND", ""))
    parser.add_argument("--replay-existing", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("once")
    sub.add_parser("run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.routes_file:
        raise SystemExit("BROWSER_CHAT_ROUTES_FILE is required")
    routes = BrowserRouteRegistry.from_file(args.routes_file)
    sender = CommandBrowserSender(args.browser_command)
    connection = sqlite3.connect(args.db)
    coordinator = WakeCoordinator(
        connection, routes, sender,
        master_repo=args.master_repo, master_issue=args.master_issue,
        debounce_seconds=args.debounce_seconds,
    )
    from .github_client import GitHubClient
    gh = GitHubClient.from_env()

    def run_once() -> dict[str, int | str]:
        return coordinator.reconcile(
            gh.read_master_items(args.master_repo, args.master_issue),
            replay_existing=args.replay_existing,
        )

    try:
        if args.cmd == "once":
            print(json.dumps(run_once(), sort_keys=True))
            return 0
        while True:
            try:
                print(json.dumps(run_once(), sort_keys=True), flush=True)
            except Exception as exc:
                print(json.dumps({"state": "ERROR", "error": f"{type(exc).__name__}: {exc}"[:240]}), flush=True)
            time.sleep(max(5, args.poll_seconds))
    except KeyboardInterrupt:
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
