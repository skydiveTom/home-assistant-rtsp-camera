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

import aiohttp
import pytest
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


class FakeResponse:
    """Minimal stand-in for the aiohttp response of a snapshot URL.

    The Home Assistant test harness blocks sockets, so the snapshot URL is checked
    with a fake session instead of a web server - the integration only reads the
    status code and the body.
    """

    def __init__(self, body: bytes = b"", status: int = 200) -> None:
        self.content = body
        self.status = status
        self.requested: list[str] = []

    async def read(self) -> bytes:
        """Return the body of the snapshot."""
        return self.content

    async def __aenter__(self) -> FakeResponse:
        """Enter the response context."""
        return self

    async def __aexit__(self, *args: object) -> bool:
        """Leave the response context."""
        return False


class FakeSession:
    """Stand-in for the shared aiohttp session of Home Assistant."""

    def __init__(self, response: FakeResponse | None = None, error: Exception | None = None):
        self.response = response or FakeResponse()
        self.error = error
        self.urls: list[str] = []

    def get(self, url: str) -> FakeResponse:
        """Record the URL and return the prepared response."""
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.response


def use_session(monkeypatch: pytest.MonkeyPatch, session: FakeSession) -> None:
    """Make the integration use the fake aiohttp session."""
    monkeypatch.setattr(camera_module, "async_get_clientsession", lambda hass: session)


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


async def test_snapshot_url_has_priority(hass, entry, monkeypatch):
    """A snapshot URL published by the add-on is used for the still."""
    payload = b"\xff\xd8\xff\xe0" + b"from-the-snapshot-url" + b"\xff\xd9"
    url = "http://10.0.0.5/snapshot.jpg"

    camera = await setup_camera(hass, entry, snapshot_url=url)
    stream = FakeStream()
    monkeypatch.setattr(camera, "async_create_stream", stream_factory(stream))
    session = FakeSession(FakeResponse(body=payload))
    use_session(monkeypatch, session)

    assert await camera.async_camera_image() == payload
    assert session.urls == [url]
    assert stream.calls == [], "the stream is not needed when a snapshot exists"


async def test_failing_snapshot_url_falls_back_to_the_stream(hass, entry, monkeypatch, caplog):
    """A snapshot URL that does not answer must not break the still."""
    camera = await setup_camera(
        hass, entry, snapshot_url="http://10.0.0.5/snapshot.jpg"
    )
    stream = FakeStream()
    monkeypatch.setattr(camera, "async_create_stream", stream_factory(stream))
    use_session(monkeypatch, FakeSession(error=aiohttp.ClientError("no answer")))

    with caplog.at_level("WARNING"):
        assert await camera.async_camera_image() == JPEG

    assert stream.calls == [True]
    assert any("Snapshot of camera.front_door failed" in record.message for record in caplog.records)


async def test_error_status_of_the_snapshot_url_falls_back(hass, entry, monkeypatch, caplog):
    """A snapshot URL answering with an error status is ignored."""
    camera = await setup_camera(
        hass, entry, snapshot_url="http://10.0.0.5/snapshot.jpg"
    )
    stream = FakeStream()
    monkeypatch.setattr(camera, "async_create_stream", stream_factory(stream))
    use_session(monkeypatch, FakeSession(FakeResponse(status=404)))

    with caplog.at_level("WARNING"):
        assert await camera.async_camera_image() == JPEG

    assert stream.calls == [True]
    assert any("returned HTTP 404" in record.message for record in caplog.records)
