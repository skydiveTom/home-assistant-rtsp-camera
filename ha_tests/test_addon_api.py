"""Tests for the address the integration uses to reach the add-on.

The add-on is a container next to Home Assistant Core: the browser talks to it
through the ingress, Core has to ask the Supervisor for its own way in. These
tests keep that lookup honest without a Supervisor (the real one is exercised by
the live test of the camera stills).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.rtsp_cameras import addon as addon_module
from custom_components.rtsp_cameras.const import DATA_ADDON_API_URLS


class FakeAddons:
    """Stand-in for the add-on endpoints of the Supervisor client."""

    def __init__(
        self,
        addons: list[SimpleNamespace] | None = None,
        info: SimpleNamespace | None = None,
        error: Exception | None = None,
    ) -> None:
        self._addons = addons or []
        self._info = info
        self._error = error
        self.info_calls: list[str] = []

    async def list(self) -> list[SimpleNamespace]:
        """Return the installed add-ons."""
        if self._error is not None:
            raise self._error
        return self._addons

    async def addon_info(self, slug: str) -> SimpleNamespace:
        """Return the record of one add-on."""
        self.info_calls.append(slug)
        return self._info or SimpleNamespace()


def use_supervisor(monkeypatch: pytest.MonkeyPatch, addons: FakeAddons) -> None:
    """Make the integration use the fake Supervisor client."""
    from homeassistant.components.hassio import handler  # noqa: PLC0415

    monkeypatch.setattr(
        handler, "get_supervisor_client", lambda hass: SimpleNamespace(addons=addons)
    )


def addon_record(slug: str, ip: str = "172.30.33.4", port: int = 8099) -> SimpleNamespace:
    """Return what the Supervisor answers about the add-on."""
    return SimpleNamespace(
        slug=slug,
        ip_address=ip,
        ingress_port=port,
        ingress_entry="/api/hassio_ingress/Token123",
    )


def test_snapshot_url_names_the_camera_in_the_path() -> None:
    """One still is a GET on the camera of the add-on."""
    assert addon_module.snapshot_url(
        "http://172.30.33.4:8099/", "balkon-2"
    ) == "http://172.30.33.4:8099/api/cameras/balkon-2/snapshot.jpg"


def test_a_camera_id_is_quoted_for_the_path() -> None:
    """A camera id with a space cannot break the request line."""
    assert addon_module.snapshot_url("http://host", "front door") == (
        "http://host/api/cameras/front%20door/snapshot.jpg"
    )


def test_the_port_comes_from_the_supervisor_or_the_default() -> None:
    """A Supervisor that does not name a usable port falls back to the add-on's."""
    assert addon_module._port(8099) == 8099
    assert addon_module._port("8099") == 8099
    assert addon_module._port(None) == addon_module.ADDON_API_PORT
    assert addon_module._port(0) == addon_module.ADDON_API_PORT
    assert addon_module._port("nonsense") == addon_module.ADDON_API_PORT


def test_the_ingress_of_the_addon_is_the_second_door(hass) -> None:
    """The Supervisor proxies the ingress of the add-on at ``/ingress/<token>``."""
    hass.data["hassio"] = SimpleNamespace(base_url="http://supervisor")

    assert addon_module._ingress_base_url(hass, "/api/hassio_ingress/Token123") == (
        "http://supervisor/ingress/Token123"
    )
    assert addon_module._ingress_base_url(hass, None) is None


async def test_the_lookup_finds_a_local_addon_by_the_tail_of_its_slug(hass, monkeypatch):
    """A local add-on is named ``<hash>_rtsp_cameras`` by the Supervisor."""
    addons = FakeAddons(
        addons=[
            SimpleNamespace(slug="core_ssh"),
            SimpleNamespace(slug="97dbc456_rtsp_cameras"),
        ],
        info=addon_record("97dbc456_rtsp_cameras"),
    )
    use_supervisor(monkeypatch, addons)
    hass.data["hassio"] = SimpleNamespace(base_url="http://supervisor")

    urls = await addon_module.async_addon_urls(hass, refresh=True)

    assert urls == [
        "http://172.30.33.4:8099",
        "http://supervisor/ingress/Token123",
    ]
    assert addons.info_calls == ["97dbc456_rtsp_cameras"]


async def test_a_supervisor_without_an_answer_gives_no_address(hass, monkeypatch):
    """Without a Supervisor the add-on is simply not part of the path."""
    use_supervisor(monkeypatch, FakeAddons(error=OSError("no Supervisor")))

    assert await addon_module.async_addon_urls(hass, refresh=True) == []


async def test_the_address_is_asked_once(hass, monkeypatch):
    """A dashboard asks again and again, the Supervisor only once in a while."""
    addons = FakeAddons(
        addons=[SimpleNamespace(slug="rtsp_cameras")],
        info=addon_record("rtsp_cameras"),
    )
    use_supervisor(monkeypatch, addons)
    hass.data["hassio"] = SimpleNamespace(base_url="http://supervisor")

    await addon_module.async_addon_urls(hass, refresh=True)
    await addon_module.async_addon_urls(hass)

    assert addons.info_calls == ["rtsp_cameras"], "the answer was kept"

    await addon_module.async_addon_urls(hass, refresh=True)

    assert addons.info_calls == ["rtsp_cameras", "rtsp_cameras"]
    assert DATA_ADDON_API_URLS in hass.data
