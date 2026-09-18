from __future__ import annotations

from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from auto_control_center.simulation import build_simulation
from custom_components.auto_control_center.const import CONF_URL, DEFAULT_URL, DOMAIN, INSTANCE_UID


async def test_setup_unload_reload_contract_is_isolated(hass, aioclient_mock):
    aioclient_mock.get(f"{DEFAULT_URL}/api/dashboard", json=build_simulation(worker_count=16))
    entry = MockConfigEntry(domain=DOMAIN, title="ACC", data={CONF_URL: DEFAULT_URL}, unique_id=INSTANCE_UID)
    entry.add_to_hass(hass)
    with patch("custom_components.auto_control_center.coordinator.AutoControlCenterCoordinator.async_start", new=AsyncMock()) as start:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        start.assert_awaited_once()
    coordinator = entry.runtime_data.coordinator
    with patch.object(coordinator, "async_stop", new=AsyncMock()) as stop:
        assert await hass.config_entries.async_unload(entry.entry_id)
        stop.assert_awaited_once()
