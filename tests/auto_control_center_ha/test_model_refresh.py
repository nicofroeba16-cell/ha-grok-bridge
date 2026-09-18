from __future__ import annotations

import asyncio

from pytest_homeassistant_custom_component.common import MockConfigEntry

from auto_control_center.simulation import build_simulation
from custom_components.auto_control_center.client import ControlCenterClientError, StreamEvent
from custom_components.auto_control_center.const import DOMAIN
from custom_components.auto_control_center.coordinator import AutoControlCenterCoordinator, fallback_interval, reconnect_delay
from custom_components.auto_control_center.model import ControlCenterSnapshot


class FakeClient:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.polls = 0
        self.stream_calls = 0

    async def async_get_dashboard(self):
        self.polls += 1
        return self.snapshots[min(self.polls - 1, len(self.snapshots) - 1)]

    async def async_stream(self):
        self.stream_calls += 1
        if self.stream_calls == 1:
            raise ControlCenterClientError("stream_disconnected")
        yield StreamEvent("heartbeat", {"event_id": "heartbeat"})
        yield StreamEvent("dashboard", self.snapshots[-1].raw)
        await asyncio.sleep(3600)


def _snapshot(count: int) -> ControlCenterSnapshot:
    return ControlCenterSnapshot.from_payload(build_simulation(worker_count=count, event_count=10, wake_count=12))


def test_model_exposes_provenance_status_registry_rejection_and_media_contract():
    snapshot = ControlCenterSnapshot.from_payload(build_simulation(worker_count=32, wake_count=48))
    assert snapshot.activation_unconfirmed >= 1
    assert snapshot.activation_source_summary in {"mixed", "wake_uncertain", "wake_verified", "worker_report"}
    assert snapshot.status_resume_workers > 0
    assert snapshot.registry_drift > 0
    assert snapshot.malformed_master_requests == 1
    assert snapshot.media_schema_ready is True
    assert (snapshot.media_jobs_pending, snapshot.media_jobs_running, snapshot.media_jobs_blocked, snapshot.media_jobs_verified) == (1, 1, 1, 1)


def test_refresh_policy_is_bounded():
    assert 0.8 <= reconnect_delay(0, 0.0) <= 1.2
    assert reconnect_delay(20, 1.0) <= 30
    assert fallback_interval(0) == 12
    assert fallback_interval(119) == 12
    assert fallback_interval(120) == 30


async def test_duplicate_burst_does_not_churn(hass):
    entry = MockConfigEntry(domain=DOMAIN, title="ACC", data={}, unique_id="dedupe")
    entry.add_to_hass(hass)
    first, second = _snapshot(8), _snapshot(9)
    coordinator = AutoControlCenterCoordinator(hass, FakeClient([first, second]), config_entry=entry)
    coordinator.data = first
    coordinator._last_signature = first.signature
    assert coordinator._accept_snapshot(first) is False
    assert coordinator._accept_snapshot(second) is True
    accepted = coordinator.accepted_updates
    for _ in range(100):
        assert coordinator._accept_snapshot(second) is False
    assert coordinator.accepted_updates == accepted
    assert coordinator.duplicate_updates == 101


async def test_disconnect_reconnect_and_fallback_poll(hass, monkeypatch):
    first, second = _snapshot(8), _snapshot(10)
    client = FakeClient([first, second])
    entry = MockConfigEntry(domain=DOMAIN, title="ACC", data={}, unique_id="reconnect")
    entry.add_to_hass(hass)
    coordinator = AutoControlCenterCoordinator(hass, client, config_entry=entry)
    coordinator.data = first
    coordinator._last_signature = first.signature
    coordinator._mark_contact()
    monkeypatch.setattr("custom_components.auto_control_center.coordinator.reconnect_delay", lambda attempt, jitter=None: 0.01)
    monkeypatch.setattr("custom_components.auto_control_center.coordinator.fallback_interval", lambda outage: 0.01)
    task = asyncio.create_task(coordinator._push_loop())
    reconcile = asyncio.create_task(coordinator._reconcile_loop())
    await asyncio.sleep(0.08)
    await coordinator.async_stop()
    task.cancel(); reconcile.cancel()
    await asyncio.gather(task, reconcile, return_exceptions=True)
    assert client.stream_calls >= 2
    assert client.polls >= 1
    assert coordinator.accepted_updates >= 1


async def test_start_is_three_task_bounded_and_idempotent(hass):
    class BlockingClient:
        async def async_get_dashboard(self): return _snapshot(8)
        async def async_stream(self):
            await asyncio.sleep(3600)
            if False: yield None
    entry = MockConfigEntry(domain=DOMAIN, title="ACC", data={}, unique_id="bounded")
    entry.add_to_hass(hass)
    coordinator = AutoControlCenterCoordinator(hass, BlockingClient(), config_entry=entry)
    await coordinator.async_start()
    first = set(coordinator._tasks)
    assert len(first) == 3
    await coordinator.async_start()
    assert set(coordinator._tasks) == first
    await coordinator.async_stop()
    assert not coordinator._tasks
