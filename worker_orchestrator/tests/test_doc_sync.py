from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from worker_orchestrator.doc_sync import (
    DOC_SYNC_BLOCKED, DOC_SYNC_MASTER_PENDING, DOC_SYNC_VERIFIED,
    DocumentationSyncOutbox,
)
from worker_orchestrator.engine import Orchestrator, format_report, report_fingerprint
from worker_orchestrator.models import Goal, LifecycleState, WorkerResult
from worker_orchestrator.store import Registry


class FakeGitHub:
    def __init__(self):
        self.items = {}
        self.posts = []
        self.fail = set()
        self.next_id = 100

    def read(self, repo, issue):
        return list(self.items.get((repo, issue), []))

    def post(self, repo, issue, body):
        self.posts.append((repo, issue, body))
        if (repo, issue) in self.fail:
            raise OSError(f"blocked {repo}#{issue}")
        self.next_id += 1
        self.items.setdefault((repo, issue), []).append({"id": self.next_id, "body": body})


class NoWorker:
    def __init__(self, result=None):
        self.result = result or WorkerResult()
        self.calls = 0
    def execute(self, goal, previous, *, dry_run):
        self.calls += 1
        return self.result


class DocSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.sqlite3"
        self.registry = Registry(self.db)
        self.gh = FakeGitHub()
        self.outbox = DocumentationSyncOutbox(
            self.registry.conn,
            post_comment=self.gh.post,
            read_items=self.gh.read,
            master_repo="owner/master",
            master_issue=3,
            now=lambda: 1000.0,
        )
        self.goal = Goal(
            project="Auto Chat", chat="AUTO - Test", repository="owner/repo",
            branch="auto/test", prompt="test", done_criteria=("green",),
            workstream_issue=45, explicit_version="goal-v2",
            files=("worker_orchestrator/**",), scope="test",
        )
        self.registry.upsert_goal(self.goal)

    def tearDown(self):
        self.registry.close()
        self.tmp.cleanup()

    def queue(self, state="RUNNING", evidence=None):
        payload = {
            "state": state, "head": "abc", "ci": "GREEN", "done": "0/1",
            "blockers": [], "user_action_required": [], "last_progress": "p",
            "next": "n", "evidence": evidence or {"revision": 1},
        }
        fp = report_fingerprint("WORKER_STATUS", payload)
        payload["fingerprint"] = fp
        body = format_report("WORKER_STATUS", self.goal, payload)
        key = self.outbox.queue(
            worker_key=self.goal.key, goal_version=self.goal.version,
            desired_state=state, kind="WORKER_STATUS", fingerprint=fp, body=body,
            workstream_repo=self.goal.repository, workstream_issue=45,
        )
        return key, fp

    def test_two_phase_success_verifies_workstream_before_master(self):
        key, _ = self.queue()
        self.assertEqual(self.outbox.process(key), DOC_SYNC_VERIFIED)
        self.assertEqual([(r, i) for r, i, _ in self.gh.posts], [("owner/repo", 45), ("owner/master", 3)])
        row = self.outbox.get(key)
        self.assertEqual(row["workstream_verified"], 1)
        self.assertEqual(row["master_verified"], 1)

    def test_workstream_failure_blocks_without_master_write(self):
        self.gh.fail.add(("owner/repo", 45))
        key, _ = self.queue()
        self.assertEqual(self.outbox.process(key), DOC_SYNC_BLOCKED)
        self.assertEqual([(r, i) for r, i, _ in self.gh.posts], [("owner/repo", 45)])
        self.assertEqual(self.outbox.get(key)["master_verified"], 0)

    def test_master_failure_preserves_workstream_and_retries_mirror_only(self):
        self.gh.fail.add(("owner/master", 3))
        key, _ = self.queue()
        self.assertEqual(self.outbox.process(key), DOC_SYNC_BLOCKED)
        row = self.outbox.get(key)
        self.assertEqual(row["workstream_verified"], 1)
        before = len([1 for r, i, _ in self.gh.posts if (r, i) == ("owner/repo", 45)])
        self.gh.fail.clear()
        self.assertEqual(self.outbox.process(key), DOC_SYNC_VERIFIED)
        after = len([1 for r, i, _ in self.gh.posts if (r, i) == ("owner/repo", 45)])
        self.assertEqual(before, after)
        self.assertEqual(len([1 for r, i, _ in self.gh.posts if (r, i) == ("owner/master", 3)]), 2)

    def test_restart_between_phases_resumes_master_only(self):
        key, _ = self.queue()
        self.outbox.force_workstream_verified(key, 777)
        restarted = DocumentationSyncOutbox(
            self.registry.conn, post_comment=self.gh.post, read_items=self.gh.read,
            master_repo="owner/master", master_issue=3, now=lambda: 2000.0,
        )
        self.assertEqual(restarted.state(restarted.get(key)), DOC_SYNC_MASTER_PENDING)
        self.assertEqual(restarted.process(key), DOC_SYNC_VERIFIED)
        self.assertEqual([(r, i) for r, i, _ in self.gh.posts], [("owner/master", 3)])

    def test_duplicate_queue_is_semantically_idempotent(self):
        key1, _ = self.queue()
        key2, _ = self.queue()
        self.assertEqual(key1, key2)
        self.assertEqual(self.registry.conn.execute("SELECT COUNT(*) FROM doc_sync_outbox").fetchone()[0], 1)
        self.assertEqual(self.outbox.process(key1), DOC_SYNC_VERIFIED)
        self.assertEqual(self.outbox.process(key2), DOC_SYNC_VERIFIED)
        self.assertEqual(len(self.gh.posts), 2)

    def test_evidence_revision_changes_idempotence_key(self):
        key1, _ = self.queue(evidence={"revision": 1})
        key2, _ = self.queue(evidence={"revision": 2})
        self.assertNotEqual(key1, key2)

    def test_terminal_done_is_not_exposed_until_doc_sync_verified(self):
        self.gh.fail.add(("owner/master", 3))
        worker = NoWorker(WorkerResult(
            head="abc", ci="GREEN", verified_criteria=("green",),
            evidence={"tests": "ok"}, progress="done",
        ))
        engine = Orchestrator(self.registry, worker, doc_sync=self.outbox)
        self.assertEqual(engine.dispatch_goal(self.goal), LifecycleState.BLOCKED)
        row = self.registry.get(self.goal.key)
        self.assertNotEqual(row["state"], LifecycleState.DONE)
        self.assertEqual(row["doc_sync_state"], DOC_SYNC_BLOCKED)
        self.gh.fail.clear()
        engine.reconcile_documentation([], {}, ("owner/master", 3))
        row = self.registry.get(self.goal.key)
        self.assertEqual(row["state"], LifecycleState.DONE)
        self.assertEqual(row["doc_sync_state"], DOC_SYNC_VERIFIED)
        self.assertEqual(worker.calls, 1)

    def test_stale_local_status_repairs_documentation_without_product_rerun(self):
        worker = NoWorker()
        engine = Orchestrator(self.registry, worker, doc_sync=self.outbox)
        self.registry.set_state(
            self.goal.key, LifecycleState.RUNNING, last_head="new-head",
            last_progress="newer local progress", last_report_fingerprint="stale",
        )
        repaired = engine.reconcile_documentation([], {}, ("owner/master", 3))
        self.assertEqual(repaired, 1)
        self.assertEqual(worker.calls, 0)
        self.assertEqual(self.registry.get(self.goal.key)["doc_sync_state"], DOC_SYNC_VERIFIED)


if __name__ == "__main__":
    unittest.main()
