from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable, Mapping

from .goals import parse_goals
from .models import LifecycleState

STATUS_RESUME_POLICY_ID = "auto-chat-status-report-and-resume-v1"
STATUS_CHECKPOINT_TOKENS = frozenset({"status", "status?", "stand", "stand?"})
TERMINAL_GATES = frozenset({
    LifecycleState.WAITING_FOR_USER,
    LifecycleState.BLOCKED,
})
TERMINAL_DORMANT = frozenset({
    LifecycleState.DONE,
    LifecycleState.READY,
    LifecycleState.STALLED,
})


def policy_reference_lines() -> tuple[str, ...]:
    return (
        f"AUTO_POLICY_ID: {STATUS_RESUME_POLICY_ID}",
        "STATUS_CHECKPOINT_RULE: A status-only user message is a non-stopping checkpoint.",
        "STATUS_CHECKPOINT_ACTION: Report concise current status, then continue the already-authorized current goal without another Go.",
        "STATUS_CURRENT_GOAL_RULE: Resolve the newest canonical current goal before older worker terminal status.",
        "STATUS_DUPLICATE_RULE: Do not duplicate an already-active equivalent CI/run/restart/activation.",
        "STATUS_GATE_RULE: WAITING_FOR_USER, BLOCKED, live, merge, release, device, network and secret gates remain gates.",
        "STATUS_DORMANT_RULE: If no current goal exists, remain dormant and invent no work.",
        "STATUS_LOOP_GUARD: Status reporting must not create wake/status echoes.",
    )


def policy_sync_payload(worker_key: str) -> str:
    return "\n".join((
        "AUTO_POLICY_SYNC",
        f"POLICY_ID: {STATUS_RESUME_POLICY_ID}",
        f"CANONICAL_WORKER: {worker_key}",
        *policy_reference_lines()[1:],
        "ACTION: Apply this shared policy to future status-only checkpoints and continue only the current authorized scope.",
    ))


def policy_sync_message_id(worker_key: str) -> str:
    token = sha256(worker_key.encode()).hexdigest()[:20]
    return f"auto-policy-sync:{STATUS_RESUME_POLICY_ID}:{token}"


def is_status_checkpoint(text: str) -> bool:
    return str(text).strip().lower() in STATUS_CHECKPOINT_TOKENS


@dataclass(frozen=True, slots=True)
class StatusResolution:
    worker_key: str
    goal_version: str
    state: str
    action: str
    resume: bool
    duplicate_activation: bool
    activation_source: str
    activation_confirmed: bool
    source_comment_id: int | None
    reason: str


def _row_value(row: Mapping | None, key: str, default=""):
    if row is None:
        return default
    keys = row.keys() if hasattr(row, "keys") else ()
    return row[key] if key in keys else default


def _source_id(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def resolve_status_checkpoint(
    items: Iterable[dict],
    worker_key: str,
    *,
    worker_row: Mapping | None = None,
    wake_state: Mapping[str, object] | None = None,
    active_equivalent: bool = False,
) -> StatusResolution:
    goals = [goal for goal in parse_goals(items) if goal.key == worker_key]
    current = max(
        goals,
        key=lambda goal: _source_id(goal.source_comment_id),
        default=None,
    )
    row_version = str(_row_value(worker_row, "goal_version", ""))
    row_source = _source_id(_row_value(worker_row, "source_comment_id", -1))
    current_source = _source_id(current.source_comment_id) if current else -1

    if current is None and worker_row is None:
        return StatusResolution(
            worker_key, "", "IDLE", "DORMANT", False, False, "", False, None,
            "no current canonical goal",
        )

    if current is not None and current_source > row_source:
        goal_version = current.version
        source_comment_id = current.source_comment_id
        base_state = LifecycleState.ASSIGNED
        row_is_current = False
    else:
        goal_version = current.version if current is not None else row_version
        source_comment_id = (
            current.source_comment_id if current is not None
            else _row_value(worker_row, "source_comment_id", None)
        )
        base_state = str(_row_value(worker_row, "state", LifecycleState.ASSIGNED))
        row_is_current = bool(worker_row is not None and row_version == goal_version)

    wake_goal = str((wake_state or {}).get("goal_version", ""))
    wake_status = str((wake_state or {}).get("status", "")).upper()
    wake_source = str((wake_state or {}).get("activation_source", ""))
    wake_matches = bool(wake_state and wake_goal == goal_version)

    if row_is_current and base_state in TERMINAL_GATES:
        return StatusResolution(
            worker_key, goal_version, base_state, "REPORT_GATE", False, False,
            "", False, source_comment_id, "current goal is gated terminal state",
        )
    if row_is_current and base_state in TERMINAL_DORMANT:
        return StatusResolution(
            worker_key, goal_version, base_state, "REPORT_DORMANT", False, False,
            "", False, source_comment_id, "current goal is terminal/dormant",
        )

    if active_equivalent or (row_is_current and base_state == LifecycleState.RUNNING):
        source = str((wake_state or {}).get("activation_source", "worker_report"))
        confirmed = bool((wake_state or {}).get("activation_confirmed", True))
        return StatusResolution(
            worker_key, goal_version, LifecycleState.RUNNING, "REPORT_AND_CONTINUE",
            True, True, source, confirmed, source_comment_id,
            "equivalent work already active; continue without duplicate activation",
        )

    if wake_matches and wake_status == "UNCERTAIN":
        source = wake_source or "wake_uncertain"
        return StatusResolution(
            worker_key, goal_version, LifecycleState.RUNNING, "REPORT_AND_RESUME",
            True, False, source, False, source_comment_id,
            "newest goal has post-send uncertain wake; operationally running",
        )
    if wake_matches and wake_status in {"DELIVERED", "VERIFIED"}:
        source = wake_source or "wake_verified"
        return StatusResolution(
            worker_key, goal_version, LifecycleState.RUNNING, "REPORT_AND_RESUME",
            True, False, source, True, source_comment_id,
            "newest goal has positively verified wake",
        )

    return StatusResolution(
        worker_key, goal_version, LifecycleState.ASSIGNED, "REPORT_AND_RESUME",
        True, False, "", False, source_comment_id,
        "newest canonical goal is resumable and not terminal",
    )
