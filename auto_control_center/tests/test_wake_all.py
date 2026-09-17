import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from auto_control_center import app as app_module
from auto_control_center.security import issue_csrf, validate_loopback_request, verify_csrf
from auto_control_center.wake_all import WakeAllError, WakeAllService


class WakeAllFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.wake_db = root / "wake.sqlite3"
        self.routes = root / "routes.json"
        conn = sqlite3.connect(self.wake_db)
        conn.executescript(
            """
            CREATE TABLE browser_wake_delivery(
              message_id TEXT PRIMARY KEY, route_key TEXT NOT NULL,
              status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              last_error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL
            );
            CREATE TABLE browser_wake_pending_worker(
              message_id TEXT PRIMARY KEY, route_key TEXT NOT NULL,
              destination TEXT NOT NULL, payload TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO browser_wake_delivery VALUES(?,?,?,?,?,?)",
            ("old-uncertain", "uncertain", "UNCERTAIN", 1, "post-send unknown", 20.0),
        )
        conn.execute(
            "INSERT INTO browser_wake_pending_worker VALUES(?,?,?,?)",
            ("old-pending", "pending", "chat-title:Pending", "existing"),
        )
        conn.commit()
        conn.close()

        self.routes.write_text(
            json.dumps(
                {
                    "__master__": {"title": "Master"},
                    "assigned": {"title": "Assigned"},
                    "running": {"title": "Running"},
                    "blocked": {"title": "Blocked"},
                    "waiting": {"title": "Waiting"},
                    "ready": {"title": "Ready"},
                    "done": {"title": "Done"},
                    "uncertain": {"title": "Uncertain"},
                    "pending": {"title": "Pending"},
                    "ambiguous-a": {"title": "Same destination"},
                    "ambiguous-b": {"title": "Same destination"},
                    "orphan-route": {"title": "Orphan"},
                }
            ),
            encoding="utf-8",
        )
        states = [
            ("assigned", "ASSIGNED"),
            ("running", "RUNNING"),
            ("blocked", "BLOCKED"),
            ("waiting", "WAITING_FOR_USER"),
            ("ready", "READY"),
            ("done", "DONE"),
            ("uncertain", "ASSIGNED"),
            ("pending", "RUNNING"),
            ("ambiguous-a", "ASSIGNED"),
            ("ambiguous-b", "ASSIGNED"),
            ("missing", "ASSIGNED"),
        ]
        self.workers = [
            {
                "worker_key": key,
                "project": "Project " + key,
                "chat": "Chat " + key,
                "goal_version": "goal-" + key,
                "state": state,
                "resolved_state": state,
            }
            for key, state in states
        ]
        self.service = WakeAllService(
            wake_db=self.wake_db,
            routes_file=self.routes,
            worker_provider=lambda: list(self.workers),
            enabled=True,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_preview_is_route_driven_and_explains_every_skip(self):
        preview = self.service.preview()
        self.assertTrue(preview["enabled"])
        self.assertTrue(preview["available"])
        self.assertEqual(preview["routed_count"], 11)
        self.assertEqual(preview["eligible_count"], 5)
        self.assertEqual(
            {row["worker_key"] for row in preview["eligible"]},
            {"assigned", "running", "blocked", "waiting", "ready"},
        )
        reasons = {row["worker_key"]: row["skip_reason"] for row in preview["skipped"]}
        self.assertEqual(reasons["done"], "done_without_changed_goal_proof")
        self.assertEqual(reasons["uncertain"], "uncertain_delivery_exists")
        self.assertEqual(reasons["pending"], "pending_delivery_exists")
        self.assertEqual(reasons["ambiguous-a"], "route_destination_ambiguous")
        self.assertEqual(reasons["ambiguous-b"], "route_destination_ambiguous")
        self.assertEqual(reasons["missing"], "missing_route")
        self.assertEqual(reasons["orphan-route"], "worker_not_registered")
        serialized = json.dumps(preview)
        self.assertNotIn("chat-title:", serialized)
        self.assertNotIn("Same destination", serialized)
        self.assertNotIn("__master__", serialized)

    def test_capability_disabled_and_control_plane_failure_fail_closed(self):
        disabled = WakeAllService(
            wake_db=self.wake_db,
            routes_file=self.routes,
            worker_provider=lambda: self.workers,
            enabled=False,
        ).preview()
        self.assertFalse(disabled["enabled"])
        self.assertFalse(disabled["can_submit"])
        missing = WakeAllService(
            wake_db=Path(self.tmp.name) / "missing.sqlite3",
            routes_file=self.routes,
            worker_provider=lambda: self.workers,
            enabled=True,
        ).preview()
        self.assertTrue(missing["enabled"])
        self.assertFalse(missing["available"])
        self.assertFalse(missing["can_submit"])

    def test_confirmed_submit_enqueues_only_eligible_workers_and_preserves_goals(self):
        preview = self.service.preview()
        with self.assertRaises(WakeAllError):
            self.service.submit(
                idempotency_key=preview["idempotency_key"],
                preview_hash=preview["preview_hash"],
                confirmed=False,
            )
        result = self.service.submit(
            idempotency_key=preview["idempotency_key"],
            preview_hash=preview["preview_hash"],
            confirmed=True,
        )
        self.assertEqual(result["queued_count"], 5)
        conn = sqlite3.connect(self.wake_db)
        rows = conn.execute(
            "SELECT route_key,destination,payload FROM browser_wake_pending_worker WHERE message_id LIKE 'control-center-wake:%'"
        ).fetchall()
        conn.close()
        self.assertEqual({row[0] for row in rows}, {"assigned", "running", "blocked", "waiting", "ready"})
        for route_key, destination, payload in rows:
            self.assertIn("WORKER_WAKE", payload)
            self.assertIn("GOAL_VERSION: goal-" + route_key, payload)
            self.assertTrue(destination.startswith("chat-title:"))

    def test_route_change_after_preview_requires_refresh(self):
        preview = self.service.preview()
        routes = json.loads(self.routes.read_text(encoding="utf-8"))
        routes["assigned"] = {"title": "Assigned changed"}
        self.routes.write_text(json.dumps(routes), encoding="utf-8")
        with self.assertRaises(WakeAllError) as ctx:
            self.service.submit(
                idempotency_key=preview["idempotency_key"],
                preview_hash=preview["preview_hash"],
                confirmed=True,
            )
        self.assertEqual(str(ctx.exception), "preview_changed_refresh_required")

    def test_same_idempotency_key_is_double_submit_safe(self):
        preview = self.service.preview()
        first = self.service.submit(
            idempotency_key=preview["idempotency_key"],
            preview_hash=preview["preview_hash"],
            confirmed=True,
        )
        second = self.service.submit(
            idempotency_key=preview["idempotency_key"],
            preview_hash=preview["preview_hash"],
            confirmed=True,
        )
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        conn = sqlite3.connect(self.wake_db)
        count = conn.execute(
            "SELECT COUNT(*) FROM browser_wake_pending_worker WHERE message_id LIKE 'control-center-wake:%'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 5)

    def test_concurrent_same_operation_does_not_duplicate(self):
        preview = self.service.preview()

        def submit():
            return self.service.submit(
                idempotency_key=preview["idempotency_key"],
                preview_hash=preview["preview_hash"],
                confirmed=True,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(len(results), 2)
        conn = sqlite3.connect(self.wake_db)
        count = conn.execute(
            "SELECT COUNT(*) FROM browser_wake_pending_worker WHERE message_id LIKE 'control-center-wake:%'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 5)

    def test_reload_preview_does_not_duplicate_existing_pending_wakes(self):
        preview = self.service.preview()
        self.service.submit(
            idempotency_key=preview["idempotency_key"],
            preview_hash=preview["preview_hash"],
            confirmed=True,
        )
        refreshed = self.service.preview()
        self.assertEqual(refreshed["eligible_count"], 0)
        reasons = {row["worker_key"]: row["skip_reason"] for row in refreshed["skipped"]}
        for key in {"assigned", "running", "blocked", "waiting", "ready"}:
            self.assertEqual(reasons[key], "pending_delivery_exists")

    def test_result_truthfully_maps_delivery_outcomes(self):
        preview = self.service.preview()
        self.service.submit(
            idempotency_key=preview["idempotency_key"],
            preview_hash=preview["preview_hash"],
            confirmed=True,
        )
        conn = sqlite3.connect(self.wake_db)
        rows = conn.execute(
            "SELECT message_id,route_key FROM browser_wake_pending_worker WHERE message_id LIKE 'control-center-wake:%' ORDER BY route_key"
        ).fetchall()
        statuses = ["BLOCKED", "DELIVERED", "FAILED_PRE_SEND", "IN_FLIGHT", "UNCERTAIN"]
        for (message_id, route_key), status in zip(rows, statuses):
            conn.execute("DELETE FROM browser_wake_pending_worker WHERE message_id=?", (message_id,))
            conn.execute(
                "INSERT INTO browser_wake_delivery VALUES(?,?,?,?,?,?)",
                (message_id, route_key, status, 1, "", 100.0),
            )
        conn.commit()
        conn.close()
        result = self.service.result(idempotency_key=preview["idempotency_key"])
        outcomes = {row["outcome"] for row in result["results"]}
        self.assertEqual(
            outcomes,
            {"blocked", "verified", "failed-pre-send", "queued", "uncertain"},
        )
        self.assertFalse(result["complete"])


class WakeAllSecurityTests(unittest.TestCase):
    def _request(self, *, client="127.0.0.1", host="127.0.0.1:8877", origin=None, csrf=None):
        headers = [(b"host", host.encode())]
        if origin:
            headers.append((b"origin", origin.encode()))
        if csrf:
            headers.append((b"x-csrf-token", csrf.encode()))
            headers.append((b"cookie", ("acc_csrf=" + csrf).encode()))
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/actions/wake-all/submit",
            "raw_path": b"/api/actions/wake-all/submit",
            "query_string": b"",
            "headers": headers,
            "client": (client, 50000),
            "server": ("127.0.0.1", 8877),
        }
        return Request(scope)

    def test_loopback_and_same_origin_are_strict(self):
        self.assertTrue(
            validate_loopback_request(
                client_host="127.0.0.1",
                request_url="http://127.0.0.1:8877/api/actions/wake-all/submit",
                origin="http://127.0.0.1:8877",
            )
        )
        self.assertFalse(
            validate_loopback_request(
                client_host="127.0.0.1",
                request_url="http://127.0.0.1:8877/api/actions/wake-all/submit",
                origin="http://localhost:8877",
            )
        )
        self.assertFalse(
            validate_loopback_request(
                client_host="192.168.1.10",
                request_url="http://127.0.0.1:8877/api/actions/wake-all/submit",
                origin="http://127.0.0.1:8877",
            )
        )

    def test_csrf_token_is_operation_bound_and_expires(self):
        token = issue_csrf("A" * 24, now=1000)
        self.assertTrue(verify_csrf(token, "A" * 24, now=1001))
        self.assertFalse(verify_csrf(token, "B" * 24, now=1001))
        self.assertFalse(verify_csrf(token, "A" * 24, now=2000))

    def test_submit_endpoint_rejects_cross_origin_before_service(self):
        request = self._request(origin="http://localhost:8877")
        body = app_module.WakeAllSubmit(confirm=True, idempotency_key="A" * 24, preview_hash="0" * 64)
        with self.assertRaises(Exception) as ctx:
            app_module.wake_all_submit(request, body)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 403)

    def test_submit_endpoint_accepts_valid_csrf_and_uses_guarded_service(self):
        key = "A" * 24
        token = issue_csrf(key)
        request = self._request(origin="http://127.0.0.1:8877", csrf=token)
        body = app_module.WakeAllSubmit(confirm=True, idempotency_key=key, preview_hash="0" * 64)

        class Stub:
            def submit(self, **kwargs):
                return {"queued_count": 1, "skipped_count": 0, "results": [], "skipped": []}

        with patch.object(app_module, "capability_enabled", return_value=True), patch.object(
            app_module, "_wake_service", return_value=Stub()
        ):
            response = app_module.wake_all_submit(request, body)
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
