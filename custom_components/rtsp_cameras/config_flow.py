"""Config and options flow for the RTSP Camera Manager integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import (
    CONF_CAMERAS_FILE,
    CONF_SCAN_INTERVAL,
    DEFAULT_CAMERAS_FILENAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    resolve_cameras_path,
)

_LOGGER = logging.getLogger(__name__)

TITLE = "RTSP Camera Manager"


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the shared config/options schema."""
    return vol.Schema(
        {
            vol.Required(
                CONF_CAMERAS_FILE,
                default=defaults.get(CONF_CAMERAS_FILE, DEFAULT_CAMERAS_FILENAME),
            ): str,
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
            ),
        }
    )


class RtspCamerasConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup of the integration."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the first (and only) setup step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._validate(user_input)
            if not errors:
                return self.async_create_entry(title=TITLE, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}),
            errors=errors,
        )

    def _validate(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Validate the camera file path and prepare its folder.

        The folder is created when it does not exist yet: the add-on publishes
        the camera file there, but it may not have run before the integration is
        added.
        """
        raw = str(user_input.get(CONF_CAMERAS_FILE, "")).strip()
        if not raw:
            return {CONF_CAMERAS_FILE: "invalid_path"}

        path = resolve_cameras_path(raw, self.hass.config.path())
        if path.is_dir():
            return {CONF_CAMERAS_FILE: "invalid_path"}

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            _LOGGER.warning("Cannot create the camera file folder %s: %s", path.parent, err)
            return {CONF_CAMERAS_FILE: "cannot_create"}
        return {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return RtspCamerasOptionsFlow()


class RtspCamerasOptionsFlow(OptionsFlow):
    """Handle the integration options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        defaults = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(defaults))