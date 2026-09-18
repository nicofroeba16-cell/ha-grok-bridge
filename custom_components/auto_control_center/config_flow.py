from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import AutoControlCenterClient, ControlCenterClientError, normalize_base_url
from .const import CONF_URL, DEFAULT_URL, DOMAIN, INSTANCE_UID


async def validate_input(hass: HomeAssistant, data: dict) -> dict:
    try:
        url = normalize_base_url(data[CONF_URL])
    except ValueError as exc:
        raise ControlCenterClientError("loopback_required") from exc
    snapshot = await AutoControlCenterClient(async_get_clientsession(hass), url).async_get_dashboard()
    if not snapshot.source_healthy:
        raise ControlCenterClientError("source_unhealthy")
    return {"title": "AUTO Control Center", "url": url}


class AutoControlCenterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(INSTANCE_UID)
            self._abort_if_unique_id_configured()
            try:
                info = await validate_input(self.hass, user_input)
            except ControlCenterClientError as exc:
                code = str(exc)
                errors["base"] = code if code in {"loopback_required", "source_unhealthy"} else "cannot_connect"
            else:
                return self.async_create_entry(title=info["title"], data={CONF_URL: info["url"]})
        schema = vol.Schema({vol.Required(CONF_URL, default=DEFAULT_URL): str})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
