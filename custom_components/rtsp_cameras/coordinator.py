"""Data update coordinator for the RTSP Camera Manager integration."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .actions import (
    actions_file_for,
    async_handle_actions,
    async_publish_addon_update,
    status_file_for,
)
from .addon import async_cameras
from .const import (
    CONF_CAMERAS_FILE,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    resolve_cameras_path,
)
from .models import RtspCameraDefinition, parse_cameras
from .ptz import adopt_position

_LOGGER = logging.getLogger(__name__)


def resolve_cameras_file(hass: HomeAssistant, entry: ConfigEntry) -> Path:
    """Return the absolute path of the camera definition file."""
    configured = entry.options.get(CONF_CAMERAS_FILE) or entry.data.get(
        CONF_CAMERAS_FILE
    )
    return resolve_cameras_path(configured, hass.config.path())


def resolve_scan_interval(entry: ConfigEntry) -> int:
    """Return the configured scan interval, clamped to sane bounds."""
    raw = entry.options.get(
        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_SCAN_INTERVAL
    return max(MIN_SCAN_INTERVAL, min(MAX_SCAN_INTERVAL, value))


class RtspCamerasCoordinator(DataUpdateCoordinator[dict[str, RtspCameraDefinition]]):
    """Read the camera file published by the add-on at a fixed interval."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator."""
        self.cameras_file = resolve_cameras_file(hass, entry)
        self.scan_interval = resolve_scan_interval(entry)
        self.missing_reported = False
        self.reported_count: int | None = None
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({self.cameras_file})",
            config_entry=entry,
            update_interval=timedelta(seconds=self.scan_interval),
            update_method=self._async_update_data,
        )

    async def _async_update_data(self) -> dict[str, RtspCameraDefinition]:
        """Read the camera file and report changes of the camera count."""
        cameras = await self.hass.async_add_executor_job(self._read_cameras)
        try:
            # The helper files used for the add-on update must never be able to
            # break the cameras.
            await async_handle_actions(self.hass, actions_file_for(self.cameras_file))
            await async_publish_addon_update(
                self.hass, status_file_for(self.cameras_file)
            )
        except Exception:  # noqa: BLE001 - helper files are optional
            _LOGGER.exception("Cannot handle the add-on helper files")
        if len(cameras) != self.reported_count:
            self.reported_count = len(cameras)
            if cameras:
                _LOGGER.info(
                    "Found %d camera(s) in %s: %s",
                    len(cameras),
                    self.cameras_file,
                    ", ".join(sorted(cameras)),
                )
            else:
                _LOGGER.info("No cameras in %s yet", self.cameras_file)
        await self._async_adopt_positions(cameras)
        return cameras

    async def _async_adopt_positions(
        self, cameras: dict[str, RtspCameraDefinition]
    ) -> None:
        """Take the coordinates of the add-on for every camera that walks a ladder.

        The panel of the add-on moves a camera as well, and a move of the panel is not in the
        camera file: the coordinates live in the memory of the add-on, which owns the ladders
        (see ``ptz.adopt_position``). Every refresh therefore asks the add-on where the cameras
        stand, so ``ptz_step`` of an entity does not keep the pair of the last move made *from
        Home Assistant* while the user stepped in the panel. An add-on that cannot be reached
        says nothing about the camera, so the coordinates of this side stay untouched then.
        """
        if not any(
            definition.ptz is not None and definition.ptz.steps
            for definition in cameras.values()
        ):
            return
        records = await async_cameras(self.hass)
        if records is None:
            return
        for record in records:
            camera_id = str(record.get("id") or "")
            definition = cameras.get(camera_id)
            if definition is None:
                continue
            adopt_position(camera_id, definition.ptz, record.get("ptz_position"))

    def _ensure_directory(self) -> None:
        """Create the folder of the camera file so the add-on can write into it."""
        try:
            self.cameras_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            _LOGGER.debug("Cannot create %s: %s", self.cameras_file.parent, err)

    def _read_cameras(self) -> dict[str, RtspCameraDefinition]:
        """Parse the camera file, tolerating a missing file."""
        if not self.cameras_file.is_file():
            self._ensure_directory()
            if not self.missing_reported:
                _LOGGER.info(
                    "Camera file %s does not exist yet, waiting for the add-on "
                    "(add cameras in the RTSP Cameras panel)",
                    self.cameras_file,
                )
                self.missing_reported = True
            return {}

        self.missing_reported = False
        try:
            raw: Any = json.loads(self.cameras_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            raise UpdateFailed(f"Cannot read {self.cameras_file}: {err}") from err

        return parse_cameras(raw)