"""Services of the RTSP Camera Manager integration.

The PTZ service mirrors ``onvif.ptz`` (pan/tilt/zoom/speed/continuous_duration/
preset/move_mode), so an automation written for an ONVIF camera keeps working after
changing the domain - plus ``action`` for the plain actions of the add-on.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    PTZ_ACTIONS,
    PTZ_DEFAULT_DURATION,
    PTZ_DEFAULT_SPEED,
    PTZ_MAX_SPEED,
    SERVICE_PTZ,
    SERVICE_PTZ_HOME,
)
from .ptz import direction_from_service

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - only for the type annotations
    from .camera import RtspCamera

MOVE_MODES = ("ContinuousMove", "RelativeMove", "AbsoluteMove", "GotoPreset", "Stop")

PTZ_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional("pan"): vol.In(["LEFT", "RIGHT"]),
        vol.Optional("tilt"): vol.In(["UP", "DOWN"]),
        vol.Optional("zoom"): vol.In(["ZOOM_IN", "ZOOM_OUT"]),
        vol.Optional("distance"): vol.Coerce(float),
        vol.Optional("speed"): vol.Coerce(float),
        vol.Optional("continuous_duration"): vol.Coerce(float),
        vol.Optional("preset"): cv.string,
        vol.Optional("move_mode"): vol.In(MOVE_MODES),
        vol.Optional("action"): vol.In(PTZ_ACTIONS),
    }
)

PTZ_HOME_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional("speed"): vol.Coerce(float),
    }
)


def resolve_action(data: dict[str, Any]) -> str:
    """Return the action a service call asks for.

    ``action`` wins, then the ONVIF style move modes, then the pan/tilt/zoom
    fields. A call without any of them is a mistake and reported as such.
    """
    explicit = str(data.get("action") or "").strip().lower()
    if explicit:
        return explicit

    move_mode = str(data.get("move_mode") or "").strip()
    preset = str(data.get("preset") or "").strip()
    if move_mode == "Stop":
        return "stop"
    if move_mode == "GotoPreset" or (preset and not any(data.get(f) for f in ("pan", "tilt", "zoom"))):
        return "preset"

    direction = direction_from_service(data)
    if direction:
        return direction

    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="ptz_action_missing",
    )


def resolve_speed(data: dict[str, Any]) -> int | None:
    """Translate the 0..1 speed of Home Assistant into the camera scale (1..8)."""
    value = data.get("speed")
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return PTZ_DEFAULT_SPEED
    if number <= 1:
        return max(1, round(number * PTZ_MAX_SPEED))
    return max(1, min(PTZ_MAX_SPEED, round(number)))


def resolve_duration(data: dict[str, Any]) -> float | None:
    """Return how long a continuous move should run."""
    value = data.get("continuous_duration")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _resolve_cameras(hass: HomeAssistant, entity_ids: Any) -> list[RtspCamera]:
    """Return the camera entities a service call is addressed to."""
    from .camera import RtspCamera

    component = hass.data.get("camera")
    cameras: list[RtspCamera] = []
    for entity_id in entity_ids or []:
        entity = component.get_entity(entity_id) if component is not None else None
        if not isinstance(entity, RtspCamera):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="ptz_unknown_camera",
                translation_placeholders={"entity_id": str(entity_id)},
            )
        cameras.append(entity)
    if not cameras:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="ptz_no_target",
        )
    return cameras


async def _async_handle_ptz(call: ServiceCall) -> None:
    """Handle the ``rtsp_cameras.ptz`` service."""
    data = {key: value for key, value in call.data.items() if key != ATTR_ENTITY_ID}
    for camera in _resolve_cameras(call.hass, call.data.get(ATTR_ENTITY_ID)):
        await camera.async_ptz(**data)


async def _async_handle_ptz_home(call: ServiceCall) -> None:
    """Handle the ``rtsp_cameras.ptz_home`` service."""
    data = {key: value for key, value in call.data.items() if key != ATTR_ENTITY_ID}
    for camera in _resolve_cameras(call.hass, call.data.get(ATTR_ENTITY_ID)):
        await camera.async_ptz_home(**data)


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the PTZ services once per Home Assistant instance."""
    data = hass.data.setdefault(DOMAIN, {})
    if data.get("ptz_services_registered"):
        return
    data["ptz_services_registered"] = True

    hass.services.async_register(
        DOMAIN, SERVICE_PTZ, _async_handle_ptz, schema=PTZ_SERVICE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PTZ_HOME, _async_handle_ptz_home, schema=PTZ_HOME_SERVICE_SCHEMA
    )
    _LOGGER.debug(
        "Registered the PTZ services %s.%s and %s.%s",
        DOMAIN,
        SERVICE_PTZ,
        DOMAIN,
        SERVICE_PTZ_HOME,
    )


def default_duration() -> float:
    """Return the duration used when a call does not say anything."""
    return PTZ_DEFAULT_DURATION


__all__ = [
    "PTZ_HOME_SERVICE_SCHEMA",
    "PTZ_SERVICE_SCHEMA",
    "async_register_services",
    "default_duration",
    "resolve_action",
    "resolve_duration",
    "resolve_speed",
]
