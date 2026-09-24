"""Runtime configuration for the RTSP Camera Manager add-on."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "de", "es", "pl")
LANGUAGE_AUTO = "auto"
DEFAULT_LANGUAGE = "en"
SUPPORTED_TRANSPORTS: tuple[str, ...] = ("tcp", "udp", "udp_multicast", "http")
# Preview implementations the add-on can run.
SUPPORTED_PREVIEW_MODES: tuple[str, ...] = ("mjpeg", "hls")
# Value for "find out which one works and keep using it".
PREVIEW_MODE_AUTO = "auto"
PREVIEW_MODE_CHOICES: tuple[str, ...] = (PREVIEW_MODE_AUTO, *SUPPORTED_PREVIEW_MODES)
DEFAULT_PREVIEW_MODE = PREVIEW_MODE_AUTO

ADDON_VERSION = "0.1.19"
ADDON_SLUG = "rtsp_cameras"
ADDON_NAME = "RTSP Camera Manager"
DEFAULT_DATA_DIR = "/data"
DEFAULT_CONFIG_DIR = "/config"
DEFAULT_TEMP_DIR = "/tmp"
DEFAULT_INTEGRATION_SOURCE = "/opt/rtsp_camera_integration/custom_components/rtsp_cameras"
DEFAULT_INGRESS_PORT = 8099


def _as_bool(value: Any, default: bool) -> bool:
    """Coerce add-on options to a boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """Coerce add-on options to an int inside the given bounds."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _as_choice(value: Any, choices: Sequence[str], default: str) -> str:
    """Coerce add-on options to one of the allowed values."""
    text = str(value or "").strip().lower()
    return text if text in choices else default


@dataclass(slots=True)
class Settings:
    """Add-on settings derived from the Supervisor options file."""

    language: str = LANGUAGE_AUTO
    default_rtsp_transport: str = "tcp"
    install_integration: bool = True
    ha_restart_after_install: bool = False
    health_check_interval: int = 60
    test_timeout: int = 15
    preview_mode: str = DEFAULT_PREVIEW_MODE
    preview_max_height: int = 1080
    preview_fps: int = 5
    redact_credentials_in_logs: bool = True

    data_dir: Path = field(default_factory=lambda: Path(DEFAULT_DATA_DIR))
    config_dir: Path = field(default_factory=lambda: Path(DEFAULT_CONFIG_DIR))
    temp_dir: Path = field(default_factory=lambda: Path(DEFAULT_TEMP_DIR))
    integration_source: Path = field(
        default_factory=lambda: Path(DEFAULT_INTEGRATION_SOURCE)
    )
    ingress_port: int = DEFAULT_INGRESS_PORT
    addon_version: str = ADDON_VERSION
    addon_name: str = ADDON_NAME
    addon_slug: str = ADDON_SLUG
    supervisor_url: str = "http://supervisor"
    supervisor_token: str | None = None

    @property
    def options_file(self) -> Path:
        """Return the path of the Supervisor options file."""
        return self.data_dir / "options.json"

    @property
    def cameras_file(self) -> Path:
        """Return the path of the add-on owned camera file."""
        return self.data_dir / "cameras.json"

    @property
    def published_file(self) -> Path:
        """Return the path of the camera file published for the integration."""
        return self.config_dir / "rtsp_cameras" / "cameras.json"

    @property
    def actions_file(self) -> Path:
        """Return the file used to ask the integration for an action."""
        return self.config_dir / "rtsp_cameras" / "actions.json"

    @property
    def addon_update_file(self) -> Path:
        """Return the file the integration reports the add-on version into."""
        return self.config_dir / "rtsp_cameras" / "addon_update.json"

    @property
    def integration_target(self) -> Path:
        """Return the directory the integration is installed into."""
        return self.config_dir / "custom_components" / ADDON_SLUG

    @property
    def preview_dir(self) -> Path:
        """Return the directory used for HLS preview segments."""
        return self.temp_dir / "rtsp_cameras_preview"

    @classmethod
    def load(
        cls,
        options_path: Path | str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Settings:
        """Read the add-on options, with environment overrides for local runs."""
        environment = dict(os.environ if env is None else env)

        def env_path(name: str, default: str) -> Path:
            return Path(environment.get(name) or default)

        data_dir = env_path("RTSP_ADDON_DATA_DIR", DEFAULT_DATA_DIR)
        path = Path(
            options_path
            or environment.get("RTSP_ADDON_OPTIONS_FILE")
            or data_dir / "options.json"
        )
        options: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = None
            if isinstance(loaded, dict):
                options = loaded

        settings = cls(
            language=_as_choice(
                options.get("language"),
                (LANGUAGE_AUTO, *SUPPORTED_LANGUAGES),
                LANGUAGE_AUTO,
            ),
            default_rtsp_transport=_as_choice(
                options.get("default_rtsp_transport"), SUPPORTED_TRANSPORTS, "tcp"
            ),
            install_integration=_as_bool(options.get("install_integration"), True),
            ha_restart_after_install=_as_bool(
                options.get("ha_restart_after_install"), False
            ),
            health_check_interval=_as_int(
                options.get("health_check_interval"), 60, 0, 86400
            ),
            test_timeout=_as_int(options.get("test_timeout"), 15, 5, 120),
            preview_mode=_as_choice(
                options.get("preview_mode"), PREVIEW_MODE_CHOICES, DEFAULT_PREVIEW_MODE
            ),
            preview_max_height=_as_int(
                options.get("preview_max_height"), 1080, 240, 2160
            ),
            preview_fps=_as_int(options.get("preview_fps"), 5, 1, 30),
            redact_credentials_in_logs=_as_bool(
                options.get("redact_credentials_in_logs"), True
            ),
            data_dir=data_dir,
            config_dir=env_path("RTSP_ADDON_CONFIG_DIR", DEFAULT_CONFIG_DIR),
            temp_dir=env_path("RTSP_ADDON_TEMP_DIR", DEFAULT_TEMP_DIR),
            integration_source=env_path(
                "RTSP_ADDON_INTEGRATION_SOURCE", DEFAULT_INTEGRATION_SOURCE
            ),
            ingress_port=_as_int(
                environment.get("RTSP_ADDON_INGRESS_PORT"), DEFAULT_INGRESS_PORT, 1, 65535
            ),
            addon_version=environment.get("RTSP_ADDON_VERSION") or ADDON_VERSION,
            addon_name=environment.get("RTSP_ADDON_NAME") or ADDON_NAME,
            supervisor_url=environment.get("SUPERVISOR_URL") or "http://supervisor",
            supervisor_token=(
                environment.get("SUPERVISOR_TOKEN")
                or environment.get("HASSIO_TOKEN")
                or None
            ),
        )
        settings.ensure_directories()
        return settings

    def ensure_directories(self) -> None:
        """Create the directories the add-on writes to, ignoring failures."""
        for directory in (
            self.data_dir,
            self.published_file.parent,
            self.preview_dir,
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue