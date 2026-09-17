import json
import unittest

from auto_control_center.simulation import WORKER_STATES, build_simulation, simulation_matrix


class SimulationHarnessTests(unittest.TestCase):
    def test_mixed_simulation_exercises_all_required_worker_states(self):
        payload = build_simulation(worker_count=32, event_count=80, wake_count=48)
        states = {row["resolved_state"] for row in payload["workers"]}
        for required in {"RUNNING", "READY", "BLOCKED", "WAITING_FOR_USER", "DONE", "DORMANT", "ERROR"}:
            self.assertIn(required, states)
        self.assertEqual(len(WORKER_STATES), 8)

    def test_ci_failure_precedence_is_exercised_separately_from_error_state(self):
        payload = build_simulation(worker_count=32)
        ci_failed = [row for row in payload["workers"] if row.get("ci_status") == "FAILURE"]
        self.assertTrue(ci_failed)
        self.assertTrue(all(row["resolved_state"] == "BLOCKED" for row in ci_failed))
        self.assertTrue(any(row["resolved_state"] == "ERROR" for row in payload["workers"]))

    def test_wake_matrix_keeps_uncertain_failure_success_and_cancelled_distinct(self):
        payload = build_simulation(worker_count=12, wake_count=60)
        classes = {row["status_class"] for row in payload["wakes"]}
        self.assertTrue({"uncertain", "red", "green", "muted"}.issubset(classes))
        uncertain = [row for row in payload["wakes"] if row["status"] == "UNCERTAIN"]
        self.assertTrue(uncertain)
        self.assertTrue(all(row["status_class"] == "uncertain" for row in uncertain))

    def test_shared_targets_preserve_every_identity_without_canonical_inference(self):
        payload = build_simulation(worker_count=16)
        workers = payload["workers"]
        keys = [row["worker_key"] for row in workers]
        self.assertEqual(len(keys), len(set(keys)))
        shared = [row for row in workers if row["registry_shared_target"]]
        self.assertGreaterEqual(len(shared), 4)
        self.assertTrue(all(row["registry_identity_count"] >= 2 for row in shared))
        self.assertNotIn("canonical", json.dumps(payload).lower())

    def test_partial_empty_degraded_stale_and_large_variants_are_stable(self):
        matrix = simulation_matrix()
        self.assertEqual(set(matrix), {"mixed", "partial", "degraded", "stale", "empty", "large"})
        self.assertEqual(matrix["empty"]["workers"], [])
        self.assertFalse(matrix["degraded"]["health"]["orchestrator_db"]["readable"])
        self.assertGreaterEqual(len(matrix["large"]["workers"]), 180)
        self.assertGreaterEqual(len(matrix["large"]["events"]), 900)
        self.assertGreaterEqual(len(matrix["large"]["wakes"]), 500)
        self.assertGreater(len(matrix["large"]["master"]["children"]), 18)
        self.assertTrue(any(row.get("branch") is None for row in matrix["partial"]["workers"]))
        self.assertTrue(any(str(row.get("updated_at", "")).startswith("2026-08-01") for row in matrix["stale"]["workers"]))

    def test_simulation_redacts_representative_secret_material(self):
        serialized = json.dumps(simulation_matrix())
        self.assertNotIn("abcdefghijklmnop", serialized)
        self.assertNotIn("github_pat_", serialized)
        self.assertIn("[REDACTED]", serialized)
        self.assertNotIn("http://user:", serialized)

    def test_simulation_routes_are_minimized_not_destinations(self):
        payload = build_simulation(worker_count=20)
        serialized = json.dumps(payload["routes"])
        self.assertNotIn("http://", serialized)
        self.assertNotIn("https://", serialized)
        self.assertTrue(all(set(row) == {"worker_key", "bound", "destination_kind"} for row in payload["routes"]))

    def test_large_payload_counts_remain_truthful(self):
        payload = build_simulation(worker_count=180, event_count=900, wake_count=500, partial=True, stale=True)
        self.assertEqual(payload["stats"]["workers"], 180)
        self.assertEqual(len(payload["events"]), 900)
        self.assertEqual(len(payload["wakes"]), 500)
        self.assertGreater(len(payload["master"]["children"]), 18)
        self.assertGreater(payload["stats"]["wake_uncertain"], 0)


if __name__ == "__main__":
    unittest.main()
