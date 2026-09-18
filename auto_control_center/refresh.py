from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable(child)
            for key, child in value.items()
            if key not in {"generated_at", "refresh"}
        }
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def stable_payload_signature(payload: dict[str, Any]) -> str:
    raw = json.dumps(_stable(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def canonical_event_id(payload: dict[str, Any]) -> str:
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    wakes = payload.get("wakes") if isinstance(payload.get("wakes"), list) else []
    master = payload.get("master") if isinstance(payload.get("master"), dict) else {}
    latest_event = max((int(row.get("id") or 0) for row in events if isinstance(row, dict)), default=0)
    latest_wake = max((float(row.get("updated_at") or 0) for row in wakes if isinstance(row, dict)), default=0.0)
    token = f"{latest_event}|{latest_wake}|{master.get('version','')}|{stable_payload_signature(payload)}"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def source_change_token(paths: Iterable[Path]) -> tuple[tuple[str, int, int], ...]:
    rows: list[tuple[str, int, int]] = []
    for index, path in enumerate(paths):
        try:
            stat = path.stat()
            rows.append((str(index), int(stat.st_mtime_ns), int(stat.st_size)))
        except OSError:
            rows.append((str(index), -1, -1))
    return tuple(rows)
