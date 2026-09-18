from __future__ import annotations

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from unittest.mock import AsyncMock, patch

from auto_control_center.simulation import build_simulation
from custom_components.auto_control_center.const import CONF_URL, DEFAULT_URL, DOMAIN, INSTANCE_UID


async def test_user_flow_success(hass, aioclient_mock):
    aioclient_mock.get(f"{DEFAULT_URL}/api/dashboard", json=build_simulation(worker_count=16))
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.auto_control_center.coordinator.AutoControlCenterCoordinator.async_start", new=AsyncMock()):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: DEFAULT_URL})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_URL] == DEFAULT_URL
    assert result["result"].unique_id == INSTANCE_UID


async def test_user_flow_fails_closed_for_unhealthy_source(hass, aioclient_mock):
    aioclient_mock.get(f"{DEFAULT_URL}/api/dashboard", json=build_simulation(worker_count=8, degraded=True))
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: DEFAULT_URL})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "source_unhealthy"


async def test_user_flow_rejects_non_loopback_before_network(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: "http://192.168.1.20:8877"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "loopback_required"


async def test_duplicate_unique_id_aborts(hass, aioclient_mock):
    existing = MockConfigEntry(domain=DOMAIN, title="ACC", data={CONF_URL: DEFAULT_URL}, unique_id=INSTANCE_UID)
    existing.add_to_hass(hass)
    aioclient_mock.get(f"{DEFAULT_URL}/api/dashboard", json=build_simulation(worker_count=8))
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] in {"single_instance_allowed", "already_configured"}
