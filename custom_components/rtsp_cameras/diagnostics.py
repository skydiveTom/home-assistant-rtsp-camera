"""Diagnostics support for the RTSP Camera Manager integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .models import RtspCameraDefinition, redact_url


def _ptz_summary(definition: RtspCameraDefinition) -> dict[str, Any] | None:
    """Return the PTZ configuration of a camera without credentials.

    The command URLs may carry user names and passwords (that is where many vendor
    CGIs want them), so only the actions are listed.
    """
    ptz = definition.ptz
    if ptz is None:
        return None
    return {
        "profile": ptz.profile,
        "speed": ptz.speed,
        "actions": list(ptz.actions),
        "presets": [name for _, name in ptz.presets],
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return the data Home Assistant collected for this config entry.

    Credentials inside stream URLs are masked, everything else is passed on so a
    support request can be answered from the downloaded file alone.
    """
    coordinator = entry.runtime_data
    cameras = coordinator.data or {}
    return {
        "entry": {
            "data": dict(entry.data),
            "options": dict(entry.options),
            "state": str(entry.state),
        },
        "cameras_file": {
            "path": str(coordinator.cameras_file),
            "exists": coordinator.cameras_file.is_file(),
        },
        "scan_interval": coordinator.scan_interval,
        "cameras": [
            {
                "id": definition.id,
                "name": definition.name,
                "url": redact_url(definition.url),
                "stream_url": redact_url(definition.stream_url or "") or None,
                "rtsp_transport": definition.rtsp_transport,
                "codec": definition.codec,
                "plays_in_browsers": definition.plays_in_browsers,
                "ptz": _ptz_summary(definition),
            }
            for definition in cameras.values()
        ],
        "entities": sorted(
            entity_id
            for entity_id in hass.states.async_entity_ids("camera")
            if entity_id.startswith("camera.")
        ),
    }
