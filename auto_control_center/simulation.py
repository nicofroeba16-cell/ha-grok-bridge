from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .refresh import canonical_event_id, stable_payload_signature
from .security import sanitize
from .state import (
    annotate_checkpoint_semantics_all,
    annotate_registry_aliases,
    apply_activation_provenance,
    evidence_for_worker,
    resolve_worker,
    wake_class,
)

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

REALISTIC_WORKERS = (
    {"project":"Auto Chat","chat":"AUTO - iOS","goal":"native-ios-delivery-master-v1","repository":"nicofroeba16-cell/ha-ios-next-ios","branch":"codex/ci-runtime-fullgate-reconciled"},
    {"project":"Auto Chat","chat":"AUTO - iOS Owner Fix","goal":"native-ios-owner-xcode27-compile-fix-v1","repository":"nicofroeba16-cell/ha-ios-next-ios","branch":"codex/owner-ticket-security"},
    {"project":"Dashboards","chat":"Fire TV Medienkarte erweitern","goal":"firetv-runtime-source-reconcile-v2","repository":"nicofroeba16-cell/AmazonTV-App","branch":"feat/firetv-device-controls-v2"},
    {"project":"AmazonTV-App / Fire TV Companion v2","chat":"Fire TV Medienkarte erweitern","goal":"firetv-runtime-source-reconcile-v2","repository":"nicofroeba16-cell/AmazonTV-App","branch":"feat/firetv-device-controls-v2"},
    {"project":"Brother Printer Companion","chat":"Brother Companion planen","goal":"brother-v0.3-status-normalization-final-v2","repository":"nicofroeba16-cell/Brother-Printer-Companion","branch":"develop/status-normalization"},
    {"project":"Drucker","chat":"Brother Companion planen","goal":"brother-v0.3-status-normalization-final-v2","repository":"nicofroeba16-cell/Brother-Printer-Companion","branch":"develop/status-normalization"},
    {"project":"Health","chat":"Global Project Health Audit","goal":"global-health-revalidation-v2","repository":"nicofroeba16-cell/ha-grok-bridge","branch":"main"},
    {"project":"Health","chat":"Schlüsselinventur planen","goal":"key-secret-inventory-readonly-v2","repository":"nicofroeba16-cell/ha-grok-bridge","branch":"main"},
    {"project":"IOS App","chat":"UI Test Zuverlässigkeit","goal":"native-ios-visual-iphone18-target-v1","repository":"nicofroeba16-cell/ha-ios-next-ios","branch":"codex/visual-acceptance-reliability"},
    {"project":"Auto Chat","chat":"AUTO - iOS Visual Acceptance","goal":"native-ios-visual-iphone18-target-v1","repository":"nicofroeba16-cell/ha-ios-next-ios","branch":"codex/visual-acceptance-reliability"},
    {"project":"Mähroboter","chat":"Status Mähroboter Read only","goal":"mammotion-5004-raw-positioning-v2","repository":"nicofroeba16-cell/ha-grok-bridge","branch":"main"},
    {"project":"HA Simulation","chat":"HA Testumgebung planen","goal":"mammotion-beta11-simulation-refresh-v1","repository":"nicofroeba16-cell/ha-grok-bridge-live","branch":"main"},
    {"project":"Master Chat Bridge","chat":"Master-Verteilung","goal":"master-chat-bridge-e2e-v1","repository":"nicofroeba16-cell/ha-grok-bridge","branch":"fix/worker-orchestrator-runtime-hardening"},
    {"project":"Master Autonomous Orchestration","chat":"Runner Control Plane Cutover","goal":"master-control-plane-cutover-v4","repository":"nicofroeba16-cell/ha-grok-bridge","branch":"main"},
    {"project":"Worker Orchestrator","chat":"Runner Worker Orchestrator","goal":"docs-freshness-v4-final","repository":"nicofroeba16-cell/ha-grok-bridge","branch":"fix/worker-orchestrator-runtime-hardening"},
    {"project":"Worker Orchestrator","chat":"Runner Docs Freshness E2E","goal":"docs-freshness-e2e-v1","repository":"nicofroeba16-cell/ha-grok-bridge","branch":"fix/worker-orchestrator-runtime-hardening"},
    {"project":"Worker Orchestrator","chat":"Runner E2E Smoke","goal":"runtime-e2e-v6","repository":"nicofroeba16-cell/ha-grok-bridge","branch":"fix/worker-orchestrator-runtime-hardening"},
    {"project":"Auto Chat","chat":"AUTO - Control Center","goal":"auto-control-center-final-polish-v5","repository":"nicofroeba16-cell/ha-grok-bridge","branch":"feature/auto-control-center-readonly-v1"},
    {"project":"Master Autonomous Orchestration","chat":"Control Plane E2E","goal":"production-e2e-v1-control-plane-e2e","repository":"nicofroeba16-cell/ha-grok-bridge","branch":"feat/master-autonomous-orchestration","blocker":"Runtime filesystem is read-only outside the assigned workspace, so the gated cutover cannot proceed without explicit approval and a writable runtime path."},
    {"project":"IOS App","chat":"iOS Admin Chat Status","goal":"native-ios-owner-xcode27-compile-fix-v1","repository":"nicofroeba16-cell/ha-ios-next-ios","branch":"codex/owner-ticket-security"},
)


def _iso(minutes_ago: int) -> str:
    stamp = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc) - timedelta(minutes=minutes_ago)
    return stamp.isoformat()


def _worker(index: int, state: str, *, partial: bool = False, stale: bool = False) -> dict[str, Any]:
    fixture = REALISTIC_WORKERS[index % len(REALISTIC_WORKERS)]
    cycle = index // len(REALISTIC_WORKERS)
    repository = fixture["repository"]
    branch = fixture["branch"]
    goal_version = fixture["goal"]
    if cycle:
        suffix = f"-fixture-{cycle}"
        branch = f"{branch}{suffix}"
        goal_version = f"{goal_version}{suffix}"
    blockers = []
    if state == "BLOCKED":
        blockers = [
            fixture.get("blocker")
            or "Current dependency evidence is incomplete; review the source-backed blocker before the next gated step."
        ]
    user_gate = ["Explicit approval is required before the next gated integration step."] if state == "WAITING_FOR_USER" else []
    if state in {"DONE", "READY"}:
        ci_status = "GREEN"
    elif state == "ASSIGNED" and index % 16 == 7:
        ci_status = "FAILURE"
    else:
        ci_status = "UNKNOWN"
    updated_at = "2026-08-01T00:00:00+00:00" if stale and index % 3 == 0 else _iso(index * 7)
    row: dict[str, Any] = {
        "worker_key": f"Projekt: {fixture['project']} → Chat: {fixture['chat']} · fixture-{index:03d}",
        "project": fixture["project"],
        "chat": fixture["chat"],
        "repository": repository,
        "branch": branch,
        "workstream_issue": 100 + index,
        "goal_version": goal_version,
        "source_comment_id": 5000 + index,
        "state": state,
        "last_head": f"{index:040x}"[-40:],
        "ci_status": ci_status,
        "blockers": blockers,
        "user_gate": user_gate,
        "last_progress": "Latest worker report is present; treat it as unverified until source evidence confirms it.",
        "verified_criteria": ["scope", "tests"] if index % 3 else ["scope"],
        "done_criteria": ["scope", "tests", "visual"],
        "updated_at": updated_at,
    }
    if index == 1:
        row["last_progress"] = "Bearer abcdefghijklmnop github_pat_abcdefghijklmnopqrstuv"
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
        "wake_path": {"healthy": not degraded, "components_expected": 3, "components_active": 0 if degraded else 3, "missing_count": 0, "unhealthy_count": 3 if degraded else 0},
        "master_request_health": {"total": 1 if degraded else 0, "recent": [], "schema_ready": True},
        "media_archive": {
            "schema_ready": True,
            "summary": {"media_jobs_pending": 0, "media_jobs_running": 0, "media_jobs_blocked": 0, "media_jobs_verified": 0, "latest_media_archive_age": None},
            "goals": [],
        },
        "route_drift": {"ledger_unrouted": 0, "route_without_ledger": 0, "legacy_unrouted": 0, "superseded": 0},
        "stats": {
            "workers": 0, "running": 0, "blocked": 0, "blocked_stalled": 0,
            "waiting_for_user": 0, "ready_done": 0, "wake_uncertain": 0,
            "activation_unconfirmed": 0, "ci_red": 0, "registry_shared_targets": 0,
            "registry_drift": 0, "malformed_master_requests": 1 if degraded else 0,
            "media_jobs_pending": 0, "media_jobs_running": 0, "media_jobs_blocked": 0,
            "media_jobs_verified": 0, "latest_media_archive_age": None,
        },
        "refresh": {"signature": "empty", "event_id": "empty", "generated_at": 0.0, "heartbeat_seconds": 15, "reconcile_seconds": 30, "stale_after_seconds": 45},
    }


def build_simulation(
    *,
    worker_count: int = 24,
    event_count: int = 120,
    wake_count: int = 72,
    degraded: bool = False,
    partial: bool = False,
    stale: bool = False,
    empty: bool = False,
) -> dict[str, Any]:
    """Build deterministic, sanitized read-only data for UI/stability acceptance."""
    if empty:
        return _empty_payload(degraded=degraded)

    worker_count = max(1, worker_count)
    raw_workers = [
        _worker(i, WORKER_STATES[i % len(WORKER_STATES)], partial=partial, stale=stale)
        for i in range(worker_count)
    ]
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
                "message_id": f"sim:{workers[i % len(workers)]['goal_version']}:{i:04d}",
                "route_key": workers[i % len(workers)]["worker_key"],
                "goal_version": workers[i % len(workers)]["goal_version"],
                "status": WAKE_STATES[i % len(WAKE_STATES)],
                "attempts": 1 + (i % 3),
                "last_error": "authorization=Bearer abcdefghijklmnop" if i == 1 else "",
                "updated_at": float(100000 - i),
            }
            for i in range(max(0, wake_count))
        ]
    )
    if len(workers) > 15 and wakes:
        wakes[0] = {
            "message_id": f"sim:{workers[15]['goal_version']}:unconfirmed",
            "route_key": workers[15]["worker_key"],
            "goal_version": workers[15]["goal_version"],
            "status": "UNCERTAIN",
            "attempts": 1,
            "last_error": "post-send persistence not verified",
            "updated_at": 2000000000.0,
            "status_class": "uncertain",
        }
    for wake in wakes:
        wake["status_class"] = wake_class(wake.get("status"))

    workers = apply_activation_provenance(workers, wakes)
    route_keys = {
        row["worker_key"] for row in workers
        if str(row.get("project") or "") in {"Auto Chat", "Dashboards", "Drucker", "Health", "Mähroboter", "HA Simulation"}
    }
    workers = annotate_checkpoint_semantics_all(annotate_registry_aliases(workers, route_keys))

    child_target = 36 if worker_count >= 80 else 10
    child_count = min(child_target, len(workers))
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
    route_rows = [
        {"worker_key": "__master__", "bound": source_ok, "destination_kind": "url"},
        *[{"worker_key": key, "bound": source_ok, "destination_kind": "url"} for key in sorted(route_keys)],
    ]
    ledger_keys = {row["worker_key"] for row in workers}
    route_only = len(route_keys - ledger_keys)
    drift = {
        "ledger_unrouted": sum(1 for row in workers if not row.get("route_bound")),
        "route_without_ledger": route_only,
        "legacy_unrouted": sum(1 for row in workers if row.get("registry_identity_state") == "LEGACY_UNROUTED"),
        "superseded": sum(1 for row in workers if row.get("registry_identity_state") == "SUPERSEDED"),
    }
    media_goals = [
        {"worker_key": workers[i % len(workers)]["worker_key"], "goal_version": workers[i % len(workers)]["goal_version"], "media_archive_state": state, "expected_count": 4, "uploaded_count": 4 if state in {"VERIFIED", "BLOCKED"} else 2 if state == "RUNNING" else 0, "verified_count": 4 if state == "VERIFIED" else 0, "last_error": "fixture integrity mismatch" if state == "BLOCKED" else "", "evidence_head": f"{900+i:040x}"[-40:], "library_target": f"/Master/Abnahmen/fixture/{state.lower()}"}
        for i, state in enumerate(("PENDING", "RUNNING", "VERIFIED", "BLOCKED"))
    ]
    media = {
        "schema_ready": True,
        "summary": {"media_jobs_pending": 1, "media_jobs_running": 1, "media_jobs_blocked": 1, "media_jobs_verified": 1, "latest_media_archive_age": 42},
        "goals": media_goals,
    }
    payload = {
        "health": health,
        "wake_path": {"healthy": source_ok, "components_expected": 3, "components_active": 3 if source_ok else 0, "missing_count": 0, "unhealthy_count": 0 if source_ok else 3},
        "master_request_health": {"total": 1, "recent": [{"source_comment_id": 4242, "kind": "MASTER_REQUEST", "reason": "invalid WORK_GRAPH_JSON", "created_at": 1789737000.0}], "schema_ready": True},
        "media_archive": media,
        "master": {
            "request_id": "sim-master",
            "version": "current-view-v5",
            "request_text": "Current orchestration state — evidence-first operator control",
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
        "routes": route_rows,
        "route_drift": drift,
        "services": services,
        "evidence": evidence,
        "stats": {
            "workers": len(workers),
            "running": sum(1 for row in workers if row.get("resolved_state") == "RUNNING"),
            "blocked": sum(1 for row in workers if row.get("resolved_state") in {"BLOCKED", "STALLED", "WAITING_FOR_USER", "ERROR"}),
            "blocked_stalled": sum(1 for row in workers if row.get("resolved_state") in {"BLOCKED", "STALLED"}),
            "waiting_for_user": sum(1 for row in workers if row.get("resolved_state") == "WAITING_FOR_USER"),
            "ready_done": sum(1 for row in workers if row.get("resolved_state") in {"READY", "DONE"}),
            "wake_uncertain": sum(1 for row in wakes if row.get("status_class") == "uncertain"),
            "activation_unconfirmed": sum(1 for row in workers if row.get("resolved_state") == "RUNNING" and row.get("activation_confirmed") is False),
            "ci_red": sum(1 for row in evidence if row.get("ci_class") == "red"),
            "registry_shared_targets": len(shared_groups),
            "registry_drift": sum(drift.values()),
            "malformed_master_requests": 1,
            **media["summary"],
        },
    }
    payload["refresh"] = {
        "signature": stable_payload_signature(payload),
        "event_id": canonical_event_id(payload),
        "generated_at": 0.0,
        "heartbeat_seconds": 15,
        "reconcile_seconds": 30,
        "stale_after_seconds": 45,
    }
    return sanitize(payload)


def simulation_matrix() -> dict[str, dict[str, Any]]:
    return {
        "mixed": build_simulation(),
        "partial": build_simulation(worker_count=18, event_count=90, wake_count=54, partial=True),
        "degraded": build_simulation(worker_count=16, event_count=40, wake_count=30, degraded=True),
        "stale": build_simulation(worker_count=16, event_count=48, wake_count=36, stale=True),
        "empty": build_simulation(empty=True),
        "large": build_simulation(worker_count=180, event_count=900, wake_count=500, partial=True, stale=True),
    }
