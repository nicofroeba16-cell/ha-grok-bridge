from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.auto_control_center.binary_sensor import BINARY_SENSORS
from custom_components.auto_control_center.client import normalize_base_url
from custom_components.auto_control_center.const import DOMAIN
from custom_components.auto_control_center.sensor import SENSORS

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = ROOT / "custom_components" / "auto_control_center"


def test_manifest_and_platform_contract():
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    assert manifest["config_flow"] is True
    assert manifest["single_config_entry"] is True
    assert manifest["iot_class"] == "local_push"
    assert manifest["integration_type"] == "service"
    assert not (INTEGRATION / "services.yaml").exists()


def test_entity_cardinality_is_fixed_and_read_only():
    assert len(SENSORS) == 23
    assert len(BINARY_SENSORS) == 5
    assert len({item.key for item in SENSORS}) == 23
    assert len({item.key for item in BINARY_SENSORS}) == 5


def test_translations_cover_every_entity_in_english_and_german():
    expected = {
        "sensor": {description.translation_key for description in SENSORS},
        "binary_sensor": {description.translation_key for description in BINARY_SENSORS},
    }
    for language in ("en", "de"):
        payload = json.loads((INTEGRATION / "translations" / f"{language}.json").read_text())
        for platform, keys in expected.items():
            assert keys == set(payload["entity"][platform])


async def test_german_translation_loads_through_home_assistant(hass):
    from homeassistant.helpers.translation import async_get_translations

    values = await async_get_translations(hass, "de", "entity", integrations={DOMAIN})
    assert values["component.auto_control_center.entity.sensor.workers_total.name"] == "Worker gesamt"
    assert values["component.auto_control_center.entity.binary_sensor.source_stale.name"] == "Quelle veraltet"


def test_client_is_numeric_loopback_only():
    assert normalize_base_url("http://127.0.0.1:8877/") == "http://127.0.0.1:8877"
    assert normalize_base_url("http://127.42.1.5:8877") == "http://127.42.1.5:8877"
    assert normalize_base_url("http://[::1]:8877/") == "http://[::1]:8877"
    for value in (
        "http://localhost:8877",
        "http://192.168.1.20:8877",
        "https://127.0.0.1:8877",
        "http://user:pass@127.0.0.1:8877",
        "http://127.0.0.1:8877/?token=secret",
    ):
        with pytest.raises(ValueError):
            normalize_base_url(value)
