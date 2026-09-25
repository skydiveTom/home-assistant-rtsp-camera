"""Data models for the RTSP Camera Manager integration."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .const import (
    DEFAULT_MODEL,
    PTZ_ACTIONS,
    PTZ_DEFAULT_SPEED,
    PTZ_MAX_SPEED,
    SUPPORTED_SCHEMES,
)


def _positive_int(value: Any, fallback: int, maximum: int = PTZ_MAX_SPEED) -> int:
    """Return a bounded positive integer."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return fallback
    if number < 1:
        return fallback
    return min(number, maximum)


def redact_url(url: str) -> str:
    """Replace the credentials of a stream URL with asterisks."""
    match = re.match(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^/\s@]+)@", url or "")
    if not match:
        return url or ""
    masked = "***:***" if ":" in match.group("userinfo") else "***"
    return f"{match.group('scheme')}{masked}@{url[match.end():]}"


@dataclass(frozen=True, slots=True)
class PtzConfig:
    """PTZ commands published by the add-on for one camera."""

    profile: str = "custom"
    speed: int = 4
    commands: Mapping[str, str] = field(default_factory=dict)
    stop_codes: Mapping[str, str] = field(default_factory=dict)
    presets: tuple[tuple[str, str], ...] = ()

    @property
    def enabled(self) -> bool:
        """Return True when the camera can be moved."""
        return bool(self.commands)

    @property
    def actions(self) -> tuple[str, ...]:
        """Return the actions this camera understands."""
        return tuple(action for action in PTZ_ACTIONS if action in self.commands)

    def uses(self, action: str) -> bool:
        """Return True when the action is configured."""
        return action in self.commands

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> PtzConfig | None:
        """Build a PTZ configuration from the published block, or None."""
        if not isinstance(data, Mapping) or not data.get("enabled", True):
            return None

        raw_commands = data.get("commands")
        if not isinstance(raw_commands, Mapping):
            return None
        commands = {
            str(action): str(command).strip()
            for action, command in raw_commands.items()
            if str(action) in PTZ_ACTIONS and str(command).strip()
        }
        if not commands:
            return None

        raw_stop_codes = data.get("stop_codes")
        stop_codes = (
            {str(key): str(value) for key, value in raw_stop_codes.items()}
            if isinstance(raw_stop_codes, Mapping)
            else {}
        )

        presets: list[tuple[str, str]] = []
        raw_presets = data.get("presets")
        if isinstance(raw_presets, Sequence) and not isinstance(raw_presets, (str, bytes)):
            for item in raw_presets:
                if not isinstance(item, Mapping):
                    continue
                preset_id = str(item.get("id") or "").strip()
                if preset_id:
                    presets.append((preset_id, str(item.get("name") or preset_id).strip()))

        return cls(
            profile=str(data.get("profile") or "custom").strip() or "custom",
            speed=_positive_int(data.get("speed"), PTZ_DEFAULT_SPEED),
            commands=commands,
            stop_codes=stop_codes,
            presets=tuple(presets),
        )


@dataclass(frozen=True, slots=True)
class RtspCameraDefinition:
    """A single camera instance published by the add-on."""

    id: str
    name: str
    url: str
    rtsp_transport: str = "tcp"
    snapshot_url: str | None = None
    stream_url: str | None = None
    codec: str | None = None
    model: str = DEFAULT_MODEL
    ptz: PtzConfig | None = None

    @property
    def slug(self) -> str:
        """Return a stable, HA friendly identifier for this camera."""
        return self.id

    @property
    def stream_source(self) -> str:
        """Return the URL the Home Assistant stream component should use.

        The add-on can publish a second URL (for example the H.264 sub stream of
        an H.265 camera); it wins over the main URL when present.
        """
        return self.stream_url or self.url

    @property
    def plays_in_browsers(self) -> bool:
        """Return True when browsers can usually play the stream directly."""
        return str(self.codec or "").lower() in ("", "h264", "avc1", "mjpeg")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RtspCameraDefinition | None:
        """Build a definition from a raw JSON object, or None when invalid."""
        if not isinstance(data, Mapping):
            return None

        camera_id = str(data.get("id") or "").strip()
        url = str(data.get("url") or "").strip()
        if not camera_id or not url:
            return None
        if not url.lower().startswith(SUPPORTED_SCHEMES):
            return None

        name = str(data.get("name") or "").strip() or camera_id
        transport = str(data.get("rtsp_transport") or "tcp").strip().lower()
        if transport not in ("tcp", "udp", "udp_multicast", "http"):
            transport = "tcp"

        snapshot_url = str(data.get("snapshot_url") or "").strip() or None

        stream_url = str(data.get("stream_url") or "").strip() or None
        if stream_url and not stream_url.lower().startswith(SUPPORTED_SCHEMES):
            stream_url = None

        return cls(
            id=camera_id,
            name=name,
            url=url,
            rtsp_transport=transport,
            snapshot_url=snapshot_url,
            stream_url=stream_url,
            codec=str(data.get("codec") or "").strip() or None,
            model=str(data.get("model") or "").strip() or DEFAULT_MODEL,
            ptz=PtzConfig.from_dict(data.get("ptz")),
        )


def parse_cameras(raw: Any) -> dict[str, RtspCameraDefinition]:
    """Parse the camera file content into a mapping keyed by camera id.

    Both the documented object form ({"cameras": [...]}) and a plain list are
    accepted so hand written files keep working.
    """
    entries: Sequence[Any] | None = None
    if isinstance(raw, Mapping):
        value = raw.get("cameras")
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            entries = value
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        entries = raw

    cameras: dict[str, RtspCameraDefinition] = {}
    for item in entries or []:
        if not isinstance(item, Mapping):
            continue
        if not item.get("enabled", True):
            continue
        definition = RtspCameraDefinition.from_dict(item)
        if definition is not None:
            cameras[definition.id] = definition
    return cameras