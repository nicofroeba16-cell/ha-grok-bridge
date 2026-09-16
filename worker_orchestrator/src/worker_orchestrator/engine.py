from __future__ import annotations

import json
from hashlib import sha256
from typing import Callable

from .goals import parse_goals
from .models import DEFAULT_ALLOWED_REPOSITORIES, GATED_ACTIONS, Goal, LifecycleState, WorkerResult
from .security import sanitize
from .store import Registry
from .worker import WorkerAdapter


class ReportFormatError(Exception):
    """Raised when canonical report data cannot be rendered."""


class Orchestrator:
    def __init__(
        self,
        registry: Registry,
        worker: WorkerAdapter,
        *,
        reporter: Callable[[str, Goal, dict], None] | None = None,
        dry_run: bool = True,
        stalled_threshold: int = 3,
        ci_verifier: Callable[[str, str], str] | None = None,
        allowed_repositories: set[str] | None = None,
    ):
        self.registry = registry
        self.worker = worker
        self.reporter = reporter
        self.dry_run = dry_run
        self.stalled_threshold = max(2, stalled_threshold)
        self.ci_verifier = ci_verifier
        self.allowed_repositories = set(DEFAULT_ALLOWED_REPOSITORIES if allowed_repositories is None else allowed_repositories)

    def ingest_items(self, items: list[dict]) -> list[Goal]:
        accepted: list[Goal] = []
        for goal in parse_goals(items):
            if not goal.repository or not goal.branch:
                continue
            if goal.repository not in self.allowed_repositories:
                changed, _ = self.registry.upsert_goal(goal)
                self.registry.set_state(
                    goal.key,
                    LifecycleState.BLOCKED,
                    blockers=["REPOSITORY_NOT_ALLOWED"],
                    last_progress="Goal rejected by repository allowlist.",
                )
                if changed:
                    self._report(
                        "WORKER_STATUS",
                        goal,
                        {
                            "state": LifecycleState.BLOCKED,
                            "blockers": ["REPOSITORY_NOT_ALLOWED"],
                            "next": "explicit allowlist update",
                        },
                    )
                continue
            changed, _ = self.registry.upsert_goal(goal)
            if changed:
                accepted.append(goal)
                self.registry.record_event(goal.key, goal.version, "GOAL_ASSIGNED", {"goal_hash": goal.hash})
                self._report(
                    "WORKER_STATUS",
                    goal,
                    {"state": LifecycleState.ASSIGNED, "next": "dispatch"},
                )
        return accepted

    def dispatch_goal(self, goal: Goal) -> LifecycleState:
        row = self.registry.get(goal.key)
        if row is None:
            self.registry.upsert_goal(goal)
            row = self.registry.get(goal.key)
        if row["goal_hash"] != goal.hash:
            self.registry.upsert_goal(goal)
            row = self.registry.get(goal.key)
        if row["state"] in (LifecycleState.DONE, LifecycleState.WAITING_FOR_USER, LifecycleState.STALLED):
            return LifecycleState(row["state"])

        if goal.branch.lower() in {"main", "master"} and not ({"base_branch_mutation", "main_mutation", "master_mutation"} & {x.lower() for x in goal.approved_actions}):
            blockers = ["BASE_BRANCH_GUARD"]
            self.registry.set_state(goal.key, LifecycleState.BLOCKED, blockers=blockers, last_progress="Direct base-branch work rejected.")
            self._report("WORKER_STATUS", goal, {"state": LifecycleState.BLOCKED, "blockers": blockers, "next": "assign a workstream branch"})
            return LifecycleState.BLOCKED

        locked, owner = self.registry.try_acquire_lock(
            goal.key, goal.repository, goal.branch, goal.files, goal.scope
        )
        if not locked:
            blockers = [f"INTEGRATION_CONFLICT with {owner}"]
            self.registry.set_state(
                goal.key,
                LifecycleState.BLOCKED,
                blockers=blockers,
                last_progress="Duplicate writer prevented.",
            )
            self.registry.record_event(
                goal.key, goal.version, "INTEGRATION_CONFLICT", {"owner": owner}
            )
            self._report(
                "INTEGRATION_CONFLICT",
                goal,
                {
                    "state": LifecycleState.BLOCKED,
                    "blockers": blockers,
                    "next": "master reconciliation",
                },
            )
            return LifecycleState.BLOCKED

        try:
            self.registry.set_state(
                goal.key,
                LifecycleState.RUNNING,
                execution_count=row["execution_count"] + 1,
            )
            previous = self._row_as_safe_dict(self.registry.get(goal.key))
            try:
                result = WorkerResult.normalize(
                    self.worker.execute(goal, previous, dry_run=self.dry_run)
                )
            except Exception as exc:
                result = WorkerResult(
                    error="WORKER_EXECUTION_FAILED",
                    blockers=("WORKER_EXECUTION_FAILED",),
                    evidence={"reason": sanitize(f"{type(exc).__name__}: {exc}")[:240]},
                )
            if result.error.startswith("WORKSPACE_") and result.error != "WORKSPACE_PREP_FAILED":
                result.error = "WORKSPACE_PREP_FAILED"
                result.blockers = tuple(dict.fromkeys(("WORKSPACE_PREP_FAILED", *result.blockers)))
            if self.ci_verifier and result.head:
                try:
                    result.ci = self.ci_verifier(goal.repository, result.head)
                except Exception:
                    result.ci = "UNKNOWN"
            try:
                return self._evaluate(goal, result)
            except Exception as exc:
                result = WorkerResult(
                    error="ORCHESTRATOR_INTERNAL_ERROR",
                    blockers=("ORCHESTRATOR_INTERNAL_ERROR",),
                    evidence={"reason": sanitize(str(exc))[:240]},
                )
                self.registry.set_state(
                    goal.key, LifecycleState.BLOCKED,
                    blockers=list(result.blockers), last_progress=result.error,
                    completion_evidence=result.evidence,
                    error_signature=result.error_signature(),
                )
                self.registry.record_event(goal.key, goal.version, result.error, result.evidence)
                return LifecycleState.BLOCKED
        finally:
            self.registry.release_lock(goal.key)

    def _evaluate(self, goal: Goal, result: WorkerResult) -> LifecycleState:
        row = self.registry.get(goal.key)
        verified = set(result.verified_criteria)
        required = set(goal.done_criteria)
        requested = {x.lower() for x in result.requested_actions}
        approved = {x.lower() for x in goal.approved_actions}
        unapproved_gated = sorted((requested & GATED_ACTIONS) - approved)

        same_head = bool(result.head) and result.head == row["last_head"]
        same_error = bool(result.error_signature()) and result.error_signature() == row["error_signature"]
        no_material_progress = not result.progress.strip() or result.progress.strip() == row["last_progress"].strip()
        unchanged = row["unchanged_runs"] + 1 if (same_head and same_error and no_material_progress) else 0

        if unapproved_gated:
            state = LifecycleState.WAITING_FOR_USER
            blockers = list(result.blockers)
            self.registry.set_state(
                goal.key,
                state,
                last_head=result.head,
                ci_status=result.ci,
                blockers=blockers,
                user_gate=unapproved_gated,
                last_progress=result.progress or "Technical work reached a gated action.",
                completion_evidence=result.evidence,
                verified_criteria=sorted(verified),
                error_signature=result.error_signature(),
                unchanged_runs=unchanged,
                session_state=result.session_state,
            )
            self.registry.record_event(
                goal.key, goal.version, "WAITING_FOR_USER", {"actions": unapproved_gated}
            )
            self._report(
                "WORKER_STATUS",
                goal,
                self._status_payload(goal, state, result, blockers, unapproved_gated),
            )
            return state

        if unchanged >= self.stalled_threshold:
            state = LifecycleState.STALLED
            blockers = list(result.blockers) or (
                [result.error] if result.error else ["No material progress across repeated executions"]
            )
            self.registry.set_state(
                goal.key,
                state,
                last_head=result.head,
                ci_status=result.ci,
                blockers=blockers,
                last_progress=result.progress,
                completion_evidence=result.evidence,
                verified_criteria=sorted(verified),
                error_signature=result.error_signature(),
                unchanged_runs=unchanged,
                session_state=result.session_state,
            )
            self.registry.record_event(
                goal.key,
                goal.version,
                "WORKER_STALLED",
                {"unchanged_runs": unchanged, "blockers": blockers},
            )
            self._report(
                "WORKER_STALLED",
                goal,
                self._status_payload(goal, state, result, blockers, []),
            )
            return state

        all_done = bool(required) and required.issubset(verified)
        ci_green = result.ci.upper() in {"GREEN", "SUCCESS", "PASS", "PASSED"}
        if all_done and not result.blockers and not result.error and ci_green:
            state = LifecycleState.DONE
            self.registry.set_state(
                goal.key,
                state,
                last_head=result.head,
                ci_status=result.ci,
                blockers=[],
                user_gate=[],
                last_progress=result.progress or "All explicit Done Criteria verified.",
                completion_evidence=result.evidence,
                verified_criteria=sorted(verified),
                error_signature="",
                unchanged_runs=0,
                session_state=result.session_state,
            )
            self.registry.record_event(
                goal.key,
                goal.version,
                "WORKER_DONE",
                {"head": result.head, "evidence": result.evidence},
            )
            self._report(
                "WORKER_DONE",
                goal,
                self._status_payload(goal, state, result, [], []),
            )
            return state

        blockers = list(result.blockers)
        if result.error:
            blockers.append(result.error)
        state = (
            LifecycleState.BLOCKED
            if blockers
            else (LifecycleState.READY if result.ready else LifecycleState.RUNNING)
        )
        self.registry.set_state(
            goal.key,
            state,
            last_head=result.head,
            ci_status=result.ci,
            blockers=blockers,
            user_gate=[],
            last_progress=result.progress,
            completion_evidence=result.evidence,
            verified_criteria=sorted(verified),
            error_signature=result.error_signature(),
            unchanged_runs=unchanged,
            session_state=result.session_state,
        )
        self.registry.record_event(
            goal.key, goal.version, "WORKER_STATUS", {"state": state, "blockers": blockers}
        )
        self._report(
            "WORKER_STATUS",
            goal,
            self._status_payload(goal, state, result, blockers, []),
        )
        return state

    def _status_payload(
        self,
        goal: Goal,
        state: LifecycleState,
        result: WorkerResult,
        blockers: list[str],
        user_gate: list[str],
    ) -> dict:
        return {
            "state": state,
            "head": result.head,
            "ci": result.ci,
            "done": f"{len(set(result.verified_criteria).intersection(goal.done_criteria))}/{len(goal.done_criteria)}",
            "blockers": blockers,
            "user_action_required": user_gate,
            "last_progress": result.progress,
            "next": result.next_step,
            "evidence": result.evidence,
        }

    def _report(self, kind: str, goal: Goal, payload: dict) -> None:
        if not self.reporter:
            return
        safe_payload = sanitize(payload)
        fingerprint = sha256(
            json.dumps({"kind": kind, "payload": safe_payload}, sort_keys=True, default=str).encode()
        ).hexdigest()
        row = self.registry.get(goal.key)
        if row is not None and row["last_report_fingerprint"] == fingerprint:
            return
        try:
            self.reporter(kind, goal, safe_payload)
        except ReportFormatError as exc:
            self._record_report_failure(goal, "REPORT_FORMAT_FAILED", exc)
            return
        except Exception as exc:
            # Reporting is best-effort; a GitHub/API failure must not interrupt
            # state persistence or release of the repository lock.
            self._record_report_failure(goal, "REPORT_WRITE_FAILED", exc)
            return
        if row is not None:
            try:
                self.registry.set_state(
                    goal.key,
                    LifecycleState(row["state"]),
                    last_report_fingerprint=fingerprint,
                )
            except Exception as exc:
                self._record_report_failure(goal, "REPORT_WRITE_FAILED", exc)

    def _record_report_failure(self, goal: Goal, kind: str, exc: Exception) -> None:
        # Do not advance the fingerprint: the next reconciliation may retry.
        try:
            self.registry.record_event(goal.key, goal.version, kind, {"reason": str(exc)[:240]})
        except Exception:
            pass

    @staticmethod
    def _row_as_safe_dict(row) -> dict:
        if row is None:
            return {}
        allowed = {
            "worker_key", "project", "chat", "repository", "branch",
            "workstream_issue", "goal_version", "state", "last_head",
            "ci_status", "blockers", "user_gate", "last_progress",
            "verified_criteria", "unchanged_runs", "execution_count",
            "recovery_count", "session_state",
        }
        out = {k: row[k] for k in row.keys() if k in allowed}
        for key in ("blockers", "user_gate", "verified_criteria", "session_state"):
            if key in out and isinstance(out[key], str):
                try:
                    out[key] = json.loads(out[key])
                except json.JSONDecodeError:
                    pass
        return sanitize(out)

def format_report(kind: str, goal: Goal, payload: dict) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("evidence", {}), dict):
        raise ReportFormatError("report payload is not canonical")
    if kind == "WORKER_DONE":
        return "\n".join([
            "WORKER_DONE",
            f"PROJECT: {goal.project}",
            f"CHAT: {goal.chat}",
            f"GOAL_VERSION: {goal.version}",
            f"FINAL_HEAD: {payload.get('head', '')}",
            f"TESTS: {payload.get('evidence', {}).get('tests', '')}",
            f"CI: {payload.get('ci', 'UNKNOWN')}",
            "DONE_CRITERIA: all verified",
            "BLOCKERS: none",
            f"USER_ACTION_REQUIRED: {', '.join(payload.get('user_action_required', [])) or 'none'}",
            f"EVIDENCE: {json.dumps(payload.get('evidence', {}), sort_keys=True)}",
        ])
    return "\n".join([
        "WORKER_STATUS" if kind == "WORKER_STATUS" else kind,
        f"PROJECT: {goal.project}",
        f"CHAT: {goal.chat}",
        f"GOAL_VERSION: {goal.version}",
        f"STATE: {payload.get('state', '')}",
        f"REPOSITORY: {goal.repository}",
        f"BRANCH: {goal.branch}",
        f"HEAD: {payload.get('head', '')}",
        f"CI: {payload.get('ci', 'UNKNOWN')}",
        f"DONE_CRITERIA: {payload.get('done', '0/' + str(len(goal.done_criteria)))}",
        f"BLOCKERS: {json.dumps(payload.get('blockers', []))}",
        f"USER_ACTION_REQUIRED: {json.dumps(payload.get('user_action_required', []))}",
        f"LAST_PROGRESS: {payload.get('last_progress', '')}",
        f"NEXT: {payload.get('next', '')}",
    ])
