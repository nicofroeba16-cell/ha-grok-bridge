from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


class ChatRelayError(RuntimeError):
    pass


@dataclass(slots=True)
class ChatRelayClient:
    endpoint: str
    token: str = ""
    timeout: float = 15.0

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ChatRelayError("CHAT_RELAY_URL must be an absolute http(s) URL")
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not loopback:
            raise ChatRelayError("non-loopback CHAT_RELAY_URL must use https")

    def deliver(self, message_id: str, destination: str, payload: str) -> None:
        body = json.dumps({
            "message_id": message_id,
            "destination": destination,
            "payload": payload,
        }).encode()
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": message_id,
            "User-Agent": "runner-worker-orchestrator/0.2",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.endpoint, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise ChatRelayError(f"chat relay returned HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            raise ChatRelayError(f"chat relay returned HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise ChatRelayError("chat relay is unavailable") from None
