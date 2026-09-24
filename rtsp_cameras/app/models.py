"""Camera model and helpers for the RTSP Camera Manager add-on."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import SUPPORTED_PREVIEW_MODES

SUPPORTED_SCHEMES: tuple[str, ...] = (
    "rtsp://",
    "rtsps://",
    "rtmp://",
    "http://",
    "https://",
)

STATUS_UNKNOWN = "unknown"
STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"


def utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


_TRANSLITERATION = str.maketrans(
    {
        "ł": "l",
        "Ł": "L",
        "ø": "o",
        "Ø": "O",
        "đ": "d",
        "Đ": "D",
        "ß": "ss",
        "æ": "ae",
        "Æ": "AE",
        "œ": "oe",
        "Œ": "OE",
    }
)


def slugify(value: str) -> str:
    """Turn a camera name into a stable, ASCII only identifier."""
    text = (value or "").translate(_TRANSLITERATION)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text or "camera"


def validate_stream_url(url: Any) -> str | None:
    """Return an error code when the URL cannot be used, otherwise None."""
    value = str(url or "").strip()
    if not value:
        return "url_required"
    if any(character.isspace() for character in value):
        return "url_invalid"
    if not value.lower().startswith(SUPPORTED_SCHEMES):
        return "url_scheme"
    if not urlsplit(value).netloc:
        return "url_invalid"
    return None


def validate_optional_stream_url(url: Any) -> str | None:
    """Validate an optional URL: an empty value is allowed and means "unset"."""
    if not str(url or "").strip():
        return None
    return validate_stream_url(url)


def redact_url(url: Any) -> str:
    """Replace user and password inside a stream URL with asterisks."""
    value = str(url or "")
    parts = urlsplit(value)
    if not parts.username and not parts.password:
        return value

    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    if parts.password is not None:
        credentials = "***:***@"
    else:
        credentials = "***@"
    return urlunsplit(
        (parts.scheme, f"{credentials}{host}", parts.path, parts.query, parts.fragment)
    )


def has_credentials(url: Any) -> bool:
    """Return True when the URL embeds a user name or password."""
    parts = urlsplit(str(url or ""))
    return bool(parts.username or parts.password)


_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^/\s@]+)@"
)


def _mask_userinfo(match: re.Match[str]) -> str:
    """Replace the user information of a URL with asterisks."""
    userinfo = match.group("userinfo")
    masked = "***:***" if ":" in userinfo else "***"
    return f"{match.group('scheme')}{masked}@"


def redact_credentials(text: str) -> str:
    """Mask the credentials of every URL found inside a text."""
    return _URL_CREDENTIALS.sub(_mask_userinfo, text or "")


@dataclass(slots=True)
class Camera:
    """A single camera instance managed by the add-on."""

    id: str
    name: str
    url: str
    rtsp_transport: str = "tcp"
    enabled: bool = True
    # Optional second URL used by the Home Assistant camera entity. Cameras that
    # stream H.265 (or a very large picture) are best exposed to Home Assistant
    # through their H.264 sub stream.
    ha_stream_url: str | None = None
    # Preview implementation that was found to work for this camera (mjpeg/hls),
    # learned automatically when the add-on option is set to "auto".
    preview_mode: str | None = None
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    # Volatile runtime information, never persisted
    status: str = STATUS_UNKNOWN
    last_checked: str | None = None
    last_error: str | None = None
    last_probe: dict[str, Any] | None = None

    @property
    def entity_id(self) -> str:
        """Return the camera entity id Home Assistant will most likely use."""
        return f"camera.{slugify(self.name)}"

    def to_integration_dict(self) -> dict[str, Any]:
        """Return the representation consumed by the Home Assistant integration."""
        details = (self.last_probe or {}).get("details") or {}
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "rtsp_transport": self.rtsp_transport,
            "enabled": self.enabled,
        }
        if self.ha_stream_url:
            # Home Assistant uses this URL instead of the main one when present.
            payload["stream_url"] = self.ha_stream_url
        if details.get("codec"):
            payload["codec"] = str(details["codec"])
        if self.preview_mode:
            payload["preview_mode"] = self.preview_mode
        return payload

    def to_api_dict(self) -> dict[str, Any]:
        """Return the representation used by the web interface."""
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "url_masked": redact_url(self.url),
            "has_credentials": has_credentials(self.url),
            "rtsp_transport": self.rtsp_transport,
            "enabled": self.enabled,
            "ha_stream_url": self.ha_stream_url,
            "ha_stream_url_masked": redact_url(self.ha_stream_url or "") or None,
            "preview_mode": self.preview_mode,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "last_checked": self.last_checked,
            "last_error": self.last_error,
            "last_probe": self.last_probe,
            "entity_id": self.entity_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Camera | None:
        """Build a camera from stored data, returning None when unusable."""
        if not isinstance(data, Mapping):
            return None
        camera_id = str(data.get("id") or "").strip()
        name = str(data.get("name") or "").strip()
        url = str(data.get("url") or "").strip()
        if not camera_id or not name or validate_stream_url(url):
            return None
        ha_stream_url = str(data.get("ha_stream_url") or "").strip() or None
        if ha_stream_url and validate_stream_url(ha_stream_url):
            ha_stream_url = None
        preview_mode = str(data.get("preview_mode") or "").strip().lower() or None
        if preview_mode not in SUPPORTED_PREVIEW_MODES:
            preview_mode = None
        return cls(
            id=camera_id,
            name=name,
            url=url,
            rtsp_transport=str(data.get("rtsp_transport") or "tcp").strip().lower(),
            enabled=bool(data.get("enabled", True)),
            ha_stream_url=ha_stream_url,
            preview_mode=preview_mode,
            created_at=str(data.get("created_at") or utcnow()),
            updated_at=str(data.get("updated_at") or utcnow()),
        )