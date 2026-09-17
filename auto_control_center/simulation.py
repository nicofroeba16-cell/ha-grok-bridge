from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .security import sanitize
from .state import annotate_registry_aliases, evidence_for_worker, resolve_worker, wake_class

WORKER_STATES = (
    "RUNNING",
    "READY",
    "BLOCKED",
    "WAITING_FOR_USER",
    "DONE",
    "DORMANT",
    "ERROR",
    "ASSIGNED",
)
WAKE_STATES = (
    "UNCERTAIN",
    "FAILED_PRE_SEND",
    "CANCELLED_SUPERSEDED",
    "DELIVERED",
    "BLOCKED",
    "SUPERSEDED",
)


def _iso(minutes_ago: int) -> str:
    stamp = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc) - timedelta(minutes=minutes_ago)
    return stamp.isoformat()


def _worker(index: int, state: str, *, partial: bool = False) -> dict[str, Any]:
    shared_pair = index in {2, 3, 8, 9}
    pair_root = 2 if index in {2, 3} else 8 if index in {8, 9} else index
    repository = f"example/control-{pair_root % 6}"
    branch = f"feature/sim-{pair_root % 5}"
    goal_version = f"sim-goal-{pair_root % 7}-v1"
    blockers = ["Dependency evidence is still missing"] if state == "BLOCKED" else []
    user_gate = ["Explicit user approval required"] if state == "WAITING_FOR_USER" else []
    if state in {"DONE", "READY"}:
        ci_status = "GREEN"
    elif state == "ASSIGNED" and index % 16 == 7:
        ci_status = "FAILURE"
    else:
        ci_status = "UNKNOWN"
    row: dict[str, Any] = {
        "worker_key": f"Projekt: Simulation → Chat: Worker {index:03d}",
        "project": "Simulation",
        "chat": f"Worker {index:03d}",
        "repository": repository,
        "branch": branch,
        "workstream_issue": 100 + index,
        "goal_version": goal_version,
        "state": state,
        "last_head": f"{index:040x}"[-40:],
        "ci_status": ci_status,
        "blockers": blockers,
        "user_gate": user_gate,
        "last_progress": "Representative worker report",
        "verified_criteria": ["scope", "tests"] if index % 3 else ["scope"],
        "done_criteria": ["scope", "tests", "visual"],
        "updated_at": _iso(index * 7),
    }
    if index == 1:
        row["last_progress"] = "Bearer abcdefghijklmnop github_pat_abcdefghijklmnopqrstuv"
    if shared_pair:
        row["project"] = "Legacy Simulation" if index % 2 else "Simulation"
    if partial:
        if index % 4 == 0:
            row["last_head"] = None
            row["ci_status"] = None
        if index % 5 == 0:
            row["branch"] = None
        if index % 6 == 0:
            row["last_progress"] = ""
    return row


def _empty_payload(*, degraded: bool = False) -> dict[str, Any]:
    source = {"present": not degraded, "readable": not degraded}
    return {
        "health": {
            "orchestrator_db": dict(source),
            "browser_wake_db": dict(source),
            "browser_routes": dict(source),
            "generated_at": 0.0,
        },
        "master": None,
        "workers": [],
        "events": [],
        "wakes": [],
        "wake_queues": {"workers": 0, "master": 0},
        "routes": [],
        "services": [],
        "evidence": [],
        "stats": {
            "workers": 0,
            "running": 0,
            "blocked": 0,
            "wake_uncertain": 0,
            "ci_red": 0,
            "registry_shared_targets": 0,
        },
    }


def build_simulation(
    *,
    worker_count: int = 24,
    event_count: int = 120,
    wake_count: int = 72,
    degraded: bool = False,
    partial: bool = False,
    empty: bool = False,
) -> dict[str, Any]:
    """Build deterministic, sanitized read-only data for UI/stability acceptance."""
    if empty:
        return _empty_payload(degraded=degraded)

    worker_count = max(1, worker_count)
    raw_workers = [_worker(i, WORKER_STATES[i % len(WORKER_STATES)], partial=partial) for i in range(worker_count)]
    workers = annotate_registry_aliases([resolve_worker(sanitize(row)) for row in raw_workers])

    events = sanitize(
        [
            {
                "id": event_count - i,
                "worker_key": workers[i % len(workers)]["worker_key"],
                "goal_version": workers[i % len(workers)].get("goal_version"),
                "event_type": ("STATE_CHANGED", "PROGRESS", "EVIDENCE", "WAKE")[i % 4],
                "payload": {"note": "github_pat_abcdefghijklmnopqrstuv" if i == 0 else f"event-{i}"},
                "created_at": _iso(i),
            }
            for i in range(max(0, event_count))
        ]
    )

    wakes = sanitize(
        [
            {
                "message_id": f"sim-{i:04d}",
                "route_key": workers[i % len(workers)]["worker_key"],
                "status": WAKE_STATES[i % len(WAKE_STATES)],
                "attempts": 1 + (i % 3),
                "last_error": "authorization=Bearer abcdefghijklmnop" if i == 1 else "",
                "updated_at": float(100000 - i),
            }
            for i in range(max(0, wake_count))
        ]
    )
    for wake in wakes:
        wake["status_class"] = wake_class(wake.get("status"))

    child_count = min(10, len(workers))
    children = []
    for i in range(child_count):
        children.append(
            {
                "child_id": f"child-{i + 1}",
                "worker_key": workers[i]["worker_key"],
                "dependencies": [f"child-{i}"] if i else [],
                "state": workers[i].get("resolved_state") or "UNKNOWN",
                "blocker": (workers[i].get("blockers") or [""])[0] if workers[i].get("blockers") else "",
                "dispatched": 1,
            }
        )

    evidence = [evidence_for_worker(row) for row in workers]
    shared_groups = {
        (row.get("repository"), row.get("branch"), row.get("goal_version"))
        for row in workers
        if row.get("registry_shared_target")
    }
    source_ok = not degraded
    health = {
        "orchestrator_db": {"present": source_ok, "readable": source_ok},
        "browser_wake_db": {"present": source_ok, "readable": source_ok},
        "browser_routes": {"present": source_ok, "readable": source_ok},
        "generated_at": 0.0,
    }
    services = [
        {
            "name": name,
            "active": "active" if source_ok else "unavailable",
            "sub": "running" if source_ok else "unknown",
            "result": "success" if source_ok else "unknown",
        }
        for name in ("worker-orchestrator.service", "browser-wake.service", "browser-wake-chrome.service")
    ]
    payload = {
        "health": health,
        "master": {
            "request_id": "sim-master",
            "version": "simulation-v3",
            "request_text": "Stability simulation — evidence-first Control Center",
            "done_criteria": ["state churn", "large data", "visual acceptance"],
            "state": "RUNNING" if source_ok else "BLOCKED",
            "updated_at": _iso(0),
            "children": children,
            "progress": {"done": 1, "total": 3, "percent": 33},
        },
        "workers": workers,
        "events": events,
        "wakes": wakes,
        "wake_queues": {"workers": 3 if not degraded else 7, "master": 1},
        "routes": [
            {"worker_key": "__master__", "bound": source_ok, "destination_kind": "url"},
            *[
                {"worker_key": row["worker_key"], "bound": source_ok, "destination_kind": "url"}
                for row in workers[: min(12, len(workers))]
            ],
        ],
        "services": services,
        "evidence": evidence,
        "stats": {
            "workers": len(workers),
            "running": sum(1 for row in workers if row.get("resolved_state") == "RUNNING"),
            "blocked": sum(
                1
                for row in workers
                if row.get("resolved_state") in {"BLOCKED", "STALLED", "WAITING_FOR_USER", "ERROR"}
            ),
            "wake_uncertain": sum(1 for row in wakes if row.get("status_class") == "uncertain"),
            "ci_red": sum(1 for row in evidence if row.get("ci_class") == "red"),
            "registry_shared_targets": len(shared_groups),
        },
    }
    return sanitize(payload)


def simulation_matrix() -> dict[str, dict[str, Any]]:
    return {
        "mixed": build_simulation(),
        "partial": build_simulation(worker_count=18, event_count=90, wake_count=54, partial=True),
        "degraded": build_simulation(worker_count=16, event_count=40, wake_count=30, degraded=True),
        "empty": build_simulation(empty=True),
        "large": build_simulation(worker_count=180, event_count=900, wake_count=500, partial=True),
    }
