from __future__ import annotations

import unittest

from worker_orchestrator.auto_policy import (
    STATUS_RESUME_POLICY_ID,
    is_status_checkpoint,
    policy_sync_payload,
    resolve_status_checkpoint,
)
from worker_orchestrator.models import LifecycleState


WORKER_KEY = "Projekt: Dashboards → Chat: AUTO - HA Dashboard Next iOS"


def goal_body(version: str) -> str:
    return "\n".join([
        "GOAL PROMPT",
        "PROJECT: Dashboards",
        "CHAT: AUTO - HA Dashboard Next iOS",
        f"GOAL_VERSION: {version}",
        "REPOSITORY: nicofroeba16-cell/HA-CONFIG",
        "BRANCH: auto/ha-dashboard-next-ios-motion-v1",
        "DONE_CRITERIA:",
        "- exact successor scope completed",
    ])


def row(version: str, state: str, source: int) -> dict:
    return {
        "worker_key": WORKER_KEY,
        "goal_version": version,
        "state": state,
        "source_comment_id": source,
    }


class AutoPolicyTests(unittest.TestCase):
    def test_status_tokens_are_checkpoint_only(self):
        for value in ("Status", "Status?", "stand", "Stand?"):
            with self.subTest(value=value):
                self.assertTrue(is_status_checkpoint(value))
        self.assertFalse(is_status_checkpoint("Go"))
        self.assertFalse(is_status_checkpoint("stop"))

    def test_running_checkpoint_reports_and_continues_without_duplicate_activation(self):
        items = [{"id": 100, "body": goal_body("v1")}]
        result = resolve_status_checkpoint(
            items,
            WORKER_KEY,
            worker_row=row("v1", LifecycleState.RUNNING, 100),
            active_equivalent=True,
        )
        self.assertEqual(result.state, LifecycleState.RUNNING)
        self.assertTrue(result.resume)
        self.assertTrue(result.duplicate_activation)
        self.assertEqual(result.action, "REPORT_AND_CONTINUE")

    def test_waiting_for_user_and_blocked_remain_gates(self):
        items = [{"id": 100, "body": goal_body("v1")}]
        for state in (LifecycleState.WAITING_FOR_USER, LifecycleState.BLOCKED):
            with self.subTest(state=state):
                result = resolve_status_checkpoint(
                    items, WORKER_KEY, worker_row=row("v1", state, 100)
                )
                self.assertFalse(result.resume)
                self.assertEqual(result.action, "REPORT_GATE")

    def test_done_ready_stalled_current_goal_remain_dormant(self):
        items = [{"id": 100, "body": goal_body("v1")}]
        for state in (LifecycleState.DONE, LifecycleState.READY, LifecycleState.STALLED):
            with self.subTest(state=state):
                result = resolve_status_checkpoint(
                    items, WORKER_KEY, worker_row=row("v1", state, 100)
                )
                self.assertFalse(result.resume)
                self.assertEqual(result.action, "REPORT_DORMANT")

    def test_no_goal_is_dormant_and_invents_no_work(self):
        result = resolve_status_checkpoint([], WORKER_KEY)
        self.assertEqual(result.state, "IDLE")
        self.assertFalse(result.resume)
        self.assertEqual(result.action, "DORMANT")

    def test_active_equivalent_ci_does_not_duplicate_work(self):
        items = [{"id": 100, "body": goal_body("v1")}]
        result = resolve_status_checkpoint(
            items,
            WORKER_KEY,
            worker_row=row("v1", LifecycleState.ASSIGNED, 100),
            active_equivalent=True,
        )
        self.assertTrue(result.resume)
        self.assertTrue(result.duplicate_activation)
        self.assertEqual(result.action, "REPORT_AND_CONTINUE")

    def test_interrupted_resumable_goal_reports_then_resumes(self):
        items = [{"id": 100, "body": goal_body("v1")}]
        result = resolve_status_checkpoint(
            items,
            WORKER_KEY,
            worker_row=row("v1", LifecycleState.ASSIGNED, 100),
        )
        self.assertTrue(result.resume)
        self.assertFalse(result.duplicate_activation)
        self.assertEqual(result.action, "REPORT_AND_RESUME")

    def test_dashboard_predecessor_terminal_never_suppresses_newer_successor(self):
        items = [
            {"id": 100, "body": goal_body("ha-dashboard-de-ios27-library-export-v1")},
            {"id": 200, "body": goal_body("ha-dashboard-de-ios27-library-placement-v2")},
        ]
        predecessor = row(
            "ha-dashboard-de-ios27-library-export-v1",
            LifecycleState.WAITING_FOR_USER,
            100,
        )
        wake = {
            "goal_version": "ha-dashboard-de-ios27-library-placement-v2",
            "status": "UNCERTAIN",
            "activation_source": "wake_uncertain",
            "activation_confirmed": False,
        }
        result = resolve_status_checkpoint(
            items,
            WORKER_KEY,
            worker_row=predecessor,
            wake_state=wake,
        )
        self.assertEqual(
            result.goal_version,
            "ha-dashboard-de-ios27-library-placement-v2",
        )
        self.assertEqual(result.state, LifecycleState.RUNNING)
        self.assertTrue(result.resume)
        self.assertFalse(result.duplicate_activation)
        self.assertEqual(result.activation_source, "wake_uncertain")
        self.assertFalse(result.activation_confirmed)
        self.assertEqual(result.source_comment_id, 200)

    def test_policy_sync_payload_is_shared_and_explicit(self):
        payload = policy_sync_payload(WORKER_KEY)
        self.assertIn(f"POLICY_ID: {STATUS_RESUME_POLICY_ID}", payload)
        self.assertIn("STATUS_CHECKPOINT_RULE:", payload)
        self.assertIn("STATUS_CURRENT_GOAL_RULE:", payload)
        self.assertIn(WORKER_KEY, payload)


if __name__ == "__main__":
    unittest.main()
