"""Actions the add-on asks Home Assistant to perform.

The add-on cannot update itself when it has no Supervisor token, so it drops a
request into the folder both sides share (``actions.json`` next to
``cameras.json``). The integration polls that file and performs the action
through Home Assistant, which always has full access to the Supervisor.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import ADDON_SLUG, DOMAIN

_LOGGER = logging.getLogger(__name__)

ACTIONS_FILENAME = "actions.json"
STATUS_FILENAME = "addon_update.json"
ACTION_INSTALL_ADDON_UPDATE = "install_addon_update"
ACTION_REFRESH_ADDON_UPDATE = "refresh_addon_update"
# Requests older than this are ignored, so a stale file never triggers anything.
MAX_REQUEST_AGE = timedelta(minutes=5)
HASSIO_DOMAIN = "hassio"


def actions_file_for(cameras_file: Path) -> Path:
    """Return the action file that belongs to a camera file."""
    return cameras_file.with_name(ACTIONS_FILENAME)


def status_file_for(cameras_file: Path) -> Path:
    """Return the file the integration publishes the add-on version into."""
    return cameras_file.with_name(STATUS_FILENAME)


async def async_publish_addon_update(hass: HomeAssistant, path: Path) -> None:
    """Publish what Home Assistant knows about the add-on update into the file.

    Home Assistant talks to Supervisor itself, so this works even when the add-on
    container has no Supervisor token of its own.
    """
    entity_id = addon_update_entity_id(hass)
    state = hass.states.get(entity_id) if entity_id else None
    if state is None:
        return

    attributes = state.attributes
    installed = attributes.get("installed_version")
    latest = attributes.get("latest_version")
    payload = {
        "entity_id": entity_id,
        "checked_at": dt_util.utcnow().isoformat(),
        "installed_version": installed,
        "latest_version": latest,
        "update_available": bool(latest and installed and str(latest) != str(installed)),
        "state": state.state,
    }
    await hass.async_add_executor_job(_write_status, path, payload)


async def async_handle_actions(hass: HomeAssistant, path: Path) -> None:
    """Perform a pending action requested by the add-on."""
    payload = await hass.async_add_executor_job(_read_actions, path)
    if not payload or payload.get("handled_at"):
        return

    requested_at = dt_util.parse_datetime(str(payload.get("requested_at") or ""))
    if requested_at is None or dt_util.utcnow() - requested_at > MAX_REQUEST_AGE:
        return

    if not await hass.async_add_executor_job(_mark_handled, path, payload):
        return

    action = str(payload.get("action") or "")
    _LOGGER.info("Add-on requested %s", action)
    if action == ACTION_INSTALL_ADDON_UPDATE:
        await _async_install_addon_update(hass)
    elif action == ACTION_REFRESH_ADDON_UPDATE:
        await _async_refresh_addon_update(hass)
    else:
        _LOGGER.warning("Ignoring unknown action requested by the add-on: %s", action)


async def _async_install_addon_update(hass: HomeAssistant) -> None:
    """Ask the Supervisor, through Home Assistant, to update the add-on."""
    entity_id = addon_update_entity_id(hass)
    if entity_id is None:
        _LOGGER.error(
            "The add-on asked for an update, but Home Assistant has no update "
            "entity for %s. Update it in Settings -> Add-ons.",
            DOMAIN,
        )
        return

    try:
        await hass.services.async_call(
            "update", "install", {"entity_id": entity_id}, blocking=False
        )
    except Exception:  # noqa: BLE001 - a failing service must not break the poll
        _LOGGER.exception("Cannot start the add-on update through Home Assistant")
        return

    _LOGGER.info("Add-on update started through Home Assistant (%s)", entity_id)


async def _async_refresh_addon_update(hass: HomeAssistant) -> None:
    """Let Home Assistant ask Supervisor for the newest add-on version."""
    entity_id = addon_update_entity_id(hass)
    if entity_id is None:
        return
    try:
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": entity_id}, blocking=True
        )
    except Exception:  # noqa: BLE001 - a failing service must not break the poll
        _LOGGER.exception("Cannot refresh the add-on update information")
        return
    _LOGGER.info("Asked Home Assistant to check for a new add-on version")


def addon_update_entity_id(hass: HomeAssistant) -> str | None:
    """Return the Home Assistant update entity of the add-on, if it exists."""
    registry = er.async_get(hass)
    for unique_id in (ADDON_SLUG, ADDON_SLUG.replace("_", "-"), DOMAIN):
        entity_id = registry.async_get_entity_id("update", HASSIO_DOMAIN, unique_id)
        if entity_id:
            return entity_id

    for entry in registry.entities.values():
        if (
            entry.platform == HASSIO_DOMAIN
            and entry.domain == "update"
            and ADDON_SLUG in str(entry.unique_id or "")
        ):
            return entry.entity_id
    return None


def _read_actions(path: Path) -> dict[str, Any]:
    """Read the action file, ignoring anything unusable."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        _LOGGER.debug("Cannot read %s: %s", path, err)
        return {}
    return data if isinstance(data, dict) else {}


def _mark_handled(path: Path, payload: dict[str, Any]) -> bool:
    """Stamp the request as handled before the action runs (no double runs)."""
    updated = dict(payload)
    updated["handled_at"] = dt_util.utcnow().isoformat()
    return _write_json(path, updated)


def _write_status(path: Path, payload: dict[str, Any]) -> bool:
    """Write the add-on version file, skipping identical content."""
    if _read_actions(path) == payload:
        return True
    return _write_json(path, payload)


def _write_json(path: Path, payload: dict[str, Any]) -> bool:
    """Write a JSON document atomically."""
    try:
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
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        finally:
            handle.close()
        os.replace(handle.name, path)
    except OSError as err:
        _LOGGER.error("Cannot update %s: %s", path, err)
        return False
    return True
