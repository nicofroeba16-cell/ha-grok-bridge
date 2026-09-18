from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import AutoControlCenterClient, ControlCenterClientError
from .const import (
    DOMAIN,
    FALLBACK_FAST_SECONDS,
    FALLBACK_SLOW_AFTER_SECONDS,
    FALLBACK_SLOW_SECONDS,
    RECONNECT_MAX_SECONDS,
    RECONCILE_SECONDS,
    STALE_AFTER_SECONDS,
    STALE_WATCHDOG_SECONDS,
)
from .model import ControlCenterSnapshot

LOGGER = logging.getLogger(__name__)


def reconnect_delay(attempt: int, jitter: float | None = None) -> float:
    base = min(RECONNECT_MAX_SECONDS, float(2 ** min(max(attempt, 0), 6)))
    factor = random.uniform(0.8, 1.2) if jitter is None else 0.8 + max(0.0, min(1.0, jitter)) * 0.4
    return min(RECONNECT_MAX_SECONDS, base * factor)


def fallback_interval(outage_seconds: float) -> float:
    return FALLBACK_SLOW_SECONDS if outage_seconds >= FALLBACK_SLOW_AFTER_SECONDS else FALLBACK_FAST_SECONDS


class AutoControlCenterCoordinator(DataUpdateCoordinator[ControlCenterSnapshot]):
    def __init__(
        self,
        hass: HomeAssistant,
        client: AutoControlCenterClient,
        *,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=None,
            always_update=False,
        )
        self.client = client
        self.connected = False
        self.source_stale = True
        self.last_successful_refresh: datetime | None = None
        self.last_contact_monotonic = 0.0
        self.accepted_updates = 0
        self.duplicate_updates = 0
        self._last_signature = ""
        self._tasks: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self._outage_started = 0.0

    async def _async_update_data(self) -> ControlCenterSnapshot:
        try:
            snapshot = await self.client.async_get_dashboard()
        except ControlCenterClientError as exc:
            raise UpdateFailed("control_center_unavailable") from exc
        self._mark_contact()
        return snapshot

    def _mark_contact(self) -> None:
        self.last_contact_monotonic = time.monotonic()
        self.last_successful_refresh = datetime.now(timezone.utc)
        self.source_stale = False

    def _accept_snapshot(self, snapshot: ControlCenterSnapshot) -> bool:
        self._mark_contact()
        if snapshot.signature == self._last_signature:
            self.duplicate_updates += 1
            return False
        self._last_signature = snapshot.signature
        self.accepted_updates += 1
        self.async_set_updated_data(snapshot)
        return True

    async def async_start(self) -> None:
        if any(not task.done() for task in self._tasks):
            return
        self._stop.clear()
        loops = (
            ("push", self._push_loop()),
            ("reconcile", self._reconcile_loop()),
            ("stale-watchdog", self._stale_watchdog()),
        )
        for name, coro in loops:
            task = self.hass.async_create_background_task(coro, f"auto_control_center {name}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def async_stop(self) -> None:
        self._stop.set()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self.connected = False

    async def _push_loop(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                async for event in self.client.async_stream():
                    self.connected = True
                    self._outage_started = 0.0
                    attempt = 0
                    self._mark_contact()
                    if event.event == "dashboard":
                        self._accept_snapshot(ControlCenterSnapshot.from_payload(event.data))
                    if self._stop.is_set():
                        return
                raise ControlCenterClientError("stream_ended")
            except (ControlCenterClientError, OSError):
                self.connected = False
                if not self._outage_started:
                    self._outage_started = time.monotonic()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=reconnect_delay(attempt))
                except TimeoutError:
                    pass
                attempt += 1

    async def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            outage = max(0.0, time.monotonic() - self._outage_started) if self._outage_started else 0.0
            delay = RECONCILE_SECONDS if self.connected else fallback_interval(outage)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            try:
                snapshot = await self.client.async_get_dashboard()
            except ControlCenterClientError:
                continue
            self._accept_snapshot(snapshot)

    async def _stale_watchdog(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=STALE_WATCHDOG_SECONDS)
                return
            except TimeoutError:
                pass
            if not self.last_contact_monotonic:
                continue
            stale = time.monotonic() - self.last_contact_monotonic >= STALE_AFTER_SECONDS
            if stale != self.source_stale:
                self.source_stale = stale
                self.async_update_listeners()
