from __future__ import annotations

from typing import Any

CI_FAILURE = {"RED", "FAILED", "FAILURE", "ERROR", "CANCELLED", "TIMED_OUT"}
CI_RUNNING = {"RUNNING", "PENDING", "QUEUED", "IN_PROGRESS"}
CI_SUCCESS = {"GREEN", "SUCCESS", "PASSED", "PASS"}


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
        "source": "orchestrator_ledger",
    }
