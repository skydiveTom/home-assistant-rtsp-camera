"""Offline tests for the still image path of the camera entity.

Home Assistant's camera card, the entity thumbnail and ``camera.snapshot`` all end
up in ``camera.async_get_image``. Home Assistant would take the still from its own
stream, but ``use_stream_for_stills`` is a plain property in 2026.9 (there is no
``_attr_use_stream_for_stills``) and its stream returns no image until a keyframe
was decoded - so the entity has to fetch the still itself.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtsp_cameras import camera as camera_module
from custom_components.rtsp_cameras.const import (
    CONF_CAMERAS_FILE,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)

CAMERAS_FILE = "rtsp_cameras/cameras.json"
JPEG = b"\xff\xd8\xff\xe0" + b"still-image-bytes" + b"\xff\xd9"


def stream_factory(stream: FakeStream) -> Any:
    """Return a zero-argument stand-in for ``Camera.async_create_stream``."""

    async def create() -> FakeStream:
        return stream

    return create


def snapshot_app(handler: Any) -> web.Application:
    """Return an application serving ``/snapshot.jpg`` with a handler."""
    app = web.Application()
    app.router.add_get("/snapshot.jpg", handler)
    return app


class FakeStream:
    """Stand-in for ``homeassistant.components.stream.Stream``."""

    def __init__(self, image: bytes | None = JPEG, delay: float = 0.0) -> None:
        self.image = image
        self.delay = delay
        self.calls: list[bool] = []

    async def async_get_image(
        self,
        width: int | None = None,
        height: int | None = None,
        wait_for_next_keyframe: bool = False,
    ) -> bytes | None:
        """Record how the image was requested."""
        self.calls.append(wait_for_next_keyframe)
        if wait_for_next_keyframe:
            await asyncio.sleep(self.delay)
        return self.image


def write_cameras_file(hass: HomeAssistant, camera: dict[str, Any]) -> None:
    """Publish a single camera the way the add-on does."""
    path = Path(hass.config.path("rtsp_cameras", "cameras.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "cameras": [camera]}), encoding="utf-8")


@pytest.fixture(name="entry")
def entry_fixture(hass: HomeAssistant) -> MockConfigEntry:
    """Create the config entry the add-on ships with its integration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="RTSP Camera Manager",
        data={CONF_CAMERAS_FILE: CAMERAS_FILE, CONF_SCAN_INTERVAL: 2},
    )
    entry.add_to_hass(hass)
    return entry


async def setup_camera(hass: HomeAssistant, entry: MockConfigEntry, **extra: Any) -> Any:
    """Set the integration up and return the camera entity object."""
    write_cameras_file(
        hass,
        {
            "id": "front_door",
            "name": "Front Door",
            "url": "rtsp://10.0.0.5:554/stream1",
            "rtsp_transport": "tcp",
            "enabled": True,
            **extra,
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    camera = hass.data["camera"].get_entity("camera.front_door")
    assert camera is not None
    return camera


async def test_still_awaits_a_keyframe_from_the_stream(hass, entry, monkeypatch):
    """The still is taken from the live stream, waiting for a keyframe."""
    camera = await setup_camera(hass, entry)
    stream = FakeStream()
    monkeypatch.setattr(camera, "async_create_stream", stream_factory(stream))

    assert await camera.async_camera_image() == JPEG
    assert stream.calls == [True], "the keyframe must be awaited first"


async def test_still_falls_back_when_no_keyframe_arrives(hass, entry, monkeypatch):
    """A slow keyframe does not turn into 'Unable to get image'."""
    camera = await setup_camera(hass, entry)
    stream = FakeStream(delay=5)
    monkeypatch.setattr(camera, "async_create_stream", stream_factory(stream))
    monkeypatch.setattr(camera_module, "KEYFRAME_WAIT_SECONDS", 0.05)

    assert await camera.async_camera_image() == JPEG
    assert stream.calls == [True, False], "after the timeout the last frame is used"


async def test_snapshot_url_has_priority(hass, entry, aiohttp_client, monkeypatch):
    """A snapshot URL published by the add-on is used for the still."""
    payload = b"\xff\xd8\xff\xe0" + b"from-the-snapshot-url" + b"\xff\xd9"

    async def snapshot(request: web.Request) -> web.Response:
        return web.Response(body=payload, content_type="image/jpeg")

    server = await aiohttp_client(snapshot_app(snapshot))
    url = str(server.make_url("/snapshot.jpg"))

    camera = await setup_camera(hass, entry, snapshot_url=url)
    stream = FakeStream()
    monkeypatch.setattr(camera, "async_create_stream", stream_factory(stream))

    assert await camera.async_camera_image() == payload
    assert stream.calls == [], "the stream is not needed when a snapshot exists"


async def test_broken_snapshot_url_falls_back_to_the_stream(hass, entry, monkeypatch):
    """A snapshot URL that does not answer must not break the still."""
    camera = await setup_camera(hass, entry, snapshot_url="http://127.0.0.1:9/snapshot.jpg")
    stream = FakeStream()
    monkeypatch.setattr(camera, "async_create_stream", stream_factory(stream))

    assert await camera.async_camera_image() == JPEG
    assert stream.calls == [True]
