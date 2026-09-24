"""Minimal client for the Supervisor and Home Assistant Core APIs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Settings

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20


class HomeAssistantUnavailable(RuntimeError):
    """Raised when Home Assistant cannot be reached from the add-on."""


@dataclass(slots=True)
class CoreConfig:
    """The interesting parts of the Home Assistant configuration."""

    language: str | None = None
    version: str | None = None
    location_name: str | None = None


class HomeAssistantClient:
    """Talk to Supervisor and Core using the add-on token."""

    def __init__(self, settings: Settings) -> None:
        """Store the settings used for every request."""
        self.settings = settings
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        """Return True when a Supervisor token is available."""
        return bool(self.settings.supervisor_token)

    def _request(
        self,
        path: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> Any:
        """Run a blocking HTTP request against the Supervisor proxy."""
        url = f"{self.settings.supervisor_url.rstrip('/')}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.settings.supervisor_token}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            text = response.read().decode("utf-8", "replace")
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    async def _call(
        self,
        path: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> Any:
        """Run a request in a worker thread."""
        if not self.enabled:
            raise HomeAssistantUnavailable("No Supervisor token available")
        return await asyncio.to_thread(self._request, path, method, payload)

    # --------------------------------------------------------------- public
    async def async_core_config(self) -> CoreConfig:
        """Return the Home Assistant configuration, including the language."""
        data = await self._call("/core/api/config")
        if not isinstance(data, dict):
            data = {}
        return CoreConfig(
            language=data.get("language"),
            version=data.get("version"),
            location_name=data.get("location_name"),
        )

    async def async_language(self) -> str | None:
        """Return the language configured in Home Assistant, if reachable."""
        try:
            config = await self.async_core_config()
        except (HomeAssistantUnavailable, HTTPError, URLError, OSError, ValueError) as err:
            _LOGGER.debug("Cannot read the Home Assistant language: %s", err)
            return None
        return config.language

    async def async_self_info(self) -> dict[str, Any]:
        """Return the Supervisor information about this add-on."""
        try:
            response = await self._call("/addons/self/info")
        except (HomeAssistantUnavailable, HTTPError, URLError, OSError, ValueError) as err:
            _LOGGER.debug("Cannot read the add-on information: %s", err)
            return {}
        data = response.get("data") if isinstance(response, dict) else None
        return data if isinstance(data, dict) else {}

    async def async_addon_update_info(self) -> dict[str, Any]:
        """Return the version information of this add-on.

        Supervisor reports ``version_latest`` together with ``update_available``,
        which is what the interface needs to offer an update button. If the
        Supervisor API cannot be used at all (or does not know the newest
        version), the add-on list Home Assistant keeps in
        ``/config/.storage/hassio`` is read instead - that works without any API
        access.
        """
        info = await self.async_self_info()
        version = info.get("version")
        latest = info.get("version_latest")
        update_available = bool(info.get("update_available"))
        source = "self"

        if not update_available and not latest:
            from_updates = await self.async_available_addon_update()
            if from_updates:
                latest = from_updates
                update_available = True
                source = "available_updates"

        local: dict[str, Any] = {}
        if not info:
            local = self.read_home_assistant_status()
            if local:
                version = version or local.get("version")
                latest = latest or local.get("version_latest")
                update_available = update_available or bool(local.get("update_available"))
                source = "home_assistant"

        return {
            "available": bool(info) or source == "home_assistant",
            "version": version,
            "version_latest": latest,
            "update_available": update_available,
            "state": info.get("state") or local.get("state"),
            "source": source,
            "can_install": self.enabled,
            "hint": None if self.enabled else "token_missing",
            "error": None if info else self.last_error,
            "checked_at": local.get("checked_at"),
            "entity_id": local.get("entity_id"),
        }

    def read_home_assistant_status(self) -> dict[str, Any]:
        """Read the add-on version the integration published for us.

        Home Assistant asks the Supervisor itself and writes the answer into a
        file both sides share, which needs no API access from this container.
        """
        path = self.settings.addon_update_file
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            _LOGGER.debug("Cannot read %s: %s", path, err)
            return {}
        if not isinstance(data, dict):
            return {}

        installed = data.get("installed_version")
        latest = data.get("latest_version")
        return {
            "version": installed,
            "version_latest": latest,
            "update_available": bool(
                data.get("update_available")
                or (installed and latest and str(installed) != str(latest))
            ),
            "state": data.get("state"),
            "checked_at": data.get("checked_at"),
            "entity_id": data.get("entity_id"),
        }

    def environment_report(self) -> dict[str, Any]:
        """Describe how (and whether) this container can reach the Supervisor.

        Only names are reported, never values, so the log stays free of secrets.
        """
        names = sorted(
            name
            for name in os.environ
            if "SUPERVISOR" in name.upper() or "HASSIO" in name.upper()
        )
        return {
            "api_url": self.settings.supervisor_url,
            "token": self.enabled,
            "variables": names,
            "socket": (Path("/run/supervisor.sock")).exists(),
        }

    async def async_available_addon_update(self) -> str | None:
        """Return the newest version of this add-on from the update list."""
        try:
            response = await self._call("/available_updates")
        except (HomeAssistantUnavailable, HTTPError, URLError, OSError, ValueError) as err:
            self.last_error = f"/available_updates: {err}"
            _LOGGER.debug("Cannot read the list of available updates: %s", err)
            return None

        entries = response.get("data", {}).get("available_updates", []) if isinstance(
            response, dict
        ) else []
        if not isinstance(entries, list):
            return None

        name = (self.settings.addon_name or "").strip().lower()
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("update_type") != "addon":
                continue
            entry_name = str(entry.get("name") or "").strip().lower()
            if name and entry_name and entry_name != name:
                continue
            version = str(entry.get("version_latest") or "").strip()
            if version:
                return version
        return None

    async def async_store_reload(self) -> bool:
        """Ask Supervisor to look for new add-on versions.

        ``/store/reload`` is the documented way, the other two endpoints are older
        aliases that some Supervisor versions answer instead.
        """
        for path in ("/store/reload", "/addons/reload", "/reload_updates"):
            try:
                await self._call(path, method="POST", payload={})
            except (HomeAssistantUnavailable, HTTPError, URLError, OSError, ValueError) as err:
                self.last_error = f"{path}: {err}"
                _LOGGER.warning("Cannot reload the add-on store via %s: %s", path, err)
                continue
            _LOGGER.info("Add-on store reloaded via %s", path)
            self.last_error = None
            return True
        return False

    async def async_update_addon(self) -> bool:
        """Ask Supervisor to update this add-on to the newest version."""
        try:
            await self._call("/addons/self/update", method="POST", payload={})
        except (HomeAssistantUnavailable, HTTPError, URLError, OSError, ValueError) as err:
            self.last_error = f"/addons/self/update: {err}"
            _LOGGER.error("Cannot update the add-on: %s", err)
            return False
        self.last_error = None
        return True

    async def async_restart_core(self) -> bool:
        """Ask Home Assistant to restart itself."""
        try:
            await self._call(
                "/core/api/services/homeassistant/restart", method="POST", payload={}
            )
        except (HomeAssistantUnavailable, HTTPError, URLError, OSError, ValueError) as err:
            _LOGGER.error("Cannot restart Home Assistant: %s", err)
            return False
        return True