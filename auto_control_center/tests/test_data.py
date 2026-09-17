import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from auto_control_center import data


class ReadOnlyDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.orch = root / "orch.sqlite3"
        self.wake = root / "wake.sqlite3"
        self.routes = root / "routes.json"
        self._seed_orchestrator()
        self._seed_wake()
        self.routes.write_text(
            json.dumps(
                {
                    "__master__": {"url": "https://user:password@example.invalid/master?token=secret123"},
                    "Worker": {"title": "Secret destination"},
                }
            ),
            encoding="utf-8",
        )
        self.old = (data.ORCHESTRATOR_DB, data.BROWSER_WAKE_DB, data.BROWSER_ROUTES, data.SERVICES)
        data.ORCHESTRATOR_DB = self.orch
        data.BROWSER_WAKE_DB = self.wake
        data.BROWSER_ROUTES = self.routes
        data.SERVICES = ()

    def tearDown(self):
        data.ORCHESTRATOR_DB, data.BROWSER_WAKE_DB, data.BROWSER_ROUTES, data.SERVICES = self.old
        self.tmp.cleanup()

    def _seed_orchestrator(self):
        conn = sqlite3.connect(self.orch)
        conn.executescript(
            """
            CREATE TABLE workers(worker_key TEXT,project TEXT,chat TEXT,repository TEXT,branch TEXT,workstream_issue INTEGER,goal_version TEXT,state TEXT,last_head TEXT,ci_status TEXT,blockers TEXT,user_gate TEXT,last_progress TEXT,verified_criteria TEXT,done_criteria TEXT,updated_at TEXT);
            CREATE TABLE events(id INTEGER,worker_key TEXT,goal_version TEXT,event_type TEXT,payload TEXT,created_at TEXT);
            CREATE TABLE master_requests(request_id TEXT,version TEXT,request_text TEXT,done_criteria TEXT,state TEXT,source_comment_id INTEGER,updated_at TEXT);
            CREATE TABLE master_children(request_id TEXT,child_id TEXT,worker_key TEXT,dependencies TEXT,state TEXT,blocker TEXT,dispatched INTEGER);
            """
        )
        conn.execute(
            "INSERT INTO workers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "Worker",
                "P",
                "Chat",
                "o/r",
                "branch",
                1,
                "v1",
                "DONE",
                "abcdef1234567890",
                "RED",
                "[]",
                '["explicit approval"]',
                "Worker says complete token=super-secret-value",
                '["adapter"]',
                '["adapter","api"]',
                "2026-01-01",
            ),
        )
        conn.execute(
            "INSERT INTO master_requests VALUES(?,?,?,?,?,?,?)",
            ("r1", "v1", "Finish app", '["done"]', "RUNNING", 10, "2026-01-01"),
        )
        conn.execute(
            "INSERT INTO master_children VALUES(?,?,?,?,?,?,?)",
            ("r1", "c1", "Worker", '["bootstrap"]', "RUNNING", "", 1),
        )
        conn.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?)",
            (1, "Worker", "v1", "STARTED", '{"authorization":"Bearer abcdefghijklmnop","note":"github_pat_abcdefghijklmnopqrstuv"}', "2026-01-01"),
        )
        conn.commit()
        conn.close()

    def _seed_wake(self):
        conn = sqlite3.connect(self.wake)
        conn.executescript(
            """
            CREATE TABLE browser_wake_delivery(message_id TEXT,route_key TEXT,status TEXT,attempts INTEGER,last_error TEXT,updated_at REAL);
            CREATE TABLE browser_wake_pending_worker(message_id TEXT,route_key TEXT,destination TEXT,payload TEXT);
            CREATE TABLE browser_wake_pending_master(id INTEGER,event_ids TEXT,first_seen REAL,max_event_id INTEGER);
            """
        )
        conn.execute(
            "INSERT INTO browser_wake_delivery VALUES(?,?,?,?,?,?)",
            ("m1", "Worker", "UNCERTAIN", 2, "authorization=Bearer abcdefghijklmnop", 1.0),
        )
        conn.commit()
        conn.close()

    def test_dashboard_reads_without_mutating_databases(self):
        before_orch = self.orch.read_bytes()
        before_wake = self.wake.read_bytes()
        payload = data.dashboard()
        self.assertEqual(before_orch, self.orch.read_bytes())
        self.assertEqual(before_wake, self.wake.read_bytes())
        self.assertEqual(payload["workers"][0]["resolved_state"], "WAITING_FOR_USER")
        self.assertEqual(payload["master"]["request_id"], "r1")
        self.assertEqual(payload["wakes"][0]["status"], "UNCERTAIN")
        self.assertEqual(payload["stats"]["ci_red"], 1)

    def test_secret_material_is_redacted_from_public_rows(self):
        payload = data.dashboard()
        serialized = json.dumps(payload)
        self.assertNotIn("super-secret-value", serialized)
        self.assertNotIn("abcdefghijklmnop", serialized)
        self.assertNotIn("github_pat_", serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_routes_hide_destination_values(self):
        routes = data.route_summary()
        serialized = json.dumps(routes)
        self.assertEqual(len(routes), 2)
        self.assertNotIn("example.invalid", serialized)
        self.assertNotIn("Secret destination", serialized)
        self.assertEqual({r["destination_kind"] for r in routes}, {"url", "title"})

    def test_source_health_does_not_expose_paths(self):
        health = data.source_health()
        self.assertNotIn(str(self.orch), json.dumps(health))
        self.assertTrue(health["orchestrator_db"]["readable"])

    def test_event_limit_is_clamped(self):
        self.assertEqual(len(data.orchestrator_events(0)), 1)
        self.assertLessEqual(len(data.orchestrator_events(9999)), 500)


if __name__ == "__main__":
    unittest.main()
