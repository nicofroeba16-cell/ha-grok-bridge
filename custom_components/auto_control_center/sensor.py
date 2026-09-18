from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AutoControlCenterConfigEntry
from .entity import AutoControlCenterEntity
from .model import ControlCenterSnapshot


@dataclass(frozen=True, kw_only=True)
class AutoControlCenterSensorDescription(SensorEntityDescription):
    value_fn: Callable[[AutoControlCenterConfigEntry, ControlCenterSnapshot], object | None]


SENSORS: tuple[AutoControlCenterSensorDescription, ...] = (
    AutoControlCenterSensorDescription(key="workers_total", translation_key="workers_total", value_fn=lambda e, s: s.workers_total),
    AutoControlCenterSensorDescription(key="workers_running", translation_key="workers_running", value_fn=lambda e, s: s.workers_running),
    AutoControlCenterSensorDescription(key="workers_blocked", translation_key="workers_blocked", value_fn=lambda e, s: s.workers_blocked),
    AutoControlCenterSensorDescription(key="workers_waiting", translation_key="workers_waiting", value_fn=lambda e, s: s.workers_waiting),
    AutoControlCenterSensorDescription(key="workers_ready_done", translation_key="workers_ready_done", value_fn=lambda e, s: s.workers_ready_done),
    AutoControlCenterSensorDescription(key="wake_uncertain", translation_key="wake_uncertain", value_fn=lambda e, s: s.wake_uncertain),
    AutoControlCenterSensorDescription(key="activation_unconfirmed", translation_key="activation_unconfirmed", value_fn=lambda e, s: s.activation_unconfirmed),
    AutoControlCenterSensorDescription(key="activation_confirmed", translation_key="activation_confirmed", value_fn=lambda e, s: s.activation_confirmed),
    AutoControlCenterSensorDescription(key="activation_source", translation_key="activation_source", value_fn=lambda e, s: s.activation_source_summary),
    AutoControlCenterSensorDescription(key="status_resume_workers", translation_key="status_resume_workers", value_fn=lambda e, s: s.status_resume_workers),
    AutoControlCenterSensorDescription(key="ci_red", translation_key="ci_red", value_fn=lambda e, s: s.ci_red),
    AutoControlCenterSensorDescription(key="master_progress", translation_key="master_progress", native_unit_of_measurement=PERCENTAGE, value_fn=lambda e, s: s.master_progress),
    AutoControlCenterSensorDescription(key="registry_drift", translation_key="registry_drift", value_fn=lambda e, s: s.registry_drift),
    AutoControlCenterSensorDescription(key="legacy_unrouted", translation_key="legacy_unrouted", value_fn=lambda e, s: s.legacy_unrouted),
    AutoControlCenterSensorDescription(key="superseded", translation_key="superseded", value_fn=lambda e, s: s.superseded),
    AutoControlCenterSensorDescription(key="malformed_master_requests", translation_key="malformed_master_requests", value_fn=lambda e, s: s.malformed_master_requests),
    AutoControlCenterSensorDescription(key="media_jobs_pending", translation_key="media_jobs_pending", value_fn=lambda e, s: s.media_jobs_pending),
    AutoControlCenterSensorDescription(key="media_jobs_running", translation_key="media_jobs_running", value_fn=lambda e, s: s.media_jobs_running),
    AutoControlCenterSensorDescription(key="media_jobs_blocked", translation_key="media_jobs_blocked", value_fn=lambda e, s: s.media_jobs_blocked),
    AutoControlCenterSensorDescription(key="media_jobs_verified", translation_key="media_jobs_verified", value_fn=lambda e, s: s.media_jobs_verified),
    AutoControlCenterSensorDescription(key="latest_media_archive_age", translation_key="latest_media_archive_age", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.SECONDS, value_fn=lambda e, s: s.latest_media_archive_age),
    AutoControlCenterSensorDescription(key="last_successful_refresh", translation_key="last_successful_refresh", device_class=SensorDeviceClass.TIMESTAMP, value_fn=lambda e, s: e.runtime_data.coordinator.last_successful_refresh),
    AutoControlCenterSensorDescription(key="last_event_age", translation_key="last_event_age", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.SECONDS, value_fn=lambda e, s: s.event_age_seconds()),
)


async def async_setup_entry(hass, entry: AutoControlCenterConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities(AutoControlCenterSensor(entry, description) for description in SENSORS)


class AutoControlCenterSensor(AutoControlCenterEntity, SensorEntity):
    entity_description: AutoControlCenterSensorDescription

    def __init__(self, entry: AutoControlCenterConfigEntry, description: AutoControlCenterSensorDescription) -> None:
        super().__init__(entry, description.key)
        self.entry = entry
        self.entity_description = description

    @property
    def available(self) -> bool:
        if self.entity_description.key in {"last_successful_refresh", "last_event_age"}:
            return self.coordinator.data is not None
        return super().available

    @property
    def native_value(self) -> object | None:
        data = self.coordinator.data
        if data is None:
            return None
        return self.entity_description.value_fn(self.entry, data)
