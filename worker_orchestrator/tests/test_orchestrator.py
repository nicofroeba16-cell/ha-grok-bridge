from __future__ import annotations

import json
import os
import sys
import tempfile
import subprocess
from hashlib import sha256
import unittest
from pathlib import Path
from unittest.mock import patch

from worker_orchestrator.browser_wake import format_verified_delivery_event
from worker_orchestrator.cli import main as cli_main, make_reporter, reconcile
from worker_orchestrator.engine import (
    Orchestrator,
    ReportFormatError,
    format_report,
    parse_verified_delivery_events,
    report_fingerprint,
)
from worker_orchestrator.goals import parse_goal_text, parse_goals
from worker_orchestrator.models import Goal, LifecycleState, WorkerResult, normalize_worker_result
from worker_orchestrator.security import redact_text
from worker_orchestrator.store import Registry
from worker_orchestrator.worker import CommandWorkerAdapter


class ScriptedWorker:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.dry_run_values = []

    def execute(self, goal, previous, *, dry_run):
        self.calls += 1
        self.dry_run_values.append(dry_run)
        return self.results[min(self.calls - 1, len(self.results) - 1)]


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.sqlite3"
        self.registry = Registry(self.db)
        self.reports = []

    def tearDown(self):
        self.registry.close()
        self.tmp.cleanup()

    def goal(self, **kw):
        base = dict(
            project="Worker Orchestrator",
            chat="Runner Worker Orchestrator",
            repository="nicofroeba16-cell/ha-grok-bridge",
            branch="codex/worker-orchestrator-v1",
            prompt="implement",
            done_criteria=("registry works", "tests green"),
            files=("worker_orchestrator/",),
            scope="worker-orchestrator",
        )
        base.update(kw)
        return Goal(**base)

    def engine(self, worker, threshold=3, **kw):
        return Orchestrator(
            self.registry,
            worker,
            reporter=lambda *x: self.reports.append(x),
            stalled_threshold=threshold,
            **kw,
        )

    def test_goal_hash_is_idempotent_and_material_change_reactivates(self):
        g1 = self.goal()
        changed, _ = self.registry.upsert_goal(g1)
        self.assertTrue(changed)
        changed, _ = self.registry.upsert_goal(g1)
        self.assertFalse(changed)
        g2 = self.goal(prompt="implement v2")
        changed, row = self.registry.upsert_goal(g2)
        self.assertTrue(changed)
        self.assertEqual(row["state"], "ASSIGNED")
        self.assertNotEqual(g1.hash, g2.hash)

    def test_verified_delivery_formatter_and_parser_contract(self):
        body = format_verified_delivery_event({
            "message_id": "worker-wake:req:v1:child",
            "project": "P",
            "chat": "C",
            "goal_version": "v1-child",
            "evidence": {
                "reason": "VERIFIED_BROWSER_WAKE_DELIVERY",
                "destination_verified": True,
                "persisted_after_reload": True,
                "verification_source": "persisted_user_turn_after_reload_same_conversation",
            },
        })
        parsed = parse_verified_delivery_events([{"id": 699, "body": body}])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["worker_key"], "Projekt: P → Chat: C")
        self.assertEqual(parsed[0]["goal_version"], "v1-child")
        self.assertEqual(parsed[0]["wake_id"], "worker-wake:req:v1:child")

    def test_verified_browser_delivery_promotes_current_assigned_goal_to_running_once(self):
        g = self.goal(explicit_version="wake-v1", workstream_issue=45)
        self.registry.upsert_goal(g)
        worker = ScriptedWorker([])
        engine = self.engine(worker)
        body = "\n".join([
            "BROWSER_WAKE_DELIVERY",
            f"PROJECT: {g.project}",
            f"CHAT: {g.chat}",
            f"GOAL_VERSION: {g.version}",
            "WAKE_ID: worker-wake:req:wake-v1:child",
            "DELIVERY_STATUS: VERIFIED",
            "DELIVERY_VERIFIED: true",
            "SOURCE: browser_wake_verified_delivery_v1",
            'EVIDENCE: {"reason":"VERIFIED_BROWSER_WAKE_DELIVERY","destination_verified":true,"persisted_after_reload":true,"verification_source":"persisted_user_turn_after_reload_same_conversation"}',
        ])
        items = [{"id": 700, "body": body}]
        engine.ingest_items(items)

        row = self.registry.get(g.key)
        self.assertEqual(row["state"], LifecycleState.RUNNING)
        self.assertEqual(worker.calls, 0)
        evidence = json.loads(row["completion_evidence"])
        self.assertEqual(evidence["reason"], "VERIFIED_BROWSER_WAKE_DELIVERY")
        self.assertTrue(evidence["persisted_after_reload"])
        events = [e for e in self.registry.events(g.key) if e["event_type"] == "VERIFIED_WAKE_DELIVERY"]
        self.assertEqual(len(events), 1)
        running_reports = [
            report for report in self.reports
            if report[0] == "WORKER_STATUS" and report[2].get("state") == LifecycleState.RUNNING
        ]
        self.assertEqual(len(running_reports), 1)

        engine.ingest_items(items)
        events = [e for e in self.registry.events(g.key) if e["event_type"] == "VERIFIED_WAKE_DELIVERY"]
        self.assertEqual(len(events), 1)
        running_reports = [
            report for report in self.reports
            if report[0] == "WORKER_STATUS" and report[2].get("state") == LifecycleState.RUNNING
        ]
        self.assertEqual(len(running_reports), 1)

    def test_unverified_or_nonverified_delivery_states_never_promote_running(self):
        for delivery_status, verified in (
            ("FAILED_PRE_SEND", "false"),
            ("IN_FLIGHT", "false"),
            ("UNCERTAIN", "false"),
            ("DELIVERED", "false"),
        ):
            with self.subTest(delivery_status=delivery_status):
                g = self.goal(
                    project=f"Wake {delivery_status}",
                    chat=delivery_status,
                    explicit_version=f"wake-{delivery_status}",
                )
                self.registry.upsert_goal(g)
                engine = self.engine(ScriptedWorker([]))
                body = "\n".join([
                    "BROWSER_WAKE_DELIVERY",
                    f"PROJECT: {g.project}",
                    f"CHAT: {g.chat}",
                    f"GOAL_VERSION: {g.version}",
                    f"WAKE_ID: wake-{delivery_status}",
                    f"DELIVERY_STATUS: {delivery_status}",
                    f"DELIVERY_VERIFIED: {verified}",
                    "SOURCE: browser_wake_verified_delivery_v1",
                ])
                engine.ingest_items([{"id": 701, "body": body}])
                self.assertEqual(self.registry.get(g.key)["state"], LifecycleState.ASSIGNED)

    def test_verified_delivery_never_regresses_terminal_or_blocked_state(self):
        protected = (
            LifecycleState.DONE,
            LifecycleState.READY,
            LifecycleState.WAITING_FOR_USER,
            LifecycleState.BLOCKED,
            LifecycleState.STALLED,
        )
        for state in protected:
            with self.subTest(state=state):
                g = self.goal(
                    project=f"Protected {state}",
                    chat=str(state),
                    explicit_version=f"protected-{state}",
                )
                self.registry.upsert_goal(g)
                self.registry.set_state(g.key, state)
                engine = self.engine(ScriptedWorker([]))
                body = "\n".join([
                    "BROWSER_WAKE_DELIVERY",
                    f"PROJECT: {g.project}",
                    f"CHAT: {g.chat}",
                    f"GOAL_VERSION: {g.version}",
                    f"WAKE_ID: wake-{state}",
                    "DELIVERY_STATUS: VERIFIED",
                    "DELIVERY_VERIFIED: true",
                    "SOURCE: browser_wake_verified_delivery_v1",
                    'EVIDENCE: {"reason":"VERIFIED_BROWSER_WAKE_DELIVERY","destination_verified":true,"persisted_after_reload":true,"verification_source":"persisted_user_turn_after_reload_same_conversation"}',
                ])
                engine.ingest_items([{"id": 702, "body": body}])
                self.assertEqual(self.registry.get(g.key)["state"], state)

    def test_stale_verified_delivery_does_not_promote_changed_goal(self):
        current = self.goal(explicit_version="new-goal")
        self.registry.upsert_goal(current)
        engine = self.engine(ScriptedWorker([]))
        body = "\n".join([
            "BROWSER_WAKE_DELIVERY",
            f"PROJECT: {current.project}",
            f"CHAT: {current.chat}",
            "GOAL_VERSION: old-goal",
            "WAKE_ID: stale-wake",
            "DELIVERY_STATUS: VERIFIED",
            "DELIVERY_VERIFIED: true",
            "SOURCE: browser_wake_verified_delivery_v1",
            'EVIDENCE: {"reason":"VERIFIED_BROWSER_WAKE_DELIVERY","destination_verified":true,"persisted_after_reload":true,"verification_source":"persisted_user_turn_after_reload_same_conversation"}',
        ])
        engine.ingest_items([{"id": 703, "body": body}])
        self.assertEqual(self.registry.get(current.key)["state"], LifecycleState.ASSIGNED)
        self.assertFalse(any(
            e["event_type"] == "VERIFIED_WAKE_DELIVERY"
            for e in self.registry.events(current.key)
        ))

    def test_done_requires_all_criteria_and_green_ci(self):
        g = self.goal()
        worker = ScriptedWorker([
            WorkerResult(
                head="a",
                ci="GREEN",
                verified_criteria=("registry works",),
                ready=True,
            )
        ])
        engine = self.engine(worker)
        self.registry.upsert_goal(g)
        self.assertEqual(engine.dispatch_goal(g), LifecycleState.READY)
        self.assertNotEqual(self.registry.get(g.key)["state"], "DONE")

    def test_e2e_new_goal_done_reported_then_dormant(self):
        criteria = ("persistent registry", "simulation green")
        text = (
            "PROJECT: Worker Orchestrator\n"
            "CHAT: Runner Worker Orchestrator\n"
            "REPOSITORY: nicofroeba16-cell/ha-grok-bridge\n"
            "BRANCH: codex/worker-orchestrator-v1\n"
            "GOAL_VERSION: v1\n"
            "SCOPE: worker-orchestrator\n"
            "FILES: worker_orchestrator/\n"
            "DONE_CRITERIA:\n"
            "- persistent registry\n"
            "- simulation green\n"
        )
        worker = ScriptedWorker([
            WorkerResult(
                head="abc123",
                ci="GREEN",
                verified_criteria=criteria,
                evidence={"tests": "green"},
                progress="all verified",
            )
        ])
        rendered = []
        engine = Orchestrator(
            self.registry,
            worker,
            reporter=lambda kind, goal, payload: rendered.append(
                format_report(kind, goal, payload)
            ),
        )
        goals = engine.ingest_items([{"id": 1, "body": text}])
        self.assertEqual(len(goals), 1)
        g = goals[0]
        self.assertEqual(engine.dispatch_goal(g), LifecycleState.DONE)
        self.assertEqual(worker.calls, 1)
        self.assertIn("WORKER_DONE", rendered[-1])
        self.assertIn("DONE_CRITERIA: all verified", rendered[-1])
        self.assertEqual(engine.dispatch_goal(g), LifecycleState.DONE)
        self.assertEqual(worker.calls, 1)
        self.assertEqual(engine.ingest_items([{"id": 1, "body": text}]), [])
        self.assertEqual(self.registry.get(g.key)["state"], "DONE")

    def test_waiting_for_user_blocks_gated_action(self):
        g = self.goal()
        worker = ScriptedWorker([
            WorkerResult(
                head="abc",
                ci="GREEN",
                verified_criteria=g.done_criteria,
                requested_actions=("merge",),
                progress="technical work complete",
            )
        ])
        engine = self.engine(worker)
        self.registry.upsert_goal(g)
        self.assertEqual(engine.dispatch_goal(g), LifecycleState.WAITING_FOR_USER)
        row = self.registry.get(g.key)
        self.assertIn("merge", json.loads(row["user_gate"]))
        self.assertTrue(worker.dry_run_values[0])
        self.assertEqual(engine.dispatch_goal(g), LifecycleState.WAITING_FOR_USER)
        self.assertEqual(worker.calls, 1)

    def test_stalled_is_detected_and_then_dormant(self):
        g = self.goal()
        same = WorkerResult(head="deadbeef", ci="RED", error="same failure", progress="")
        worker = ScriptedWorker([same, same, same, same])
        engine = self.engine(worker, threshold=2)
        self.registry.upsert_goal(g)
        self.assertEqual(engine.dispatch_goal(g), LifecycleState.BLOCKED)
        self.assertEqual(engine.dispatch_goal(g), LifecycleState.BLOCKED)
        self.assertEqual(engine.dispatch_goal(g), LifecycleState.STALLED)
        calls = worker.calls
        self.assertEqual(engine.dispatch_goal(g), LifecycleState.STALLED)
        self.assertEqual(worker.calls, calls)
        self.assertTrue(any(r[0] == "WORKER_STALLED" for r in self.reports))

    def test_duplicate_writer_is_stopped(self):
        g1 = self.goal(project="A", chat="One")
        g2 = self.goal(project="B", chat="Two")
        self.registry.upsert_goal(g1)
        self.registry.upsert_goal(g2)
        ok, _ = self.registry.try_acquire_lock(
            g1.key, g1.repository, g1.branch, g1.files, g1.scope
        )
        self.assertTrue(ok)
        worker = ScriptedWorker([WorkerResult()])
        engine = self.engine(worker)
        self.assertEqual(engine.dispatch_goal(g2), LifecycleState.BLOCKED)
        self.assertEqual(worker.calls, 0)
        self.assertIn("INTEGRATION_CONFLICT", self.registry.get(g2.key)["blockers"])

    def test_blocked_workers_are_dormant_for_dispatch(self):
        blocked = self.goal(project="Blocked", chat="Dormant")
        ready = self.goal(project="Ready", chat="Retry", scope="ready")
        self.registry.upsert_goal(blocked)
        self.registry.upsert_goal(ready)
        self.registry.set_state(blocked.key, LifecycleState.BLOCKED, blockers=["retry elsewhere"])
        self.registry.set_state(ready.key, LifecycleState.READY)
        keys = {row["worker_key"] for row in self.registry.list_dispatchable()}
        self.assertNotIn(blocked.key, keys)
        self.assertNotIn(ready.key, keys)

    def test_ready_worker_is_dormant_until_material_goal_change(self):
        g = self.goal(project="Bridge", chat="Dormant Ready")
        self.registry.upsert_goal(g)
        self.registry.set_state(g.key, LifecycleState.READY, execution_count=17)
        self.assertEqual(self.registry.list_dispatchable(), [])
        changed = self.goal(
            project="Bridge", chat="Dormant Ready", prompt="material v2"
        )
        was_changed, row = self.registry.upsert_goal(changed)
        self.assertTrue(was_changed)
        self.assertEqual(row["state"], LifecycleState.ASSIGNED)
        self.assertEqual(
            [x["worker_key"] for x in self.registry.list_dispatchable()], [g.key]
        )

    def test_stale_ci_blocker_reconciles_to_done_without_worker_respawn(self):
        ci_criterion = "branch HEAD is exact with GREEN exact-head CI"
        g = self.goal(
            project="Master Autonomous Orchestration",
            chat="Control Plane E2E",
            done_criteria=("README verified", ci_criterion, "workspace unchanged"),
        )
        worker = ScriptedWorker([WorkerResult()])
        engine = self.engine(worker, ci_verifier=lambda repo, head: "GREEN")
        self.registry.upsert_goal(g)
        self.registry.set_state(
            g.key,
            LifecycleState.BLOCKED,
            last_head="abc123",
            ci_status="UNKNOWN",
            blockers=[
                "GitHub API unavailable; GREEN exact-head CI could not be verified"
            ],
            verified_criteria=["README verified", "workspace unchanged"],
            completion_evidence={"local_tests": "green"},
            execution_count=1,
        )
        self.assertEqual(engine.reconcile_external_blockers(), 1)
        row = self.registry.get(g.key)
        self.assertEqual(row["state"], LifecycleState.DONE)
        self.assertEqual(row["ci_status"], "GREEN")
        self.assertEqual(row["execution_count"], 1)
        self.assertEqual(worker.calls, 0)
        self.assertIn(ci_criterion, json.loads(row["verified_criteria"]))

    def test_non_ci_blocker_is_not_auto_cleared(self):
        g = self.goal(project="Blocked", chat="Real blocker")
        engine = self.engine(
            ScriptedWorker([WorkerResult()]),
            ci_verifier=lambda repo, head: "GREEN",
        )
        self.registry.upsert_goal(g)
        self.registry.set_state(
            g.key,
            LifecycleState.BLOCKED,
            last_head="abc123",
            blockers=["runtime filesystem is read-only"],
        )
        self.assertEqual(engine.reconcile_external_blockers(), 0)
        self.assertEqual(self.registry.get(g.key)["state"], LifecycleState.BLOCKED)

    def test_executor_failures_requeue_when_execution_disabled(self):
        g = self.goal(project="Dispatch Only", chat="External Worker")
        self.registry.upsert_goal(g)
        self.registry.set_state(
            g.key,
            LifecycleState.BLOCKED,
            blockers=["WORKER_EXECUTION_FAILED", "codex worker failed with exit code 1"],
            error_signature="executor-failure",
            execution_count=1,
        )
        self.assertEqual(self.registry.requeue_executor_failures(), 1)
        row = self.registry.get(g.key)
        self.assertEqual(row["state"], LifecycleState.ASSIGNED)
        self.assertEqual(json.loads(row["blockers"]), [])
        self.assertIn("awaiting external", row["last_progress"].lower())

    def test_restart_recovers_running_worker(self):
        g = self.goal()
        self.registry.upsert_goal(g)
        self.registry.set_state(g.key, LifecycleState.RUNNING)
        self.registry.close()
        self.registry = Registry(self.db)
        recovered = self.registry.recover_interrupted()
        self.assertEqual(recovered, [g.key])
        self.assertEqual(self.registry.get(g.key)["state"], "ASSIGNED")
        self.assertEqual(self.registry.get(g.key)["recovery_count"], 1)

    def test_restart_preserves_browser_verified_external_running_worker(self):
        g = self.goal(project="Browser Wake", chat="External", explicit_version="wake-v1")
        self.registry.upsert_goal(g)
        self.registry.set_state(
            g.key,
            LifecycleState.RUNNING,
            session_state={
                "verified_wake_delivery": {
                    "wake_id": "worker-wake:req:wake-v1:child",
                    "reason": "VERIFIED_BROWSER_WAKE_DELIVERY",
                }
            },
        )
        self.registry.close()
        self.registry = Registry(self.db)
        recovered = self.registry.recover_interrupted()
        self.assertEqual(recovered, [])
        row = self.registry.get(g.key)
        self.assertEqual(row["state"], LifecycleState.RUNNING)
        self.assertEqual(row["recovery_count"], 0)

    def test_status_is_read_only_and_does_not_recover_running_worker(self):
        g = self.goal(project="Status", chat="Probe")
        self.registry.upsert_goal(g)
        self.registry.set_state(g.key, LifecycleState.RUNNING)
        self.registry.close()
        with patch("builtins.print"):
            self.assertEqual(cli_main(["--db", str(self.db), "status"]), 0)
        self.registry = Registry(self.db)
        self.assertEqual(self.registry.get(g.key)["state"], "RUNNING")
        self.assertEqual(self.registry.get(g.key)["recovery_count"], 0)

    def test_exact_head_ci_overrides_worker_claim(self):
        g = self.goal()
        worker = ScriptedWorker([
            WorkerResult(
                head="abc",
                ci="GREEN",
                verified_criteria=g.done_criteria,
            )
        ])
        engine = self.engine(worker, ci_verifier=lambda repo, head: "RED")
        self.registry.upsert_goal(g)
        self.assertNotEqual(engine.dispatch_goal(g), LifecycleState.DONE)
        self.assertEqual(self.registry.get(g.key)["ci_status"], "RED")

    def test_repository_allowlist_is_fail_closed(self):
        text = (
            "PROJECT: Demo\n"
            "CHAT: Worker\n"
            "REPOSITORY: other/repo\n"
            "BRANCH: feat/x\n"
            "DONE_CRITERIA:\n"
            "- one\n"
        )
        worker = ScriptedWorker([WorkerResult()])
        engine = self.engine(worker, allowed_repositories={"allowed/repo"})
        self.assertEqual(engine.ingest_items([{"id": 9, "body": text}]), [])
        row = self.registry.get("Projekt: Demo → Chat: Worker")
        self.assertEqual(row["state"], "BLOCKED")
        self.assertIn("REPOSITORY_NOT_ALLOWED", row["blockers"])

    def test_goal_parser_maps_exact_project_and_chat(self):
        text = (
            "PROJECT: Demo\n"
            "CHAT: Exact Worker\n"
            "REPOSITORY: o/r\n"
            "BRANCH: feat/x\n"
            "DONE_CRITERIA:\n"
            "- one\n"
            "- two\n"
        )
        goal = parse_goal_text(text, source_comment_id=4)
        self.assertIsNotNone(goal)
        self.assertEqual(goal.key, "Projekt: Demo → Chat: Exact Worker")
        self.assertEqual(goal.done_criteria, ("one", "two"))

    def test_goal_parser_accepts_canonical_project_chat_line(self):
        text = (
            "Projekt: Demo Project → Chat: Exact Worker\n"
            "Repository: o/r\n"
            "Branch: feat/x\n"
            "Done-Kriterien:\n"
            "- one\n"
            "- two\n"
        )
        goal = parse_goal_text(text, source_comment_id=5)
        self.assertIsNotNone(goal)
        self.assertEqual(goal.key, "Projekt: Demo Project → Chat: Exact Worker")
        self.assertEqual(goal.repository, "o/r")
        self.assertEqual(goal.branch, "feat/x")
        self.assertEqual(goal.done_criteria, ("one", "two"))

    def test_explicit_identity_is_not_overridden_by_referenced_route(self):
        text = (
            "GOAL PROMPT\n"
            "PROJECT: Master Autonomous Orchestration\n"
            "CHAT: Runner Control Plane Cutover\n"
            "REPOSITORY: nicofroeba16-cell/ha-grok-bridge\n"
            "BRANCH: fix/goal-identity-precedence\n"
            "GOAL_VERSION: identity-precedence-v1\n"
            "ROUTE TO CONFIGURE:\n"
            "Projekt: Master Autonomous Orchestration → Chat: Control Plane E2E\n"
            "DONE_CRITERIA:\n"
            "- explicit identity remains authoritative\n"
        )
        goal = parse_goal_text(text, source_comment_id=12)
        self.assertEqual(goal.project, "Master Autonomous Orchestration")
        self.assertEqual(goal.chat, "Runner Control Plane Cutover")

    def test_duplicate_report_is_suppressed(self):
        g = self.goal()
        worker = ScriptedWorker([
            WorkerResult(head="x", ci="RED", blockers=("blocked",), progress="same")
        ])
        engine = self.engine(worker)
        self.registry.upsert_goal(g)
        engine.dispatch_goal(g)
        first = len(self.reports)
        engine.dispatch_goal(g)
        self.assertEqual(len(self.reports), first)

    def test_canonical_transition_writes_both_destinations_once(self):
        g = self.goal(workstream_issue=4)
        calls = []
        gh = type("GitHub", (), {"post_issue_comment": lambda _, repo, issue, body: calls.append((repo, issue, body))})()
        reporter = make_reporter(gh, "owner/master", 3)
        reporter("WORKER_STATUS", g, {"state": "RUNNING", "evidence": {}})
        self.assertEqual([(call[0], call[1]) for call in calls],
                         [(g.repository, 4), ("owner/master", 3)])

    def test_terminal_statuses_mirror_to_both_destinations_idempotently(self):
        for state, kind in (
            ("READY", "WORKER_STATUS"),
            ("DONE", "WORKER_DONE"),
            ("WAITING_FOR_USER", "WORKER_STATUS"),
            ("BLOCKED", "WORKER_STATUS"),
        ):
            with self.subTest(state=state):
                g = self.goal(project=f"Terminal {state}", chat=state, workstream_issue=4)
                calls = []
                gh = type(
                    "GitHub",
                    (),
                    {"post_issue_comment": lambda _, repo, issue, body: calls.append((repo, issue, body))},
                )()
                self.registry.upsert_goal(g)
                self.registry.set_state(g.key, LifecycleState(state))
                engine = Orchestrator(
                    self.registry,
                    ScriptedWorker([]),
                    reporter=make_reporter(gh, "owner/master", 3),
                )
                payload = {
                    "state": state,
                    "evidence": {"mirror": "terminal"},
                    "blockers": [],
                    "user_action_required": [],
                }
                engine._report(kind, g, payload)
                engine._report(kind, g, payload)
                self.assertEqual(len(calls), 2)
                self.assertEqual(
                    [(repo, issue) for repo, issue, _ in calls],
                    [(g.repository, 4), ("owner/master", 3)],
                )
                if kind == "WORKER_DONE":
                    self.assertTrue(all(body.startswith("WORKER_DONE") for _, _, body in calls))
                else:
                    self.assertTrue(all(f"STATE: {state}" in body for _, _, body in calls))

    def test_partial_canonical_write_blocks_with_documentation_drift(self):
        g = self.goal(workstream_issue=4)
        calls = []
        def post(_, repo, issue, body):
            calls.append((repo, issue))
            if issue == 3:
                raise OSError("master unavailable")
        gh = type("GitHub", (), {"post_issue_comment": post})()
        self.registry.upsert_goal(g)
        engine = Orchestrator(self.registry, ScriptedWorker([WorkerResult(progress="x")]),
                              reporter=make_reporter(gh, "owner/master", 3))
        self.assertEqual(engine.dispatch_goal(g), LifecycleState.BLOCKED)
        self.assertIn("DOCUMENTATION_DRIFT", json.loads(self.registry.get(g.key)["blockers"]))
        self.assertEqual(calls, [(g.repository, 4), ("owner/master", 3)])

    def test_restart_reconciles_stale_canonical_status_without_worker_execution(self):
        g = self.goal(workstream_issue=4)
        self.registry.upsert_goal(g)
        self.registry.set_state(g.key, LifecycleState.DONE, last_head="head", ci_status="GREEN",
                                verified_criteria=list(g.done_criteria), completion_evidence={"tests": "ok"})
        rendered = []
        engine = Orchestrator(self.registry, ScriptedWorker([]),
                               reporter=lambda kind, goal, payload: rendered.append((kind, payload)))
        stale = [{"body": format_report("WORKER_STATUS", g, {"state": "RUNNING", "evidence": {}})}]
        self.assertEqual(engine.reconcile_documentation(stale), 1)
        self.assertEqual(len(rendered), 1)
        self.assertEqual(rendered[0][0], "WORKER_DONE")
        self.assertEqual(engine.worker.calls, 0)

    def test_restart_with_current_fingerprint_does_not_republish(self):
        g = self.goal(workstream_issue=4)
        self.registry.upsert_goal(g)
        worker = ScriptedWorker([WorkerResult(head="head", ci="GREEN", progress="done")])
        rendered = []
        engine = Orchestrator(self.registry, worker,
                              reporter=lambda kind, goal, payload: rendered.append(
                                  format_report(kind, goal, payload)))
        engine.dispatch_goal(g)
        current = [{"body": rendered[-1]}]
        self.assertEqual(engine.reconcile_documentation(current), 0)
        self.assertEqual(len(rendered), 1)
        self.assertEqual(worker.calls, 1)

    def test_missing_canonical_status_is_reconciled(self):
        g = self.goal()
        self.registry.upsert_goal(g)
        rendered = []
        engine = Orchestrator(self.registry, ScriptedWorker([]),
                               reporter=lambda kind, goal, payload: rendered.append(kind))
        self.assertEqual(engine.reconcile_documentation([]), 1)
        self.assertEqual(rendered, ["WORKER_STATUS"])

    def test_master_current_workstream_stale_repairs_workstream_only(self):
        g = self.goal(workstream_issue=4)
        self.registry.upsert_goal(g)
        self.registry.set_state(
            g.key, LifecycleState.DONE, last_head="head", ci_status="GREEN",
            verified_criteria=list(g.done_criteria), completion_evidence={"tests": "ok"},
        )
        rendered = []
        engine = Orchestrator(
            self.registry, ScriptedWorker([]),
            reporter=lambda kind, goal, payload: rendered.append((kind, payload)),
        )
        result = WorkerResult(
            head="head", ci="GREEN", verified_criteria=g.done_criteria,
            evidence={"tests": "ok"},
        )
        payload = engine._status_payload(g, LifecycleState.DONE, result, [], [])
        fp = report_fingerprint("WORKER_DONE", payload)
        self.registry.set_state(g.key, LifecycleState.DONE, last_report_fingerprint=fp)
        payload["fingerprint"] = fp
        current = [{"body": format_report("WORKER_DONE", g, payload)}]
        stale = [{"body": format_report("WORKER_STATUS", g, {
            "state": "RUNNING", "evidence": {}, "fingerprint": "stale",
        })}]
        self.assertEqual(
            engine.reconcile_documentation(
                current, {(g.repository, 4): stale}, ("owner/master", 3)
            ), 1,
        )
        self.assertEqual(rendered[0][1]["_destinations"], [[g.repository, 4]])
        self.assertEqual(engine.worker.calls, 0)

    def test_workstream_current_master_stale_repairs_master_only(self):
        g = self.goal(workstream_issue=4)
        self.registry.upsert_goal(g)
        self.registry.set_state(
            g.key, LifecycleState.DONE, last_head="head", ci_status="GREEN",
            verified_criteria=list(g.done_criteria), completion_evidence={"tests": "ok"},
        )
        rendered = []
        engine = Orchestrator(
            self.registry, ScriptedWorker([]),
            reporter=lambda kind, goal, payload: rendered.append((kind, payload)),
        )
        result = WorkerResult(
            head="head", ci="GREEN", verified_criteria=g.done_criteria,
            evidence={"tests": "ok"},
        )
        payload = engine._status_payload(g, LifecycleState.DONE, result, [], [])
        fp = report_fingerprint("WORKER_DONE", payload)
        self.registry.set_state(g.key, LifecycleState.DONE, last_report_fingerprint=fp)
        payload["fingerprint"] = fp
        current = [{"body": format_report("WORKER_DONE", g, payload)}]
        stale = [{"body": format_report("WORKER_STATUS", g, {
            "state": "RUNNING", "evidence": {}, "fingerprint": "stale",
        })}]
        self.assertEqual(
            engine.reconcile_documentation(
                stale, {(g.repository, 4): current}, ("owner/master", 3)
            ), 1,
        )
        self.assertEqual(rendered[0][1]["_destinations"], [["owner/master", 3]])
        self.assertEqual(engine.worker.calls, 0)

    def test_both_canonical_destinations_current_restart_posts_nothing(self):
        g = self.goal(workstream_issue=4)
        self.registry.upsert_goal(g)
        self.registry.set_state(
            g.key, LifecycleState.DONE, last_head="head", ci_status="GREEN",
            verified_criteria=list(g.done_criteria), completion_evidence={"tests": "ok"},
        )
        rendered = []
        engine = Orchestrator(
            self.registry, ScriptedWorker([]),
            reporter=lambda kind, goal, payload: rendered.append((kind, payload)),
        )
        result = WorkerResult(
            head="head", ci="GREEN", verified_criteria=g.done_criteria,
            evidence={"tests": "ok"},
        )
        payload = engine._status_payload(g, LifecycleState.DONE, result, [], [])
        fp = report_fingerprint("WORKER_DONE", payload)
        self.registry.set_state(g.key, LifecycleState.DONE, last_report_fingerprint=fp)
        payload["fingerprint"] = fp
        current = [{"body": format_report("WORKER_DONE", g, payload)}]
        self.assertEqual(
            engine.reconcile_documentation(
                current, {(g.repository, 4): current}, ("owner/master", 3)
            ), 0,
        )
        self.assertEqual(rendered, [])
        self.assertEqual(engine.worker.calls, 0)

    def test_partial_write_retry_does_not_duplicate_current_destination(self):
        g = self.goal(workstream_issue=4)
        calls = []
        fail_master = {"value": True}

        class GitHub:
            def post_issue_comment(self, repo, issue, body):
                calls.append((repo, issue, body))
                if (repo, issue) == ("owner/master", 3) and fail_master["value"]:
                    raise OSError("master unavailable")

        worker = ScriptedWorker([WorkerResult(progress="running")])
        self.registry.upsert_goal(g)
        engine = Orchestrator(
            self.registry, worker, reporter=make_reporter(GitHub(), "owner/master", 3)
        )
        engine.dispatch_goal(g)
        self.assertEqual([(r, i) for r, i, _ in calls[:2]], [(g.repository, 4), ("owner/master", 3)])
        workstream_body = calls[0][2]
        fail_master["value"] = False
        before = len([1 for r, i, _ in calls if (r, i) == (g.repository, 4)])
        self.assertEqual(
            engine.reconcile_documentation(
                [], {(g.repository, 4): [{"body": workstream_body}]}, ("owner/master", 3)
            ), 1,
        )
        after = len([1 for r, i, _ in calls if (r, i) == (g.repository, 4)])
        self.assertEqual(before, after)
        self.assertEqual([(r, i) for r, i, _ in calls][-1], ("owner/master", 3))
        self.assertEqual(worker.calls, 1)

    def test_reconcile_reads_workstream_issue_separately(self):
        g = self.goal(workstream_issue=4)
        self.registry.upsert_goal(g)
        worker = ScriptedWorker([WorkerResult(progress="must not run")])
        engine = self.engine(worker)
        reads = []

        class GitHub:
            def read_master_items(self, repo, issue):
                return []
            def read_issue_items(self, repo, issue):
                reads.append((repo, issue))
                return []

        reconcile(engine, GitHub(), "owner/master", 3)
        self.assertEqual(reads, [(g.repository, 4)])
        self.assertEqual(worker.calls, 0)

    def test_issue_number_collision_still_writes_distinct_repositories(self):
        g = self.goal(workstream_issue=3)
        calls = []
        gh = type("GitHub", (), {"post_issue_comment": lambda _, repo, issue, body: calls.append((repo, issue))})()
        make_reporter(gh, "owner/master", 3)("WORKER_STATUS", g, {"state": "RUNNING", "evidence": {}})
        self.assertEqual(calls, [(g.repository, 3), ("owner/master", 3)])

    def test_secret_redaction_and_worker_environment_isolation(self):
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "github_pat_abcdefghijklmnopqrstuvwxyz123456",
                "WORKER_TOKEN": "dedicated-worker-token",
            },
            clear=False,
        ):
            self.assertNotIn(
                "abcdefghijklmnopqrstuvwxyz",
                redact_text("token=github_pat_abcdefghijklmnopqrstuvwxyz123456"),
            )
            adapter = CommandWorkerAdapter("echo", env_allowlist=("WORKER_TOKEN",))
            env = adapter._env()
            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertEqual(env["WORKER_TOKEN"], "dedicated-worker-token")

    def test_command_worker_adapter_executes_json_contract(self):
        script = Path(self.tmp.name) / "worker.py"
        script.write_text(
            "import json,sys\n"
            "request=json.load(sys.stdin)\n"
            "json.dump({'head':'cafe','ci':'GREEN','verified_criteria':request['goal']['done_criteria'],'progress':'ok'},sys.stdout)\n"
        )
        adapter = CommandWorkerAdapter(f"{sys.executable} {script}")
        result = adapter.execute(self.goal(), {}, dry_run=True)
        self.assertEqual(result.head, "cafe")
        self.assertEqual(result.progress, "ok")
        self.assertEqual(result.verified_criteria, self.goal().done_criteria)

    def test_worker_contract_allows_read_only_without_approval(self):
        script = Path(self.tmp.name) / "contract_worker.py"
        script.write_text(
            "import json,sys\n"
            "request=json.load(sys.stdin)\n"
            "json.dump({'progress':request['gated_action_contract']},sys.stdout)\n"
        )
        adapter = CommandWorkerAdapter(f"{sys.executable} {script}")
        result = adapter.execute(self.goal(), {}, dry_run=True)
        self.assertIn("Read-only inspection is always allowed", result.progress)
        self.assertIn("privileged/gated actions", result.progress)


    def assignment(self, version, body_suffix="", source=1, chat="Latest Worker"):
        text = (f"PROJECT: Worker Orchestrator\nCHAT: {chat}\nREPOSITORY: nicofroeba16-cell/ha-grok-bridge\n"
                f"BRANCH: fix/runtime\nGOAL_VERSION: {version}\nDONE_CRITERIA:\n- read README\n{body_suffix}")
        return {"id": source, "body": text}

    def test_latest_goal_wins_and_stays_winner_next_poll(self):
        goals = parse_goals([self.assignment("v1", source=10), self.assignment("v2", source=20)])
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0].version, "v2")
        worker = ScriptedWorker([WorkerResult()])
        engine = self.engine(worker)
        engine.ingest_items([self.assignment("v1", source=10), self.assignment("v2", source=20)])
        row = self.registry.get(goals[0].key)
        self.assertEqual(row["goal_version"], "v2")
        engine.ingest_items([self.assignment("v1", source=10)])
        self.assertEqual(self.registry.get(goals[0].key)["goal_version"], "v2")

    def test_same_version_new_hash_newer_source_reactivates_once(self):
        g1 = parse_goal_text(self.assignment("same", source=10)["body"], source_comment_id=10)
        g2 = parse_goal_text(self.assignment("same", "NOTE: changed\n", 20)["body"], source_comment_id=20)
        self.assertTrue(self.registry.upsert_goal(g1)[0])
        self.registry.set_state(g1.key, LifecycleState.DONE)
        self.assertTrue(self.registry.upsert_goal(g2)[0])
        self.assertEqual(self.registry.get(g1.key)["state"], "ASSIGNED")
        self.assertFalse(self.registry.upsert_goal(g2)[0])
        self.assertFalse(self.registry.upsert_goal(g1)[0])

    def test_reports_are_not_goals(self):
        base = "\nPROJECT: Worker Orchestrator\nCHAT: Report Worker\nREPOSITORY: nicofroeba16-cell/ha-grok-bridge\nBRANCH: fix/x\nGOAL_VERSION: v1\nDONE_CRITERIA:\n- x\n"
        for marker in ("WORKER_STATUS", "WORKER_DONE", "WORKER_STALLED", "INTEGRATION_CONFLICT"):
            self.assertIsNone(parse_goal_text(marker + base, source_comment_id=99), marker)

    def test_base_branch_guard(self):
        g = self.goal(branch="main")
        worker = ScriptedWorker([WorkerResult()])
        self.registry.upsert_goal(g)
        self.assertEqual(self.engine(worker).dispatch_goal(g), LifecycleState.BLOCKED)
        self.assertEqual(worker.calls, 0)
        self.assertIn("BASE_BRANCH_GUARD", self.registry.get(g.key)["blockers"])

    def test_done_and_waiting_remain_dormant_after_recovery(self):
        for state, chat in ((LifecycleState.DONE, "Done"), (LifecycleState.WAITING_FOR_USER, "Wait")):
            g = self.goal(chat=chat)
            self.registry.upsert_goal(g)
            self.registry.set_state(g.key, state)
        self.assertEqual(self.registry.recover_interrupted(), [])
        self.assertEqual(self.registry.get(self.goal(chat="Done").key)["state"], "DONE")
        self.assertEqual(self.registry.get(self.goal(chat="Wait").key)["state"], "WAITING_FOR_USER")


    def test_dirty_workspace_is_preserved_and_blocks_execution(self):
        g = self.goal(branch="fix/runtime")
        root = Path(self.tmp.name) / "workspaces"
        ws = root / sha256(g.key.encode()).hexdigest()[:16]
        ws.mkdir(parents=True)
        subprocess.run(["git", "init", "-b", g.branch], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", f"https://github.com/{g.repository}.git"], cwd=ws, check=True)
        marker = ws / "local-work.txt"
        marker.write_text("keep me")
        adapter = CommandWorkerAdapter("echo", workspace_root=root)
        result = adapter.execute(g, {}, dry_run=False)
        self.assertEqual(result.error, "WORKSPACE_DIRTY")
        self.assertEqual(marker.read_text(), "keep me")


    def test_worker_result_normalizes_non_mapping_evidence(self):
        script = Path(self.tmp.name) / "evidence_worker.py"
        script.write_text("import json; print(json.dumps({'evidence':['one','two'],'session_state':['bad']}))")
        result = CommandWorkerAdapter(f"{sys.executable} {script}").execute(self.goal(), {}, dry_run=True)
        self.assertEqual(result.evidence, {"details": ["one", "two"]})
        self.assertEqual(result.session_state, {})

    def test_engine_centrally_normalizes_malformed_worker_result(self):
        g = self.goal()
        worker = ScriptedWorker([{
            "head": 123,
            "ci": None,
            "blockers": "not-a-list",
            "evidence": ["unsafe-shape"],
            "ready": "yes",
        }])
        engine = self.engine(worker)
        self.registry.upsert_goal(g)
        state = engine.dispatch_goal(g)
        self.assertEqual(state, LifecycleState.BLOCKED)
        row = self.registry.get(g.key)
        self.assertEqual(row["ci_status"], "UNKNOWN")
        evidence = json.loads(row["completion_evidence"])
        self.assertEqual(evidence.get("reason"), "head is not a string")

    def test_worker_exception_isolated_and_lock_released(self):
        g = self.goal()

        class ExplodingWorker:
            def execute(self, goal, previous, *, dry_run):
                raise RuntimeError("worker failure")

        engine = self.engine(ExplodingWorker())
        self.registry.upsert_goal(g)
        self.assertEqual(engine.dispatch_goal(g), LifecycleState.BLOCKED)
        self.assertIn("WORKER_EXECUTION_FAILED", self.registry.get(g.key)["blockers"])
        self.assertEqual(self.registry.conn.execute("SELECT COUNT(*) FROM locks").fetchone()[0], 0)

    def test_reporting_exception_does_not_abort_dispatch(self):
        g = self.goal()
        worker = ScriptedWorker([WorkerResult(progress="reported")])

        def broken_reporter(*args):
            raise RuntimeError("GitHub unavailable")

        engine = Orchestrator(self.registry, worker, reporter=broken_reporter)
        self.registry.upsert_goal(g)
        self.assertEqual(engine.dispatch_goal(g), LifecycleState.RUNNING)
        self.assertEqual(worker.calls, 1)
        self.assertEqual(self.registry.get(g.key)["state"], "RUNNING")

    def test_worker_result_normalization_contract_variants(self):
        for evidence, expected in (
            ({"key": "value"}, {"key": "value"}),
            (["one"], {"details": ["one"]}),
            ("text", {"details": "text"}),
            (None, {}),
            ([], {"details": []}),
            ("", {}),
        ):
            with self.subTest(evidence=evidence):
                result = normalize_worker_result({
                    "head": "h", "ci": None, "evidence": evidence,
                    "verified_criteria": "criterion", "blockers": None,
                    "requested_actions": [], "changed_files": "file.py",
                    "ready": "TrUe", "session_state": ["wrong"], "ignored": object(),
                })
                self.assertEqual(result.evidence, expected)
                self.assertEqual(result.ci, "UNKNOWN")
                self.assertEqual(result.verified_criteria, ("criterion",))
                self.assertEqual(result.changed_files, ("file.py",))
                self.assertTrue(result.ready)
                self.assertEqual(result.session_state, {})
                self.assertEqual(result.error, "")

    def test_unsafe_worker_result_becomes_structured_invalid(self):
        for raw in (
            None, [], {"head": 1}, {"verified_criteria": ["ok", 2]},
            {"evidence": 3}, {"ready": "maybe"}, {"ci": object()},
        ):
            with self.subTest(raw=raw):
                result = normalize_worker_result(raw)
                self.assertEqual(result.error, "WORKER_RESULT_INVALID")
                self.assertEqual(result.blockers, ("WORKER_RESULT_INVALID",))
                self.assertIsInstance(result.evidence, dict)

    def test_each_worker_result_field_shape_failure_is_structured(self):
        valid = {
            "head": "head",
            "ci": "GREEN",
            "verified_criteria": ["criterion"],
            "blockers": ["blocker"],
            "requested_actions": ["inspect"],
            "evidence": {"tests": "green"},
            "progress": "progress",
            "next_step": "next",
            "ready": True,
            "error": "error",
            "changed_files": ["file.py"],
            "session_state": {"phase": "test"},
        }
        malformed = {
            "head": 1,
            "ci": [],
            "verified_criteria": ["ok", 2],
            "blockers": {"not": "a collection"},
            "requested_actions": [False],
            "evidence": 3,
            "progress": {},
            "next_step": ["not a string"],
            "ready": object(),
            "error": 4,
            "changed_files": [Path("file.py")],
        }
        for field, bad_value in malformed.items():
            with self.subTest(field=field):
                raw = dict(valid)
                raw[field] = bad_value
                result = normalize_worker_result(raw)
                self.assertEqual(result.error, "WORKER_RESULT_INVALID")
                self.assertEqual(result.blockers, ("WORKER_RESULT_INVALID",))
                self.assertTrue(result.evidence.get("reason"), field)

    def test_malformed_worker_result_instance_is_normalized(self):
        raw = WorkerResult()
        raw.head = 1
        result = normalize_worker_result(raw)
        self.assertEqual(result.error, "WORKER_RESULT_INVALID")
        self.assertEqual(result.blockers, ("WORKER_RESULT_INVALID",))
        self.assertIn("head", result.evidence["reason"])

    def test_report_format_failure_is_recorded_and_retryable(self):
        g = self.goal()
        self.registry.upsert_goal(g)
        engine = Orchestrator(self.registry, ScriptedWorker([WorkerResult()]), reporter=lambda *args: (_ for _ in ()).throw(ReportFormatError("bad format")))
        engine.dispatch_goal(g)
        events = self.registry.events(g.key)
        self.assertTrue(any(event["event_type"] == "REPORT_FORMAT_FAILED" for event in events))
        self.assertEqual(self.registry.get(g.key)["last_report_fingerprint"], "")

    def test_report_fingerprint_persistence_failure_does_not_abort_dispatch(self):
        g = self.goal()
        self.registry.upsert_goal(g)
        real_set_state = self.registry.set_state

        def fail_fingerprint_write(worker_key, state, **fields):
            if "last_report_fingerprint" in fields:
                raise OSError("state database temporarily unavailable")
            return real_set_state(worker_key, state, **fields)

        with patch.object(self.registry, "set_state", side_effect=fail_fingerprint_write):
            engine = self.engine(ScriptedWorker([WorkerResult(progress="persisted")]))
            self.assertEqual(engine.dispatch_goal(g), LifecycleState.RUNNING)
        self.assertEqual(self.registry.get(g.key)["state"], "RUNNING")
        self.assertEqual(self.registry.get(g.key)["last_report_fingerprint"], "")
        self.assertTrue(any(event["event_type"] == "REPORT_WRITE_FAILED" for event in self.registry.events(g.key)))

    def test_report_failure_event_failure_is_swallowed(self):
        g = self.goal()
        self.registry.upsert_goal(g)
        real_record_event = self.registry.record_event

        def fail_report_event(worker_key, version, event_type, payload):
            if event_type == "REPORT_WRITE_FAILED":
                raise OSError("event database temporarily unavailable")
            return real_record_event(worker_key, version, event_type, payload)

        with patch.object(self.registry, "record_event", side_effect=fail_report_event):
            engine = Orchestrator(
                self.registry,
                ScriptedWorker([WorkerResult(progress="still running")]),
                reporter=lambda *args: (_ for _ in ()).throw(RuntimeError("API unavailable")),
            )
            self.assertEqual(engine.dispatch_goal(g), LifecycleState.RUNNING)

    def test_reconcile_continues_after_worker_failure(self):
        first = self.goal(project="A", chat="One")
        second = self.goal(project="B", chat="Two", scope="other")
        self.registry.upsert_goal(first)
        self.registry.upsert_goal(second)

        class AThenB:
            def __init__(self):
                self.calls = []
            def execute(self, goal, previous, *, dry_run):
                self.calls.append(goal.project)
                if goal.project == "A":
                    raise RuntimeError("A failed")
                return WorkerResult(progress="B proceeded")

        worker = AThenB()
        engine = self.engine(worker)

        class FakeGitHub:
            def read_master_items(self, repo, issue):
                return []

        reconcile(engine, FakeGitHub(), "owner/repo", 1)
        self.assertEqual(worker.calls, ["A", "B"])
        self.assertEqual(self.registry.get(first.key)["state"], "BLOCKED")
        self.assertEqual(self.registry.get(second.key)["state"], "RUNNING")
        self.assertEqual(self.registry.conn.execute("SELECT COUNT(*) FROM locks").fetchone()[0], 0)

    def test_reconcile_survives_github_poll_failure(self):
        class BrokenGitHub:
            def read_master_items(self, repo, issue):
                raise OSError("offline")
        reconcile(self.engine(ScriptedWorker([WorkerResult()])), BrokenGitHub(), "owner/repo", 1)

    def test_reconcile_isolates_dispatch_boundary_exception(self):
        first = self.goal(project="A", chat="Boundary A")
        second = self.goal(project="B", chat="Boundary B", scope="other")
        self.registry.upsert_goal(first)
        self.registry.upsert_goal(second)

        class BoundaryEngine:
            def __init__(self, registry):
                self.registry = registry
                self.calls = []

            def ingest_items(self, items):
                return []

            def dispatch_goal(self, goal):
                self.calls.append(goal.project)
                if goal.project == "A":
                    raise RuntimeError("dispatch boundary failure")
                return LifecycleState.RUNNING

        engine = BoundaryEngine(self.registry)
        github = type("GitHub", (), {"read_master_items": lambda *_: []})()
        reconcile(engine, github, "owner/repo", 1)
        self.assertEqual(engine.calls, ["A", "B"])
        self.assertEqual(self.registry.get(first.key)["state"], "BLOCKED")
        self.assertIn("ORCHESTRATOR_INTERNAL_ERROR", self.registry.get(first.key)["blockers"])
        self.assertEqual(self.registry.get(second.key)["state"], "ASSIGNED")

    def test_reconcile_survives_ingest_boundary_failure(self):
        class BrokenEngine:
            def __init__(self, registry):
                self.registry = registry

            def ingest_items(self, items):
                raise RuntimeError("ingest failed")

        github = type("GitHub", (), {"read_master_items": lambda *_: []})()
        reconcile(BrokenEngine(self.registry), github, "owner/repo", 1)


if __name__ == "__main__":
    unittest.main()
