from __future__ import annotations

import ipaddress
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlparse

from aiohttp import ClientError, ClientSession

from .model import ControlCenterSnapshot


class ControlCenterClientError(Exception):
    """Read-only Control Center client error."""


@dataclass(frozen=True, slots=True)
class StreamEvent:
    event: str
    data: dict


def normalize_base_url(value: str) -> str:
    parsed = urlparse(str(value).strip())
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("loopback_required")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("loopback_required")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise ValueError("loopback_required") from exc
    if not address.is_loopback:
        raise ValueError("loopback_required")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("invalid_port")
    return value.rstrip("/")


class AutoControlCenterClient:
    def __init__(self, session: ClientSession, base_url: str) -> None:
        self.session = session
        self.base_url = normalize_base_url(base_url)

    async def async_get_dashboard(self) -> ControlCenterSnapshot:
        try:
            async with self.session.get(f"{self.base_url}/api/dashboard", timeout=10) as response:
                if response.status != 200:
                    raise ControlCenterClientError(f"dashboard_http_{response.status}")
                payload = await response.json()
        except (ClientError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            raise ControlCenterClientError("dashboard_unavailable") from exc
        if not isinstance(payload, dict):
            raise ControlCenterClientError("dashboard_invalid")
        return ControlCenterSnapshot.from_payload(payload)

    async def async_stream(self) -> AsyncIterator[StreamEvent]:
        try:
            async with self.session.get(
                f"{self.base_url}/api/stream",
                timeout=None,
                headers={"Accept": "text/event-stream"},
            ) as response:
                if response.status != 200:
                    raise ControlCenterClientError(f"stream_http_{response.status}")
                event = "message"
                data: list[str] = []
                async for raw in response.content:
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        if data:
                            try:
                                payload = json.loads("\n".join(data))
                            except json.JSONDecodeError as exc:
                                raise ControlCenterClientError("stream_invalid_json") from exc
                            if isinstance(payload, dict):
                                yield StreamEvent(event, payload)
                        event, data = "message", []
                        continue
                    if line.startswith(":"):
                        continue
                    field, _, value = line.partition(":")
                    value = value.lstrip()
                    if field == "event":
                        event = value
                    elif field == "data":
                        data.append(value)
        except (ClientError, TimeoutError, OSError) as exc:
            raise ControlCenterClientError("stream_disconnected") from exc
