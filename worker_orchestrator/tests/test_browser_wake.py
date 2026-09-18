from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from worker_orchestrator.browser_wake import (
    MASTER_ROUTE_KEY,
    BrowserRouteRegistry,
    BrowserWakeError,
    BrowserWakePreSendError,
    BrowserWakeUncertainError,
    CommandBrowserSender,
    DeliveryReceipt,
    WakeCoordinator,
)


MASTER_URL = "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
WORKER_URL = "https://chatgpt.com/g/g-p-project123/c/11111111-2222-3333-4444-555555555555"
WORKER_KEY = "Projekt: P → Chat: C"


def routes():
    return BrowserRouteRegistry.from_json(
        '{'
        f'"{MASTER_ROUTE_KEY}":{{"url":"{MASTER_URL}"}},'
        f'"{WORKER_KEY}":{{"url":"{WORKER_URL}"}}'
        '}'
    )


def request_body(request_id="req-1", version="v1"):
    return "\n".join([
        "MASTER_REQUEST",
        f"REQUEST_ID: {request_id}",
        f"GOAL_VERSION: {version}",
        "REQUEST: Do the assigned worker task.",
        "WORK_GRAPH_JSON:",
        '[{"id":"child","project":"P","chat":"C","repository":"nicofroeba16-cell/ha-grok-bridge","branch":"feat/test","workstream_issue":7,"done_criteria":["tests green"]}]',
        "GLOBAL_DONE_CRITERIA:",
        "- child done",
    ])


def status_body(state="DONE", fingerprint="fp-1", kind="WORKER_STATUS"):
    lines = [
        kind,
        "PROJECT: P",
        "CHAT: C",
        "GOAL_VERSION: v1-child",
        f"STATE: {state}",
        "CI: GREEN",
        "BLOCKERS: []",
        "USER_ACTION_REQUIRED: []",
        f"FINGERPRINT: {fingerprint}",
    ]
    return "\n".join(lines)


class RecordingSender:
    def __init__(self):
        self.calls = []

    def __call__(self, message_id, destination, payload):
        self.calls.append((message_id, destination, payload))


class BrowserWakeTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def coordinator(self, sender=None, debounce=0, verified_reporter=None):
        return WakeCoordinator(
            self.conn,
            routes(),
            sender or RecordingSender(),
            master_repo="nicofroeba16-cell/ha-grok-bridge",
            master_issue=3,
            debounce_seconds=debounce,
            now=lambda: 1000.0,
            verified_delivery_reporter=verified_reporter,
        )

    def test_route_validation_is_exact_chatgpt_conversation_only(self):
        with self.assertRaises(BrowserWakeError):
            BrowserRouteRegistry.from_json('{"x":{"url":"http://chatgpt.com/c/a"}}')
        with self.assertRaises(BrowserWakeError):
            BrowserRouteRegistry.from_json('{"x":{"url":"https://chatgpt.com/"}}')
        with self.assertRaises(BrowserWakeError):
            BrowserRouteRegistry.from_json('{"x":{"url":"https://example.com/c/a"}}')

    def test_duplicate_or_ambiguous_browser_routes_fail_closed(self):
        duplicate_key = (
            "{"
            f'"{WORKER_KEY}":{{"url":"{WORKER_URL}"}},'
            f'"{WORKER_KEY}":{{"url":"{MASTER_URL}"}}'
            "}"
        )
        with self.assertRaises(BrowserWakeError):
            BrowserRouteRegistry.from_json(duplicate_key)

        duplicate_destination = (
            "{"
            f'"{WORKER_KEY}":{{"url":"{WORKER_URL}"}},'
            f'"Projekt: Other → Chat: Worker":{{"url":"{WORKER_URL}"}}'
            "}"
        )
        with self.assertRaises(BrowserWakeError):
            BrowserRouteRegistry.from_json(duplicate_destination)

    def test_invalid_master_request_is_observable_and_corrected_request_wakes_once(self):
        sender = RecordingSender()
        coord = self.coordinator(sender)
        invalid = {
            "id": 100,
            "body": "\n".join([
                "MASTER_REQUEST",
                "REQUEST_ID: broken",
                "GOAL_VERSION: broken-v1",
                "REQUEST: malformed request",
                "WORK_GRAPH_JSON:",
                '[{"id":"child","project":"P","chat":"C","repository":"nicofroeba16-cell/ha-grok-bridge","branch":"feat/test","done_criteria":["green"]}]',
            ]),
        }
        result = coord.reconcile([invalid], replay_existing=True)
        self.assertEqual(result["worker_wakes"], 0)
        self.assertEqual(result["rejected_master_requests"], 1)
        self.assertEqual(result["cursor"], 100)
        self.assertEqual(
            result["rejected_master_request_errors"][0]["source_comment_id"],
            100,
        )
        rejected = coord.ledger.rejected_master_requests()
        self.assertEqual(rejected[0]["source_comment_id"], 100)
        self.assertIn("GLOBAL_DONE_CRITERIA", rejected[0]["reason"])
        self.assertNotIn("malformed request", rejected[0]["reason"])

        corrected = {"id": 101, "body": request_body("fixed", "fixed-v1")}
        result = coord.reconcile([invalid, corrected], replay_existing=True)
        self.assertEqual(result["worker_wakes"], 1)
        self.assertEqual(len(sender.calls), 1)
        payload = sender.calls[0][2]
        self.assertIn("AUTO_POLICY_ID: auto-chat-status-report-and-resume-v1", payload)
        self.assertIn("STATUS_CHECKPOINT_RULE:", payload)

        result = coord.reconcile([invalid, corrected], replay_existing=True)
        self.assertEqual(result["worker_wakes"], 0)
        self.assertEqual(len(sender.calls), 1)

    def test_policy_sync_uses_registered_workers_not_all_routes_and_is_idempotent(self):
        sender = RecordingSender()
        coord = self.coordinator(sender)
        rows = [{
            "worker_key": WORKER_KEY,
            "goal_version": "current-v1",
        }]
        self.assertEqual(coord.queue_policy_sync(rows), 1)
        self.assertEqual(coord.queue_policy_sync(rows), 0)
        pending = coord.ledger.pending_workers()
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0][0].startswith("auto-policy-sync:"))
        self.assertIn("AUTO_POLICY_SYNC", pending[0][3])
        self.assertIn("STATUS_CURRENT_GOAL_RULE:", pending[0][3])

        self.assertEqual(coord._flush_worker_pending(), 1)
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(coord.queue_policy_sync(rows), 0)

    def test_policy_sync_ignores_unregistered_route_entries(self):
        legacy_key = "Projekt: Legacy → Chat: Old Chat"
        registry = BrowserRouteRegistry.from_json(
            "{"
            f'"{MASTER_ROUTE_KEY}":{{"url":"{MASTER_URL}"}},'
            f'"{WORKER_KEY}":{{"url":"{WORKER_URL}"}},'
            f'"{legacy_key}":{{"url":"https://chatgpt.com/c/99999999-2222-3333-4444-555555555555"}}'
            "}"
        )
        sender = RecordingSender()
        coord = WakeCoordinator(
            self.conn,
            registry,
            sender,
            master_repo="nicofroeba16-cell/ha-grok-bridge",
            master_issue=3,
            debounce_seconds=0,
            now=lambda: 1000.0,
        )
        rows = [{"worker_key": WORKER_KEY, "goal_version": "v1"}]
        self.assertEqual(coord.queue_policy_sync(rows), 1)
        pending_routes = [row[1] for row in coord.ledger.pending_workers()]
        self.assertEqual(pending_routes, [WORKER_KEY])
        self.assertNotIn(legacy_key, pending_routes)

    def test_first_start_bootstraps_without_waking_historical_events(self):
        sender = RecordingSender()
        coord = self.coordinator(sender)
        result = coord.reconcile([
            {"id": 10, "body": request_body()},
            {"id": 11, "body": status_body()},
        ])
        self.assertEqual(result["state"], "BOOTSTRAPPED")
        self.assertEqual(sender.calls, [])
        self.assertEqual(coord.ledger.get_int("scan_cursor"), 11)

    def test_new_master_request_wakes_worker_once(self):
        sender = RecordingSender()
        coord = self.coordinator(sender)
        coord.reconcile([{"id": 1, "body": "historical"}])
        items = [
            {"id": 1, "body": "historical"},
            {"id": 2, "body": request_body()},
        ]
        result = coord.reconcile(items)
        self.assertEqual(result["worker_wakes"], 1)
        self.assertEqual(len(sender.calls), 1)
        self.assertTrue(sender.calls[0][0].startswith("worker-wake:req-1:v1:child"))
        self.assertIn("LIVE_GATE:", sender.calls[0][2])
        coord.reconcile(items)
        self.assertEqual(len(sender.calls), 1)

    def test_pre_send_failure_retries_from_persistent_queue_then_stops_after_success(self):
        calls = []

        def sender(message_id, destination, payload):
            calls.append(message_id)
            if len(calls) < 3:
                raise BrowserWakePreSendError("composer not available")

        coord = self.coordinator(sender)
        coord.reconcile([{"id": 1, "body": request_body()}], replay_existing=True)
        self.assertEqual(len(calls), 1)
        coord.reconcile([{"id": 1, "body": request_body()}], replay_existing=True)
        self.assertEqual(len(calls), 2)
        coord.reconcile([{"id": 1, "body": request_body()}], replay_existing=True)
        self.assertEqual(len(calls), 3)
        coord.reconcile([{"id": 1, "body": request_body()}], replay_existing=True)
        self.assertEqual(len(calls), 3)


    def test_command_sender_timeout_uses_send_commit_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            sender = CommandBrowserSender("echo ok", marker_dir=td)
            self.assertGreaterEqual(sender.timeout, 300)
            with patch(
                "worker_orchestrator.browser_wake.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["echo"], 1),
            ):
                with self.assertRaises(BrowserWakePreSendError):
                    sender("m-pre-timeout", WORKER_URL, "wake")

            def committed_timeout(*args, **kwargs):
                marker = Path(sender.commit_marker_path("m-post-timeout"))
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(
                    json.dumps({"message_id": "m-post-timeout", "state": "SEND_COMMITTED"}),
                    encoding="utf-8",
                )
                raise subprocess.TimeoutExpired(["echo"], 1)

            with patch("worker_orchestrator.browser_wake.subprocess.run", side_effect=committed_timeout):
                with self.assertRaises(BrowserWakeUncertainError):
                    sender("m-post-timeout", WORKER_URL, "wake")

    def test_uncertain_delivery_is_never_retried(self):
        calls = []

        def sender(message_id, destination, payload):
            calls.append(message_id)
            raise BrowserWakeUncertainError("click may already have happened")

        coord = self.coordinator(sender)
        items = [{"id": 1, "body": request_body()}]
        coord.reconcile(items, replay_existing=True)
        coord.reconcile(items, replay_existing=True)
        self.assertEqual(len(calls), 1)
        message_id = "worker-wake:req-1:v1:child"
        row = coord.ledger.delivery(message_id)
        self.assertEqual(row[0], "UNCERTAIN")
        evidence = coord.ledger.evaluate_delivery(message_id)
        self.assertFalse(evidence["automatic_retry"])
        self.assertEqual(
            evidence["required_action"],
            "VERIFY_DESTINATION_BEFORE_MANUAL_RESOLUTION",
        )
        self.assertIn("click may already have happened", evidence["evidence"])
        result = coord.reconcile(items, replay_existing=True)
        self.assertEqual(result["uncertain_deliveries"], 1)
        self.assertEqual(len(coord.ledger.uncertain_deliveries()), 1)

    def test_verified_delivery_publishes_once_and_survives_duplicate_reconcile(self):
        reports = []

        def sender(message_id, destination, payload):
            return DeliveryReceipt(
                message_id=message_id,
                verified=True,
                verification_source="persisted_user_turn_after_reload_same_conversation",
                persisted_after_reload=True,
                destination_verified=True,
                transport="test-browser",
            )

        coord = self.coordinator(sender, verified_reporter=reports.append)
        items = [{"id": 1, "body": request_body()}]
        result = coord.reconcile(items, replay_existing=True)
        self.assertEqual(result["worker_wakes"], 1)
        self.assertEqual(result["verified_delivery_events"], 1)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["goal_version"], "v1-child")
        self.assertEqual(reports[0]["route_key"], WORKER_KEY)
        self.assertEqual(coord.ledger.pending_verified_deliveries(), [])

        result = coord.reconcile(items, replay_existing=True)
        self.assertEqual(result["verified_delivery_events"], 0)
        self.assertEqual(len(reports), 1)

    def test_verified_delivery_report_retries_without_resending_worker(self):
        sends = []
        reports = []

        def sender(message_id, destination, payload):
            sends.append(message_id)
            return DeliveryReceipt(
                message_id=message_id,
                verified=True,
                verification_source="persisted_user_turn_after_reload_same_conversation",
                persisted_after_reload=True,
                destination_verified=True,
            )

        def reporter(record):
            reports.append(record)
            if len(reports) == 1:
                raise OSError("github unavailable")

        coord = self.coordinator(sender, verified_reporter=reporter)
        items = [{"id": 1, "body": request_body()}]
        first = coord.reconcile(items, replay_existing=True)
        self.assertEqual(first["verified_delivery_events"], 0)
        self.assertEqual(len(sends), 1)
        self.assertEqual(len(coord.ledger.pending_verified_deliveries()), 1)

        second = coord.reconcile(items, replay_existing=True)
        self.assertEqual(second["verified_delivery_events"], 1)
        self.assertEqual(len(sends), 1)
        self.assertEqual(len(reports), 2)
        self.assertEqual(coord.ledger.pending_verified_deliveries(), [])

    def test_verified_receipt_with_wrong_route_identity_fails_closed(self):
        reports = []

        def sender(message_id, destination, payload):
            return DeliveryReceipt(
                message_id=message_id,
                verified=True,
                verification_source="persisted_user_turn_after_reload_same_conversation",
                persisted_after_reload=True,
                destination_verified=True,
            )

        coord = self.coordinator(sender, verified_reporter=reports.append)
        payload = "\n".join([
            "WORKER_WAKE",
            "PROJECT: Wrong",
            "CHAT: Route",
            "GOAL_VERSION: v1-child",
        ])
        self.assertTrue(coord._send_once("m-wrong-route", WORKER_KEY, WORKER_URL, payload))
        self.assertEqual(coord.ledger.pending_verified_deliveries(), [])
        self.assertEqual(coord._flush_verified_delivery_events(), 0)
        self.assertEqual(reports, [])

    def test_command_sender_requires_positive_persisted_destination_verification(self):
        sender = CommandBrowserSender("echo ok")
        verified = {
            "status": "sent",
            "message_id": "m1",
            "delivery_verified": True,
            "destination_verified": True,
            "persisted_after_reload": True,
            "verification_source": "persisted_user_turn_after_reload_same_conversation",
        }
        with patch(
            "worker_orchestrator.browser_wake.subprocess.run",
            return_value=subprocess.CompletedProcess(["echo"], 0, stdout=json.dumps(verified), stderr=""),
        ):
            receipt = sender("m1", WORKER_URL, "wake")
        self.assertTrue(receipt.verified)

        unverified = {"status": "sent", "message_id": "m2", "transport": "desktop"}
        with patch(
            "worker_orchestrator.browser_wake.subprocess.run",
            return_value=subprocess.CompletedProcess(["echo"], 0, stdout=json.dumps(unverified), stderr=""),
        ):
            receipt = sender("m2", WORKER_URL, "wake")
        self.assertFalse(receipt.verified)

        mismatch = {**verified, "message_id": "other"}
        with patch(
            "worker_orchestrator.browser_wake.subprocess.run",
            return_value=subprocess.CompletedProcess(["echo"], 0, stdout=json.dumps(mismatch), stderr=""),
        ):
            with self.assertRaises(BrowserWakeUncertainError):
                sender("m3", WORKER_URL, "wake")

    def test_two_worker_statuses_batch_into_exactly_one_master_wake(self):
        sender = RecordingSender()
        coord = self.coordinator(sender)
        items = [
            {"id": 10, "body": status_body("DONE", "fp-a", "WORKER_DONE")},
            {"id": 11, "body": status_body("BLOCKED", "fp-b")},
        ]
        result = coord.reconcile(items, replay_existing=True)
        self.assertEqual(result["master_wakes"], 1)
        self.assertEqual(len(sender.calls), 1)
        self.assertTrue(sender.calls[0][0].startswith("master-wake:"))
        self.assertIn("NEW_RELEVANT_EVENTS: 2", sender.calls[0][2])
        coord.reconcile(items, replay_existing=True)
        self.assertEqual(len(sender.calls), 1)

    def test_duplicate_canonical_fingerprint_does_not_wake_master_twice(self):
        sender = RecordingSender()
        coord = self.coordinator(sender)
        coord.reconcile([{"id": 1, "body": status_body("DONE", "same")}], replay_existing=True)
        self.assertEqual(len(sender.calls), 1)
        coord.reconcile([
            {"id": 1, "body": status_body("DONE", "same")},
            {"id": 2, "body": status_body("DONE", "same")},
        ], replay_existing=True)
        self.assertEqual(len(sender.calls), 1)

    def test_running_and_master_originated_statuses_do_not_wake_master(self):
        sender = RecordingSender()
        coord = self.coordinator(sender)
        running = status_body("RUNNING", "run-1").replace("CI: GREEN", "CI: GREEN")
        master = "MASTER_STATUS\nSTATE: RUNNING\nFINGERPRINT: master-1"
        browser = "BROWSER_WAKE_STATUS\nSTATE: DONE"
        result = coord.reconcile([
            {"id": 1, "body": running},
            {"id": 2, "body": master},
            {"id": 3, "body": browser},
        ], replay_existing=True)
        self.assertEqual(result["master_wakes"], 0)
        self.assertEqual(sender.calls, [])

    def test_interrupted_in_flight_delivery_becomes_uncertain_not_retryable(self):
        coord = self.coordinator(RecordingSender())
        self.assertTrue(coord.ledger.claim("m1", WORKER_KEY))
        coord.ledger.recover_interrupted()
        row = coord.ledger.delivery("m1")
        self.assertEqual(row[0], "UNCERTAIN")
        self.assertFalse(coord.ledger.claim("m1", WORKER_KEY))

    def test_interrupted_marker_aware_pre_send_is_retryable(self):
        with tempfile.TemporaryDirectory() as td:
            marker = str(Path(td) / "m-pre.json")
            coord = self.coordinator(RecordingSender())
            self.assertTrue(coord.ledger.claim("m-pre", WORKER_KEY, commit_marker=marker))
            coord.ledger.recover_interrupted()
            row = coord.ledger.delivery("m-pre")
            self.assertEqual(row[0], "FAILED_PRE_SEND")
            self.assertEqual(row[2], "INTERRUPTED_BEFORE_SEND_COMMIT")
            self.assertTrue(coord.ledger.claim("m-pre", WORKER_KEY, commit_marker=marker))

    def test_interrupted_marker_aware_post_commit_is_uncertain(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "m-post.json"
            marker.write_text(
                json.dumps({"message_id": "m-post", "state": "SEND_COMMITTED"}),
                encoding="utf-8",
            )
            coord = self.coordinator(RecordingSender())
            self.assertTrue(coord.ledger.claim("m-post", WORKER_KEY, commit_marker=str(marker)))
            coord.ledger.recover_interrupted()
            row = coord.ledger.delivery("m-post")
            self.assertEqual(row[0], "UNCERTAIN")
            self.assertEqual(row[2], "INTERRUPTED_AFTER_SEND_COMMIT")
            self.assertFalse(coord.ledger.claim("m-post", WORKER_KEY, commit_marker=str(marker)))


if __name__ == "__main__":
    unittest.main()
