"""PTZ for the RTSP Camera Manager integration.

The add-on publishes ready to use HTTP commands per action (see the PTZ section of
the add-on documentation). Home Assistant only fills in the values of the moment -
speed, preset number and the direction of a stop command - and sends the request.
That keeps this integration free of vendor tables and works for every camera the
add-on can talk to.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import PTZ_TIMEOUT_SECONDS
from .models import PtzConfig

_LOGGER = logging.getLogger(__name__)

#: Home Assistant states "pan"/"tilt" the way ONVIF does.
ONVIF_DIRECTIONS = {
    "pan": {"LEFT": "left", "RIGHT": "right"},
    "tilt": {"UP": "up", "DOWN": "down"},
    "zoom": {"ZOOM_IN": "zoom_in", "ZOOM_OUT": "zoom_out"},
}


@dataclass(slots=True)
class PtzOutcome:
    """What a PTZ command did."""

    action: str
    url: str
    status: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Return True when the camera accepted the command."""
        return self.error is None


def render(
    config: PtzConfig,
    action: str,
    *,
    speed: int | None = None,
    preset: str | None = None,
    direction: str | None = None,
    seconds: float | None = None,
) -> str | None:
    """Return the command of an action with the current values filled in."""
    template = config.commands.get(action)
    if not template:
        return None

    values: dict[str, Any] = {"speed": speed if speed is not None else config.speed}
    if preset is not None:
        values["preset"] = str(preset)
    if direction:
        values["direction"] = str(config.stop_codes.get(direction, direction))
    if seconds is not None:
        values["seconds"] = f"{float(seconds):g}"

    command = template
    for name, value in values.items():
        command = command.replace("{" + name + "}", str(value))
    return command


def parse_command(command: str) -> tuple[str, str, bytes | None]:
    """Split a command into method, URL and optional body."""
    method, _, rest = command.partition(" ")
    url, _, body = rest.strip().partition(" ")
    return method.upper() or "GET", url, body.encode("utf-8") if body else None


async def async_send(hass: HomeAssistant, command: str) -> PtzOutcome:
    """Send one PTZ command with the shared aiohttp session."""
    method, url, body = parse_command(command)
    action = command
    session = async_get_clientsession(hass)
    headers = {"Content-Type": "application/xml"} if body else {}
    try:
        async with asyncio.timeout(PTZ_TIMEOUT_SECONDS):
            async with session.request(method, url, data=body, headers=headers) as response:
                status = int(response.status)
                if status >= HTTPStatus.BAD_REQUEST:
                    return PtzOutcome(
                        action=action,
                        url=url,
                        status=status,
                        error=f"HTTP {status}",
                    )
                return PtzOutcome(action=action, url=url, status=status)
    except TimeoutError:
        return PtzOutcome(action=action, url=url, error="timeout")
    except aiohttp.ClientError as err:
        return PtzOutcome(action=action, url=url, error=str(err))


async def async_execute(
    hass: HomeAssistant,
    config: PtzConfig | None,
    action: str,
    *,
    speed: int | None = None,
    preset: str | None = None,
    direction: str | None = None,
    seconds: float | None = None,
) -> PtzOutcome:
    """Render and send a command, raising a readable error when it fails."""
    if config is None or not config.enabled:
        raise HomeAssistantError(
            translation_domain="rtsp_cameras",
            translation_key="ptz_not_configured",
        )

    command = render(
        config, action, speed=speed, preset=preset, direction=direction, seconds=seconds
    )
    if command is None:
        raise HomeAssistantError(
            translation_domain="rtsp_cameras",
            translation_key="ptz_action_unsupported",
            translation_placeholders={"action": action},
        )

    outcome = await async_send(hass, command)
    if not outcome.ok:
        _LOGGER.warning("PTZ %s failed for %s: %s", action, outcome.url, outcome.error)
        raise HomeAssistantError(
            translation_domain="rtsp_cameras",
            translation_key="ptz_failed",
            translation_placeholders={"action": action, "error": str(outcome.error)},
        )
    return outcome


async def async_move(
    hass: HomeAssistant,
    config: PtzConfig,
    direction: str,
    *,
    speed: int | None = None,
    duration: float | None = None,
) -> PtzOutcome:
    """Move into a direction and - for continuous APIs - stop afterwards."""
    outcome = await async_execute(hass, config, direction, speed=speed)

    if config.uses("stop"):
        pause = duration if duration is not None else 0.0
        if pause:
            await asyncio.sleep(pause)
        await async_execute(
            hass, config, "stop", speed=speed, direction=direction, seconds=pause or None
        )
    return outcome


def direction_from_service(data: dict[str, Any]) -> str | None:
    """Translate the ONVIF style pan/tilt/zoom fields into an action."""
    for field, mapping in ONVIF_DIRECTIONS.items():
        value = str(data.get(field) or "").strip().upper()
        if value and value in mapping:
            return mapping[value]
    return None
