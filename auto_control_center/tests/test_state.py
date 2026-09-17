import unittest

from auto_control_center.state import ci_class, evidence_for_worker, resolve_worker


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


if __name__ == "__main__":
    unittest.main()
