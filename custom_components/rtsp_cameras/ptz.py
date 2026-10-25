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

from .const import (
    DVRIP_TIMEOUT_SECONDS,
    PTZ_DEFAULT_DURATION,
    PTZ_STEP_AXES,
    PTZ_STEP_PAUSE_SECONDS,
    PTZ_TIMEOUT_SECONDS,
)
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


#: Which rung of its ladder each camera stands on, per camera id and axis. A step is a
#: *position* - the only bounded move a camera without a stop has - so a walk has to know
#: where it starts. Anything that leaves the camera between two rungs forgets the memory
#: (see ``forget_step``); the panel of the add-on keeps the same memory on its side.
_STEP_INDEX: dict[str, dict[str, int]] = {}


def step_axis(action: str) -> str | None:
    """Return the axis a direction walks, or None when it is no direction."""
    for axis, arrows in PTZ_STEP_AXES.items():
        if action in arrows:
            return axis
    return None


def forget_step(camera_id: str) -> None:
    """Forget where a camera stands on its ladders - it was moved outside one."""
    if camera_id:
        _STEP_INDEX.pop(camera_id, None)


def step_position(
    camera_id: str, config: PtzConfig | None
) -> dict[str, dict[str, int | None]] | None:
    """Return where a camera stands on its ladders, per axis, or None without ladders.

    This is the coordinate model of the walk: every move of the camera rewrites it (see
    ``next_step`` and ``remember_preset``), and a rung is a whole position, so a step of one
    axis changes the other coordinate as well - the other axis is carried back to its crossing
    rung (see ``step_drag``). The entity shows it, so a user can see *where* the camera stands
    and not only that a rung was reached.
    """
    if config is None:
        return None
    position: dict[str, dict[str, int | None]] = {}
    rungs = _STEP_INDEX.get(camera_id) or {}
    for axis in PTZ_STEP_AXES:
        slots = config.step_slots(axis)
        if not slots:
            continue
        index = rungs.get(axis)
        known = isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(slots)
        position[axis] = {
            "index": index if known else None,
            "slot": slots[index] if known else None,
            "rungs": len(slots),
            "crossing": config.crossing(axis),
        }
    return position or None


def step_slots(config: PtzConfig | None, action: str) -> tuple[int, ...]:
    """Return the ladder a direction walks, empty when the axis has none."""
    axis = step_axis(action)
    if config is None or axis is None:
        return ()
    return config.step_slots(axis)


def rung_of(config: PtzConfig | None, preset: Any) -> tuple[str, int] | None:
    """Return the axis and index of the rung a preset slot is, or None."""
    text = str(preset or "").strip()
    if config is None or not text:
        return None
    for axis in PTZ_STEP_AXES:
        for index, slot in enumerate(config.step_slots(axis)):
            if str(slot) == text:
                return axis, index
    return None


def crossing_index(config: PtzConfig | None, axis: str) -> int | None:
    """Return the rung of an axis whose ladder crosses the other one, or None.

    A ladder is stored along one axis at a fixed value of the other one, so the two ladders
    of a camera meet in a single *crossing*, and every rung of a ladder carries the crossing
    value of the other axis. The add-on names that rung in ``step_crossings`` of the PTZ
    block (``PtzConfig.step_crossings``) - it is the place the ladder was built at, and a
    rung stored later moves the middle of the list but not that place.
    """
    if config is None:
        return None
    return config.crossing(axis)


def _mark_crossing(
    rungs: dict[str, int], config: PtzConfig | None, axis: str, index: int
) -> None:
    """Record where the other axis stands after a rung of ``axis`` was reached.

    A preset is a whole position and the two ladders of a camera are stored around one
    *crossing*: every rung of one ladder carries the crossing value of the other axis, which
    the camera file names in ``step_crossings``. Landing on a rung of either ladder therefore
    leaves the other axis exactly on *its* crossing rung - where it may be walked, and where
    the walk of that axis starts from (see ``next_step``).

    Recording that (instead of forgetting the other axis, which this did until 0.7.9) is what
    keeps both pairs of arrows alive: a forgotten axis was refused on its next press, and that
    press recorded the axis it walked, so the two pairs locked each other out.
    """
    for other in PTZ_STEP_AXES:
        if other == axis:
            continue
        sibling = crossing_index(config, other)
        if sibling is None:
            rungs.pop(other, None)
        else:
            rungs[other] = sibling


def remember_preset(camera_id: str, config: PtzConfig | None, preset: Any) -> None:
    """Remember the rung a preset leaves the camera on, or forget the ladder.

    A ``GotoPreset`` is the only bounded move a camera without a stop has, so a preset that
    *is* a rung keeps the walk where it is. Every other preset - and the home key, and a
    stop, which leaves the camera where it was released - leaves it between two rungs, and
    a walk may not start from there.

    A rung is a whole position, so reaching one leaves the other axis on its crossing rung
    (see ``_mark_crossing``): the walk of that axis continues from there.
    """
    rung = rung_of(config, preset)
    if rung is None:
        forget_step(camera_id)
        return
    axis, index = rung
    rungs = _STEP_INDEX.setdefault(camera_id, {})
    rungs[axis] = index
    _mark_crossing(rungs, config, axis, index)


def step_drag(
    camera_id: str, config: PtzConfig | None, action: str
) -> tuple[str, int] | None:
    """Return the axis a walk of ``action`` drags along, and how many rungs of it.

    The rungs of one ladder all carry the *crossing* value of the other axis, which is what
    keeps the other axis where it stands while a ladder is walked. Standing off that crossing
    along the other axis - the last walk was on the other ladder and it left its middle rung
    - the rung carries the other axis back to the crossing. Measured on the test set: a
    ``right`` of three rungs followed by an ``up`` sent the pan back to the column the tilt
    rungs were stored at.

    That drag cannot be avoided on a camera without a bounded relative move (see
    ``walks_the_ladder``): a tilt in place would need a rung stored at the pan of the moment,
    and a rung can only be written where the camera already stands. The walk is therefore
    sent, and this answer is what a caller reports - refusing it instead made both pairs of
    arrows dead as soon as a user alternated them (after one ``up`` every ``right`` was
    refused and after one ``right`` every ``up``).

    The answer is the axis the walk drags, and how many rungs of it: negative towards the
    first rung of that ladder, positive towards its last one.
    """
    axis = step_axis(action)
    if axis is None or config is None:
        return None
    other = next(name for name in PTZ_STEP_AXES if name != axis)
    if not config.step_slots(axis) or not config.step_slots(other):
        # An axis without rungs is never walked, so there is nothing a walk could drag.
        return None
    rungs = _STEP_INDEX.get(camera_id) or {}
    index = rungs.get(other)
    crossing = crossing_index(config, other)
    if index is None or crossing is None or index == crossing:
        # Unknown: the walk starts on the crossing (see ``next_step``). On the crossing
        # itself the rung of this axis carries the value the camera holds - no drag.
        return None
    return other, crossing - index


def walks_the_ladder(config: PtzConfig | None, action: str) -> bool:
    """Return True when a move has to walk the ladder instead of starting one.

    A camera whose stop cannot halt an axis (``PtzConfig.stops_in_place``) is driven to the
    limit of the axis by every direction command, and the capture behind it only drags the
    camera back afterwards. Such a camera is moved rung by rung along the ladder of stored
    positions instead - one bounded ``GotoPreset`` at a time - which is exactly what the
    panel of the add-on does while an arrow is held. A camera without a rung on that axis
    keeps its move and its own stop.
    """
    if config is None or config.stops_in_place:
        return False
    return bool(step_slots(config, action))


def next_step(
    camera_id: str, config: PtzConfig | None, action: str, count: int = 1
) -> tuple[int, int] | None:
    """Return the rung a step of ``action`` lands on, together with its index.

    ``None`` means the axis has no ladder. When the camera's rung is unknown (the first
    step of a session, or a move outside the ladder) the walk starts on the **crossing** rung
    of the ladder - the rung that shares its place with the other ladder (see
    ``crossing_index``) - and not at the end the direction comes from: starting at that end
    sweeps half of the axis on the very first press - the camera dives to the limit of the
    rung and climbs back from there, which is exactly the uncontrolled travel the ladder
    exists to avoid (measured on the test set: the first ``up`` of a camera standing at the
    middle of its tilt drove it down to the balustrade). A step that would leave the ladder
    stops on its first or its last rung, so a press always moves the camera somewhere it
    knows.
    """
    slots = step_slots(config, action)
    axis = step_axis(action)
    if axis is None or not slots:
        return None
    forward = action == PTZ_STEP_AXES[axis][0]
    rungs = _STEP_INDEX.setdefault(camera_id, {})
    index = rungs.get(axis)
    crossing = crossing_index(config, axis)
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(slots):
        start = index
    elif isinstance(crossing, int) and 0 <= crossing < len(slots):
        start = crossing
    else:
        start = len(slots) // 2
    count = max(1, abs(int(count)))
    target = max(0, min(len(slots) - 1, start + (count if forward else -count)))
    # A rung is a whole position, so reaching it leaves the other axis on its crossing rung -
    # where the walk of that axis continues from (see ``_mark_crossing``). What the rung does
    # to the other axis when the camera stands off the crossing is reported by
    # ``step_drag``, not refused: this camera has no rung at the other axis's position.
    rungs[axis] = target
    _mark_crossing(rungs, config, axis, target)
    return slots[target], target


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


#: The method word that separates the payloads of one DVRIP command.
DVRIP_STEP = " DVRIP "


def dvrip_payloads(rest: str) -> list[dict[str, Any]] | None:
    """Return the JSON payloads of a DVRIP body, in the order they have to be sent.

    ``rest`` is everything behind the ``DVRIP`` method of a command. A command may carry
    more than one payload: the stop of the ``xiongmai_dvrip`` profile captures the position
    of the release first and returns to it afterwards. Returns ``None`` when a part is not a
    JSON object, so a caller can report the invalid payload it was given.
    """
    payloads: list[dict[str, Any]] = []
    for part in rest.split(DVRIP_STEP):
        try:
            payload = json.loads(part.strip())
        except ValueError:
            return None
        if not isinstance(payload, dict) or not payload:
            return None
        payloads.append(payload)
    return payloads or None


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
    """Send the DVRIP payloads of a command with the credentials of the camera.

    A command may carry more than one payload, and they are sent in that order: the stop of
    the ``xiongmai_dvrip`` profile captures the position it is sent at first and returns to
    it afterwards. The payloads of one command share the connection of their login - the way
    the vendor SDK keeps one session per camera, and every stop shape that was measured on the
    device of the test set went out on a connection of its own.

    The deadline is the long one of this transport (``DVRIP_TIMEOUT_SECONDS``): the device
    answers the ``GotoPreset`` of that stop only once the axis arrived - measured 10 to
    16 s - and a shorter one would report a working command as a failure. It belongs to a
    single answer and is multiplied by the payloads of the command.
    """
    from .dvrip import DEFAULT_PORT, sequence_timeout
    from .dvrip import async_send_many as dvrip_send_many

    _method, payload, body = parse_command(command)
    rest = payload if body is None else f"{payload} {body.decode('utf-8', 'replace')}"
    payloads = dvrip_payloads(rest)
    if payloads is None:
        return PtzOutcome(action=command, url="dvrip", error="invalid_payload")

    host = (config.host if config else "") or ""
    port = (config.port if config else DEFAULT_PORT) or DEFAULT_PORT
    username = (config.username if config else "") or ""
    password = (config.password if config else "") or ""
    ok, error = True, None
    try:
        async with asyncio.timeout(sequence_timeout(DVRIP_TIMEOUT_SECONDS, payloads)):
            ok, error = await dvrip_send_many(
                host, port, username, password, payloads, timeout=DVRIP_TIMEOUT_SECONDS
            )
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


async def async_walk_steps(
    hass: HomeAssistant,
    config: PtzConfig,
    camera_id: str,
    direction: str,
    *,
    duration: float | None = None,
    speed: int | None = None,
) -> PtzOutcome | None:
    """Walk the ladder of stored positions of an axis, for ``duration`` seconds.

    One ``GotoPreset`` at a time: the answer of the camera comes when it has arrived, so
    the rungs never queue, and the walk ends on the rung the camera reaches when the time
    is over - what a release of an arrow does in the panel of the add-on. No direction
    command and no capture is sent: neither of them can end a move on a camera whose stop
    is a capture (see ``walks_the_ladder``).

    A duration of zero still walks one rung: a move that asks for nothing has to move the
    camera somewhere it knows, not start a sweep of the whole axis. The end of the ladder
    ends the walk, so a long duration can never ask for the same rung again and again.

    A ladder only holds its axis while the camera stands on it or on the crossing it shares
    with the other one: a walk off that crossing drags the other axis back to it, which is
    the price of a camera that only has whole positions (see ``step_drag``). That drag is
    logged and the walk is sent - refusing it left the user with arrows that did nothing.
    """
    drag = step_drag(camera_id, config, direction)
    if drag is not None:
        _LOGGER.info(
            "the %s walk of %s drags the %s by %s rungs", direction, camera_id, drag[0], -drag[1]
        )
    pause = PTZ_DEFAULT_DURATION if duration is None else max(0.0, float(duration))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + pause
    previous: int | None = None
    outcome: PtzOutcome | None = None
    while True:
        started = loop.time()
        landed = next_step(camera_id, config, direction)
        if landed is None:
            break
        slot, index = landed
        if index == previous:
            # The rung does not move any more: the ladder ends here.
            break
        previous = index
        outcome = await async_execute(hass, config, "preset", speed=speed, preset=str(slot))
        if loop.time() >= deadline:
            break
        rest = PTZ_STEP_PAUSE_SECONDS - (loop.time() - started)
        if rest > 0:
            await asyncio.sleep(rest)
    return outcome


def direction_from_service(data: dict[str, Any]) -> str | None:
    """Translate the ONVIF style pan/tilt/zoom fields into an action."""
    for field, mapping in ONVIF_DIRECTIONS.items():
        value = str(data.get(field) or "").strip().upper()
        if value and value in mapping:
            return mapping[value]
    return None
