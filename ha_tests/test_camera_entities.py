"""End-to-end tests that run a real Home Assistant core with this integration.

They answer the question users care about the most: "does the camera from the
add-on panel show up in Home Assistant as a ``camera`` entity?"

Run them with the dedicated virtual environment (see ``conftest.py``)::

    .venv-ha\\Scripts\\python -m pytest -c ha_tests/pytest.ini ha_tests -q
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.camera import CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtsp_cameras.const import (
    CONF_CAMERAS_FILE,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)

CAMERAS_FILE = "rtsp_cameras/cameras.json"

CAMERAS: list[dict[str, Any]] = [
    {
        "id": "front_door",
        "name": "Front Door",
        "url": "rtsp://user:pass@10.0.0.5:554/stream1",
        "rtsp_transport": "tcp",
        "snapshot_url": "http://10.0.0.5/snapshot.jpg",
        "enabled": True,
    },
    {
        "id": "garage",
        "name": "Garage",
        "url": "rtsp://10.0.0.6:554/Streaming/Channels/101",
        "rtsp_transport": "udp",
        "enabled": True,
    },
    {
        "id": "disabled_cam",
        "name": "Disabled Camera",
        "url": "rtsp://10.0.0.7:554/disabled",
        "enabled": False,
    },
]


def cameras_file_path(hass: HomeAssistant) -> Path:
    """Return the absolute path of the camera file the integration watches."""
    return Path(hass.config.path(*CAMERAS_FILE.split("/")))


def write_cameras_file(hass: HomeAssistant, cameras: list[dict[str, Any]] | None = None) -> Path:
    """Write the file the add-on normally publishes."""
    path = cameras_file_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "cameras": CAMERAS if cameras is None else cameras}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


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


async def test_published_cameras_become_camera_entities(hass, entry):
    """Enabled cameras turn into camera entities with the RTSP stream source."""
    write_cameras_file(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert sorted(hass.states.async_entity_ids("camera")) == [
        "camera.front_door",
        "camera.garage",
    ]

    state = hass.states.get("camera.front_door")
    assert state is not None
    assert state.name == "Front Door"
    assert state.attributes["rtsp_camera_id"] == "front_door"
    assert state.attributes["rtsp_transport"] == "tcp"
    assert state.attributes["source_file"] == str(cameras_file_path(hass))
    assert state.attributes["supported_features"] == CameraEntityFeature.STREAM

    camera = hass.data["camera"].get_entity("camera.front_door")
    assert camera is not None
    assert await camera.stream_source() == "rtsp://user:pass@10.0.0.5:554/stream1"
    assert camera.entity_picture == "http://10.0.0.5/snapshot.jpg"

    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id("camera", DOMAIN, f"{DOMAIN}_front_door")
        == "camera.front_door"
    )

    entity_entry = registry.async_get("camera.front_door")
    assert entity_entry is not None and entity_entry.device_id is not None
    device = dr.async_get(hass).async_get(entity_entry.device_id)
    assert device is not None
    assert device.name == "Front Door"


async def test_camera_added_after_startup_appears_without_restart(hass, entry):
    """Home Assistant may start long before the add-on writes the file."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.async_entity_ids("camera") == []

    write_cameras_file(hass)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert sorted(hass.states.async_entity_ids("camera")) == [
        "camera.front_door",
        "camera.garage",
    ]


async def test_removed_camera_disappears_again(hass, entry):
    """Deleting a camera in the add-on removes its entity on the next poll."""
    write_cameras_file(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    write_cameras_file(hass, [CAMERAS[0]])
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert sorted(hass.states.async_entity_ids("camera")) == ["camera.front_door"]


async def test_addon_update_request_is_executed_through_home_assistant(hass, entry):
    """Without a Supervisor token the add-on asks Home Assistant for the update."""
    import json
    from pathlib import Path

    from homeassistant.util import dt as dt_util

    write_cameras_file(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Home Assistant owns a hassio update entity for the add-on ...
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "update", "hassio", "rtsp_cameras", suggested_object_id="rtsp_camera_manager"
    )
    calls: list[dict] = []

    async def _install(call):
        calls.append(dict(call.data))

    hass.services.async_register("update", "install", _install)

    # ... and the add-on drops a request into the shared folder.
    actions = Path(hass.config.path("rtsp_cameras", "actions.json"))
    actions.write_text(
        json.dumps(
            {
                "action": "install_addon_update",
                "requested_at": dt_util.utcnow().isoformat(),
                "handled_at": None,
            }
        ),
        encoding="utf-8",
    )

    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert calls == [{"entity_id": "update.rtsp_camera_manager"}]
    handled = json.loads(actions.read_text(encoding="utf-8"))
    assert handled["handled_at"], "a handled request must not run twice"

    # A second poll with the same file must not call the service again.
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_stale_addon_update_requests_are_ignored(hass, entry):
    """A request that nobody picked up within minutes is not executed later."""
    import json
    from datetime import timedelta
    from pathlib import Path

    from homeassistant.util import dt as dt_util

    write_cameras_file(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    calls: list[dict] = []

    async def _install(call):
        calls.append(dict(call.data))

    hass.services.async_register("update", "install", _install)

    actions = Path(hass.config.path("rtsp_cameras", "actions.json"))
    actions.write_text(
        json.dumps(
            {
                "action": "install_addon_update",
                "requested_at": (dt_util.utcnow() - timedelta(hours=1)).isoformat(),
                "handled_at": None,
            }
        ),
        encoding="utf-8",
    )

    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert calls == []


async def test_home_assistant_publishes_the_addon_update_state(hass, entry):
    """The add-on learns about new versions from the update entity state."""
    import json
    from pathlib import Path

    write_cameras_file(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "update", "hassio", "rtsp_cameras", suggested_object_id="rtsp_camera_manager"
    )
    hass.states.async_set(
        "update.rtsp_camera_manager",
        "on",
        {"installed_version": "0.1.13", "latest_version": "0.1.14", "title": "RTSP Camera Manager"},
    )

    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    published = Path(hass.config.path("rtsp_cameras", "addon_update.json"))
    assert published.is_file()
    payload = json.loads(published.read_text(encoding="utf-8"))
    assert payload["installed_version"] == "0.1.13"
    assert payload["latest_version"] == "0.1.14"
    assert payload["update_available"] is True
    assert payload["entity_id"] == "update.rtsp_camera_manager"


async def test_addon_refresh_request_asks_home_assistant(hass, entry):
    """The check in the panel makes Home Assistant refresh the update entity."""
    import json
    from pathlib import Path

    from homeassistant.util import dt as dt_util

    write_cameras_file(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "update", "hassio", "rtsp_cameras", suggested_object_id="rtsp_camera_manager"
    )
    calls: list[dict] = []

    async def _update_entity(call):
        calls.append(dict(call.data))

    hass.services.async_register("homeassistant", "update_entity", _update_entity)

    actions = Path(hass.config.path("rtsp_cameras", "actions.json"))
    actions.write_text(
        json.dumps(
            {
                "action": "refresh_addon_update",
                "requested_at": dt_util.utcnow().isoformat(),
                "handled_at": None,
            }
        ),
        encoding="utf-8",
    )

    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert calls == [{"entity_id": "update.rtsp_camera_manager"}]


async def test_helper_file_problems_do_not_break_the_cameras(hass, entry):
    """A broken helper file must never take the camera entities down."""
    from pathlib import Path

    write_cameras_file(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.async_entity_ids("camera")

    # Make the action file unreadable/unwritable (a directory instead of a file).
    actions = Path(hass.config.path("rtsp_cameras", "actions.json"))
    actions.unlink(missing_ok=True)
    actions.mkdir(parents=True, exist_ok=True)

    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert entry.runtime_data.last_update_success is True
    assert sorted(hass.states.async_entity_ids("camera")) == [
        "camera.front_door",
        "camera.garage",
    ]


async def test_stream_source_follows_the_home_assistant_api(hass, entry):
    """Home Assistant awaits ``camera.stream_source()`` (2026.9 API).

    Exposing it as a property (as older versions did) makes Home Assistant fail
    while adding the entity: "TypeError: 'str' object is not callable".
    """
    from homeassistant.components.camera import async_get_stream_source

    write_cameras_file(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert (
        await async_get_stream_source(hass, "camera.front_door")
        == "rtsp://user:pass@10.0.0.5:554/stream1"
    )


async def test_camera_file_is_resolved_inside_the_config_folder(hass, entry):
    """The default path matches exactly what the add-on publishes."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.runtime_data.cameras_file == cameras_file_path(hass)


async def test_home_assistant_stream_url_wins(hass, entry):
    """An H.264 sub stream published by the add-on is used for the stream."""
    cameras = [dict(CAMERAS[0]), dict(CAMERAS[1])]
    cameras[0]["stream_url"] = "rtsp://10.0.0.5:554/Streaming/Channels/102"
    write_cameras_file(hass, cameras)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    camera = hass.data["camera"].get_entity("camera.front_door")
    assert camera is not None
    assert await camera.stream_source() == "rtsp://10.0.0.5:554/Streaming/Channels/102"
    assert camera.extra_state_attributes["stream_url"].startswith("rtsp://")


async def test_codec_is_exposed_and_h265_is_flagged(hass, entry, caplog):
    """The published codec lands in the attributes and warns about H.265."""
    cameras = [dict(CAMERAS[0])]
    cameras[0]["codec"] = "hevc"
    write_cameras_file(hass, cameras)

    with caplog.at_level("WARNING"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("camera.front_door")
    assert state is not None
    assert state.attributes["codec"] == "hevc"
    assert any("H.265" in record.message for record in caplog.records)


async def test_diagnostics_masks_credentials(hass, entry):
    """The diagnostics download must not leak the camera password."""
    from custom_components.rtsp_cameras.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    write_cameras_file(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["cameras_file"]["exists"] is True
    assert "camera.front_door" in diagnostics["entities"]
    url = diagnostics["cameras"][0]["url"]
    assert "pass" not in url and "***" in url
