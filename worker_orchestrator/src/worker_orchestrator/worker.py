from __future__ import annotations

import json
import os
import shlex
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from .models import Goal, WorkerResult
from .security import sanitize


class WorkerAdapter(Protocol):
    def execute(self, goal: Goal, previous: dict, *, dry_run: bool) -> WorkerResult: ...


class NoExecutionWorkerAdapter:
    """Fail closed if execution is accidentally attempted in dispatch-only mode."""

    def execute(self, goal: Goal, previous: dict, *, dry_run: bool) -> WorkerResult:
        return WorkerResult(
            error="WORKER_EXECUTOR_DISABLED",
            blockers=("WORKER_EXECUTOR_DISABLED",),
            progress="Local worker execution is disabled.",
            next_step="Await external workstream execution.",
        )


class CommandWorkerAdapter:
    """Programmatic worker adapter. Browser/UI automation is intentionally unsupported."""

    def __init__(self, command: str, *, env_allowlist: tuple[str, ...] = (), workspace_root: str | Path | None = None):
        if not command.strip():
            raise ValueError("worker command must not be empty")
        self.argv = shlex.split(command)
        self.env_allowlist = env_allowlist
        self.workspace_root = Path(workspace_root).expanduser() if workspace_root else None

    @staticmethod
    def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, timeout=120, check=False)

    def _workspace(self, goal: Goal) -> tuple[Path | None, str]:
        if self.workspace_root is None:
            return None, ""
        slug = sha256(goal.key.encode()).hexdigest()[:16]
        path = self.workspace_root / slug
        expected = f"https://github.com/{goal.repository}.git"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            proc = self._git(["clone", "--single-branch", "--branch", goal.branch, expected, str(path)])
            if proc.returncode != 0:
                return None, "WORKSPACE_CLONE_FAILED"
        if not (path / ".git").exists():
            return None, "WORKSPACE_NOT_GIT"
        origin = self._git(["remote", "get-url", "origin"], path)
        actual = origin.stdout.strip().removesuffix(".git")
        allowed_origins = {expected.removesuffix(".git"), f"git@github.com:{goal.repository}"}
        if origin.returncode != 0 or actual not in allowed_origins:
            return None, "WORKSPACE_ORIGIN_MISMATCH"
        branch = self._git(["branch", "--show-current"], path)
        if branch.returncode != 0 or branch.stdout.strip() != goal.branch:
            return None, "WORKSPACE_BRANCH_MISMATCH"
        dirty = self._git(["status", "--porcelain"], path)
        if dirty.returncode != 0 or dirty.stdout.strip():
            return None, "WORKSPACE_DIRTY"
        return path, ""

    def _env(self) -> dict[str, str]:
        base_names = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")
        names = set(base_names).union(self.env_allowlist)
        forbidden = {"GITHUB_TOKEN", "GH_TOKEN", "GITHUB_APP_PRIVATE_KEY"}
        return {name: os.environ[name] for name in names if name in os.environ and name not in forbidden}

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
                "Approval gating applies only to privileged/gated actions. "
                "Read-only inspection is always allowed. Ordinary goal-scoped source, test, "
                "and documentation work is allowed when dry_run is false. "
                "A gated action may be performed only when it is explicitly listed in "
                "goal.approved_actions; otherwise return it in requested_actions."
            ),
        }
        try:
            workspace, workspace_error = self._workspace(goal)
        except Exception as exc:
            return WorkerResult(error="WORKSPACE_PREP_FAILED", blockers=("WORKSPACE_PREP_FAILED",), evidence={"reason": str(exc)[:240]})
        if workspace_error:
            return WorkerResult(error=workspace_error, blockers=(workspace_error,), evidence={"reason": workspace_error})
        payload["workspace"] = str(workspace) if workspace else ""
        try:
            proc = subprocess.run(
                self.argv, input=json.dumps(payload), text=True, capture_output=True,
                timeout=1800, check=False, env=self._env(), cwd=workspace,
            )
        except Exception as exc:
            return WorkerResult(error="WORKER_EXECUTION_FAILED", blockers=("WORKER_EXECUTION_FAILED",), evidence={"reason": str(exc)[:240]})
        if proc.returncode != 0:
            return WorkerResult(error="WORKER_EXECUTION_FAILED", blockers=("WORKER_EXECUTION_FAILED",), evidence={"exit_code": proc.returncode})
        try:
            data = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return WorkerResult(error="WORKER_RESULT_INVALID", blockers=("WORKER_RESULT_INVALID",), evidence={"reason": str(exc)[:240]})
        return WorkerResult.normalize(sanitize(data))
