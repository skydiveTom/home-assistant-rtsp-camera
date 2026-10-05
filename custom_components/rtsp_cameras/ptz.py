"""PTZ for the RTSP Camera Manager integration.

The add-on publishes ready to use HTTP commands per action (see the PTZ section of
the add-on documentation). Home Assistant only fills in the values of the moment -
speed, preset number and the direction of a stop command - and sends the request.
That keeps this integration free of vendor tables and works for every camera the
add-on can talk to.

An answer of ``200 OK`` is not proof that the camera moved: Dahua and Xiongmai answer a
rejected command with ``200 OK`` and ``Error`` in the body. Such an answer is reported
as a failure as well, so an automation does not silently assume that the camera moved.
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


#: Answers that report a failure although the status line said ``2xx``. Several vendor
#: CGIs do that: Dahua and Xiongmai answer "Error" when a code, a channel or the
#: credentials are wrong, others a ``result=-1`` or a SOAP fault.
ANSWER_FAILURE_MARKERS = (
    "result=-1",
    "notauthorized",
    "not authorized",
    "unauthorized",
    "authentication",
    "result=-3",
    "<fault",
    "notsupported",
    "not supported",
    "invalidoperation",
)
#: Bodies that consist of nothing but a word - the whole answer is the failure report.
ANSWER_FAILURE_WORDS = ("error", "failed", "failure", "not support", "unsupported")
#: Bodies that are a web page: the command URL does not exist on the device, its web
#: interface served a page instead of running the command. Xiongmai devices answer each
#: path of their port 80 with ``200 OK`` and HTML, so the status line alone makes every
#: variant of a PTZ test look like a camera that answered.
ANSWER_HTML_MARKERS = ("<!doctype", "<html")
#: How much of such an answer is kept for the error message of the failed command.
MAX_ANSWER_LENGTH = 200


def answer_says_failure(snippet: str) -> bool:
    """Return True when a camera answered ``2xx`` and still reported a failure."""
    text = " ".join(str(snippet or "").split()).lower().strip(" .:!;")
    if not text:
        return False
    if any(marker in text for marker in ANSWER_FAILURE_MARKERS):
        return True
    if text.startswith(ANSWER_HTML_MARKERS):
        return True
    return text in ANSWER_FAILURE_WORDS


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


#: Which way each camera was moved last, keyed by camera id. A stop that does not say
#: which axis it should stop uses this memory instead of a guess: a guessed direction
#: does not stop a camera, it moves it - a Xiongmai device drives to its top position
#: when a stop says "up" (measured, see the changelog of 0.3.6). The panel of the add-on
#: keeps the same memory for itself.
_LAST_DIRECTION: dict[str, str] = {}


def remember_direction(camera_id: str, action: str) -> None:
    """Remember which axis a camera was moved along."""
    if camera_id and action:
        _LAST_DIRECTION[camera_id] = action


def last_direction(camera_id: str) -> str | None:
    """Return the axis a camera was moved along last, when it is known."""
    return _LAST_DIRECTION.get(camera_id) if camera_id else None


def needs_direction(config: PtzConfig, action: str) -> bool:
    """Return True when the command of an action asks for the ``{direction}`` value."""
    return "{direction}" in str(config.commands.get(action) or "")


def _speeds(
    config: PtzConfig,
    speed: int | None,
    speed_horizontal: int | None,
    speed_vertical: int | None,
) -> dict[str, int]:
    """Return the general speed and the speed of both axes of one command.

    A speed that a call passes is the speed of that whole move, so it drives both axes -
    an automation that asks for a slow move has to get a slow camera. The per axis speeds
    of the camera only fill in what the call leaves open.
    """
    general = speed if speed is not None else config.speed
    horizontal = speed_horizontal
    if horizontal is None:
        horizontal = speed if speed is not None else config.axis_speed(True)
    vertical = speed_vertical
    if vertical is None:
        vertical = speed if speed is not None else config.axis_speed(False)
    return {"speed": general, "speed_horizontal": horizontal, "speed_vertical": vertical}


def render(
    config: PtzConfig,
    action: str,
    *,
    speed: int | None = None,
    speed_horizontal: int | None = None,
    speed_vertical: int | None = None,
    preset: str | None = None,
    direction: str | None = None,
    seconds: float | None = None,
) -> str | None:
    """Return the command of an action with the current values filled in."""
    template = config.commands.get(action)
    if not template:
        return None

    values: dict[str, Any] = _speeds(config, speed, speed_horizontal, speed_vertical)
    if preset is not None:
        values["preset"] = str(preset)
    if direction:
        values["direction"] = str(config.stop_codes.get(direction, direction))
    elif "{direction}" in template:
        # A stop that does not name the axis it should stop is refused instead of being
        # answered with a guess, see ``_LAST_DIRECTION`` above.
        return None
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
                snippet = await response.text()
                if answer_says_failure(snippet):
                    answer = " ".join(snippet.split())[:MAX_ANSWER_LENGTH]
                    return PtzOutcome(
                        action=command,
                        url=url,
                        status=status,
                        error=f"camera answered: {answer}",
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
    speed_horizontal: int | None = None,
    speed_vertical: int | None = None,
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
        config,
        action,
        speed=speed,
        speed_horizontal=speed_horizontal,
        speed_vertical=speed_vertical,
        preset=preset,
        direction=direction,
        seconds=seconds,
    )
    if command is None:
        if action == "stop" and needs_direction(config, action):
            # The camera needs a direction to stop, and the caller did not say which
            # axis is moving (see ``last_direction`` for the memory that answers it).
            raise HomeAssistantError(
                translation_domain="rtsp_cameras",
                translation_key="ptz_direction_required",
            )
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
