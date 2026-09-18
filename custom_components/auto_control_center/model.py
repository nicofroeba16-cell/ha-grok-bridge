from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in {"generated_at", "refresh"}}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def _signature(payload: dict[str, Any]) -> str:
    refresh = payload.get("refresh") if isinstance(payload.get("refresh"), dict) else {}
    if refresh.get("signature"):
        return str(refresh["signature"])
    raw = json.dumps(_stable(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _count_state(workers: list[dict[str, Any]], *states: str) -> int:
    wanted = {state.upper() for state in states}
    return sum(1 for row in workers if str(row.get("resolved_state") or row.get("state") or "").upper() in wanted)


@dataclass(frozen=True, slots=True)
class ControlCenterSnapshot:
    signature: str
    workers_total: int
    workers_running: int
    workers_blocked: int
    workers_waiting: int
    workers_ready_done: int
    wake_uncertain: int
    activation_unconfirmed: int
    activation_confirmed: int
    activation_source_summary: str
    status_resume_workers: int
    ci_red: int
    master_progress: int | None
    last_event_at: datetime | None
    source_healthy: bool
    wake_path_healthy: bool
    registry_drift: int
    legacy_unrouted: int
    superseded: int
    malformed_master_requests: int
    media_schema_ready: bool
    media_jobs_pending: int | None
    media_jobs_running: int | None
    media_jobs_blocked: int | None
    media_jobs_verified: int | None
    latest_media_archive_age: int | None
    raw: dict[str, Any] = field(compare=False, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ControlCenterSnapshot":
        stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
        workers = payload.get("workers") if isinstance(payload.get("workers"), list) else []
        health = payload.get("health") if isinstance(payload.get("health"), dict) else {}
        sources = [v for k, v in health.items() if k != "generated_at" and isinstance(v, dict)]
        source_healthy = bool(sources) and all(bool(v.get("readable")) for v in sources)
        wake_path = payload.get("wake_path") if isinstance(payload.get("wake_path"), dict) else {}
        drift = payload.get("route_drift") if isinstance(payload.get("route_drift"), dict) else {}
        rejection = payload.get("master_request_health") if isinstance(payload.get("master_request_health"), dict) else {}
        media = payload.get("media_archive") if isinstance(payload.get("media_archive"), dict) else {}
        media_summary = media.get("summary") if isinstance(media.get("summary"), dict) else {}
        activation_sources = {
            str(row.get("activation_source")) for row in workers
            if row.get("activation_source") in {"wake_uncertain", "wake_verified", "worker_report"}
        }
        activation_summary = "none" if not activation_sources else next(iter(activation_sources)) if len(activation_sources) == 1 else "mixed"
        events = payload.get("events") if isinstance(payload.get("events"), list) else []
        event_times = [_timestamp(row.get("created_at")) for row in events if isinstance(row, dict)]
        event_times = [value for value in event_times if value is not None]
        master = payload.get("master") if isinstance(payload.get("master"), dict) else {}
        progress = master.get("progress") if isinstance(master.get("progress"), dict) else {}
        percent = progress.get("percent")
        return cls(
            signature=_signature(payload),
            workers_total=int(stats.get("workers", len(workers)) or 0),
            workers_running=int(stats.get("running", _count_state(workers, "RUNNING")) or 0),
            workers_blocked=int(stats.get("blocked_stalled", _count_state(workers, "BLOCKED", "STALLED")) or 0),
            workers_waiting=int(stats.get("waiting_for_user", _count_state(workers, "WAITING_FOR_USER")) or 0),
            workers_ready_done=int(stats.get("ready_done", _count_state(workers, "READY", "DONE")) or 0),
            wake_uncertain=int(stats.get("wake_uncertain", 0) or 0),
            activation_unconfirmed=int(stats.get("activation_unconfirmed", sum(1 for row in workers if row.get("activation_confirmed") is False and str(row.get("resolved_state") or "").upper() == "RUNNING")) or 0),
            activation_confirmed=sum(1 for row in workers if row.get("activation_confirmed") is True and str(row.get("resolved_state") or "").upper() == "RUNNING"),
            activation_source_summary=activation_summary,
            status_resume_workers=sum(1 for row in workers if row.get("checkpoint_resume") is True),
            ci_red=int(stats.get("ci_red", 0) or 0),
            master_progress=int(percent) if isinstance(percent, (int, float)) else None,
            last_event_at=max(event_times) if event_times else None,
            source_healthy=source_healthy,
            wake_path_healthy=bool(wake_path.get("healthy")),
            registry_drift=int(stats.get("registry_drift", sum(int(drift.get(k) or 0) for k in ("ledger_unrouted", "route_without_ledger", "legacy_unrouted", "superseded"))) or 0),
            legacy_unrouted=int(drift.get("legacy_unrouted") or 0),
            superseded=int(drift.get("superseded") or 0),
            malformed_master_requests=int(stats.get("malformed_master_requests", rejection.get("total", 0)) or 0),
            media_schema_ready=bool(media.get("schema_ready")),
            media_jobs_pending=_optional_int(media_summary.get("media_jobs_pending")),
            media_jobs_running=_optional_int(media_summary.get("media_jobs_running")),
            media_jobs_blocked=_optional_int(media_summary.get("media_jobs_blocked")),
            media_jobs_verified=_optional_int(media_summary.get("media_jobs_verified")),
            latest_media_archive_age=_optional_int(media_summary.get("latest_media_archive_age")),
            raw=payload,
        )

    def event_age_seconds(self, now: datetime | None = None) -> int | None:
        if self.last_event_at is None:
            return None
        return max(0, int(((now or datetime.now(timezone.utc)) - self.last_event_at).total_seconds()))


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None
