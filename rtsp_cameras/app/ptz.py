"""PTZ support: vendor command templates, validation and execution.

Every PTZ command is a single string::

    "GET http://host/cgi-bin/ptz.cgi?action=start&code=Left&arg2={speed}"
    "PUT http://host/ISAPI/PTZCtrl/channels/1/continuous <?xml ...>...</PTZData>"

The first word is the HTTP method (GET/POST/PUT), the second the URL and everything
behind it the optional body. Placeholders in curly braces are filled in:

* when a camera is stored: ``{base}``, ``{username}``, ``{password}``, ``{channel}``
* when a command is sent: ``{speed}``, ``{preset}``, ``{direction}``, ``{seconds}``

``{direction}`` is replaced with the value of ``stop_codes`` for the direction that
is currently moving, because most vendor APIs need the direction on stop as well.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

_LOGGER = logging.getLogger(__name__)

#: Actions the panel and the integration understand.
PTZ_ACTIONS = (
    "up",
    "down",
    "left",
    "right",
    "zoom_in",
    "zoom_out",
    "home",
    "stop",
    "preset",
)
DIRECTION_ACTIONS = ("up", "down", "left", "right", "zoom_in", "zoom_out")
HTTP_METHODS = ("GET", "POST", "PUT")
MAX_COMMAND_LENGTH = 600
MAX_PRESETS = 16
MAX_SPEED = 8
DEFAULT_SPEED = 4
DEFAULT_CHANNEL = 1
COMMAND_TIMEOUT = 6.0
MAX_RESPONSE_SNIPPET = 200

#: Vendor presets. ``direction_codes`` translate our actions into the vendor codes
#: used by the stop command and by the axis ``move=`` values.
PTZ_PROFILES: dict[str, dict[str, Any]] = {
    "custom": {
        "label": "Custom (own commands)",
        "description": "Fill in the URLs from the documentation of your camera.",
        "commands": {},
        "direction_codes": {},
    },
    "axis": {
        "label": "Axis (VAPIX)",
        "description": "Axis cameras with the VAPIX PTZ CGI.",
        "commands": {
            "up": "GET {base}/axis-cgi/com/ptz.cgi?move=up&speed={speed}",
            "down": "GET {base}/axis-cgi/com/ptz.cgi?move=down&speed={speed}",
            "left": "GET {base}/axis-cgi/com/ptz.cgi?move=left&speed={speed}",
            "right": "GET {base}/axis-cgi/com/ptz.cgi?move=right&speed={speed}",
            "zoom_in": "GET {base}/axis-cgi/com/ptz.cgi?rzoom=100",
            "zoom_out": "GET {base}/axis-cgi/com/ptz.cgi?rzoom=-100",
            "home": "GET {base}/axis-cgi/com/ptz.cgi?move=home",
            "stop": "GET {base}/axis-cgi/com/ptz.cgi?move=stop",
            "preset": "GET {base}/axis-cgi/com/ptz.cgi?gotoserverpresetno={preset}",
        },
        "direction_codes": {
            "up": "up",
            "down": "down",
            "left": "left",
            "right": "right",
            "zoom_in": "zoom_in",
            "zoom_out": "zoom_out",
        },
    },
    "dahua": {
        "label": "Dahua / Amcrest",
        "description": "Dahua, Amcrest and other cameras with the ptz.cgi interface.",
        "commands": {
            "up": "GET {base}/cgi-bin/ptz.cgi?action=start&channel={channel}&code=Up"
            "&arg1=0&arg2={speed}&arg3=0",
            "down": "GET {base}/cgi-bin/ptz.cgi?action=start&channel={channel}&code=Down"
            "&arg1=0&arg2={speed}&arg3=0",
            "left": "GET {base}/cgi-bin/ptz.cgi?action=start&channel={channel}&code=Left"
            "&arg1=0&arg2={speed}&arg3=0",
            "right": "GET {base}/cgi-bin/ptz.cgi?action=start&channel={channel}&code=Right"
            "&arg1=0&arg2={speed}&arg3=0",
            "zoom_in": "GET {base}/cgi-bin/ptz.cgi?action=start&channel={channel}&code=ZoomTile"
            "&arg1=0&arg2={speed}&arg3=0",
            "zoom_out": "GET {base}/cgi-bin/ptz.cgi?action=start&channel={channel}&code=ZoomWide"
            "&arg1=0&arg2={speed}&arg3=0",
            "home": "GET {base}/cgi-bin/ptz.cgi?action=start&channel={channel}&code=Home"
            "&arg1=0&arg2=0&arg3=0",
            "stop": "GET {base}/cgi-bin/ptz.cgi?action=stop&channel={channel}&code={direction}"
            "&arg1=0&arg2=0&arg3=0",
            "preset": "GET {base}/cgi-bin/ptz.cgi?action=start&channel={channel}&code=GotoPreset"
            "&arg1=0&arg2={preset}&arg3=0",
        },
        "direction_codes": {
            "up": "Up",
            "down": "Down",
            "left": "Left",
            "right": "Right",
            "zoom_in": "ZoomTile",
            "zoom_out": "ZoomWide",
        },
    },
    "xiongmai": {
        "label": "Xiongmai / NETSurveillance (DVR & NVR)",
        "description": "Devices with an RTSP URL like "
        "rtsp://host:554/user=admin&password=&channel=1&stream=0.sdp",
        "commands": {
            "up": "GET {base}/cgi-bin/ptz.cgi?action=start&channel={channel}"
            "&code=DirectionUp&arg1=0&arg2={speed}&arg3=0",
            "down": "GET {base}/cgi-bin/ptz.cgi?action=start&channel={channel}"
            "&code=DirectionDown&arg1=0&arg2={speed}&arg3=0",
            "left": "GET {base}/cgi-bin/ptz.cgi?action=start&channel={channel}"
            "&code=DirectionLeft&arg1=0&arg2={speed}&arg3=0",
            "right": "GET {base}/cgi-bin/ptz.cgi?action=start&channel={channel}"
            "&code=DirectionRight&arg1=0&arg2={speed}&arg3=0",
            "zoom_in": "GET {base}/cgi-bin/ptz.cgi?action=start&channel={channel}"
            "&code=ZoomTile&arg1=0&arg2={speed}&arg3=0",
            "zoom_out": "GET {base}/cgi-bin/ptz.cgi?action=start&channel={channel}"
            "&code=ZoomWide&arg1=0&arg2={speed}&arg3=0",
            "stop": "GET {base}/cgi-bin/ptz.cgi?action=stop&channel={channel}&code={direction}"
            "&arg1=0&arg2=0&arg3=0",
            "preset": "GET {base}/cgi-bin/ptz.cgi?action=start&channel={channel}"
            "&code=GotoPreset&arg1=0&arg2={preset}&arg3=0",
        },
        "direction_codes": {
            "up": "DirectionUp",
            "down": "DirectionDown",
            "left": "DirectionLeft",
            "right": "DirectionRight",
            "zoom_in": "ZoomTile",
            "zoom_out": "ZoomWide",
        },
    },
    "hikvision": {
        "label": "Hikvision (ISAPI)",
        "description": "Hikvision IP cameras and NVRs with the ISAPI PTZ interface.",
        "commands": {
            "up": 'PUT {base}/ISAPI/PTZCtrl/channels/{channel}/continuous <?xml version="1.0" '
            'encoding="UTF-8"?><PTZData><pan>0</pan><tilt>60</tilt></PTZData>',
            "down": 'PUT {base}/ISAPI/PTZCtrl/channels/{channel}/continuous <?xml version="1.0" '
            'encoding="UTF-8"?><PTZData><pan>0</pan><tilt>-60</tilt></PTZData>',
            "left": 'PUT {base}/ISAPI/PTZCtrl/channels/{channel}/continuous <?xml version="1.0" '
            'encoding="UTF-8"?><PTZData><pan>-60</pan><tilt>0</tilt></PTZData>',
            "right": 'PUT {base}/ISAPI/PTZCtrl/channels/{channel}/continuous <?xml version="1.0" '
            'encoding="UTF-8"?><PTZData><pan>60</pan><tilt>0</tilt></PTZData>',
            "zoom_in": 'PUT {base}/ISAPI/PTZCtrl/channels/{channel}/continuous <?xml version="1.0" '
            'encoding="UTF-8"?><PTZData><zoom>60</zoom></PTZData>',
            "zoom_out": 'PUT {base}/ISAPI/PTZCtrl/channels/{channel}/continuous <?xml version="1.0" '
            'encoding="UTF-8"?><PTZData><zoom>-60</zoom></PTZData>',
            "home": "PUT {base}/ISAPI/PTZCtrl/channels/{channel}/homeposition/goto",
            "stop": 'PUT {base}/ISAPI/PTZCtrl/channels/{channel}/continuous <?xml version="1.0" '
            'encoding="UTF-8"?><PTZData><pan>0</pan><tilt>0</tilt></PTZData>',
            "preset": "PUT {base}/ISAPI/PTZCtrl/channels/{channel}/presets/{preset}/goto",
        },
        "direction_codes": {
            "up": "tilt",
            "down": "tilt",
            "left": "pan",
            "right": "pan",
            "zoom_in": "zoom",
            "zoom_out": "zoom",
        },
    },
    "foscam": {
        "label": "Foscam (CGIProxy)",
        "description": "Foscam cameras whose CGI takes usr/pwd in the query string.",
        "commands": {
            "up": "GET {base}/cgi-bin/CGIProxy.fcgi?cmd=ptzMoveUp&usr={username}&pwd={password}",
            "down": "GET {base}/cgi-bin/CGIProxy.fcgi?cmd=ptzMoveDown&usr={username}&pwd={password}",
            "left": "GET {base}/cgi-bin/CGIProxy.fcgi?cmd=ptzMoveLeft&usr={username}&pwd={password}",
            "right": "GET {base}/cgi-bin/CGIProxy.fcgi?cmd=ptzMoveRight&usr={username}&pwd={password}",
            "zoom_in": "GET {base}/cgi-bin/CGIProxy.fcgi?cmd=zoomIn&usr={username}&pwd={password}",
            "zoom_out": "GET {base}/cgi-bin/CGIProxy.fcgi?cmd=zoomOut&usr={username}&pwd={password}",
            "stop": "GET {base}/cgi-bin/CGIProxy.fcgi?cmd=ptzStopRun&usr={username}&pwd={password}",
            "preset": "GET {base}/cgi-bin/CGIProxy.fcgi?cmd=ptzGotoPreset&preset={preset}"
            "&usr={username}&pwd={password}",
        },
        "direction_codes": {},
    },
}

_ACTION_ALIASES = {
    "zoom-in": "zoom_in",
    "zoom-out": "zoom_out",
    "zoomin": "zoom_in",
    "zoomout": "zoom_out",
    "tilt_up": "up",
    "tilt_down": "down",
    "pan_left": "left",
    "pan_right": "right",
}


@dataclass(slots=True)
class PtzResult:
    """Outcome of a single PTZ command."""

    ok: bool
    action: str
    command: str
    status: int | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the representation used by the web interface."""
        return {
            "ok": self.ok,
            "action": self.action,
            "command": self.command,
            "status": self.status,
            "detail": self.detail,
        }


def profile_list() -> list[dict[str, Any]]:
    """Return the vendor presets for the web interface."""
    return [
        {
            "id": key,
            "label": value["label"],
            "description": value["description"],
            "commands": value["commands"],
            "direction_codes": value["direction_codes"],
        }
        for key, value in PTZ_PROFILES.items()
    ]


def normalize_action(value: Any) -> str | None:
    """Return a known action name for a user supplied value."""
    action = str(value or "").strip().lower().replace(" ", "_")
    action = _ACTION_ALIASES.get(action, action)
    return action if action in PTZ_ACTIONS else None


def base_url_for(stream_url: str) -> str:
    """Return the default HTTP base URL of a camera (scheme://host)."""
    parts = urlsplit(str(stream_url or ""))
    host = parts.hostname or ""
    if not host:
        return ""
    return f"http://{host}"


def _channels(value: Any) -> int:
    """Return a sane PTZ channel number."""
    try:
        channel = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_CHANNEL
    return channel if 1 <= channel <= 16 else DEFAULT_CHANNEL


def _speed(value: Any) -> int:
    """Return a sane PTZ speed."""
    try:
        speed = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_SPEED
    return speed if 1 <= speed <= MAX_SPEED else DEFAULT_SPEED


def _clean_command(value: Any) -> str:
    """Return a validated command string or an empty string."""
    command = " ".join(str(value or "").split())
    if not command or len(command) > MAX_COMMAND_LENGTH:
        return ""
    method, _, rest = command.partition(" ")
    if method.upper() not in HTTP_METHODS or not rest.strip():
        return ""
    return f"{method.upper()} {rest.strip()}"


def _presets(value: Any) -> list[dict[str, str]]:
    """Return the configured presets as a clean list."""
    presets: list[dict[str, str]] = []
    if not isinstance(value, list):
        return presets
    for item in value:
        if isinstance(item, dict):
            preset_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
        elif isinstance(item, str) and "=" in item:
            preset_id, _, name = item.partition("=")
            preset_id, name = preset_id.strip(), name.strip()
        else:
            continue
        if not preset_id or len(presets) >= MAX_PRESETS:
            continue
        presets.append({"id": preset_id[:16], "name": (name or preset_id)[:40]})
    return presets


def normalize_ptz(raw: Any, stream_url: str) -> dict[str, Any] | None:
    """Validate the PTZ block of a camera and fill in its defaults.

    Returns None when PTZ is switched off or nothing usable is configured.
    """
    if not isinstance(raw, Mapping) or not raw.get("enabled", True):
        return None

    profile = str(raw.get("profile") or "custom").strip().lower()
    if profile not in PTZ_PROFILES:
        profile = "custom"

    base = str(raw.get("base_url") or "").strip() or base_url_for(stream_url)
    username = str(raw.get("username") or "").strip()
    password = str(raw.get("password") or "")
    channel = _channels(raw.get("channel"))
    speed = _speed(raw.get("speed"))

    profile_commands = PTZ_PROFILES[profile]["commands"]
    supplied = raw.get("commands") if isinstance(raw.get("commands"), Mapping) else {}
    values = {
        "base": base,
        "username": quote(username, safe=""),
        "password": quote(password, safe=""),
        "channel": str(channel),
    }
    commands: dict[str, str] = {}
    for action in PTZ_ACTIONS:
        command = _clean_command(supplied.get(action)) or _clean_command(
            profile_commands.get(action)
        )
        if command:
            commands[action] = fill(command, values)

    if not commands:
        return None

    return {
        "enabled": True,
        "profile": profile,
        "base_url": base,
        "channel": channel,
        "speed": speed,
        "commands": commands,
        "stop_codes": dict(PTZ_PROFILES[profile]["direction_codes"]),
        "presets": _presets(raw.get("presets")),
    }


def fill(command: str, values: Mapping[str, Any]) -> str:
    """Replace ``{name}`` placeholders inside a command."""
    result = command
    for name, value in values.items():
        result = result.replace("{" + name + "}", str(value))
    return result


def parse_command(command: str) -> tuple[str, str, bytes | None]:
    """Split a command into method, URL and optional body."""
    method, _, rest = command.partition(" ")
    url, _, body = rest.strip().partition(" ")
    return method.upper(), url, body.encode("utf-8") if body else None


def build_command(
    config: Mapping[str, Any],
    action: str,
    *,
    speed: int | None = None,
    preset: str | None = None,
    direction: str | None = None,
    seconds: float | None = None,
) -> str | None:
    """Render the command of an action from a normalized PTZ configuration."""
    commands = config.get("commands") or {}
    template = commands.get(action)
    if not template:
        return None

    values: dict[str, Any] = {
        "speed": _speed(speed if speed is not None else config.get("speed")),
    }
    if preset is not None:
        values["preset"] = str(preset)
    if direction:
        codes = config.get("stop_codes") or {}
        values["direction"] = str(codes.get(direction, direction))
    if seconds is not None:
        values["seconds"] = f"{float(seconds):g}"
    return fill(template, values)


def configured_actions(config: Mapping[str, Any] | None) -> list[str]:
    """Return the actions a camera can perform, in a stable order."""
    if not config:
        return []
    commands = config.get("commands") or {}
    return [action for action in PTZ_ACTIONS if action in commands]


async def async_send(
    command: str, timeout: float = COMMAND_TIMEOUT
) -> tuple[int | None, str | None]:
    """Send a PTZ command and return its status code and an error message."""
    method, url, body = parse_command(command)
    return await asyncio.to_thread(_send_blocking, method, url, body, timeout)


def _send_blocking(
    method: str, url: str, body: bytes | None, timeout: float
) -> tuple[int | None, str | None]:
    """Send the HTTP request of a command in a worker thread."""
    request = Request(url, data=body, method=method)
    if body:
        request.add_header("Content-Type", "application/xml")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - user configured URL
            snippet = response.read(MAX_RESPONSE_SNIPPET).decode("utf-8", "replace")
            status = int(response.status)
    except HTTPError as err:
        return int(err.code), f"HTTP {err.code}"
    except (URLError, OSError, ValueError) as err:
        return None, str(err)
    if status >= 400:
        return status, f"HTTP {status} {snippet.strip()}".strip()
    return status, None

async def async_run(
    config: Mapping[str, Any],
    action: str,
    *,
    speed: int | None = None,
    preset: str | None = None,
    direction: str | None = None,
    seconds: float | None = None,
) -> PtzResult:
    """Build and send the command of an action."""
    command = build_command(
        config, action, speed=speed, preset=preset, direction=direction, seconds=seconds
    )
    if command is None:
        return PtzResult(ok=False, action=action, command="", detail="no_command_configured")
    status, error = await async_send(command)
    return PtzResult(ok=error is None, action=action, command=command, status=status, detail=error)


def public_config(config: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the PTZ configuration with masked credentials for the web interface.

    Usernames and passwords may live inside the command URLs (many vendor CGIs
    only accept them there), so they are hidden from the browser.
    """
    if not config:
        return None

    def mask(text: Any) -> str:
        value = str(text or "")
        for marker in ("password=", "pwd=", "pass="):
            head, separator, _ = value.partition(marker)
            if separator:
                value = f"{head}{separator}***"
        return value

    return {
        "enabled": True,
        "profile": config.get("profile"),
        "base_url": config.get("base_url"),
        "channel": config.get("channel"),
        "speed": config.get("speed"),
        "commands": {
            action: mask(command) for action, command in (config.get("commands") or {}).items()
        },
        "stop_codes": dict(config.get("stop_codes") or {}),
        "presets": list(config.get("presets") or []),
        "actions": configured_actions(config),
    }
