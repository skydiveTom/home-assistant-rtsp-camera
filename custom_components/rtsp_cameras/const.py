"""Constants for the RTSP Camera Manager integration."""

from __future__ import annotations

from pathlib import Path

DOMAIN = "rtsp_cameras"
# The add-on uses the same string as its slug, which the hassio integration uses
# as the unique id of the add-on update entity.
ADDON_SLUG = DOMAIN

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
ATTR_STREAM_URL = "stream_url"
ATTR_CODEC = "codec"

SUPPORTED_SCHEMES = ("rtsp://", "rtsps://", "rtmp://", "http://", "https://")

# Stills: Home Assistant's stream needs a decoded keyframe before it can hand out
# an image. The first call right after starting a stream has none yet, so the
# entity waits this long for the next keyframe before it uses the last frame.
KEYFRAME_WAIT_SECONDS = 8
# Optional snapshot URL of a camera (downloaded by the integration for stills).
SNAPSHOT_TIMEOUT_SECONDS = 10


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