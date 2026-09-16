from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json
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

    def error_signature(self) -> str:
        if not self.error:
            return ""
        return sha256(self.error.strip().encode()).hexdigest()[:16]
