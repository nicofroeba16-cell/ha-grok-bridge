from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from auto_control_center.simulation import build_simulation
from custom_components.auto_control_center.const import CONF_URL, DEFAULT_URL, DOMAIN, INSTANCE_UID


async def _setup(hass, aioclient_mock, *, payload=None):
    payload = payload or build_simulation(worker_count=32, event_count=30, wake_count=48)
    aioclient_mock.get(f"{DEFAULT_URL}/api/dashboard", json=payload)
    entry = MockConfigEntry(domain=DOMAIN, title="AUTO Control Center", data={CONF_URL: DEFAULT_URL}, unique_id=INSTANCE_UID)
    entry.add_to_hass(hass)
    with patch("custom_components.auto_control_center.coordinator.AutoControlCenterCoordinator.async_start", new=AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, payload


async def test_fixed_entity_set_and_stable_unique_ids(hass, aioclient_mock):
    entry, payload = await _setup(hass, aioclient_mock)
    registry = er.async_get(hass)
    expected_sensor = {
        "workers_total", "workers_running", "workers_blocked", "workers_waiting", "workers_ready_done",
        "wake_uncertain", "activation_unconfirmed", "activation_confirmed", "activation_source",
        "status_resume_workers", "ci_red", "master_progress", "registry_drift", "legacy_unrouted",
        "superseded", "malformed_master_requests", "media_jobs_pending", "media_jobs_running",
        "media_jobs_blocked", "media_jobs_verified", "latest_media_archive_age",
        "last_successful_refresh", "last_event_age",
    }
    expected_binary = {"source_healthy", "source_stale", "wake_path_healthy", "master_request_rejection", "media_archive_schema_ready"}
    found = set()
    for key in expected_sensor:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{INSTANCE_UID}_{key}")
        assert entity_id is not None, key
        found.add(entity_id)
    for key in expected_binary:
        entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, f"{INSTANCE_UID}_{key}")
        assert entity_id is not None, key
        found.add(entity_id)
    assert len(found) == 28
    workers = registry.async_get_entity_id("sensor", DOMAIN, f"{INSTANCE_UID}_workers_total")
    assert hass.states.get(workers).state == str(payload["stats"]["workers"])
    assert entry.unique_id == INSTANCE_UID
    assert all("127.0.0.1" not in item.unique_id for item in registry.entities.values() if item.platform == DOMAIN)


async def test_unique_ids_survive_loopback_url_change(hass, aioclient_mock):
    entry, _ = await _setup(hass, aioclient_mock)
    registry = er.async_get(hass)
    before = {item.unique_id for item in registry.entities.values() if item.platform == DOMAIN}
    hass.config_entries.async_update_entry(entry, data={CONF_URL: "http://[::1]:8877"})
    after = {item.unique_id for item in registry.entities.values() if item.platform == DOMAIN}
    assert before == after


async def test_stale_state_never_claims_operational_values_current(hass, aioclient_mock):
    entry, _ = await _setup(hass, aioclient_mock)
    coordinator = entry.runtime_data.coordinator
    registry = er.async_get(hass)
    workers_id = registry.async_get_entity_id("sensor", DOMAIN, f"{INSTANCE_UID}_workers_total")
    refresh_id = registry.async_get_entity_id("sensor", DOMAIN, f"{INSTANCE_UID}_last_successful_refresh")
    stale_id = registry.async_get_entity_id("binary_sensor", DOMAIN, f"{INSTANCE_UID}_source_stale")
    healthy_id = registry.async_get_entity_id("binary_sensor", DOMAIN, f"{INSTANCE_UID}_source_healthy")
    coordinator.connected = True
    coordinator.source_stale = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(workers_id).state == "unavailable"
    assert hass.states.get(refresh_id).state != "unavailable"
    assert hass.states.get(stale_id).state == "on"
    assert hass.states.get(healthy_id).state in {"off", "unavailable"}
