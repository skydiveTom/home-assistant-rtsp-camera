"""The API of the add-on, as seen from Home Assistant.

Home Assistant Core and the add-on are two containers of the same Supervisor, so
Core cannot use the address the browser uses (the authenticated ingress). The
Supervisor knows the add-on's own address, and the integration asks it once and
keeps the answer for a while - a dashboard asks for a still of every camera on
every view, and a Supervisor call in front of each one would cost more than the
image.

Two things go through that API: a still of a camera, and every PTZ move. The
move is delegated on purpose - the add-on is the one owner of the coordinates of
the ladders of step positions (it writes the camera file, it keeps the panel, and
``step_crossings`` of a ladder is its data), so a step taken here starts where the
panel left the camera and the panel starts where this left it. Everything that
changes the configuration still goes through the camera file the add-on writes,
which keeps one owner per piece of state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, NamedTuple
from urllib.parse import quote, urlsplit

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ADDON_API_PORT,
    ADDON_PTZ_PATH,
    ADDON_PTZ_TIMEOUT_SECONDS,
    ADDON_SLUG,
    ADDON_SNAPSHOT_PATH,
    ADDON_URL_RESOLVE_SECONDS,
    ADDON_URL_RETRY_SECONDS,
    DATA_ADDON_API_URLS,
)

_LOGGER = logging.getLogger(__name__)


class AddonAnswer(NamedTuple):
    """One answer of the add-on: the status code and the JSON body behind it."""

    status: int
    body: dict[str, Any]


async def async_addon_urls(hass: HomeAssistant, *, refresh: bool = False) -> list[str]:
    """Return the base URLs of the add-on's API, the best guess first.

    Two doors lead to the add-on: the address of its container (one request, no
    detour) and the ingress the Supervisor proxies for the panel. The container
    address is tried first; the ingress keeps a camera preview working on a
    system where the container address is out of reach of Core, and it is the
    reason a failing still is not simply reported as *no picture*.
    """
    cached = hass.data.get(DATA_ADDON_API_URLS)
    now = time.monotonic()
    if not refresh and cached is not None and now < cached["expires"]:
        return list(cached["urls"])

    urls = await _async_resolve_urls(hass)
    ttl = ADDON_URL_RESOLVE_SECONDS if urls else ADDON_URL_RETRY_SECONDS
    hass.data[DATA_ADDON_API_URLS] = {"urls": urls, "expires": now + ttl}
    return list(urls)


def snapshot_url(base_url: str, camera_id: str) -> str:
    """Return the URL of one still of a camera inside the add-on's API."""
    path = ADDON_SNAPSHOT_PATH.format(camera_id=quote(str(camera_id), safe=""))
    return f"{base_url.rstrip('/')}{path}"


def ptz_url(base_url: str, camera_id: str) -> str:
    """Return the URL of the PTZ endpoint of one camera inside the add-on's API."""
    path = ADDON_PTZ_PATH.format(camera_id=quote(str(camera_id), safe=""))
    return f"{base_url.rstrip('/')}{path}"


async def async_ptz(
    hass: HomeAssistant, camera_id: str, body: dict[str, Any]
) -> AddonAnswer | None:
    """Ask the add-on to move a camera, or return None when it cannot be reached.

    The add-on is the one owner of the coordinates of a camera on its ladders of step
    positions: it writes the camera file, it keeps the panel and it knows which rung a
    ladder was stored at (``step_crossings``). A move that went around it would leave the
    other side on a stale rung - a step after a panel step started one rung off, and the
    panel started one rung off after a step taken here.

    ``None`` means *no address answered* - a stopped container, an unreachable network, a
    body that is not JSON (an ingress that asks for a login first). The caller sends the
    command of the camera itself then: the integration still moves a camera on a system
    where the add-on is not running.
    """
    bases = await async_addon_urls(hass)
    answer = await _async_post_ptz(hass, bases, camera_id, body)
    if answer is not None or not bases:
        return answer
    # The address may be stale (a container that was rebuilt gets a new one).
    return await _async_post_ptz(hass, await async_addon_urls(hass, refresh=True), camera_id, body)


async def _async_post_ptz(
    hass: HomeAssistant, bases: list[str], camera_id: str, body: dict[str, Any]
) -> AddonAnswer | None:
    """POST one PTZ action to every known address of the add-on, the first answer wins."""
    session = async_get_clientsession(hass)
    for base in bases:
        url = ptz_url(base, camera_id)
        try:
            async with asyncio.timeout(ADDON_PTZ_TIMEOUT_SECONDS):
                async with session.post(url, json=body) as response:
                    payload = await _async_json(response)
                    if payload is not None:
                        return AddonAnswer(int(response.status), payload)
                    _LOGGER.debug(
                        "The add-on answered %s on %s without a PTZ body", response.status, url
                    )
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("The add-on did not answer the PTZ call on %s: %s", url, err)
    return None


async def _async_json(response: aiohttp.ClientResponse) -> dict[str, Any] | None:
    """Return the JSON object of a response, or None when the body is something else."""
    try:
        payload = await response.json(content_type=None)
    except (ValueError, aiohttp.ContentTypeError):
        return None
    return payload if isinstance(payload, dict) else None


async def _async_resolve_urls(hass: HomeAssistant) -> list[str]:
    """Ask the Supervisor where the add-on and its ingress live."""
    try:
        from homeassistant.components.hassio.handler import (  # noqa: PLC0415
            get_supervisor_client,
        )
    except ImportError:  # pragma: no cover - every Home Assistant ships hassio
        return []

    try:
        client = get_supervisor_client(hass)
        slug = await _async_addon_slug(client)
        if slug is None:
            _LOGGER.debug("Supervisor knows no add-on whose slug ends in %s", ADDON_SLUG)
            return []
        info = await client.addons.addon_info(slug)
    except Exception as err:  # noqa: BLE001 - no Supervisor means no add-on API
        _LOGGER.debug("Cannot ask the Supervisor for the address of the add-on: %s", err)
        return []

    urls: list[str] = []
    host = str(getattr(info, "ip_address", "") or "").strip()
    port = _port(getattr(info, "ingress_port", None))
    if host:
        urls.append(f"http://{host}:{port}")

    ingress = _ingress_base_url(hass, getattr(info, "ingress_entry", None))
    if ingress:
        urls.append(ingress)
    return urls


async def _async_addon_slug(client: object) -> str | None:
    """Return the slug of this add-on.

    A local add-on (one installed from a folder rather than from a repository)
    is named ``<hash>_<slug>`` by the Supervisor - the shipped ``rtsp_cameras``
    is only its tail - so the list is searched for the matching tail.
    """
    addons = None
    try:
        addons = await client.addons.list()  # type: ignore[attr-defined]
    except Exception as err:  # noqa: BLE001 - an answer without the list is enough
        _LOGGER.debug("Cannot list the add-ons of the Supervisor: %s", err)

    for addon in addons or []:
        slug = str(getattr(addon, "slug", "") or "")
        if slug == ADDON_SLUG or slug.endswith(f"_{ADDON_SLUG}"):
            return slug
    return None


def _port(value: object) -> int:
    """Return the port the add-on listens on."""
    try:
        port = int(str(value))
    except (TypeError, ValueError):
        return ADDON_API_PORT
    return port if 0 < port < 65536 else ADDON_API_PORT


def _ingress_base_url(hass: HomeAssistant, entry: object) -> str | None:
    """Return ``http://<supervisor>/ingress/<token>`` of the add-on."""
    token = urlsplit(str(entry or "")).path.rstrip("/").rsplit("/", 1)[-1]
    hassio = hass.data.get("hassio")
    base_url = getattr(hassio, "base_url", None)
    if not token or base_url is None:
        return None
    return f"{str(base_url).rstrip('/')}/ingress/{token}"
