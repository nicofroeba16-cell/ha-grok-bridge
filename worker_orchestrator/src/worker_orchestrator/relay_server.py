from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping


DISPATCH_PATH = "/v1/chat-relay/messages"
HEALTH_PATH = "/healthz"
MAX_BODY_BYTES = 1_048_576
MAX_PAYLOAD_CHARS = 250_000
MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9._:@/+~-]{1,240}$")
DESTINATION_RE = re.compile(r"^chat-route:[A-Za-z0-9._:@/+~-]{1,200}$")
CONVERSATION_RE = re.compile(r"^conv_[A-Za-z0-9_-]{1,200}$")


class RelayError(RuntimeError):
    pass


class RelayValidationError(RelayError):
    pass


class RelayConflictError(RelayError):
    pass


class RelayRouteError(RelayError):
    pass


class RelayProviderError(RelayError):
    pass


@dataclass(frozen=True, slots=True)
class RelayMessage:
    message_id: str
    destination: str
    payload: str

    @classmethod
    def from_json(cls, value: object) -> "RelayMessage":
        if not isinstance(value, Mapping):
            raise RelayValidationError("request body must be a JSON object")
        allowed = {"message_id", "destination", "payload"}
        unknown = set(value) - allowed
        if unknown:
            raise RelayValidationError("request body contains unsupported fields")
        message_id = value.get("message_id")
        destination = value.get("destination")
        payload = value.get("payload")
        if not isinstance(message_id, str) or not MESSAGE_ID_RE.fullmatch(message_id):
            raise RelayValidationError("invalid message_id")
        if not isinstance(destination, str) or not DESTINATION_RE.fullmatch(destination):
            raise RelayValidationError("invalid destination")
        if not isinstance(payload, str) or not payload.strip():
            raise RelayValidationError("payload must be a non-empty string")
        if len(payload) > MAX_PAYLOAD_CHARS:
            raise RelayValidationError("payload is too large")
        return cls(message_id=message_id, destination=destination, payload=payload)

    @property
    def payload_hash(self) -> str:
        material = json.dumps(
            {"destination": self.destination, "payload": self.payload},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class RelayRoute:
    provider: str
    conversation: str
    model: str


class RelayRouteRegistry:
    def __init__(self, routes: Mapping[str, RelayRoute]):
        self.routes = dict(routes)

    @classmethod
    def from_json(cls, raw: str, *, default_model: str) -> "RelayRouteRegistry":
        if not raw.strip():
            raise RelayRouteError("CHAT_RELAY_ROUTES_JSON is required")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RelayRouteError("CHAT_RELAY_ROUTES_JSON is invalid JSON") from exc
        if not isinstance(value, Mapping) or not value:
            raise RelayRouteError("CHAT_RELAY_ROUTES_JSON must be a non-empty object")

        routes: dict[str, RelayRoute] = {}
        for destination, config in value.items():
            if not isinstance(destination, str) or not DESTINATION_RE.fullmatch(destination):
                raise RelayRouteError("route keys must use chat-route:<alias>")
            if not isinstance(config, Mapping):
                raise RelayRouteError(f"route {destination} must be an object")
            provider = str(config.get("provider", "")).strip()
            conversation = str(config.get("conversation", "")).strip()
            model = str(config.get("model", default_model)).strip()
            if provider != "openai_responses":
                raise RelayRouteError(f"unsupported provider for {destination}")
            if not CONVERSATION_RE.fullmatch(conversation):
                raise RelayRouteError(f"route {destination} requires an OpenAI API conversation id")
            if not model or len(model) > 120:
                raise RelayRouteError(f"route {destination} has an invalid model")
            routes[destination] = RelayRoute(provider, conversation, model)
        return cls(routes)

    def get(self, destination: str) -> RelayRoute:
        route = self.routes.get(destination)
        if route is None:
            raise RelayRouteError("unknown chat relay destination")
        return route

    def __len__(self) -> int:
        return len(self.routes)


class RelayLedger:
    """Durable dedupe ledger for project-level relay message ids."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        path_obj = Path(self.path)
        if path_obj.parent != Path("."):
            path_obj.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS relay_messages (
                    message_id TEXT PRIMARY KEY,
                    destination TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    response_id TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._conn.execute(
                "UPDATE relay_messages SET state='FAILED', "
                "last_error='INTERRUPTED', updated_at=CURRENT_TIMESTAMP "
                "WHERE state='IN_FLIGHT'"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def reserve(self, message: RelayMessage) -> tuple[str, str]:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT destination,payload_hash,state,response_id "
                "FROM relay_messages WHERE message_id=?",
                (message.message_id,),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO relay_messages(message_id,destination,payload_hash,state) "
                    "VALUES(?,?,?,'IN_FLIGHT')",
                    (message.message_id, message.destination, message.payload_hash),
                )
                return "DELIVER", ""
            if row["destination"] != message.destination or row["payload_hash"] != message.payload_hash:
                raise RelayConflictError("message_id is already bound to different content")
            if row["state"] == "DELIVERED":
                return "DUPLICATE_DELIVERED", str(row["response_id"])
            if row["state"] == "IN_FLIGHT":
                return "DUPLICATE_IN_FLIGHT", str(row["response_id"])
            self._conn.execute(
                "UPDATE relay_messages SET state='IN_FLIGHT',last_error='',"
                "updated_at=CURRENT_TIMESTAMP WHERE message_id=?",
                (message.message_id,),
            )
            return "DELIVER", ""

    def mark_delivered(self, message_id: str, response_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE relay_messages SET state='DELIVERED',response_id=?,last_error='',"
                "updated_at=CURRENT_TIMESTAMP WHERE message_id=?",
                (response_id, message_id),
            )

    def mark_failed(self, message_id: str, reason: str) -> None:
        safe_reason = reason[:120]
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE relay_messages SET state='FAILED',last_error=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE message_id=?",
                (safe_reason, message_id),
            )


@dataclass(slots=True)
class OpenAIResponsesProvider:
    api_key: str
    endpoint: str = "https://api.openai.com/v1/responses"
    timeout: float = 30.0

    def __post_init__(self) -> None:
        if not self.api_key:
            raise RelayProviderError("OPENAI_API_KEY is required")
        parsed = urllib.parse.urlparse(self.endpoint)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RelayProviderError("OpenAI Responses endpoint must be an absolute http(s) URL")
        if parsed.scheme != "https" and not loopback:
            raise RelayProviderError("non-loopback OpenAI Responses endpoint must use https")

    def deliver(self, route: RelayRoute, message: RelayMessage) -> str:
        body = json.dumps(
            {
                "model": route.model,
                "conversation": route.conversation,
                "input": message.payload,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        request_id = hashlib.sha256(message.message_id.encode()).hexdigest()[:32]
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-Client-Request-Id": request_id,
                "User-Agent": "ha-grok-bridge-chat-relay/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raise RelayProviderError(f"OpenAI Responses API returned HTTP {exc.code}") from None
        except urllib.error.URLError:
            raise RelayProviderError("OpenAI Responses API is unavailable") from None
        if not 200 <= status < 300:
            raise RelayProviderError(f"OpenAI Responses API returned HTTP {status}")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RelayProviderError("OpenAI Responses API returned invalid JSON") from exc
        response_id = value.get("id") if isinstance(value, Mapping) else None
        if not isinstance(response_id, str) or not response_id:
            raise RelayProviderError("OpenAI Responses API response id is missing")
        return response_id


@dataclass(slots=True)
class RelayRouter:
    routes: RelayRouteRegistry
    openai: OpenAIResponsesProvider

    def deliver(self, message: RelayMessage) -> str:
        route = self.routes.get(message.destination)
        if route.provider == "openai_responses":
            return self.openai.deliver(route, message)
        raise RelayRouteError("unsupported relay provider")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def build_server(
    host: str,
    port: int,
    *,
    token: str,
    ledger: RelayLedger,
    router: RelayRouter,
) -> ThreadingHTTPServer:
    if not token:
        raise RelayValidationError("CHAT_RELAY_TOKEN is required")

    class Handler(BaseHTTPRequestHandler):
        server_version = "ChatRelay/1.0"

        def log_message(self, fmt, *args):
            return

        def _send_json(self, status: int, value: object) -> None:
            body = _json_bytes(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return False
            supplied = header[7:]
            return bool(supplied) and hmac.compare_digest(supplied, token)

        def do_GET(self):
            if self.path != HEALTH_PATH:
                self._send_json(404, {"error": "not_found"})
                return
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "chat-relay",
                    "api_version": "v1",
                    "routes": len(router.routes),
                    "delivery_semantics": "openai_api_conversation",
                    "chatgpt_ui_injection": False,
                },
            )

        def do_POST(self):
            if self.path != DISPATCH_PATH:
                self._send_json(404, {"error": "not_found"})
                return
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(400, {"error": "invalid_content_length"})
                return
            if length <= 0:
                self._send_json(400, {"error": "empty_body"})
                return
            if length > MAX_BODY_BYTES:
                self._send_json(413, {"error": "body_too_large"})
                return
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw)
                message = RelayMessage.from_json(value)
                action, response_id = ledger.reserve(message)
            except (UnicodeDecodeError, json.JSONDecodeError, RelayValidationError) as exc:
                self._send_json(400, {"error": "invalid_request", "detail": str(exc)})
                return
            except RelayConflictError as exc:
                self._send_json(409, {"error": "idempotency_conflict", "detail": str(exc)})
                return

            if action != "DELIVER":
                self._send_json(
                    202,
                    {
                        "accepted": True,
                        "duplicate": True,
                        "message_id": message.message_id,
                        "state": (
                            "DELIVERED"
                            if action == "DUPLICATE_DELIVERED"
                            else "IN_FLIGHT"
                        ),
                        "response_id": response_id,
                        "delivery_semantics": "openai_api_conversation",
                    },
                )
                return

            try:
                response_id = router.deliver(message)
            except RelayRouteError as exc:
                ledger.mark_failed(message.message_id, "ROUTE_REJECTED")
                self._send_json(422, {"error": "route_rejected", "detail": str(exc)})
                return
            except RelayProviderError:
                ledger.mark_failed(message.message_id, "PROVIDER_ERROR")
                self._send_json(502, {"error": "provider_error"})
                return
            except Exception:
                ledger.mark_failed(message.message_id, "INTERNAL_ERROR")
                self._send_json(500, {"error": "internal_error"})
                return

            ledger.mark_delivered(message.message_id, response_id)
            self._send_json(
                202,
                {
                    "accepted": True,
                    "duplicate": False,
                    "message_id": message.message_id,
                    "state": "DELIVERED",
                    "response_id": response_id,
                    "delivery_semantics": "openai_api_conversation",
                },
            )

    return ThreadingHTTPServer((host, port), Handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chat-relay-server")
    parser.add_argument("--host", default=os.environ.get("CHAT_RELAY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CHAT_RELAY_PORT", "8790")))
    parser.add_argument("--db", default=os.environ.get("CHAT_RELAY_DB", "./chat-relay.sqlite3"))
    parser.add_argument(
        "--routes-json",
        default=os.environ.get("CHAT_RELAY_ROUTES_JSON", ""),
    )
    parser.add_argument(
        "--openai-endpoint",
        default=os.environ.get("OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses"),
    )
    parser.add_argument(
        "--default-model",
        default=os.environ.get("OPENAI_MODEL", "gpt-5.6"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = os.environ.get("CHAT_RELAY_TOKEN", "")
    routes = RelayRouteRegistry.from_json(args.routes_json, default_model=args.default_model)
    provider = OpenAIResponsesProvider(
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        endpoint=args.openai_endpoint,
    )
    ledger = RelayLedger(args.db)
    server = build_server(
        args.host,
        args.port,
        token=token,
        ledger=ledger,
        router=RelayRouter(routes, provider),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
