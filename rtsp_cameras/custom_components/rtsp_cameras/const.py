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
ATTR_PTZ = "ptz"
ATTR_PTZ_PRESETS = "ptz_presets"
#: Which PTZ variant (vendor command set) moves this camera.
ATTR_PTZ_PROFILE = "ptz_profile"
#: Where a camera stands on the ladders of step positions of the add-on: per axis the rung
#: it was walked to (``{"vertical": 3, "horizontal": 5, ...}`` with the rung count), which
#: every move of the camera rewrites - a rung is a whole position, so a step of one axis
#: changes the other coordinate as well.
ATTR_PTZ_STEP = "ptz_step"

SUPPORTED_SCHEMES = ("rtsp://", "rtsps://", "rtmp://", "http://", "https://")

# PTZ: the add-on publishes ready to use HTTP commands per action; Home Assistant
# only has to fill in the values of the moment.
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
PTZ_DIRECTIONS = {"up": "up", "down": "down", "left": "left", "right": "right"}
PTZ_DEFAULT_SPEED = 4
PTZ_MAX_SPEED = 8
PTZ_TIMEOUT_SECONDS = 8
#: How long the DVRIP transport may take to *answer*. A vendor CGI answers within a
#: second, but this family replies to a ``GotoPreset`` - the stop of the bundled profile,
#: which has to drag the camera back - only once the axis arrived: measured on the device
#: of the test set 10 to 16 s after the command, where every shorter deadline reports a
#: command that works as a failure (see the changelog of 0.3.8).
DVRIP_TIMEOUT_SECONDS = 25

#: Port of the Xiongmai DVRIP protocol (PTZ for DVRs without a web interface).
DEFAULT_DVRIP_PORT = 34567
#: Seconds a continuous move runs when the caller does not say it (ONVIF uses 0.5).
PTZ_DEFAULT_DURATION = 0.5
#: Which axis a direction walks. A *ladder* of step positions is kept per axis, and the
#: arrows of an axis walk it from one end to the other: ``up``/``down`` climb the
#: ``vertical`` ladder, ``left``/``right`` the ``horizontal`` one. The first arrow of a pair
#: is the one that walks the ladder *forward* - to the end it was stored towards - and the
#: add-on stores a ladder from the end its first arrow walks away from (``ptz.STEP_AXES``):
#: ``up`` climbs towards the last rung, ``right`` too, which is why ``right`` comes first.
PTZ_STEP_AXES = {"vertical": ("up", "down"), "horizontal": ("right", "left")}
#: Seconds between two rungs of a walk, counted from the moment the rung was asked. The
#: answer of a camera usually comes when it has arrived, so the rungs never queue; this
#: keeps a device that acknowledges at once from being asked for a whole ladder in one
#: burst.
PTZ_STEP_PAUSE_SECONDS = 0.12
SERVICE_PTZ = "ptz"
SERVICE_PTZ_HOME = "ptz_home"
# Stills: Home Assistant's stream needs a decoded keyframe before it can hand out
# an image. The first call right after starting a stream has none yet, so the
# entity waits this long for the next keyframe before it uses the last frame.
KEYFRAME_WAIT_SECONDS = 8
# Optional snapshot URL of a camera (downloaded by the integration for stills).
SNAPSHOT_TIMEOUT_SECONDS = 10
# The add-on grabs a frame with ffmpeg: connecting to the camera and waiting for a
# keyframe took 2 to 9 s on the test set, so the add-on gets a little more room than
# a plain HTTP snapshot - its own ffmpeg gives up after ``test_timeout`` + 5 s.
ADDON_SNAPSHOT_TIMEOUT_SECONDS = 15
# The add-on renders a still with its own ffmpeg (one frame of the camera, no
# stream in Home Assistant), which is what a dashboard shows while nobody watches
# the stream - a card asks for it again and again, so the answer is kept this long
# before the add-on has to run ffmpeg once more.
SNAPSHOT_CACHE_SECONDS = 5
#: Path of the still inside the API of the add-on (JPEG, ``?height=`` optional).
ADDON_SNAPSHOT_PATH = "/api/cameras/{camera_id}/snapshot.jpg"
#: Port the add-on's web server listens on when the Supervisor does not say it.
ADDON_API_PORT = 8099
#: How long the address of the add-on is reused, and how long a failed lookup waits.
ADDON_URL_RESOLVE_SECONDS = 300
ADDON_URL_RETRY_SECONDS = 30
DATA_ADDON_API_URLS = f"{DOMAIN}_addon_api_urls"


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