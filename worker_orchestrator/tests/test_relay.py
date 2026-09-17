from __future__ import annotations

import unittest
from unittest.mock import patch

from worker_orchestrator.relay import ChatRelayClient, ChatRelayError


class _Response:
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class RelayHarness(unittest.TestCase):
    def test_relay_requires_http_or_https(self):
        with self.assertRaises(ChatRelayError):
            ChatRelayClient("file:///tmp/relay")

    def test_non_loopback_http_is_rejected(self):
        with self.assertRaises(ChatRelayError):
            ChatRelayClient("http://relay.example.test/messages")

    def test_relay_sends_idempotency_and_bearer_token(self):
        client = ChatRelayClient(
            "https://relay.example.test/messages",
            token="secret-token",
        )
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response()

        with patch("worker_orchestrator.relay.urllib.request.urlopen", fake_urlopen):
            client.deliver("msg-1", "chat-route:example/chat", "hello")

        request = captured["request"]
        self.assertEqual(request.get_header("Idempotency-key"), "msg-1")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(captured["timeout"], 15.0)
        self.assertIn(b'"destination": "chat-route:example/chat"', request.data)

    def test_loopback_http_is_allowed(self):
        client = ChatRelayClient("http://127.0.0.1:9999/messages")
        self.assertEqual(client.endpoint, "http://127.0.0.1:9999/messages")


if __name__ == "__main__":
    unittest.main()
