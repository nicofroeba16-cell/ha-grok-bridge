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


class ActivationAndResumeContractTests(unittest.TestCase):
    def _worker(self, state="ASSIGNED"):
        return {
            "worker_key": "W",
            "goal_version": "goal-v3",
            "state": state,
            "resolved_state": state,
            "resolution_basis": ["ledger_state"],
            "state_source": "orchestrator_ledger",
            "updated_at": "2026-09-18T10:00:00+00:00",
        }

    def test_current_goal_uncertain_is_running_unconfirmed(self):
        from auto_control_center.state import apply_activation_provenance
        row = apply_activation_provenance(
            [self._worker()],
            [{"route_key": "W", "goal_version": "goal-v3", "status": "UNCERTAIN", "updated_at": 2000000000.0}],
        )[0]
        self.assertEqual(row["resolved_state"], "RUNNING")
        self.assertEqual(row["activation_source"], "wake_uncertain")
        self.assertFalse(row["activation_confirmed"])

    def test_verified_delivery_is_running_confirmed(self):
        from auto_control_center.state import apply_activation_provenance
        row = apply_activation_provenance(
            [self._worker()],
            [{"route_key": "W", "goal_version": "goal-v3", "status": "DELIVERED", "updated_at": 2000000000.0}],
        )[0]
        self.assertEqual(row["resolved_state"], "RUNNING")
        self.assertEqual(row["activation_source"], "wake_verified")
        self.assertTrue(row["activation_confirmed"])

    def test_failed_pre_send_and_wrong_goal_never_promote(self):
        from auto_control_center.state import apply_activation_provenance
        for wake in (
            {"route_key": "W", "goal_version": "goal-v3", "status": "FAILED_PRE_SEND", "updated_at": 2000000000.0},
            {"route_key": "W", "goal_version": "old-goal", "status": "UNCERTAIN", "updated_at": 2000000000.0},
        ):
            row = apply_activation_provenance([self._worker()], [wake])[0]
            self.assertEqual(row["resolved_state"], "ASSIGNED")
            self.assertIsNone(row["activation_source"])

    def test_terminal_worker_state_is_protected_from_wake(self):
        from auto_control_center.state import apply_activation_provenance
        row = apply_activation_provenance(
            [self._worker("READY")],
            [{"route_key": "W", "goal_version": "goal-v3", "status": "UNCERTAIN", "updated_at": 2000000000.0}],
        )[0]
        self.assertEqual(row["resolved_state"], "READY")
        self.assertIsNone(row["activation_source"])

    def test_worker_report_running_is_confirmed(self):
        from auto_control_center.state import apply_activation_provenance
        row = apply_activation_provenance([self._worker("RUNNING")], [])[0]
        self.assertEqual(row["activation_source"], "worker_report")
        self.assertTrue(row["activation_confirmed"])

    def test_checkpoint_resume_semantics_preserve_gates_and_no_duplicate_running(self):
        from auto_control_center.state import annotate_checkpoint_semantics
        running = annotate_checkpoint_semantics({"resolved_state": "RUNNING", "activation_source": "worker_report"})
        uncertain = annotate_checkpoint_semantics({"resolved_state": "RUNNING", "activation_source": "wake_uncertain"})
        gated = annotate_checkpoint_semantics({"resolved_state": "WAITING_FOR_USER"})
        done = annotate_checkpoint_semantics({"resolved_state": "DONE"})
        self.assertEqual((running["checkpoint_action"], running["checkpoint_resume"], running["checkpoint_duplicate_activation"]), ("REPORT_AND_CONTINUE", True, True))
        self.assertEqual((uncertain["checkpoint_action"], uncertain["checkpoint_resume"]), ("REPORT_AND_RESUME", True))
        self.assertEqual((gated["checkpoint_action"], gated["checkpoint_resume"]), ("REPORT_GATE", False))
        self.assertEqual((done["checkpoint_action"], done["checkpoint_resume"]), ("REPORT_DORMANT", False))

    def test_registry_marks_active_legacy_and_superseded_without_collapsing_rows(self):
        from auto_control_center.state import annotate_registry_aliases
        rows = annotate_registry_aliases(
            [
                {"worker_key": "active", "repository": "o/r", "branch": "b", "goal_version": "g", "source_comment_id": 20},
                {"worker_key": "legacy", "repository": "o/r", "branch": "b", "goal_version": "g", "source_comment_id": 20},
                {"worker_key": "old", "repository": "o/r", "branch": "b", "goal_version": "g", "source_comment_id": 10},
            ],
            {"active"},
        )
        by_key = {row["worker_key"]: row for row in rows}
        self.assertEqual(by_key["active"]["registry_identity_state"], "ACTIVE")
        self.assertEqual(by_key["legacy"]["registry_identity_state"], "LEGACY_UNROUTED")
        self.assertEqual(by_key["old"]["registry_identity_state"], "SUPERSEDED")
        self.assertEqual(len(rows), 3)


if __name__ == "__main__":
    unittest.main()
