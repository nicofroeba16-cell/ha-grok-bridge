from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AutoControlCenterConfigEntry
from .entity import AutoControlCenterEntity


BINARY_SENSORS = (
    BinarySensorEntityDescription(key="source_healthy", translation_key="source_healthy", device_class=BinarySensorDeviceClass.CONNECTIVITY),
    BinarySensorEntityDescription(key="source_stale", translation_key="source_stale", device_class=BinarySensorDeviceClass.PROBLEM),
    BinarySensorEntityDescription(key="wake_path_healthy", translation_key="wake_path_healthy", device_class=BinarySensorDeviceClass.CONNECTIVITY),
    BinarySensorEntityDescription(key="master_request_rejection", translation_key="master_request_rejection", device_class=BinarySensorDeviceClass.PROBLEM),
    BinarySensorEntityDescription(key="media_archive_schema_ready", translation_key="media_archive_schema_ready", device_class=BinarySensorDeviceClass.CONNECTIVITY),
)


async def async_setup_entry(hass, entry: AutoControlCenterConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities(AutoControlCenterBinarySensor(entry, description) for description in BINARY_SENSORS)


class AutoControlCenterBinarySensor(AutoControlCenterEntity, BinarySensorEntity):
    entity_description: BinarySensorEntityDescription

    def __init__(self, entry: AutoControlCenterConfigEntry, description: BinarySensorEntityDescription) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        key = self.entity_description.key
        if key == "source_stale":
            return self.coordinator.source_stale
        if key == "source_healthy":
            return bool(data.source_healthy and self.coordinator.connected and not self.coordinator.source_stale)
        if key == "wake_path_healthy":
            return data.wake_path_healthy
        if key == "master_request_rejection":
            return data.malformed_master_requests > 0
        if key == "media_archive_schema_ready":
            return data.media_schema_ready
        return None

    @property
    def available(self) -> bool:
        if self.entity_description.key == "source_stale":
            return self.coordinator.data is not None
        return super().available
