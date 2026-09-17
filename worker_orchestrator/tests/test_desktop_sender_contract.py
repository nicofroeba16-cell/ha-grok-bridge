import json
import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "chatgpt_desktop_send.mjs"


class DesktopSenderContractTests(unittest.TestCase):
    def run_helper(self, request=None, *, env=None, args=()):
        merged = os.environ.copy()
        merged.pop("CHATGPT_DESKTOP_DEBUG_URL", None)
        if env:
            merged.update(env)
        data = "" if request is None else json.dumps(request) + "\n"
        return subprocess.run(
            ["node", str(HELPER), *args], input=data, text=True,
            capture_output=True, env=merged, check=False, timeout=10,
        )

    def test_offline_self_test(self):
        proc = self.run_helper(args=("--self-test",))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("self-test: ok", proc.stdout)

    def test_missing_debug_endpoint_is_retryable_pre_send(self):
        marker = "PAYLOAD-MUST-NOT-BE-ECHOED"
        proc = self.run_helper({
            "message_id": "dry-1", "destination": "chat-title:Master-Verteilung", "payload": marker,
        })
        self.assertEqual(proc.returncode, 2)
        result = json.loads(proc.stdout)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["safe_to_retry"])
        self.assertNotIn(marker, proc.stdout + proc.stderr)
    def test_remote_debug_endpoint_is_rejected_before_connect(self):
        proc = self.run_helper({
            "message_id": "dry-2", "destination": "chat-title:Worker", "payload": "wake",
        }, env={"CHATGPT_DESKTOP_DEBUG_URL": "http://192.168.1.10:9223"})
        self.assertEqual(proc.returncode, 2)
        result = json.loads(proc.stdout)
        self.assertTrue(result["safe_to_retry"])
        self.assertIn("loopback-only", result["error"])

    def test_unverified_thread_mapping_fails_before_driver_import(self):
        proc = self.run_helper({
            "message_id": "dry-3",
            "destination": "desktop-thread:123e4567-e89b-12d3-a456-426614174000",
            "payload": "wake",
        }, env={"CHATGPT_DESKTOP_DEBUG_URL": "http://127.0.0.1:9223"})
        self.assertEqual(proc.returncode, 2)
        result = json.loads(proc.stdout)
        self.assertTrue(result["safe_to_retry"])
        self.assertIn("live-verified", result["error"])


if __name__ == "__main__":
    unittest.main()
