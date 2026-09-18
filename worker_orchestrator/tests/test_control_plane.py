import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from worker_orchestrator.control_plane import (
    ChatRoute,
    MasterControlPlane,
    MasterRequestError,
    RouteRegistry,
    classify_registry_rows,
    parse_master_request,
)


def request_body(*, version="v1", second_depends=True, suffix=""):
    graph = [
        {
            "id": "source",
            "project": "Example",
            "chat": "Source Worker",
            "repository": "nicofroeba16-cell/ha-grok-bridge",
            "branch": "feat/source",
            "workstream_issue": 10,
            "files": ["source/"],
            "scope": "source",
            "done_criteria": ["source verified"],
        },
        {
            "id": "consumer",
            "project": "Example",
            "chat": "Consumer Worker",
            "repository": "nicofroeba16-cell/ha-grok-bridge",
            "branch": "feat/consumer",
            "workstream_issue": 11,
            "files": ["consumer/"],
            "scope": "consumer",
            "depends_on": ["source"] if second_depends else [],
            "done_criteria": ["consumer verified"],
        },
    ]
    return "\n".join([
        "MASTER_REQUEST",
        "REQUEST_ID: synthetic-e2e",
        f"GOAL_VERSION: {version}",
        f"REQUEST: Verify deterministic orchestration{suffix}",
        "GLOBAL_DONE_CRITERIA:",
        "- every child is done",
        "- routing is exact",
        "WORK_GRAPH_JSON:",
        json.dumps(graph, indent=2),
    ])


class ControlPlaneHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.dispatched = []
        self.reports = []
        self.routes = RouteRegistry({
            "Projekt: Example → Chat: Source Worker": ChatRoute(
                "Projekt: Example → Chat: Source Worker", "github_master", "issue:3"
            ),
            "Projekt: Example → Chat: Consumer Worker": ChatRoute(
                "Projekt: Example → Chat: Consumer Worker", "github_master", "issue:3"
            ),
        })
        self.control = MasterControlPlane(
            self.conn,
            self.routes,
            github_dispatch=lambda destination, body: self.dispatched.append(body),
            reporter=self.reports.append,
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def item(self, body=None, source=100):
        return {"id": source, "body": body or request_body()}

    def row(self, key, state, goal_version):
        return {"worker_key": key, "state": state, "goal_version": goal_version}

    def test_parser_builds_deterministic_acyclic_graph(self):
        first = parse_master_request(request_body(), 100)
        second = parse_master_request(request_body(), 100)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual([c.child_id for c in first.children], ["source", "consumer"])
        self.assertEqual(first.children[1].dependencies, ("source",))

    def test_parser_rejects_cycle(self):
        value = request_body().replace('"depends_on": [\n      "source"\n    ]', '"depends_on": [\n      "consumer"\n    ]', 1)
        # Make the source depend on the consumer while consumer still depends on source.
        graph_marker = "WORK_GRAPH_JSON:\n"
        prefix, raw = value.split(graph_marker, 1)
        graph = json.loads(raw)
        graph[0]["depends_on"] = ["consumer"]
        with self.assertRaises(MasterRequestError):
            parse_master_request(prefix + graph_marker + json.dumps(graph), 100)

    def test_plan_dispatch_dependency_reconcile_and_done_aggregation(self):
        state = self.control.reconcile([self.item()], [])
        self.assertEqual(state, "RUNNING")
        self.assertEqual(len(self.dispatched), 1)
        self.assertIn("CHAT: Source Worker", self.dispatched[0])
        self.assertNotIn("CHAT: Consumer Worker", self.dispatched[0])

        source = "Projekt: Example → Chat: Source Worker"
        consumer = "Projekt: Example → Chat: Consumer Worker"
        state = self.control.reconcile([self.item()], [self.row(source, "DONE", "v1-source")])
        self.assertEqual(state, "RUNNING")
        self.assertEqual(len(self.dispatched), 2)
        self.assertIn("CHAT: Consumer Worker", self.dispatched[1])

        state = self.control.reconcile(
            [self.item()], [
                self.row(source, "DONE", "v1-source"),
                self.row(consumer, "DONE", "v1-consumer"),
            ]
        )
        self.assertEqual(state, "DONE")
        self.assertTrue(self.reports[-1].startswith("MASTER_DONE"))

    def test_dispatch_is_idempotent_across_reconciles(self):
        self.control.reconcile([self.item()], [])
        self.control.reconcile([self.item()], [])
        self.control.reconcile([self.item()], [])
        self.assertEqual(len(self.dispatched), 1)

    def test_unbound_route_fails_closed(self):
        control = MasterControlPlane(self.conn, RouteRegistry(), reporter=self.reports.append)
        self.assertEqual(control.reconcile([self.item()], []), "BLOCKED")
        row = self.conn.execute(
            "SELECT blocker FROM master_children WHERE request_id=? AND child_id=?",
            ("synthetic-e2e", "source"),
        ).fetchone()
        self.assertEqual(row[0], "CHAT_ROUTE_UNBOUND")

    def test_duplicate_route_keys_fail_closed(self):
        key = "Projekt: Example → Chat: Source Worker"
        raw = (
            "{"
            f'"{key}":{{"transport":"chat_relay","destination":"chat-route:a"}},'
            f'"{key}":{{"transport":"chat_relay","destination":"chat-route:b"}}'
            "}"
        )
        with self.assertRaises(MasterRequestError):
            RouteRegistry.from_json(raw)

    def test_registry_classifier_prefers_current_and_marks_legacy_duplicate_rows(self):
        key = "Projekt: Example → Chat: Source Worker"
        rows = [
            self.row(key, "DONE", "older-goal"),
            self.row(key, "DONE", "v1-source"),
            self.row(key, "DONE", "v1-source"),
        ]
        active, evidence = classify_registry_rows(rows, key, "v1-source")
        self.assertIsNotNone(active)
        self.assertEqual(active["goal_version"], "v1-source")
        self.assertEqual(evidence["classification"], "ACTIVE_WITH_DUPLICATES")
        self.assertEqual(evidence["legacy_rows"], 1)
        self.assertEqual(evidence["duplicate_rows"], 1)

    def test_conflicting_duplicate_current_rows_block_without_redispatch(self):
        key = "Projekt: Example → Chat: Source Worker"
        rows = [
            self.row(key, "DONE", "v1-source"),
            self.row(key, "RUNNING", "v1-source"),
        ]
        state = self.control.reconcile([self.item()], rows)
        self.assertEqual(state, "BLOCKED")
        self.assertEqual(self.dispatched, [])
        blocker = self.conn.execute(
            "SELECT blocker FROM master_children WHERE request_id=? AND child_id=?",
            ("synthetic-e2e", "source"),
        ).fetchone()[0]
        self.assertEqual(blocker, "REGISTRY_DUPLICATE_AMBIGUOUS")

    def test_waiting_for_user_is_global_gate(self):
        source = "Projekt: Example → Chat: Source Worker"
        self.assertEqual(
            self.control.reconcile(
                [self.item()], [self.row(source, "WAITING_FOR_USER", "v1-source")]
            ),
            "WAITING_FOR_USER",
        )
        self.assertEqual(self.dispatched, [])

    def test_stale_done_from_previous_goal_does_not_close_new_child(self):
        source = "Projekt: Example → Chat: Source Worker"
        state = self.control.reconcile(
            [self.item()], [self.row(source, "DONE", "older-goal")]
        )
        self.assertEqual(state, "RUNNING")
        self.assertEqual(len(self.dispatched), 1)

    def test_material_request_drift_reopens_and_redispatches(self):
        self.control.reconcile([self.item()], [])
        changed = request_body(version="v2", suffix=" after drift")
        self.control.reconcile([self.item(changed, source=101)], [])
        self.assertEqual(len(self.dispatched), 2)
        self.assertIn("GOAL_VERSION: v2-source", self.dispatched[-1])

    def test_outbox_route_is_exact_and_does_not_claim_ui_delivery(self):
        outbox = Path(self.tmp.name) / "master-outbox.jsonl"
        route = RouteRegistry({
            "Projekt: Example → Chat: Source Worker": ChatRoute(
                "Projekt: Example → Chat: Source Worker", "outbox", "chat-route-source"
            )
        })
        control = MasterControlPlane(self.conn, route, outbox_path=outbox)
        one_child = request_body(second_depends=False)
        marker = "WORK_GRAPH_JSON:\n"
        prefix, raw = one_child.split(marker, 1)
        graph = json.loads(raw)[:1]
        control.reconcile([self.item(prefix + marker + json.dumps(graph))], [])
        records = [json.loads(line) for line in outbox.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["destination"], "chat-route-source")
        self.assertEqual(records[0]["delivery_semantics"], "relay_pending")

    def test_chat_relay_dispatch_is_exact_and_idempotent(self):
        calls = []
        route = RouteRegistry({
            "Projekt: Example → Chat: Source Worker": ChatRoute(
                "Projekt: Example → Chat: Source Worker",
                "chat_relay",
                "chat-route:example/source-worker",
            )
        })
        control = MasterControlPlane(
            self.conn,
            route,
            relay_dispatch=lambda message_id, destination, payload: calls.append(
                (message_id, destination, payload)
            ),
        )
        one_child = request_body(second_depends=False)
        graph_marker = "WORK_GRAPH_JSON:\n"
        prefix, raw = one_child.split(graph_marker, 1)
        graph = json.loads(raw)[:1]
        item = self.item(prefix + graph_marker + json.dumps(graph))
        control.reconcile([item], [])
        control.reconcile([item], [])
        control.reconcile([item], [])
        self.assertEqual(len(calls), 1)
        message_id, destination, payload = calls[0]
        self.assertEqual(message_id, "synthetic-e2e:v1:source")
        self.assertEqual(destination, "chat-route:example/source-worker")
        self.assertIn("CHAT: Source Worker", payload)

    def test_chat_relay_route_requires_logical_route_id(self):
        raw = json.dumps({
            "Projekt: Example → Chat: Source Worker": {
                "transport": "chat_relay",
                "destination": "plain-chat-title",
            }
        })
        with self.assertRaises(MasterRequestError):
            RouteRegistry.from_json(raw)

    def test_production_route_registry_contains_visible_chats(self):
        route_file = Path(__file__).parents[1] / "config" / "chat-routes.json"
        routes = RouteRegistry.from_json(route_file.read_text(encoding="utf-8"))
        self.assertEqual(len(routes.routes), 18)
        expected = {
            "Projekt: HA Simulation → Chat: HA Testumgebung planen",
            "Projekt: Drucker → Chat: Brother Companion planen",
            "Projekt: Dashboards → Chat: Fire TV Medienkarte erweitern",
            "Projekt: Mähroboter → Chat: Status Mähroboter Read only",
            "Projekt: Health → Chat: Schlüsselinventur planen",
            "Projekt: IOS App → Chat: iOS Admin Chat Status",
            "Projekt: Run optimization → Chat: Stand zusammenfassen",
        }
        self.assertTrue(expected.issubset(routes.routes))
        self.assertTrue(
            all(route.transport == "chat_relay" for route in routes.routes.values())
        )

    def test_unknown_direct_chat_transport_is_rejected(self):
        raw = json.dumps({
            "Projekt: Example → Chat: Source Worker": {
                "transport": "chatgpt_ui", "destination": "some-chat"
            }
        })
        with self.assertRaises(MasterRequestError):
            RouteRegistry.from_json(raw)

    def test_child_goal_prompt_carries_shared_status_resume_policy(self):
        request = parse_master_request(request_body(), 123)
        self.assertIsNotNone(request)
        prompt = request.children[0].prompt(request.version)
        self.assertIn("AUTO_POLICY_ID: auto-chat-status-report-and-resume-v1", prompt)
        self.assertIn("STATUS_CHECKPOINT_RULE:", prompt)
        self.assertIn("STATUS_CURRENT_GOAL_RULE:", prompt)
        self.assertIn("STATUS_GATE_RULE:", prompt)

    def test_invalid_latest_request_blocks_once_without_status_spam(self):
        invalid = {"id": 200, "body": "MASTER_REQUEST\nREQUEST_ID: broken"}
        self.assertEqual(self.control.reconcile([invalid], []), "BLOCKED")
        self.assertEqual(self.control.reconcile([invalid], []), "BLOCKED")
        self.assertEqual(len(self.reports), 1)
        self.assertIn("MASTER_REQUEST_INVALID", self.reports[0])


if __name__ == "__main__":
    unittest.main()
