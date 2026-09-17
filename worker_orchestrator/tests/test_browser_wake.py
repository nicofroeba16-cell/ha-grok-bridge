from __future__ import annotations

import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from worker_orchestrator.browser_wake import (
    MASTER_ROUTE_KEY,
    BrowserRouteRegistry,
    BrowserWakeError,
    BrowserWakePreSendError,
    BrowserWakeUncertainError,
    CommandBrowserSender,
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

    def coordinator(self, sender=None, debounce=0):
        return WakeCoordinator(
            self.conn,
            routes(),
            sender or RecordingSender(),
            master_repo="nicofroeba16-cell/ha-grok-bridge",
            master_issue=3,
            debounce_seconds=debounce,
            now=lambda: 1000.0,
        )

    def test_route_validation_is_exact_chatgpt_conversation_only(self):
        with self.assertRaises(BrowserWakeError):
            BrowserRouteRegistry.from_json('{"x":{"url":"http://chatgpt.com/c/a"}}')
        with self.assertRaises(BrowserWakeError):
            BrowserRouteRegistry.from_json('{"x":{"url":"https://chatgpt.com/"}}')
        with self.assertRaises(BrowserWakeError):
            BrowserRouteRegistry.from_json('{"x":{"url":"https://example.com/c/a"}}')

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


    def test_command_sender_outer_timeout_is_uncertain_not_retryable(self):
        sender = CommandBrowserSender("echo ok")
        self.assertGreaterEqual(sender.timeout, 300)
        with patch("worker_orchestrator.browser_wake.subprocess.run", side_effect=subprocess.TimeoutExpired(["echo"], 1)):
            with self.assertRaises(BrowserWakeUncertainError):
                sender("m-timeout", WORKER_URL, "wake")

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
        row = coord.ledger.delivery("worker-wake:req-1:v1:child")
        self.assertEqual(row[0], "UNCERTAIN")

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


if __name__ == "__main__":
    unittest.main()
