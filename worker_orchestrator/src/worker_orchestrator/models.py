from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json
from collections.abc import Mapping
from typing import Any


class LifecycleState(StrEnum):
    IDLE = "IDLE"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    READY = "READY"
    DONE = "DONE"
    STALLED = "STALLED"


GATED_ACTIONS = frozenset({
    "merge", "deploy", "restart", "live_mutation", "device_action",
    "network_mutation", "runner_mutation", "runtime_mutation",
    "secret_change", "key_change", "destructive_action", "release",
    "tag_mutation",
})

DEFAULT_ALLOWED_REPOSITORIES = frozenset({
    "nicofroeba16-cell/ha-grok-bridge", "nicofroeba16-cell/HA-CONFIG",
    "nicofroeba16-cell/ha-grok-bridge-live", "nicofroeba16-cell/File-Bridge-mcp",
    "nicofroeba16-cell/ha-ios-next", "nicofroeba16-cell/ha-ios-next-ios",
    "nicofroeba16-cell/Intelligence-Suite-", "nicofroeba16-cell/AmazonTV-App",
    "nicofroeba16-cell/Brother-Printer-Companion",
})


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class Goal:
    project: str
    chat: str
    repository: str
    branch: str
    prompt: str
    done_criteria: tuple[str, ...]
    workstream_issue: int | None = None
    explicit_version: str | None = None
    files: tuple[str, ...] = ()
    scope: str = ""
    approved_actions: tuple[str, ...] = ()
    source_comment_id: int | None = None

    @property
    def key(self) -> str:
        return f"Projekt: {self.project} → Chat: {self.chat}"

    @property
    def hash(self) -> str:
        payload = {
            "project": self.project.strip(),
            "chat": self.chat.strip(),
            "repository": self.repository.strip(),
            "branch": self.branch.strip(),
            "prompt": self.prompt.strip(),
            "done_criteria": [x.strip() for x in self.done_criteria],
            "workstream_issue": self.workstream_issue,
            "files": sorted(x.strip() for x in self.files),
            "scope": self.scope.strip(),
            "approved_actions": sorted(x.strip() for x in self.approved_actions),
        }
        return sha256(_canon(payload).encode()).hexdigest()

    @property
    def version(self) -> str:
        return self.explicit_version or self.hash[:12]


@dataclass(slots=True)
class WorkerResult:
    head: str = ""
    ci: str = "UNKNOWN"
    verified_criteria: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    requested_actions: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)
    progress: str = ""
    next_step: str = ""
    ready: bool = False
    error: str = ""
    changed_files: tuple[str, ...] = ()
    session_state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def normalize(cls, raw: Any) -> "WorkerResult":
        return normalize_worker_result(raw)

    def error_signature(self) -> str:
        if not self.error:
            return ""
        return sha256(self.error.strip().encode()).hexdigest()[:16]


_RESULT_FIELDS = frozenset(WorkerResult.__dataclass_fields__)
_RESULT_COLLECTIONS = ("verified_criteria", "blockers", "requested_actions", "changed_files")
_RESULT_STRINGS = ("head", "ci", "progress", "next_step", "error")


def _result_strings(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (list, tuple)):
        if not all(isinstance(item, str) for item in value):
            raise TypeError("collection contains a non-string")
        return tuple(value)
    raise TypeError("collection is not a string or list")


def _result_evidence(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, Mapping):
        return {str(key): value[key] for key in value}
    if isinstance(value, (list, tuple, str)):
        return {"details": value}
    raise TypeError("evidence is not a mapping, list, string, or null")


def _result_bool(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "yes", "1"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "no", "0"}:
        return False
    raise TypeError("ready is not a recognized boolean")


def normalize_worker_result(raw: Any) -> WorkerResult:
    """Convert an untrusted worker response into the canonical result contract."""
    try:
        if isinstance(raw, WorkerResult):
            source = {field: getattr(raw, field) for field in _RESULT_FIELDS}
        elif isinstance(raw, Mapping):
            source = {key: raw[key] for key in raw if key in _RESULT_FIELDS}
        else:
            raise TypeError("worker result is not a mapping")
        values: dict[str, Any] = {}
        for field in _RESULT_STRINGS:
            value = source.get(field, "UNKNOWN" if field == "ci" else "")
            if value is None:
                value = "UNKNOWN" if field == "ci" else ""
            if not isinstance(value, str):
                raise TypeError(f"{field} is not a string")
            values[field] = value
        for field in _RESULT_COLLECTIONS:
            values[field] = _result_strings(source.get(field))
        values["evidence"] = _result_evidence(source.get("evidence"))
        values["ready"] = _result_bool(source.get("ready"))
        session = source.get("session_state")
        values["session_state"] = (
            {str(key): session[key] for key in session} if isinstance(session, Mapping) else {}
        )
        return WorkerResult(**values)
    except Exception as exc:
        reason = str(exc)[:240] or "un-normalizable worker result"
        return WorkerResult(
            error="WORKER_RESULT_INVALID",
            blockers=("WORKER_RESULT_INVALID",),
            evidence={"reason": reason},
        )
