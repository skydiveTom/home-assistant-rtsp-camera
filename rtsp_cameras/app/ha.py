"""Minimal client for the Supervisor and Home Assistant Core APIs."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
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