from __future__ import annotations

from types import SimpleNamespace

from auto_control_center.simulation import build_simulation
from custom_components.auto_control_center.diagnostics import async_get_config_entry_diagnostics
from custom_components.auto_control_center.model import ControlCenterSnapshot


async def test_diagnostics_are_bounded_and_redacted(hass):
    snapshot = ControlCenterSnapshot.from_payload(build_simulation(worker_count=32))
    coordinator = SimpleNamespace(
        connected=True, source_stale=False, last_successful_refresh=None,
        accepted_updates=2, duplicate_updates=7, data=snapshot,
    )
    entry = SimpleNamespace(
        unique_id="auto-control-center-local-v1",
        data={"url": "http://127.0.0.1:8877?token=supersecret"},
        runtime_data=SimpleNamespace(coordinator=coordinator),
    )
    result = await async_get_config_entry_diagnostics(hass, entry)
    text = str(result)
    assert "supersecret" not in text
    assert "127.0.0.1" not in text
    assert "workers_total" in text
    assert "routes" not in text
    assert "library_target" not in text
