"""Constants for the RTSP Camera Manager integration."""

from __future__ import annotations

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

ATTR_CAMERA_ID = "rtsp_camera_id"
ATTR_RTSP_TRANSPORT = "rtsp_transport"
ATTR_SOURCE_FILE = "source_file"

SUPPORTED_SCHEMES = ("rtsp://", "rtsps://", "rtmp://", "http://", "https://")