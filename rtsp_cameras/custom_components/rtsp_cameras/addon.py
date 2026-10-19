"""The API of the add-on, as seen from Home Assistant.

Home Assistant Core and the add-on are two containers of the same Supervisor, so
Core cannot use the address the browser uses (the authenticated ingress). The
Supervisor knows the add-on's own address, and the integration asks it once and
keeps the answer for a while - a dashboard asks for a still of every camera on
every view, and a Supervisor call in front of each one would cost more than the
image.

The integration only *reads* from that API (a JPEG of a camera). Everything that
changes the configuration still goes through the camera file the add-on writes,
which keeps one owner per piece of state.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import quote, urlsplit

from homeassistant.core import HomeAssistant

from .const import (
    ADDON_API_PORT,
    ADDON_SLUG,
    ADDON_SNAPSHOT_PATH,
    ADDON_URL_RESOLVE_SECONDS,
    ADDON_URL_RETRY_SECONDS,
    DATA_ADDON_API_URLS,
)

_LOGGER = logging.getLogger(__name__)


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
