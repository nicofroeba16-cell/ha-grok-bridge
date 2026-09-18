from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

CI_FAILURE = {"RED", "FAILED", "FAILURE", "ERROR", "CANCELLED", "TIMED_OUT"}
CI_RUNNING = {"RUNNING", "PENDING", "QUEUED", "IN_PROGRESS"}
CI_SUCCESS = {"GREEN", "SUCCESS", "PASSED", "PASS"}

WAKE_SUCCESS = {"DELIVERED", "VERIFIED", "SUCCESS", "SENT", "ACKNOWLEDGED"}
WAKE_FAILURE = {"FAILED", "FAILURE", "FAILED_PRE_SEND", "ERROR", "BLOCKED"}
WAKE_UNCERTAIN = {"UNCERTAIN"}
WAKE_CANCELLED = {"CANCELLED", "CANCELLED_SUPERSEDED", "SUPERSEDED"}
TERMINAL_WORKER_STATES = {"DONE", "READY", "WAITING_FOR_USER", "BLOCKED", "STALLED", "ERROR"}
STATUS_RESUME_POLICY_ID = "auto-chat-status-report-and-resume-v1"


def _nonempty(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and value.strip() not in {"[]", "{}", "null", "None"}
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def ci_class(status: Any) -> str:
    normalized = str(status or "UNKNOWN").strip().upper()
    if normalized in CI_SUCCESS:
        return "green"
    if normalized in CI_FAILURE:
        return "red"
    if normalized in CI_RUNNING:
        return "running"
    return "unknown"


def wake_class(status: Any) -> str:
    """Classify delivery status without pretending UNCERTAIN is verified success."""
    normalized = str(status or "UNKNOWN").strip().upper()
    if normalized in WAKE_SUCCESS:
        return "green"
    if normalized in WAKE_FAILURE:
        return "red"
    if normalized in WAKE_UNCERTAIN:
        return "uncertain"
    if normalized in WAKE_CANCELLED:
        return "muted"
    return "unknown"


def resolve_worker(worker: dict[str, Any]) -> dict[str, Any]:
    """Resolve display state from canonical ledger fields, never prose progress."""
    result = dict(worker)
    stored = str(worker.get("state") or "UNKNOWN").strip().upper()
    if _nonempty(worker.get("user_gate")):
        resolved, reasons = "WAITING_FOR_USER", ["user_gate"]
    elif _nonempty(worker.get("blockers")):
        resolved, reasons = "BLOCKED", ["blockers"]
    elif ci_class(worker.get("ci_status")) == "red":
        resolved, reasons = "BLOCKED", ["ci_failure"]
    else:
        resolved, reasons = stored, ["ledger_state"]
    result["resolved_state"] = resolved
    result["resolution_basis"] = reasons
    result["state_source"] = "orchestrator_ledger"
    return result


def _shared_target_key(worker: dict[str, Any]) -> tuple[str, str, str] | None:
    key = tuple(str(worker.get(field) or "").strip() for field in ("repository", "branch", "goal_version"))
    return key if all(key) else None


def _source_comment_id(worker: dict[str, Any]) -> int:
    try:
        return int(worker.get("source_comment_id") or -1)
    except (TypeError, ValueError):
        return -1


def annotate_registry_aliases(
    workers: list[dict[str, Any]],
    route_keys: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Expose route/ledger drift without collapsing legacy or superseded identities."""
    rows = [dict(worker) for worker in workers]
    routes = {str(key) for key in route_keys}
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = _shared_target_key(row)
        if key is not None:
            groups[key].append(row)

    for row in rows:
        row["registry_shared_target"] = False
        row["registry_identity_count"] = 1
        row["registry_peer_keys"] = []
        row["registry_identity_basis"] = "repository+branch+goal_version"
        row["route_bound"] = str(row.get("worker_key") or "") in routes
        row["registry_identity_state"] = "ACTIVE" if row["route_bound"] else "UNROUTED"

    for group in groups.values():
        if len(group) < 2:
            continue
        keys = sorted(str(row.get("worker_key") or "") for row in group if row.get("worker_key"))
        max_source = max((_source_comment_id(row) for row in group), default=-1)
        any_routed = any(bool(row.get("route_bound")) for row in group)
        for row in group:
            worker_key = str(row.get("worker_key") or "")
            row["registry_shared_target"] = True
            row["registry_identity_count"] = len(group)
            row["registry_peer_keys"] = [key for key in keys if key != worker_key]
            if _source_comment_id(row) < max_source:
                row["registry_identity_state"] = "SUPERSEDED"
            elif row["route_bound"]:
                row["registry_identity_state"] = "ACTIVE"
            elif any_routed:
                row["registry_identity_state"] = "LEGACY_UNROUTED"
            else:
                row["registry_identity_state"] = "LEGACY_UNROUTED"
    return rows


def _timestamp(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _bool_or_none(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def goal_from_wake(row: dict[str, Any]) -> str:
    explicit = str(row.get("goal_version") or "").strip()
    if explicit:
        return explicit
    message_id = str(row.get("message_id") or "")
    parts = message_id.split(":")
    if len(parts) >= 4 and parts[0] == "worker-wake":
        return parts[-2]
    return ""


def apply_activation_provenance(
    workers: list[dict[str, Any]], wake_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Overlay current-goal Browser-Wake evidence without regressing terminal state."""
    wakes_by_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for wake in wake_rows:
        key = str(wake.get("route_key") or "")
        if key:
            wakes_by_route[key].append(wake)
    for rows in wakes_by_route.values():
        rows.sort(key=lambda item: _timestamp(item.get("updated_at")), reverse=True)

    result: list[dict[str, Any]] = []
    for source in workers:
        row = dict(source)
        resolved = str(row.get("resolved_state") or row.get("state") or "UNKNOWN").upper()
        goal_version = str(row.get("goal_version") or "").strip()
        canonical_source = str(row.get("activation_source") or "").strip()
        canonical_confirmed = _bool_or_none(row.get("activation_confirmed"))

        if canonical_source:
            row["activation_source"] = canonical_source
            row["activation_confirmed"] = canonical_confirmed
            row["activation_updated_at"] = row.get("activation_updated_at") or row.get("updated_at")
            row["activation_label"] = (
                "activation_unconfirmed"
                if resolved == "RUNNING" and canonical_source == "wake_uncertain"
                else "activation_confirmed"
                if resolved == "RUNNING" and canonical_confirmed is True
                else None
            )
            result.append(row)
            continue

        if resolved in TERMINAL_WORKER_STATES:
            row.update(activation_source=None, activation_confirmed=None, activation_updated_at=None, activation_label=None)
            result.append(row)
            continue

        if resolved == "RUNNING":
            row.update(
                activation_source="worker_report",
                activation_confirmed=True,
                activation_updated_at=row.get("updated_at"),
                activation_label="activation_confirmed",
            )
            result.append(row)
            continue

        candidates = wakes_by_route.get(str(row.get("worker_key") or ""), [])
        wake = candidates[0] if candidates else None
        if wake is None or not goal_version or goal_from_wake(wake) != goal_version:
            row.update(activation_source=None, activation_confirmed=None, activation_updated_at=None, activation_label=None)
            result.append(row)
            continue

        status = str(wake.get("status") or "UNKNOWN").upper()
        if status in WAKE_UNCERTAIN:
            row["resolved_state"] = "RUNNING"
            row["activation_source"] = "wake_uncertain"
            row["activation_confirmed"] = False
            row["activation_updated_at"] = wake.get("updated_at")
            row["activation_label"] = "activation_unconfirmed"
            row["resolution_basis"] = list(row.get("resolution_basis") or []) + ["wake_uncertain_current_goal"]
            row["state_source"] = "browser_wake_read_model"
        elif status in WAKE_SUCCESS:
            row["resolved_state"] = "RUNNING"
            row["activation_source"] = "wake_verified"
            row["activation_confirmed"] = True
            row["activation_updated_at"] = wake.get("updated_at")
            row["activation_label"] = "activation_confirmed"
            row["resolution_basis"] = list(row.get("resolution_basis") or []) + ["wake_verified_current_goal"]
            row["state_source"] = "browser_wake_read_model"
        else:
            row.update(activation_source=None, activation_confirmed=None, activation_updated_at=None, activation_label=None)
        result.append(row)
    return result


def annotate_checkpoint_semantics(worker: dict[str, Any]) -> dict[str, Any]:
    row = dict(worker)
    state = str(row.get("resolved_state") or row.get("state") or "UNKNOWN").upper()
    source = str(row.get("activation_source") or "")
    if state in {"WAITING_FOR_USER", "BLOCKED"}:
        action, resume, duplicate = "REPORT_GATE", False, False
    elif state in {"DONE", "READY", "STALLED", "ERROR"}:
        action, resume, duplicate = "REPORT_DORMANT", False, False
    elif state == "RUNNING" and source in {"wake_uncertain", "wake_verified"}:
        action, resume, duplicate = "REPORT_AND_RESUME", True, False
    elif state == "RUNNING":
        action, resume, duplicate = "REPORT_AND_CONTINUE", True, True
    else:
        action, resume, duplicate = "REPORT_AND_RESUME", True, False
    row["checkpoint_policy_id"] = STATUS_RESUME_POLICY_ID
    row["checkpoint_action"] = action
    row["checkpoint_resume"] = resume
    row["checkpoint_duplicate_activation"] = duplicate
    return row


def annotate_checkpoint_semantics_all(workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [annotate_checkpoint_semantics(worker) for worker in workers]


def evidence_for_worker(worker: dict[str, Any]) -> dict[str, Any]:
    verified = worker.get("verified_criteria")
    done = worker.get("done_criteria")
    return {
        "worker_key": worker.get("worker_key"),
        "chat": worker.get("chat"),
        "head": worker.get("last_head"),
        "ci_status": worker.get("ci_status") or "UNKNOWN",
        "ci_class": ci_class(worker.get("ci_status")),
        "verified_criteria": len(verified) if isinstance(verified, list) else 0,
        "done_criteria": len(done) if isinstance(done, list) else 0,
        "resolved_state": worker.get("resolved_state") or worker.get("state") or "UNKNOWN",
        "activation_source": worker.get("activation_source"),
        "activation_confirmed": worker.get("activation_confirmed"),
        "checkpoint_action": worker.get("checkpoint_action"),
        "checkpoint_resume": worker.get("checkpoint_resume"),
        "registry_shared_target": bool(worker.get("registry_shared_target")),
        "registry_identity_count": int(worker.get("registry_identity_count") or 1),
        "registry_identity_state": worker.get("registry_identity_state"),
        "route_bound": bool(worker.get("route_bound")),
        "source": worker.get("state_source") or "orchestrator_ledger",
    }
