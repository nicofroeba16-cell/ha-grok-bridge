from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from .refresh import canonical_event_id, source_change_token as _source_change_token, stable_payload_signature
from .security import sanitize
from .state import (
    annotate_checkpoint_semantics_all,
    annotate_registry_aliases,
    apply_activation_provenance,
    evidence_for_worker,
    resolve_worker,
    wake_class,
)

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

WORKER_BASE_COLUMNS = (
    "worker_key", "project", "chat", "repository", "branch", "workstream_issue",
    "goal_version", "state", "last_head", "ci_status", "blockers", "user_gate",
    "last_progress", "verified_criteria", "done_criteria", "source_comment_id",
    "updated_at", "ui_visual_scope", "visual_media_required",
)
WORKER_PROVENANCE_COLUMNS = (
    "activation_source", "activation_confirmed", "activation_updated_at", "state_updated_at",
)


def _ro_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _decode_json_fields(table: str, row: dict[str, Any]) -> dict[str, Any]:
    for key in JSON_FIELDS.get(table, set()):
        if key not in row or not isinstance(row[key], str):
            continue
        try:
            row[key] = json.loads(row[key])
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


def _table_columns(path: Path, table: str) -> set[str]:
    if not path.is_file():
        return set()
    try:
        conn = _ro_connect(path)
        try:
            return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return set()


def _table_exists(path: Path, table: str) -> bool:
    if not path.is_file():
        return False
    try:
        conn = _ro_connect(path)
        try:
            return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return False


def _route_config() -> dict[str, Any]:
    try:
        raw = json.loads(BROWSER_ROUTES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def route_keys() -> set[str]:
    return {str(key) for key in _route_config() if key != "__master__"}


def _worker_rows() -> list[dict[str, Any]]:
    available = _table_columns(ORCHESTRATOR_DB, "workers")
    columns = [name for name in (*WORKER_BASE_COLUMNS, *WORKER_PROVENANCE_COLUMNS) if name in available]
    if not {"worker_key", "state", "updated_at"}.issubset(columns):
        return []
    return _query(
        ORCHESTRATOR_DB,
        "workers",
        f"SELECT {','.join(columns)} FROM workers ORDER BY updated_at DESC, worker_key",
    )


def wake_deliveries(limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    available = _table_columns(BROWSER_WAKE_DB, "browser_wake_delivery")
    base = ("message_id", "route_key", "status", "attempts", "last_error", "updated_at")
    if not set(base).issubset(available):
        return []
    verified: dict[str, dict[str, Any]] = {}
    if _table_exists(BROWSER_WAKE_DB, "browser_wake_verified_delivery"):
        for row in _query(
            BROWSER_WAKE_DB,
            "browser_wake_verified_delivery",
            "SELECT message_id,route_key,goal_version,published,updated_at FROM browser_wake_verified_delivery",
        ):
            verified[str(row.get("message_id") or "")] = row
    rows = _query(
        BROWSER_WAKE_DB,
        "browser_wake_delivery",
        f"SELECT {','.join(base)} FROM browser_wake_delivery ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    )
    result = sanitize(rows)
    for row in result:
        match = verified.get(str(row.get("message_id") or ""))
        if match and match.get("goal_version"):
            row["goal_version"] = sanitize(match.get("goal_version"))
            row["delivery_verified_record"] = bool(match.get("published"))
        row["status_class"] = wake_class(row.get("status"))
    return result


def workers(wake_rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    resolved = [resolve_worker(sanitize(row)) for row in _worker_rows()]
    activated = apply_activation_provenance(resolved, wake_rows if wake_rows is not None else wake_deliveries(500))
    registry = annotate_registry_aliases(activated, route_keys())
    return annotate_checkpoint_semantics_all(registry)


def orchestrator_events(limit: int = 80) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    return sanitize(_query(
        ORCHESTRATOR_DB,
        "events",
        "SELECT id,worker_key,goal_version,event_type,payload,created_at FROM events ORDER BY id DESC LIMIT ?",
        (limit,),
    ))


def latest_master() -> dict[str, Any] | None:
    rows = _query(
        ORCHESTRATOR_DB,
        "master_requests",
        """SELECT request_id,version,request_text,done_criteria,state,source_comment_id,updated_at
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
    done = sum(1 for child in children if str(child.get("state") or "").upper() in {"DONE", "READY"})
    master["progress"] = {"done": done, "total": total, "percent": round(done * 100 / total) if total else 0}
    return sanitize(master)


def wake_queues() -> dict[str, int]:
    workers_pending = _query(BROWSER_WAKE_DB, "browser_wake_pending_worker", "SELECT COUNT(*) AS n FROM browser_wake_pending_worker")
    master_pending = _query(BROWSER_WAKE_DB, "browser_wake_pending_master", "SELECT COUNT(*) AS n FROM browser_wake_pending_master")
    return {
        "workers": int(workers_pending[0]["n"]) if workers_pending else 0,
        "master": int(master_pending[0]["n"]) if master_pending else 0,
    }


def route_summary() -> list[dict[str, Any]]:
    result = []
    for key, config in _route_config().items():
        kind = "url" if isinstance(config, dict) and config.get("url") else "title" if isinstance(config, dict) and config.get("title") else "unknown"
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
                check=False, capture_output=True, text=True, timeout=2, env=env,
            )
            fields = {k: v for line in proc.stdout.splitlines() if "=" in line for k, v in [line.split("=", 1)]}
            states.append({
                "name": sanitize(name),
                "active": fields.get("ActiveState", "unknown"),
                "sub": fields.get("SubState", "unknown"),
                "result": fields.get("Result", "unknown"),
            })
        except (OSError, subprocess.SubprocessError):
            states.append({"name": sanitize(name), "active": "unavailable", "sub": "unknown", "result": "unknown"})
    return states


def wake_path_health(services: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = services if services is not None else service_states()
    required = set(SERVICES)
    by_name = {str(row.get("name") or ""): row for row in rows}
    missing = sorted(required - set(by_name))
    unhealthy = sorted(name for name in required if name in by_name and by_name[name].get("active") != "active")
    return {
        "healthy": bool(required) and not missing and not unhealthy,
        "components_expected": len(required),
        "components_active": sum(1 for name in required if name in by_name and by_name[name].get("active") == "active"),
        "missing_count": len(missing),
        "unhealthy_count": len(unhealthy),
    }


def master_request_rejections(limit: int = 5) -> dict[str, Any]:
    if not _table_exists(BROWSER_WAKE_DB, "browser_wake_rejections"):
        return {"total": 0, "recent": [], "schema_ready": False}
    total_rows = _query(BROWSER_WAKE_DB, "browser_wake_rejections", "SELECT COUNT(*) AS n FROM browser_wake_rejections WHERE kind='MASTER_REQUEST'")
    recent = _query(
        BROWSER_WAKE_DB,
        "browser_wake_rejections",
        """SELECT source_comment_id,kind,reason,created_at FROM browser_wake_rejections
             WHERE kind='MASTER_REQUEST' ORDER BY created_at DESC LIMIT ?""",
        (max(1, min(limit, 20)),),
    )
    return sanitize({"total": int(total_rows[0]["n"]) if total_rows else 0, "recent": recent, "schema_ready": True})


def media_archive_overview() -> dict[str, Any]:
    """Fail closed until a canonical media-archive table/schema is actually present."""
    return {
        "schema_ready": False,
        "summary": {
            "media_jobs_pending": None,
            "media_jobs_running": None,
            "media_jobs_blocked": None,
            "media_jobs_verified": None,
            "latest_media_archive_age": None,
        },
        "goals": [],
    }


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


def source_change_token() -> tuple[tuple[str, int, int], ...]:
    return _source_change_token((ORCHESTRATOR_DB, BROWSER_WAKE_DB, BROWSER_ROUTES))


def evidence_overview(worker_rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows = worker_rows if worker_rows is not None else workers()
    return [evidence_for_worker(row) for row in rows]


def _registry_shared_target_groups(worker_rows: list[dict[str, Any]]) -> int:
    return len({
        (row.get("repository"), row.get("branch"), row.get("goal_version"))
        for row in worker_rows if row.get("registry_shared_target")
    })


def route_drift(worker_rows: list[dict[str, Any]], routes: list[dict[str, Any]]) -> dict[str, int]:
    ledger_keys = {str(row.get("worker_key") or "") for row in worker_rows}
    route_keys_set = {str(row.get("worker_key") or "") for row in routes if row.get("worker_key") != "__master__"}
    return {
        "ledger_unrouted": sum(1 for row in worker_rows if not row.get("route_bound")),
        "route_without_ledger": len(route_keys_set - ledger_keys),
        "legacy_unrouted": sum(1 for row in worker_rows if row.get("registry_identity_state") == "LEGACY_UNROUTED"),
        "superseded": sum(1 for row in worker_rows if row.get("registry_identity_state") == "SUPERSEDED"),
    }


def dashboard() -> dict[str, Any]:
    wake_rows = wake_deliveries(500)
    worker_rows = workers(wake_rows)
    master = latest_master()
    evidence = evidence_overview(worker_rows)
    services = service_states()
    routes = route_summary()
    rejects = master_request_rejections()
    media = media_archive_overview()
    payload: dict[str, Any] = {
        "health": source_health(),
        "wake_path": wake_path_health(services),
        "master_request_health": rejects,
        "media_archive": media,
        "master": master,
        "workers": worker_rows,
        "events": orchestrator_events(60),
        "wakes": wake_rows[:100],
        "wake_queues": wake_queues(),
        "routes": routes,
        "route_drift": route_drift(worker_rows, routes),
        "services": services,
        "evidence": evidence,
        "stats": {
            "workers": len(worker_rows),
            "running": sum(1 for row in worker_rows if row.get("resolved_state") == "RUNNING"),
            "blocked": sum(1 for row in worker_rows if row.get("resolved_state") in {"BLOCKED", "STALLED", "WAITING_FOR_USER", "ERROR"}),
            "blocked_stalled": sum(1 for row in worker_rows if row.get("resolved_state") in {"BLOCKED", "STALLED"}),
            "waiting_for_user": sum(1 for row in worker_rows if row.get("resolved_state") == "WAITING_FOR_USER"),
            "ready_done": sum(1 for row in worker_rows if row.get("resolved_state") in {"READY", "DONE"}),
            "wake_uncertain": sum(1 for row in wake_rows if row.get("status_class") == "uncertain"),
            "activation_unconfirmed": sum(1 for row in worker_rows if row.get("resolved_state") == "RUNNING" and row.get("activation_confirmed") is False),
            "ci_red": sum(1 for row in evidence if row.get("ci_class") == "red"),
            "registry_shared_targets": _registry_shared_target_groups(worker_rows),
            "registry_drift": sum(route_drift(worker_rows, routes).values()),
            "malformed_master_requests": int(rejects.get("total") or 0),
            "media_jobs_pending": media["summary"]["media_jobs_pending"],
            "media_jobs_running": media["summary"]["media_jobs_running"],
            "media_jobs_blocked": media["summary"]["media_jobs_blocked"],
            "media_jobs_verified": media["summary"]["media_jobs_verified"],
            "latest_media_archive_age": media["summary"]["latest_media_archive_age"],
        },
    }
    signature = stable_payload_signature(payload)
    payload["refresh"] = {
        "signature": signature,
        "event_id": canonical_event_id(payload),
        "generated_at": time.time(),
        "heartbeat_seconds": 15,
        "reconcile_seconds": 30,
        "stale_after_seconds": 45,
    }
    return payload
