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
        self.routes.write_text(json.dumps({"__master__": {"url": "https://example.invalid/master"}, "Worker": {"url": "https://example.invalid/worker"}}), encoding="utf-8")
        self.old = (data.ORCHESTRATOR_DB, data.BROWSER_WAKE_DB, data.BROWSER_ROUTES)
        data.ORCHESTRATOR_DB = self.orch
        data.BROWSER_WAKE_DB = self.wake
        data.BROWSER_ROUTES = self.routes

    def tearDown(self):
        data.ORCHESTRATOR_DB, data.BROWSER_WAKE_DB, data.BROWSER_ROUTES = self.old
        self.tmp.cleanup()

    def _seed_orchestrator(self):
        conn = sqlite3.connect(self.orch)
        conn.executescript("""
        CREATE TABLE workers(worker_key TEXT,project TEXT,chat TEXT,repository TEXT,branch TEXT,workstream_issue INTEGER,goal_version TEXT,state TEXT,last_head TEXT,ci_status TEXT,blockers TEXT,user_gate TEXT,last_progress TEXT,verified_criteria TEXT,done_criteria TEXT,updated_at TEXT);
        CREATE TABLE events(id INTEGER,worker_key TEXT,goal_version TEXT,event_type TEXT,payload TEXT,created_at TEXT);
        CREATE TABLE master_requests(request_id TEXT,version TEXT,request_text TEXT,done_criteria TEXT,state TEXT,source_comment_id INTEGER,updated_at TEXT);
        CREATE TABLE master_children(request_id TEXT,child_id TEXT,worker_key TEXT,dependencies TEXT,state TEXT,blocker TEXT,dispatched INTEGER);
        """)
        conn.execute("INSERT INTO workers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("Worker","P","Chat","o/r","branch",1,"v1","RUNNING","abc","GREEN","[]","[]","Working","[]","[\"done\"]","2026-01-01"))
        conn.execute("INSERT INTO master_requests VALUES(?,?,?,?,?,?,?)", ("r1","v1","Finish app","[\"done\"]","RUNNING",10,"2026-01-01"))
        conn.execute("INSERT INTO master_children VALUES(?,?,?,?,?,?,?)", ("r1","c1","Worker","[]","RUNNING","",1))
        conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (1,"Worker","v1","STARTED","{}","2026-01-01"))
        conn.commit(); conn.close()

    def _seed_wake(self):
        conn = sqlite3.connect(self.wake)
        conn.executescript("""
        CREATE TABLE browser_wake_delivery(message_id TEXT,route_key TEXT,status TEXT,attempts INTEGER,last_error TEXT,updated_at REAL);
        CREATE TABLE browser_wake_pending_worker(message_id TEXT,route_key TEXT,destination TEXT,payload TEXT);
        CREATE TABLE browser_wake_pending_master(id INTEGER,event_ids TEXT,first_seen REAL,max_event_id INTEGER);
        """)
        conn.execute("INSERT INTO browser_wake_delivery VALUES(?,?,?,?,?,?)", ("m1","Worker","DELIVERED",1,"",1.0))
        conn.commit(); conn.close()

    def test_dashboard_reads_without_mutating(self):
        before = self.orch.read_bytes()
        payload = data.dashboard()
        after = self.orch.read_bytes()
        self.assertEqual(before, after)
        self.assertEqual(payload["workers"][0]["state"], "RUNNING")
        self.assertEqual(payload["master"]["request_id"], "r1")
        self.assertEqual(payload["wakes"][0]["status"], "DELIVERED")
        self.assertEqual(len(payload["routes"]), 2)


if __name__ == "__main__":
    unittest.main()
