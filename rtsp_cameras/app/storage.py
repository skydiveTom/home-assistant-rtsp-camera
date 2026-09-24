"""Camera persistence for the RTSP Camera Manager add-on."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from threading import RLock
from typing import Any

from .config import Settings
from .models import Camera, slugify, utcnow

_LOGGER = logging.getLogger(__name__)

FILE_VERSION = 1
FILE_GENERATOR = "RTSP Camera Manager add-on"


class CameraStore:
    """Thread safe camera store that also publishes the integration file."""

    def __init__(self, settings: Settings) -> None:
        """Initialise an empty store bound to the given settings."""
        self.settings = settings
        self.publish_error: str | None = None
        self._lock = RLock()
        self._cameras: dict[str, Camera] = {}

    # ------------------------------------------------------------------ load
    def load(self) -> None:
        """Load the camera list, falling back to the published file."""
        with self._lock:
            cameras = self._read(self.settings.cameras_file)
            if not cameras and self.settings.published_file.is_file():
                _LOGGER.info(
                    "Importing camera definitions from %s", self.settings.published_file
                )
                cameras = self._read(self.settings.published_file)
            self._cameras = cameras
            _LOGGER.info("Loaded %d camera(s)", len(self._cameras))
        self.save()

    def _read(self, path: Path) -> dict[str, Camera]:
        """Read a camera file, ignoring unusable entries."""
        if not path.is_file():
            return {}
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            _LOGGER.warning("Cannot read %s: %s", path, err)
            return {}

        entries: Sequence[Any] = []
        if isinstance(raw, dict) and isinstance(raw.get("cameras"), list):
            entries = raw["cameras"]
        elif isinstance(raw, list):
            entries = raw

        cameras: dict[str, Camera] = {}
        for item in entries:
            camera = Camera.from_dict(item) if isinstance(item, dict) else None
            if camera is not None:
                cameras[camera.id] = camera
        return cameras

    # ------------------------------------------------------------------ save
    def save(self) -> bool:
        """Write both the add-on file and the file read by the integration."""
        with self._lock:
            payload = self._payload()
            stored = self._write(self.settings.cameras_file, payload)
            self.publish_error = None
            published = self._write(self.settings.published_file, payload)
            if not published:
                self.publish_error = str(self.settings.published_file)
            return stored and published

    def _payload(self) -> dict[str, Any]:
        """Build the JSON document describing all cameras."""
        return {
            "version": FILE_VERSION,
            "generator": FILE_GENERATOR,
            "addon_version": self.settings.addon_version,
            "generated": utcnow(),
            "cameras": [camera.to_integration_dict() for camera in self._cameras.values()],
        }

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> bool:
        """Atomically write a JSON document, returning False on failure."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
            handle = tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                dir=str(path.parent),
                prefix=f"{path.name}.",
                suffix=".tmp",
                delete=False,
            )
            try:
                handle.write(text)
            finally:
                handle.close()
            os.replace(handle.name, path)
        except OSError as err:
            _LOGGER.error("Cannot write %s: %s", path, err)
            return False
        return True

    # ------------------------------------------------------------------- crud
    def list(self) -> list[Camera]:
        """Return a snapshot of all cameras in display order."""
        with self._lock:
            return list(self._cameras.values())

    def get(self, camera_id: str) -> Camera | None:
        """Return a single camera by id."""
        with self._lock:
            return self._cameras.get(camera_id)

    def unique_id(self, name: str, exclude: str | None = None) -> str:
        """Build a unique camera id based on the camera name."""
        base = slugify(name)
        with self._lock:
            candidate = base
            counter = 2
            while candidate in self._cameras and candidate != exclude:
                candidate = f"{base}-{counter}"
                counter += 1
            return candidate

    def add(
        self,
        name: str,
        url: str,
        rtsp_transport: str = "tcp",
        enabled: bool = True,
        ha_stream_url: str | None = None,
    ) -> Camera:
        """Create a new camera and persist the list."""
        with self._lock:
            camera = Camera(
                id=self.unique_id(name),
                name=name.strip(),
                url=url.strip(),
                rtsp_transport=rtsp_transport,
                enabled=enabled,
                ha_stream_url=(ha_stream_url or "").strip() or None,
            )
            self._cameras[camera.id] = camera
        self.save()
        return camera

    def update(self, camera_id: str, **changes: Any) -> Camera | None:
        """Update an existing camera and persist the list."""
        with self._lock:
            camera = self._cameras.get(camera_id)
            if camera is None:
                return None
            if changes.get("name"):
                camera.name = str(changes["name"]).strip()
            if changes.get("url"):
                camera.url = str(changes["url"]).strip()
            if changes.get("rtsp_transport"):
                camera.rtsp_transport = str(changes["rtsp_transport"]).strip().lower()
            if changes.get("enabled") is not None:
                camera.enabled = bool(changes["enabled"])
            if "ha_stream_url" in changes:
                # An empty value removes the Home Assistant URL override.
                camera.ha_stream_url = str(changes["ha_stream_url"] or "").strip() or None
            if "preview_mode" in changes:
                value = str(changes["preview_mode"] or "").strip().lower()
                camera.preview_mode = value or None
            if changes.get("touch", True):
                camera.updated_at = utcnow()
        self.save()
        return camera

    def set_preview_mode(self, camera_id: str, mode: str | None) -> Camera | None:
        """Remember which preview implementation works for a camera."""
        value = str(mode or "").strip().lower() or None
        with self._lock:
            camera = self._cameras.get(camera_id)
            if camera is None:
                return None
            if camera.preview_mode == value:
                return camera
            camera.preview_mode = value
        self.save()
        return camera

    def delete(self, camera_id: str) -> bool:
        """Remove a camera and persist the list."""
        with self._lock:
            removed = self._cameras.pop(camera_id, None)
        if removed is None:
            return False
        self.save()
        return True

    def count(self) -> int:
        """Return the amount of configured cameras."""
        with self._lock:
            return len(self._cameras)