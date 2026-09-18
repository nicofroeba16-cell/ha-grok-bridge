from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AutoControlCenterConfigEntry
from .const import INSTANCE_UID
from .coordinator import AutoControlCenterCoordinator


class AutoControlCenterEntity(CoordinatorEntity[AutoControlCenterCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, entry: AutoControlCenterConfigEntry, key: str) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self._attr_unique_id = f"{INSTANCE_UID}_{key}"
        self._attr_device_info = {
            "identifiers": {("auto_control_center", INSTANCE_UID)},
            "name": "AUTO Control Center",
            "manufacturer": "Local read-only integration",
            "model": "Control Center v3",
        }

    @property
    def available(self) -> bool:
        return (
            self.coordinator.data is not None
            and self.coordinator.last_update_success
            and not self.coordinator.source_stale
        )
