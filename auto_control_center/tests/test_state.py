import unittest

from auto_control_center.state import (
    annotate_registry_aliases,
    ci_class,
    evidence_for_worker,
    resolve_worker,
    wake_class,
)


class StateMappingTests(unittest.TestCase):
    def test_user_gate_precedes_blocker_and_ci(self):
        worker = resolve_worker(
            {
                "state": "DONE",
                "ci_status": "RED",
                "blockers": ["blocked"],
                "user_gate": ["approval"],
            }
        )
        self.assertEqual(worker["resolved_state"], "WAITING_FOR_USER")
        self.assertEqual(worker["resolution_basis"], ["user_gate"])

    def test_blocker_precedes_ci(self):
        worker = resolve_worker(
            {"state": "RUNNING", "ci_status": "RED", "blockers": ["dependency"], "user_gate": []}
        )
        self.assertEqual(worker["resolved_state"], "BLOCKED")
        self.assertEqual(worker["resolution_basis"], ["blockers"])

    def test_red_ci_blocks_unverified_done_state(self):
        worker = resolve_worker(
            {"state": "DONE", "ci_status": "FAILURE", "blockers": [], "user_gate": []}
        )
        self.assertEqual(worker["resolved_state"], "BLOCKED")
        self.assertEqual(worker["resolution_basis"], ["ci_failure"])

    def test_normal_state_remains_ledger_state(self):
        worker = resolve_worker(
            {"state": "RUNNING", "ci_status": "GREEN", "blockers": [], "user_gate": []}
        )
        self.assertEqual(worker["resolved_state"], "RUNNING")
        self.assertEqual(worker["state_source"], "orchestrator_ledger")

    def test_ci_classes_and_evidence_counts(self):
        self.assertEqual(ci_class("GREEN"), "green")
        self.assertEqual(ci_class("pending"), "running")
        self.assertEqual(ci_class("failure"), "red")
        self.assertEqual(ci_class(None), "unknown")
        evidence = evidence_for_worker(
            {
                "worker_key": "w",
                "verified_criteria": ["a"],
                "done_criteria": ["a", "b"],
                "ci_status": "GREEN",
                "resolved_state": "RUNNING",
            }
        )
        self.assertEqual(evidence["verified_criteria"], 1)
        self.assertEqual(evidence["done_criteria"], 2)
        self.assertEqual(evidence["source"], "orchestrator_ledger")

    def test_wake_uncertain_is_not_success_or_failure(self):
        self.assertEqual(wake_class("DELIVERED"), "green")
        self.assertEqual(wake_class("FAILED_PRE_SEND"), "red")
        self.assertEqual(wake_class("BLOCKED"), "red")
        self.assertEqual(wake_class("UNCERTAIN"), "uncertain")
        self.assertEqual(wake_class("CANCELLED_SUPERSEDED"), "muted")
        self.assertEqual(wake_class("new-state"), "unknown")

    def test_shared_target_aliases_are_preserved_without_canonical_inference(self):
        rows = annotate_registry_aliases(
            [
                {"worker_key": "legacy", "repository": "o/r", "branch": "b", "goal_version": "g"},
                {"worker_key": "current", "repository": "o/r", "branch": "b", "goal_version": "g"},
                {"worker_key": "other", "repository": "o/r", "branch": "b2", "goal_version": "g2"},
            ]
        )
        by_key = {row["worker_key"]: row for row in rows}
        self.assertEqual(set(by_key), {"legacy", "current", "other"})
        self.assertTrue(by_key["legacy"]["registry_shared_target"])
        self.assertTrue(by_key["current"]["registry_shared_target"])
        self.assertEqual(by_key["legacy"]["registry_peer_keys"], ["current"])
        self.assertEqual(by_key["current"]["registry_peer_keys"], ["legacy"])
        self.assertEqual(by_key["legacy"]["registry_identity_count"], 2)
        self.assertFalse(by_key["other"]["registry_shared_target"])
        self.assertNotIn("canonical", by_key["legacy"])


if __name__ == "__main__":
    unittest.main()
