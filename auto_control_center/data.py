from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from .security import sanitize
from .state import annotate_registry_aliases, evidence_for_worker, resolve_worker, wake_class

ORCHESTRATOR_DB = Path(os.environ.get("ACC_ORCHESTRATOR_DB", "~/.local/share/worker-orchestrator/state.sqlite3")).expanduser()
BROWSER_WAKE_DB = Path(os.environ.get("ACC_BROWSER_WAKE_DB", "~/.local/share/browser-wake/state/browser-wake.sqlite3")).expanduser()
BROWSER_ROUTES = Path(os.environ.get("ACC_BROWSER_ROUTES", "~/.config/worker-orchestrator-browser-wake/browser-routes.json")).expanduser()
SERVICES = tuple(
    x.strip()
    for x in os.environ.get(
        "ACC_SYSTEMD_SERVICES",
        "worker-orchestrator.service,browser-wake.service,browser-wake-chrome.service",
    ).split(",")
    if x.strip()
)

JSON_FIELDS = {
    "workers": {"done_criteria", "files", "approved_actions", "blockers", "user_gate", "completion_evidence", "verified_criteria", "session_state"},
    "events": {"payload"},
    "master_requests": {"done_criteria"},
    "master_children": {"dependencies"},
}


def _ro_connect(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _decode_json_fields(table: str, row: dict[str, Any]) -> dict[str, Any]:
    for key in JSON_FIELDS.get(table, set()):
        if key not in row:
            continue
        raw = row[key]
        if not isinstance(raw, str):
            continue
        try:
            row[key] = json.loads(raw)
        except json.JSONDecodeError:
            pass
    return row


def _query(path: Path, table: str, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        conn = _ro_connect(path)
        try:
            rows = conn.execute(sql, params).fetchall()
            return [_decode_json_fields(table, dict(row)) for row in rows]
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return []


def workers() -> list[dict[str, Any]]:
    rows = _query(
        ORCHESTRATOR_DB,
        "workers",
        """SELECT worker_key,project,chat,repository,branch,workstream_issue,
                  goal_version,state,last_head,ci_status,blockers,user_gate,
                  last_progress,verified_criteria,done_criteria,updated_at
             FROM workers ORDER BY updated_at DESC, worker_key""",
    )
    resolved = [resolve_worker(sanitize(row)) for row in rows]
    return annotate_registry_aliases(resolved)


def orchestrator_events(limit: int = 80) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    rows = _query(
        ORCHESTRATOR_DB,
        "events",
        "SELECT id,worker_key,goal_version,event_type,payload,created_at FROM events ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return sanitize(rows)


def latest_master() -> dict[str, Any] | None:
    rows = _query(
        ORCHESTRATOR_DB,
        "master_requests",
        """SELECT request_id,version,request_text,done_criteria,state,
                  source_comment_id,updated_at
             FROM master_requests ORDER BY updated_at DESC LIMIT 1""",
    )
    if not rows:
        return None
    master = rows[0]
    children = _query(
        ORCHESTRATOR_DB,
        "master_children",
        """SELECT child_id,worker_key,dependencies,state,blocker,dispatched
             FROM master_children WHERE request_id=? ORDER BY child_id""",
        (master["request_id"],),
    )
    master["children"] = children
    total = len(children)
    done = sum(1 for child in children if str(child.get("state") or "").upper() == "DONE")
    master["progress"] = {
        "done": done,
        "total": total,
        "percent": round(done * 100 / total) if total else 0,
    }
    return sanitize(master)


def wake_deliveries(limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    rows = _query(
        BROWSER_WAKE_DB,
        "browser_wake_delivery",
        """SELECT message_id,route_key,status,attempts,last_error,updated_at
             FROM browser_wake_delivery ORDER BY updated_at DESC LIMIT ?""",
        (limit,),
    )
    result = sanitize(rows)
    for row in result:
        row["status_class"] = wake_class(row.get("status"))
    return result


def wake_queues() -> dict[str, int]:
    workers_pending = _query(
        BROWSER_WAKE_DB,
        "browser_wake_pending_worker",
        "SELECT COUNT(*) AS n FROM browser_wake_pending_worker",
    )
    master_pending = _query(
        BROWSER_WAKE_DB,
        "browser_wake_pending_master",
        "SELECT COUNT(*) AS n FROM browser_wake_pending_master",
    )
    return {
        "workers": int(workers_pending[0]["n"]) if workers_pending else 0,
        "master": int(master_pending[0]["n"]) if master_pending else 0,
    }


def route_summary() -> list[dict[str, Any]]:
    try:
        raw = json.loads(BROWSER_ROUTES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    result = []
    for key, config in raw.items():
        kind = (
            "url"
            if isinstance(config, dict) and config.get("url")
            else "title"
            if isinstance(config, dict) and config.get("title")
            else "unknown"
        )
        result.append({"worker_key": sanitize(str(key)), "bound": kind != "unknown", "destination_kind": kind})
    return sorted(result, key=lambda x: (x["worker_key"] != "__master__", x["worker_key"]))


def service_states() -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    env = os.environ.copy()
    runtime = f"/run/user/{os.getuid()}"
    env.setdefault("XDG_RUNTIME_DIR", runtime)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    for name in SERVICES:
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "show", name, "--property=ActiveState,SubState,Result", "--no-pager"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
                env=env,
            )
            fields = {
                k: v
                for line in proc.stdout.splitlines()
                if "=" in line
                for k, v in [line.split("=", 1)]
            }
            states.append(
                {
                    "name": sanitize(name),
                    "active": fields.get("ActiveState", "unknown"),
                    "sub": fields.get("SubState", "unknown"),
                    "result": fields.get("Result", "unknown"),
                }
            )
        except (OSError, subprocess.SubprocessError):
            states.append({"name": sanitize(name), "active": "unavailable", "sub": "unknown", "result": "unknown"})
    return states


def _source_state(path: Path) -> dict[str, bool]:
    present = path.is_file()
    return {"present": present, "readable": present and os.access(path, os.R_OK)}


def source_health() -> dict[str, Any]:
    return {
        "orchestrator_db": _source_state(ORCHESTRATOR_DB),
        "browser_wake_db": _source_state(BROWSER_WAKE_DB),
        "browser_routes": _source_state(BROWSER_ROUTES),
        "generated_at": time.time(),
    }


def evidence_overview(worker_rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows = worker_rows if worker_rows is not None else workers()
    return [evidence_for_worker(row) for row in rows]


def _registry_shared_target_groups(worker_rows: list[dict[str, Any]]) -> int:
    groups = {
        (row.get("repository"), row.get("branch"), row.get("goal_version"))
        for row in worker_rows
        if row.get("registry_shared_target")
    }
    return len(groups)


def dashboard() -> dict[str, Any]:
    worker_rows = workers()
    wake_rows = wake_deliveries()
    master = latest_master()
    evidence = evidence_overview(worker_rows)
    return {
        "health": source_health(),
        "master": master,
        "workers": worker_rows,
        "events": orchestrator_events(60),
        "wakes": wake_rows,
        "wake_queues": wake_queues(),
        "routes": route_summary(),
        "services": service_states(),
        "evidence": evidence,
        "stats": {
            "workers": len(worker_rows),
            "running": sum(1 for row in worker_rows if row.get("resolved_state") == "RUNNING"),
            "blocked": sum(1 for row in worker_rows if row.get("resolved_state") in {"BLOCKED", "STALLED", "WAITING_FOR_USER"}),
            "wake_uncertain": sum(1 for row in wake_rows if row.get("status_class") == "uncertain"),
            "ci_red": sum(1 for row in evidence if row.get("ci_class") == "red"),
            "registry_shared_targets": _registry_shared_target_groups(worker_rows),
        },
    }
