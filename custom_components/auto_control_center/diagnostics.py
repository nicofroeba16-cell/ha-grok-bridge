from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import AutoControlCenterConfigEntry

TO_REDACT = {
    "url", "route", "routes", "destination", "token", "secret", "password",
    "authorization", "cookie", "path", "library_target", "last_error",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AutoControlCenterConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data.coordinator
    snapshot = coordinator.data
    summary = None
    if snapshot is not None:
        summary = {
            "signature": snapshot.signature,
            "workers_total": snapshot.workers_total,
            "workers_running": snapshot.workers_running,
            "workers_blocked": snapshot.workers_blocked,
            "workers_waiting": snapshot.workers_waiting,
            "workers_ready_done": snapshot.workers_ready_done,
            "wake_uncertain": snapshot.wake_uncertain,
            "activation_unconfirmed": snapshot.activation_unconfirmed,
            "activation_confirmed": snapshot.activation_confirmed,
            "activation_source_summary": snapshot.activation_source_summary,
            "status_resume_workers": snapshot.status_resume_workers,
            "ci_red": snapshot.ci_red,
            "source_healthy": snapshot.source_healthy,
            "wake_path_healthy": snapshot.wake_path_healthy,
            "registry_drift": snapshot.registry_drift,
            "malformed_master_requests": snapshot.malformed_master_requests,
            "media_schema_ready": snapshot.media_schema_ready,
        }
    payload = {
        "entry": {"unique_id": entry.unique_id, "data": dict(entry.data)},
        "runtime": {
            "connected": coordinator.connected,
            "source_stale": coordinator.source_stale,
            "last_successful_refresh": coordinator.last_successful_refresh,
            "accepted_updates": coordinator.accepted_updates,
            "duplicate_updates": coordinator.duplicate_updates,
        },
        "summary": summary,
    }
    return async_redact_data(payload, TO_REDACT)
