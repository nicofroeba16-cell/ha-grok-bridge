from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Protocol

from .models import Goal, WorkerResult
from .security import sanitize


class WorkerAdapter(Protocol):
    def execute(self, goal: Goal, previous: dict, *, dry_run: bool) -> WorkerResult: ...


class CommandWorkerAdapter:
    """Programmatic worker adapter. Browser/UI automation is intentionally unsupported."""

    def __init__(self, command: str, *, env_allowlist: tuple[str, ...] = ()):
        if not command.strip():
            raise ValueError("worker command must not be empty")
        self.argv = shlex.split(command)
        self.env_allowlist = env_allowlist

    def _env(self) -> dict[str, str]:
        base_names = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")
        names = set(base_names).union(self.env_allowlist)
        return {name: os.environ[name] for name in names if name in os.environ}

    def execute(self, goal: Goal, previous: dict, *, dry_run: bool) -> WorkerResult:
        payload = {
            "goal": {
                "project": goal.project,
                "chat": goal.chat,
                "repository": goal.repository,
                "branch": goal.branch,
                "goal_version": goal.version,
                "prompt": goal.prompt,
                "done_criteria": list(goal.done_criteria),
                "workstream_issue": goal.workstream_issue,
                "files": list(goal.files),
                "scope": goal.scope,
                "approved_actions": list(goal.approved_actions),
            },
            "previous": previous,
            "dry_run": dry_run,
            "gated_action_contract": (
                "Only actions explicitly listed in goal.approved_actions may be performed; "
                "otherwise return requested_actions without executing them."
            ),
        }
        proc = subprocess.run(
            self.argv,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=1800,
            check=False,
            env=self._env(),
        )
        if proc.returncode != 0:
            return WorkerResult(error=f"worker command failed with exit code {proc.returncode}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return WorkerResult(error="worker command returned invalid JSON")
        data = sanitize(data)
        allowed = set(WorkerResult.__dataclass_fields__)
        clean = {k: v for k, v in data.items() if k in allowed}
        for field in ("verified_criteria", "blockers", "requested_actions", "changed_files"):
            if field in clean:
                clean[field] = tuple(clean[field])
        return WorkerResult(**clean)
