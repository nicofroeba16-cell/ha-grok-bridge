from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from worker_orchestrator.relay_server import (
    DISPATCH_PATH,
    OpenAIResponsesProvider,
    RelayLedger,
    RelayMessage,
    RelayProviderError,
    RelayRoute,
    RelayRouteError,
    RelayRouteRegistry,
    RelayRouter,
    build_server,
)


ROUTES_JSON = json.dumps(
    {
        "chat-route:example/worker": {
            "provider": "openai_responses",
            "conversation": "conv_example123",
            "model": "gpt-5.6",
        }
    }
)


class FakeProvider:
    def __init__(self):
        self.calls = []

    def deliver(self, route, message):
        self.calls.append((route, message))
        return "resp_fake_123"


class FakeHTTPResponse:
    status = 200

    def __init__(self, value):
        self._body = json.dumps(value).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class RelayServerHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = RelayLedger(Path(self.tmp.name) / "relay.sqlite3")
        self.routes = RelayRouteRegistry.from_json(ROUTES_JSON, default_model="gpt-5.6")
        self.provider = FakeProvider()
        self.router = RelayRouter(self.routes, self.provider)
        self.server = build_server(
            "127.0.0.1",
            0,
            token="relay-secret",
            ledger=self.ledger,
            router=self.router,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.ledger.close()
        self.tmp.cleanup()

    def request(self, path, *, method="GET", value=None, token=None):
        body = None if value is None else json.dumps(value).encode()
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.base + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def payload(self, *, message_id="req-1:v1:source", text="hello"):
        return {
            "message_id": message_id,
            "destination": "chat-route:example/worker",
            "payload": text,
        }

    def test_health_never_claims_chatgpt_ui_injection(self):
        status, body = self.request("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertFalse(body["chatgpt_ui_injection"])
        self.assertEqual(body["delivery_semantics"], "openai_api_conversation")
        self.assertEqual(body["routes"], 1)

    def test_dispatch_requires_bearer_authentication(self):
        status, body = self.request(DISPATCH_PATH, method="POST", value=self.payload())
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        self.assertEqual(self.provider.calls, [])

    def test_dispatch_delivers_once_and_deduplicates(self):
        first_status, first = self.request(
            DISPATCH_PATH,
            method="POST",
            value=self.payload(),
            token="relay-secret",
        )
        second_status, second = self.request(
            DISPATCH_PATH,
            method="POST",
            value=self.payload(),
            token="relay-secret",
        )
        self.assertEqual(first_status, 202)
        self.assertEqual(second_status, 202)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["state"], "DELIVERED")
        self.assertEqual(second["response_id"], "resp_fake_123")
        self.assertEqual(len(self.provider.calls), 1)

    def test_same_message_id_with_changed_payload_conflicts(self):
        self.request(
            DISPATCH_PATH,
            method="POST",
            value=self.payload(text="one"),
            token="relay-secret",
        )
        status, body = self.request(
            DISPATCH_PATH,
            method="POST",
            value=self.payload(text="two"),
            token="relay-secret",
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "idempotency_conflict")
        self.assertEqual(len(self.provider.calls), 1)

    def test_unknown_route_fails_closed(self):
        value = self.payload()
        value["destination"] = "chat-route:unknown/worker"
        status, body = self.request(
            DISPATCH_PATH,
            method="POST",
            value=value,
            token="relay-secret",
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "route_rejected")
        self.assertEqual(self.provider.calls, [])

    def test_malformed_request_fails_closed(self):
        value = self.payload()
        value["extra"] = "not-allowed"
        status, body = self.request(
            DISPATCH_PATH,
            method="POST",
            value=value,
            token="relay-secret",
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_request")
        self.assertEqual(self.provider.calls, [])

    def test_delivered_idempotency_survives_ledger_restart(self):
        status, _ = self.request(
            DISPATCH_PATH,
            method="POST",
            value=self.payload(),
            token="relay-secret",
        )
        self.assertEqual(status, 202)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.ledger.close()

        self.ledger = RelayLedger(Path(self.tmp.name) / "relay.sqlite3")
        replacement_provider = FakeProvider()
        self.provider = replacement_provider
        self.router = RelayRouter(self.routes, replacement_provider)
        self.server = build_server(
            "127.0.0.1",
            0,
            token="relay-secret",
            ledger=self.ledger,
            router=self.router,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

        status, body = self.request(
            DISPATCH_PATH,
            method="POST",
            value=self.payload(),
            token="relay-secret",
        )
        self.assertEqual(status, 202)
        self.assertTrue(body["duplicate"])
        self.assertEqual(replacement_provider.calls, [])

    def test_route_registry_rejects_non_api_conversation_target(self):
        raw = json.dumps(
            {
                "chat-route:bad": {
                    "provider": "openai_responses",
                    "conversation": "https://chatgpt.com/c/some-ui-chat",
                    "model": "gpt-5.6",
                }
            }
        )
        with self.assertRaises(RelayRouteError):
            RelayRouteRegistry.from_json(raw, default_model="gpt-5.6")


class OpenAIResponsesProviderHarness(unittest.TestCase):
    def test_provider_uses_official_responses_contract_and_api_conversation(self):
        provider = OpenAIResponsesProvider("api-secret")
        route = RelayRoute("openai_responses", "conv_example123", "gpt-5.6")
        message = RelayMessage(
            "req-1:v1:source",
            "chat-route:example/worker",
            "hello",
        )
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeHTTPResponse({"id": "resp_abc123"})

        with patch(
            "worker_orchestrator.relay_server.urllib.request.urlopen",
            fake_urlopen,
        ):
            response_id = provider.deliver(route, message)

        self.assertEqual(response_id, "resp_abc123")
        request = captured["request"]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer api-secret")
        self.assertTrue(request.get_header("X-client-request-id"))
        body = json.loads(request.data)
        self.assertEqual(body["conversation"], "conv_example123")
        self.assertEqual(body["input"], "hello")
        self.assertEqual(body["model"], "gpt-5.6")

    def test_provider_rejects_insecure_remote_endpoint(self):
        with self.assertRaises(RelayProviderError):
            OpenAIResponsesProvider(
                "api-secret",
                endpoint="http://relay.example.test/v1/responses",
            )


if __name__ == "__main__":
    unittest.main()
