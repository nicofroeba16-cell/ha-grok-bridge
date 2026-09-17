from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Callable

from .goals import parse_goals
from .models import DEFAULT_ALLOWED_REPOSITORIES, GATED_ACTIONS, Goal, LifecycleState, WorkerResult
from .security import sanitize
from .store import Registry
from .worker import WorkerAdapter


class ReportFormatError(Exception):
    """Raised when canonical report data cannot be rendered."""


class DocumentationDriftError(RuntimeError):
    """Raised when a canonical status destination cannot be written."""


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
        self.documentation_reconciled_keys: set[str] = set()

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
            self._apply_ci_evidence(goal, result)
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

    @staticmethod
    def _is_retryable_ci_blocker(value: str) -> bool:
        text = (value or "").strip().lower()
        if not text:
            return False
        markers = (
            "github api unavailable",
            "ci could not be verified",
            "exact-head ci could not be verified",
            "unable to verify ci",
            "ci verification unavailable",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _ci_done_criteria(goal: Goal) -> tuple[str, ...]:
        return tuple(
            criterion for criterion in goal.done_criteria
            if "ci" in criterion.lower() and any(
                marker in criterion.lower()
                for marker in ("green", "success", "pass", "exact-head")
            )
        )

    def _apply_ci_evidence(self, goal: Goal, result: WorkerResult) -> str:
        if not self.ci_verifier or not result.head:
            return result.ci
        try:
            status = self.ci_verifier(goal.repository, result.head)
        except Exception:
            status = "UNKNOWN"
        result.ci = status
        if status.upper() not in {"GREEN", "SUCCESS", "PASS", "PASSED"}:
            return status
        result.verified_criteria = tuple(dict.fromkeys((
            *result.verified_criteria, *self._ci_done_criteria(goal),
        )))
        result.blockers = tuple(
            blocker for blocker in result.blockers
            if not self._is_retryable_ci_blocker(blocker)
        )
        if self._is_retryable_ci_blocker(result.error):
            result.error = ""
        return status

    def reconcile_external_blockers(self) -> int:
        """Re-evaluate retryable external blockers without spawning workers."""
        if not self.ci_verifier:
            return 0
        reconciled = 0
        for row in self.registry.list_all():
            if row["state"] != LifecycleState.BLOCKED or not row["last_head"]:
                continue
            blockers = list(json.loads(row["blockers"] or "[]"))
            if not blockers or not any(self._is_retryable_ci_blocker(x) for x in blockers):
                continue
            if any(not self._is_retryable_ci_blocker(x) for x in blockers):
                continue
            goal = Goal(
                project=row["project"], chat=row["chat"], repository=row["repository"],
                branch=row["branch"], prompt=row["prompt"],
                done_criteria=tuple(json.loads(row["done_criteria"])),
                workstream_issue=row["workstream_issue"], explicit_version=row["goal_version"],
                files=tuple(json.loads(row["files"])), scope=row["scope"],
                approved_actions=tuple(json.loads(row["approved_actions"])),
                source_comment_id=row["source_comment_id"],
            )
            result = WorkerResult(
                head=row["last_head"], ci=row["ci_status"],
                verified_criteria=tuple(json.loads(row["verified_criteria"] or "[]")),
                blockers=tuple(blockers),
                evidence=json.loads(row["completion_evidence"] or "{}"),
                progress=row["last_progress"],
            )
            status = self._apply_ci_evidence(goal, result)
            if status.upper() not in {"GREEN", "SUCCESS", "PASS", "PASSED"}:
                if status != row["ci_status"]:
                    self.registry.set_state(
                        goal.key, LifecycleState.BLOCKED, ci_status=status
                    )
                continue

            required = set(goal.done_criteria)
            verified = set(result.verified_criteria)
            if required and required.issubset(verified) and not result.blockers:
                self._evaluate(goal, result)
            elif not result.blockers:
                self.registry.set_state(
                    goal.key, LifecycleState.ASSIGNED,
                    ci_status=result.ci, blockers=[], error_signature="",
                    verified_criteria=sorted(verified),
                    last_progress="External CI blocker cleared; remaining work re-assigned once.",
                )
                self.registry.record_event(
                    goal.key, goal.version, "EXTERNAL_BLOCKER_CLEARED",
                    {"ci": result.ci, "head": result.head},
                )
                self._report("WORKER_STATUS", goal, {
                    "state": LifecycleState.ASSIGNED,
                    "head": result.head,
                    "ci": result.ci,
                    "done": f"{len(verified.intersection(required))}/{len(required)}",
                    "blockers": [],
                    "user_action_required": [],
                    "last_progress": "External CI blocker cleared; remaining work re-assigned once.",
                    "next": "dispatch",
                    "evidence": result.evidence,
                })
            reconciled += 1
        return reconciled

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
            return LifecycleState(self.registry.get(goal.key)["state"])

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
            return LifecycleState(self.registry.get(goal.key)["state"])

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
            return LifecycleState(self.registry.get(goal.key)["state"])

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
        return LifecycleState(self.registry.get(goal.key)["state"])

    def _status_payload(
        self,
        goal: Goal,
        state: LifecycleState,
        result: WorkerResult,
        blockers: list[str],
        user_gate: list[str],
    ) -> dict:
        row = self.registry.get(goal.key)
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
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "supersedes": row["last_report_fingerprint"] if row else "",
        }

    def _report(self, kind: str, goal: Goal, payload: dict) -> None:
        if not self.reporter:
            return
        force = bool(payload.get("_force_report", False))
        safe_payload = sanitize(payload)
        safe_payload.pop("_force_report", None)
        fingerprint = report_fingerprint(kind, safe_payload)
        safe_payload["fingerprint"] = fingerprint
        row = self.registry.get(goal.key)
        if row is not None and row["last_report_fingerprint"] == fingerprint and not force:
            return
        try:
            self.reporter(kind, goal, safe_payload)
        except ReportFormatError as exc:
            self._record_report_failure(goal, "REPORT_FORMAT_FAILED", exc)
            return
        except DocumentationDriftError as exc:
            self._record_report_failure(goal, "DOCUMENTATION_DRIFT", exc)
            current = self.registry.get(goal.key)
            if current is not None:
                blockers = json.loads(current["blockers"] or "[]")
                blockers = list(dict.fromkeys([*blockers, "DOCUMENTATION_DRIFT"]))
            self.registry.set_state(
                goal.key, LifecycleState.BLOCKED, blockers=blockers,
                last_progress="Canonical documentation destinations diverged; retry reconciliation.",
                session_state={
                    "documentation_pending": {
                        "kind": kind, "payload": safe_payload,
                        "state": str(payload.get("state", LifecycleState.BLOCKED)),
                    }
                },
            )
            return
        except Exception as exc:
            # Non-destination reporter errors remain retryable without changing
            # the worker state; the dual-destination reporter raises the typed
            # error above for actual partial writes.
            self._record_report_failure(goal, "REPORT_WRITE_FAILED", exc)
            return
        if row is not None:
            try:
                pending = json.loads(row["session_state"] or "{}").get("documentation_pending")
                self.registry.set_state(
                    goal.key,
                    LifecycleState(pending["state"]) if pending else LifecycleState(row["state"]),
                    last_report_fingerprint=fingerprint,
                    session_state={} if pending else json.loads(row["session_state"] or "{}"),
                )
            except Exception as exc:
                self._record_report_failure(goal, "REPORT_WRITE_FAILED", exc)

    def reconcile_documentation(
        self, master_items: list[dict], destinations: dict | None = None,
        master_destination: tuple[str, int] | None = None,
    ) -> int:
        """Repair missing/stale canonical statuses without executing workers."""
        master_statuses = parse_canonical_statuses(master_items)
        destinations = destinations or {}
        self.documentation_reconciled_keys.clear()
        repaired = 0
        for row in self.registry.list_all():
            workstream_key = (row["repository"], row["workstream_issue"])
            if workstream_key == master_destination:
                workstream_statuses = master_statuses
            else:
                workstream_statuses = parse_canonical_statuses(
                    destinations.get(workstream_key, master_items) if destinations else master_items
                )
            pending = json.loads(row["session_state"] or "{}").get("documentation_pending")
            desired_kind = pending.get("kind") if pending else ("WORKER_DONE" if row["state"] == LifecycleState.DONE else "WORKER_STATUS")
            desired_payload = pending.get("payload") if pending else None
            expected = desired_payload.get("state") if desired_payload else row["state"]
            expected = str(expected)
            master_status = master_statuses.get(row["worker_key"])
            workstream_status = workstream_statuses.get(row["worker_key"])
            expected_fp = (report_fingerprint(desired_kind, desired_payload)
                           if desired_payload else row["last_report_fingerprint"])
            master_current = bool(master_status and master_status.get("FINGERPRINT") == expected_fp) if expected_fp else bool(master_status and master_status.get("STATE") == expected)
            workstream_current = bool(workstream_status and workstream_status.get("FINGERPRINT") == expected_fp) if expected_fp else bool(workstream_status and workstream_status.get("STATE") == expected)
            if master_current and (not row["workstream_issue"] or workstream_current):
                continue
            goal = Goal(
                project=row["project"], chat=row["chat"], repository=row["repository"],
                branch=row["branch"], prompt=row["prompt"],
                done_criteria=tuple(json.loads(row["done_criteria"])),
                workstream_issue=row["workstream_issue"], explicit_version=row["goal_version"],
                files=tuple(json.loads(row["files"])), scope=row["scope"],
                approved_actions=tuple(json.loads(row["approved_actions"])),
                source_comment_id=row["source_comment_id"],
            )
            result = WorkerResult(
                head=row["last_head"], ci=row["ci_status"],
                verified_criteria=tuple(json.loads(row["verified_criteria"] or "[]")),
                blockers=tuple(json.loads(row["blockers"] or "[]")),
                evidence=json.loads(row["completion_evidence"] or "{}"),
                progress=row["last_progress"],
            )
            kind = desired_kind
            payload = desired_payload or self._status_payload(goal, LifecycleState(expected), result,
                                                               list(result.blockers),
                                                               list(json.loads(row["user_gate"] or "[]")))
            missing = []
            if not workstream_current and row["workstream_issue"] and workstream_key != master_destination:
                missing.append(workstream_key)
            if not master_current:
                if master_destination:
                    missing.append(master_destination)
            missing = [x for x in missing if x]
            payload["_destinations"] = missing or None
            payload["_force_report"] = True
            # Dual-destination workers must not be executed in the same poll
            # that repaired their canonical documentation. Legacy Master-only
            # workers retain their established dispatch behavior.
            if row["workstream_issue"]:
                self.documentation_reconciled_keys.add(row["worker_key"])
            self._report(kind, goal, payload)
            repaired += 1
        return repaired

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
            f"TIMESTAMP: {payload.get('timestamp', '')}",
            f"SUPERSEDES: {payload.get('supersedes', '')}",
            f"FINGERPRINT: {payload.get('fingerprint', '')}",
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
        f"EVIDENCE: {json.dumps(payload.get('evidence', {}), sort_keys=True)}",
        f"TIMESTAMP: {payload.get('timestamp', '')}",
        f"SUPERSEDES: {payload.get('supersedes', '')}",
        f"FINGERPRINT: {payload.get('fingerprint', '')}",
    ])


def report_fingerprint(kind: str, payload: dict) -> str:
    """Hash only semantic report data; volatile linkage metadata is excluded."""
    safe_payload = sanitize(payload)
    fingerprint_payload = {
        key: value for key, value in safe_payload.items()
        if key not in {"timestamp", "supersedes", "fingerprint", "_force_report", "_destinations"}
    }
    return sha256(
        json.dumps({"kind": kind, "payload": fingerprint_payload}, sort_keys=True, default=str).encode()
    ).hexdigest()


def parse_canonical_statuses(items: list[dict]) -> dict[str, dict]:
    """Return the newest canonical status comment for each worker key."""
    found: dict[str, dict] = {}
    for item in items:
        body = str(item.get("body", ""))
        if not body.lstrip().startswith(("WORKER_STATUS", "WORKER_DONE", "WORKER_STALLED", "INTEGRATION_CONFLICT")):
            continue
        fields = {}
        for line in body.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
        project, chat = fields.get("PROJECT"), fields.get("CHAT")
        if project and chat:
            fields["_kind"] = body.lstrip().splitlines()[0].strip()
            if fields["_kind"] == "WORKER_DONE":
                fields["STATE"] = "DONE"
            found[f"Projekt: {project} → Chat: {chat}"] = fields
    return found
