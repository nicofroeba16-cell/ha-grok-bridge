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

from worker_orchestrator.cli import main as cli_main
from worker_orchestrator.engine import Orchestrator, format_report
from worker_orchestrator.goals import parse_goal_text, parse_goals
from worker_orchestrator.models import Goal, LifecycleState, WorkerResult
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


if __name__ == "__main__":
    unittest.main()
