"""PTZ for the RTSP Camera Manager integration.

The add-on publishes ready to use HTTP commands per action (see the PTZ section of
the add-on documentation). Home Assistant only fills in the values of the moment -
speed, preset number and the direction of a stop command - and sends the request.
That keeps this integration free of vendor tables and works for every camera the
add-on can talk to.
"""

from __future__ import annotations

import asyncio
import json
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
    elif "{direction}" in template:
        # A plain stop from an automation is still valid: the first code of the
        # profile (usually "up") stops that axis.
        values["direction"] = str(next(iter(config.stop_codes.values()), "up"))
    if seconds is not None:
        values["seconds"] = f"{float(seconds):g}"
    # These are already filled in by the add-on; they are repeated here so hand
    # written camera files work as well.
    values["channel"] = str(config.channel)
    if config.token:
        values["token"] = config.token
    values["port"] = str(config.port)

    command = template
    for name, value in values.items():
        command = command.replace("{" + name + "}", str(value))
    return command


def parse_command(command: str) -> tuple[str, str, bytes | None]:
    """Split a command into method, URL and optional body.

    ``DVRIP`` commands have the JSON payload where the URL would be.
    """
    method, _, rest = command.partition(" ")
    url, _, body = rest.strip().partition(" ")
    return method.upper() or "GET", url, body.encode("utf-8") if body else None


def is_dvrip(command: str) -> bool:
    """Return True when a command uses the Xiongmai DVRIP protocol."""
    return command.strip().upper().startswith("DVRIP ")


async def async_send(
    hass: HomeAssistant, command: str, config: PtzConfig | None = None
) -> PtzOutcome:
    """Send one PTZ command - over HTTP or, for DVRIP, over TCP 34567."""
    if is_dvrip(command):
        return await _async_send_dvrip(command, config)

    method, url, body = parse_command(command)
    session = async_get_clientsession(hass)
    headers = {}
    if body:
        headers["Content-Type"] = (
            "application/soap+xml; charset=utf-8"
            if b"Envelope" in body[:400]
            else "application/xml"
        )
    try:
        async with asyncio.timeout(PTZ_TIMEOUT_SECONDS):
            async with session.request(method, url, data=body, headers=headers) as response:
                status = int(response.status)
                if status >= HTTPStatus.BAD_REQUEST:
                    return PtzOutcome(
                        action=command, url=url, status=status, error=f"HTTP {status}"
                    )
                return PtzOutcome(action=command, url=url, status=status)
    except TimeoutError:
        return PtzOutcome(action=command, url=url, error="timeout")
    except aiohttp.ClientError as err:
        return PtzOutcome(action=command, url=url, error=str(err))


async def _async_send_dvrip(command: str, config: PtzConfig | None) -> PtzOutcome:
    """Send a DVRIP command with the credentials of the camera."""
    from .dvrip import DEFAULT_PORT
    from .dvrip import async_send as dvrip_send

    _method, payload, _body = parse_command(command)
    try:
        short: dict[str, Any] = json.loads(payload)
    except ValueError as err:
        return PtzOutcome(action=command, url="dvrip", error=f"invalid_payload: {err}")

    host = (config.host if config else "") or ""
    port = (config.port if config else DEFAULT_PORT) or DEFAULT_PORT
    username = (config.username if config else "") or ""
    password = (config.password if config else "") or ""
    try:
        async with asyncio.timeout(PTZ_TIMEOUT_SECONDS):
            ok, error = await dvrip_send(host, port, username, password, short)
    except TimeoutError:
        return PtzOutcome(action=command, url="dvrip", error="timeout")
    if not ok:
        return PtzOutcome(action=command, url="dvrip", error=str(error))
    return PtzOutcome(action=command, url="dvrip", status=200)


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

    outcome = await async_send(hass, command, config)
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
