"""Constants for the RTSP Camera Manager integration."""

from __future__ import annotations

from pathlib import Path

DOMAIN = "rtsp_cameras"

# Config entry data / options
CONF_CAMERAS_FILE = "cameras_file"
CONF_SCAN_INTERVAL = "scan_interval"

# Defaults
DEFAULT_CAMERAS_FILENAME = "rtsp_cameras/cameras.json"
DEFAULT_SCAN_INTERVAL = 10
MIN_SCAN_INTERVAL = 2
MAX_SCAN_INTERVAL = 600

# Camera file schema
FILE_VERSION = 1
FILE_GENERATOR = "RTSP Camera Manager add-on"

MANUFACTURER = "RTSP Camera Manager"
DEFAULT_MODEL = "Generic RTSP camera"

DOCS_URL = "https://github.com/skydiveTom/home-assistant-rtsp-camera/blob/main/rtsp_cameras/DOCS.md"
ADD_CAMERA_URL = f"{DOCS_URL}#adding-a-camera"

ATTR_CAMERA_ID = "rtsp_camera_id"
ATTR_RTSP_TRANSPORT = "rtsp_transport"
ATTR_SOURCE_FILE = "source_file"

SUPPORTED_SCHEMES = ("rtsp://", "rtsps://", "rtmp://", "http://", "https://")


def resolve_cameras_path(configured: str | None, config_dir: str | Path) -> Path:
    """Resolve the configured camera file path against the configuration folder.

    Absolute paths are used as they are, relative paths are resolved inside the
    Home Assistant configuration directory - which is exactly the place the
    add-on publishes its camera file to.
    """
    value = str(configured or "").strip() or DEFAULT_CAMERAS_FILENAME
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(config_dir) / path
    return path