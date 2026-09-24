"""Install the bundled Home Assistant integration into the config folder."""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import ADDON_SLUG, Settings
from .models import utcnow

_LOGGER = logging.getLogger(__name__)

MANIFEST = "manifest.json"
STATE_FILE = "integration_state.json"
IGNORED = ("__pycache__", "*.pyc")


@dataclass(slots=True)
class IntegrationStatus:
    """Report about the state of the bundled integration."""

    installed: bool = False
    enabled: bool = True
    version: str | None = None
    source_version: str | None = None
    update_available: bool = False
    needs_restart: bool = False
    target: str = ""
    source: str = ""
    installed_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serialisable representation."""
        return asdict(self)


class IntegrationInstaller:
    """Copy the bundled integration into /config/custom_components."""

    def __init__(self, settings: Settings) -> None:
        """Store the settings describing source and target folders."""
        self.settings = settings
        self.error: str | None = None

    @property
    def enabled(self) -> bool:
        """Return True when the add-on may install the integration."""
        return self.settings.install_integration

    @property
    def state_file(self) -> Path:
        """Return the file tracking the last installation."""
        return self.settings.data_dir / STATE_FILE

    @staticmethod
    def _read_version(directory: Path) -> str | None:
        """Read the version from a component manifest.json."""
        manifest = directory / MANIFEST
        if not manifest.is_file():
            return None
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        version = data.get("version") if isinstance(data, dict) else None
        return str(version) if version else None

    def _read_state(self) -> dict[str, Any]:
        """Read the installation state file."""
        if not self.state_file.is_file():
            return {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, state: dict[str, Any]) -> None:
        """Persist the installation state."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except OSError as err:
            _LOGGER.warning("Cannot store the integration state: %s", err)

    def status(self) -> IntegrationStatus:
        """Return the current installation status."""
        source = self.settings.integration_source
        target = self.settings.integration_target
        source_version = self._read_version(source)
        target_version = self._read_version(target)
        state = self._read_state()
        return IntegrationStatus(
            installed=target_version is not None,
            enabled=self.enabled,
            version=target_version,
            source_version=source_version,
            update_available=bool(
                target_version is not None
                and source_version is not None
                and target_version != source_version
            ),
            needs_restart=bool(state.get("restart_required")),
            target=str(target),
            source=str(source),
            installed_at=state.get("installed_at"),
            error=self.error,
        )

    def install(self) -> IntegrationStatus:
        """Copy the bundled integration over to the Home Assistant config folder."""
        self.error = None
        source = self.settings.integration_source
        target = self.settings.integration_target

        if not source.is_dir():
            self.error = "source_missing"
            _LOGGER.error("Bundled integration not found in %s", source)
            return self.status()

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(
                source,
                target,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(*IGNORED),
            )
        except OSError as err:
            self.error = str(err)
            _LOGGER.error("Cannot install the integration into %s: %s", target, err)
            return self.status()

        state = self._read_state()
        state.update(
            {
                "restart_required": True,
                "installed_at": utcnow(),
                "version": self._read_version(target),
            }
        )
        self._write_state(state)
        _LOGGER.info("Installed integration %s into %s", ADDON_SLUG, target)
        return self.status()

    def clear_restart_flag(self) -> None:
        """Remember that Home Assistant was restarted after an installation."""
        state = self._read_state()
        if state.get("restart_required"):
            state["restart_required"] = False
            self._write_state(state)

    def mark_restart_needed(self, version: str | None = None) -> None:
        """Remember that Home Assistant must be restarted to load integration files."""
        state = self._read_state()
        state["restart_required"] = True
        if version:
            state["version"] = version
        self._write_state(state)