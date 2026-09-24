"""Live end-to-end tests against a real RTSP camera on the local network.

They are skipped unless ``RTSP_LIVE_CAMERA_URL`` is exported, so CI stays offline::

    $env:RTSP_LIVE_CAMERA_URL = "rtsp://192.168.1.28:554/user=admin&password=&channel=1&stream=0.sdp"
    .venv-ha\\Scripts\\python -m pytest -c ha_tests/pytest.ini ha_tests/test_live_camera.py -q

What they prove, exactly like Home Assistant does it in production:

* the camera collected by the add-on becomes a ``camera.<name>`` entity,
* ``camera.stream_source`` hands Home Assistant the RTSP URL (the 2026.9 API),
* the RTSP transport from the add-on reaches the stream component,
* Home Assistant's own ``stream`` component decodes the live video (JPEG frame),
* ``camera.async_get_image`` (the API behind the camera card and
  ``camera.snapshot``) returns a real image.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.camera import async_get_image, async_get_stream_source
from homeassistant.components.stream.const import CONF_RTSP_TRANSPORT
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtsp_cameras.const import (
    CONF_CAMERAS_FILE,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)

LIVE_URL = os.environ.get("RTSP_LIVE_CAMERA_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not LIVE_URL, reason="set RTSP_LIVE_CAMERA_URL to run the live camera tests"
)

CAMERAS_FILE = "rtsp_cameras/cameras.json"


def publish_live_camera(hass: HomeAssistant) -> None:
    """Write the cameras.json the add-on would publish for the live camera."""
    path = Path(hass.config.path("rtsp_cameras", "cameras.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "generator": "RTSP Camera Manager add-on",
                "cameras": [
                    {
                        "id": "lokalna-kamera",
                        "name": "Lokalna kamera",
                        "url": LIVE_URL,
                        "rtsp_transport": "tcp",
                        "enabled": True,
                        "codec": "h264",
                        "preview_mode": "mjpeg",
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.fixture(name="entry")
def entry_fixture(hass: HomeAssistant) -> MockConfigEntry:
    """Create the config entry the add-on ships with its integration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="RTSP Camera Manager",
        data={CONF_CAMERAS_FILE: CAMERAS_FILE, CONF_SCAN_INTERVAL: 5},
    )
    entry.add_to_hass(hass)
    return entry


async def setup_live_camera(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Publish the camera, set the integration up and return the entity id."""
    publish_live_camera(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entity_ids = hass.states.async_entity_ids("camera")
    assert entity_ids, "the live camera must become a camera entity"
    return entity_ids[0]


async def test_live_camera_entity_uses_the_addon_url(hass, entry):
    """The entity exists and offers the RTSP URL plus its transport."""
    entity_id = await setup_live_camera(hass, entry)

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["rtsp_camera_id"] == "lokalna-kamera"
    assert state.attributes["rtsp_transport"] == "tcp"

    assert await async_get_stream_source(hass, entity_id) == LIVE_URL

    camera = hass.data["camera"].get_entity(entity_id)
    assert camera is not None
    assert camera.stream_options[CONF_RTSP_TRANSPORT] == "tcp"


async def stop_camera_stream(hass: HomeAssistant, camera: Any) -> None:
    """Stop the stream Home Assistant created for the camera.

    Without this the stream worker keeps idle timers alive and the test harness
    reports lingering timers after the test. ``Stream.stop()`` only drops the
    outputs, so their idle timers are cleared first.
    """
    stream = getattr(camera, "stream", None)
    if stream is None:
        return
    for output in stream.outputs().values():
        timer = getattr(output, "idle_timer", None)
        if timer is not None:
            timer.clear()
    await stream.stop()
    await hass.async_block_till_done()


async def test_live_camera_stream_decodes(hass, entry):
    """Home Assistant's stream component really decodes the live video."""
    import asyncio

    entity_id = await setup_live_camera(hass, entry)
    camera = hass.data["camera"].get_entity(entity_id)
    assert camera is not None

    try:
        stream = await camera.async_create_stream()
        assert stream is not None

        # Right after the start there is no keyframe yet, so one is awaited.
        async with asyncio.timeout(30):
            frame: bytes | None = await stream.async_get_image(wait_for_next_keyframe=True)

        assert frame is not None, "no frame came out of the live stream"
        assert frame[:3] == b"\xff\xd8\xff", "the frame must be a JPEG"
    finally:
        await stop_camera_stream(hass, camera)


async def test_live_camera_snapshot_through_the_camera_api(hass, entry):
    """``camera.async_get_image`` (camera card, camera.snapshot) works."""
    entity_id = await setup_live_camera(hass, entry)
    camera = hass.data["camera"].get_entity(entity_id)
    assert camera is not None

    try:
        image: Any = await async_get_image(hass, entity_id)
    finally:
        await stop_camera_stream(hass, camera)

    assert image.content_type == "image/jpeg"
    assert image.content[:3] == b"\xff\xd8\xff"
