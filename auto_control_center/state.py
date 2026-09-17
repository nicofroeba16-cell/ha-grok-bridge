from __future__ import annotations

from collections import defaultdict
from typing import Any

CI_FAILURE = {"RED", "FAILED", "FAILURE", "ERROR", "CANCELLED", "TIMED_OUT"}
CI_RUNNING = {"RUNNING", "PENDING", "QUEUED", "IN_PROGRESS"}
CI_SUCCESS = {"GREEN", "SUCCESS", "PASSED", "PASS"}

WAKE_SUCCESS = {"DELIVERED", "SUCCESS", "SENT", "ACKNOWLEDGED"}
WAKE_FAILURE = {"FAILED", "FAILURE", "FAILED_PRE_SEND", "ERROR", "BLOCKED"}
WAKE_UNCERTAIN = {"UNCERTAIN"}
WAKE_CANCELLED = {"CANCELLED", "CANCELLED_SUPERSEDED", "SUPERSEDED"}


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
    """Classify delivery status without collapsing UNCERTAIN into success or failure."""
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
    """Resolve display state from current ledger fields, never from prose progress reports."""
    result = dict(worker)
    stored = str(worker.get("state") or "UNKNOWN").strip().upper()
    reasons: list[str] = []

    if _nonempty(worker.get("user_gate")):
        resolved = "WAITING_FOR_USER"
        reasons.append("user_gate")
    elif _nonempty(worker.get("blockers")):
        resolved = "BLOCKED"
        reasons.append("blockers")
    elif ci_class(worker.get("ci_status")) == "red":
        resolved = "BLOCKED"
        reasons.append("ci_failure")
    else:
        resolved = stored
        reasons.append("ledger_state")

    result["resolved_state"] = resolved
    result["resolution_basis"] = reasons
    result["state_source"] = "orchestrator_ledger"
    return result


def _shared_target_key(worker: dict[str, Any]) -> tuple[str, str, str] | None:
    key = tuple(str(worker.get(field) or "").strip() for field in ("repository", "branch", "goal_version"))
    return key if all(key) else None


def annotate_registry_aliases(workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark shared repo/branch/goal targets while deliberately inferring no canonical identity."""
    rows = [dict(worker) for worker in workers]
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

    for group in groups.values():
        if len(group) < 2:
            continue
        keys = sorted(str(row.get("worker_key") or "") for row in group if row.get("worker_key"))
        for row in group:
            worker_key = str(row.get("worker_key") or "")
            row["registry_shared_target"] = True
            row["registry_identity_count"] = len(group)
            row["registry_peer_keys"] = [key for key in keys if key != worker_key]
    return rows


def evidence_for_worker(worker: dict[str, Any]) -> dict[str, Any]:
    verified = worker.get("verified_criteria")
    done = worker.get("done_criteria")
    verified_count = len(verified) if isinstance(verified, list) else 0
    done_count = len(done) if isinstance(done, list) else 0
    return {
        "worker_key": worker.get("worker_key"),
        "chat": worker.get("chat"),
        "head": worker.get("last_head"),
        "ci_status": worker.get("ci_status") or "UNKNOWN",
        "ci_class": ci_class(worker.get("ci_status")),
        "verified_criteria": verified_count,
        "done_criteria": done_count,
        "resolved_state": worker.get("resolved_state") or worker.get("state") or "UNKNOWN",
        "registry_shared_target": bool(worker.get("registry_shared_target")),
        "registry_identity_count": int(worker.get("registry_identity_count") or 1),
        "source": "orchestrator_ledger",
    }
