"""Data models for the RTSP Camera Manager integration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .const import DEFAULT_MODEL, SUPPORTED_SCHEMES


@dataclass(frozen=True, slots=True)
class RtspCameraDefinition:
    """A single camera instance published by the add-on."""

    id: str
    name: str
    url: str
    rtsp_transport: str = "tcp"
    snapshot_url: str | None = None
    model: str = DEFAULT_MODEL

    @property
    def slug(self) -> str:
        """Return a stable, HA friendly identifier for this camera."""
        return self.id

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

        return cls(
            id=camera_id,
            name=name,
            url=url,
            rtsp_transport=transport,
            snapshot_url=snapshot_url,
            model=str(data.get("model") or "").strip() or DEFAULT_MODEL,
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