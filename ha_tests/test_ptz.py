"""Offline tests for the PTZ services and buttons of the integration.

The add-on publishes ready to use HTTP commands; Home Assistant fills in the values
of the moment and sends them with its shared aiohttp session. The tests replace that
session, so no sockets are used (the CI harness blocks them).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtsp_cameras import ptz as ptz_module
from custom_components.rtsp_cameras.const import (
    CONF_CAMERAS_FILE,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)

CAMERAS_FILE = "rtsp_cameras/cameras.json"


class FakeResponse:
    """Stand-in for an aiohttp response of the camera."""

    def __init__(self, status: int = 200) -> None:
        self.status = status

    async def __aenter__(self) -> FakeResponse:
        """Enter the response context."""
        return self

    async def __aexit__(self, *args: object) -> bool:
        """Leave the response context."""
        return False


class FakeSession:
    """Record the PTZ requests Home Assistant would send."""

    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.requests: list[dict[str, Any]] = []

    def request(
        self, method: str, url: str, data: bytes | None = None, headers: dict[str, str] | None = None
    ) -> FakeResponse:
        """Record one command."""
        self.requests.append(
            {
                "method": method,
                "url": url,
                "body": data.decode("utf-8") if data else "",
                "headers": headers or {},
            }
        )
        if self.error is not None:
            raise self.error
        return FakeResponse(self.status)


def use_session(monkeypatch: pytest.MonkeyPatch, session: FakeSession) -> None:
    """Make the integration use the fake session."""
    monkeypatch.setattr(ptz_module, "async_get_clientsession", lambda hass: session)


def ptz_block(**overrides: Any) -> dict[str, Any]:
    """Return the PTZ block the add-on would publish for a Dahua camera."""
    block: dict[str, Any] = {
        "enabled": True,
        "profile": "dahua",
        "speed": 4,
        "commands": {
            "left": "GET http://10.0.0.5/cgi-bin/ptz.cgi?action=start&code=Left&arg2={speed}",
            "right": "GET http://10.0.0.5/cgi-bin/ptz.cgi?action=start&code=Right&arg2={speed}",
            "up": "GET http://10.0.0.5/cgi-bin/ptz.cgi?action=start&code=Up&arg2={speed}",
            "stop": "GET http://10.0.0.5/cgi-bin/ptz.cgi?action=stop&code={direction}",
            "home": "GET http://10.0.0.5/cgi-bin/ptz.cgi?action=start&code=Home",
            "preset": "GET http://10.0.0.5/cgi-bin/ptz.cgi?action=start&code=GotoPreset&arg2={preset}",
        },
        "stop_codes": {"left": "Left", "right": "Right", "up": "Up", "down": "Down"},
        "presets": [{"id": "1", "name": "Door"}, {"id": "2", "name": "Gate"}],
    }
    block.update(overrides)
    return block


def write_cameras_file(hass: HomeAssistant, camera: dict[str, Any]) -> None:
    """Publish one camera the way the add-on does."""
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


async def setup_camera(
    hass: HomeAssistant, entry: MockConfigEntry, **overrides: Any
) -> Any:
    """Set the integration up with one camera and return its entity object."""
    write_cameras_file(
        hass,
        {
            "id": "front_door",
            "name": "Front Door",
            "url": "rtsp://10.0.0.5:554/stream1",
            "rtsp_transport": "tcp",
            "enabled": True,
            "ptz": ptz_block(**overrides),
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    camera = hass.data["camera"].get_entity("camera.front_door")
    assert camera is not None
    return camera


async def test_ptz_fields_are_exposed(hass, entry):
    """The entity tells dashboards that it can be moved."""
    await setup_camera(hass, entry)

    state = hass.states.get("camera.front_door")
    assert state is not None
    assert state.attributes["ptz"] is True
    assert state.attributes["ptz_presets"] == ["Door", "Gate"]


async def test_service_moves_and_stops_the_camera(hass, entry, monkeypatch):
    """pan: LEFT moves left and stops it after the duration."""
    await setup_camera(hass, entry)
    session = FakeSession()
    use_session(monkeypatch, session)

    await hass.services.async_call(
        DOMAIN,
        "ptz",
        {"entity_id": "camera.front_door", "pan": "LEFT"},
        blocking=True,
    )

    assert len(session.requests) == 2, "the move and its stop"
    assert "code=Left" in session.requests[0]["url"]
    assert "action=stop" in session.requests[1]["url"]
    assert "code=Left" in session.requests[1]["url"], "the stop tells the direction"


async def test_service_uses_the_speed_and_the_onvif_duration(hass, entry, monkeypatch):
    """speed 1 means the maximum of the camera scale."""
    await setup_camera(hass, entry)
    session = FakeSession()
    use_session(monkeypatch, session)

    await hass.services.async_call(
        DOMAIN,
        "ptz",
        {"entity_id": "camera.front_door", "tilt": "UP", "speed": 1, "continuous_duration": 0.2},
        blocking=True,
    )

    assert "code=Up" in session.requests[0]["url"]
    assert "arg2=8" in session.requests[0]["url"]


async def test_service_move_mode_stop_does_not_move(hass, entry, monkeypatch):
    """move_mode: Stop only stops the camera."""
    await setup_camera(hass, entry)
    session = FakeSession()
    use_session(monkeypatch, session)

    await hass.services.async_call(
        DOMAIN,
        "ptz",
        {"entity_id": "camera.front_door", "move_mode": "Stop"},
        blocking=True,
    )

    assert len(session.requests) == 1
    assert "action=stop" in session.requests[0]["url"]


async def test_service_goes_to_a_preset(hass, entry, monkeypatch):
    """A preset call sends one command."""
    await setup_camera(hass, entry)
    session = FakeSession()
    use_session(monkeypatch, session)

    await hass.services.async_call(
        DOMAIN,
        "ptz",
        {"entity_id": "camera.front_door", "move_mode": "GotoPreset", "preset": "2"},
        blocking=True,
    )

    assert len(session.requests) == 1
    assert "code=GotoPreset" in session.requests[0]["url"]
    assert "arg2=2" in session.requests[0]["url"]


async def test_service_home_position(hass, entry, monkeypatch):
    """The second service only goes home."""
    await setup_camera(hass, entry)
    session = FakeSession()
    use_session(monkeypatch, session)

    await hass.services.async_call(
        DOMAIN, "ptz_home", {"entity_id": "camera.front_door"}, blocking=True
    )

    assert len(session.requests) == 1
    assert "code=Home" in session.requests[0]["url"]



async def test_service_without_direction_is_an_error(hass, entry):
    """A call that says nothing is reported instead of doing something random."""
    await setup_camera(hass, entry)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "ptz", {"entity_id": "camera.front_door"}, blocking=True
        )


async def test_service_reports_a_camera_without_ptz(hass, entry, monkeypatch):
    """A camera without PTZ says so clearly."""
    write_cameras_file(
        hass,
        {
            "id": "front_door",
            "name": "Front Door",
            "url": "rtsp://10.0.0.5:554/stream1",
            "enabled": True,
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    session = FakeSession()
    use_session(monkeypatch, session)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "ptz", {"entity_id": "camera.front_door", "action": "left"}, blocking=True
        )

    assert session.requests == []


async def test_service_reports_a_broken_camera(hass, entry, monkeypatch):
    """A camera answering with an error does not look like success."""
    await setup_camera(hass, entry)
    use_session(monkeypatch, FakeSession(status=500))

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "ptz", {"entity_id": "camera.front_door", "action": "stop"}, blocking=True
        )


async def test_preset_buttons_are_created(hass, entry, monkeypatch):
    """Each preset and the stop become a button on the camera device."""
    await setup_camera(hass, entry)
    session = FakeSession()
    use_session(monkeypatch, session)

    states = {state.entity_id: state for state in hass.states.async_all("button")}
    assert set(states) == {
        "button.front_door_ptz_door",
        "button.front_door_ptz_gate",
        "button.front_door_ptz_stop",
    }
    assert states["button.front_door_ptz_door"].name == "Front Door PTZ Door"

    await hass.services.async_call(
        "button", "press", {"entity_id": "button.front_door_ptz_gate"}, blocking=True
    )

    assert len(session.requests) == 1
    assert "arg2=2" in session.requests[0]["url"]


async def test_hikvision_style_commands_send_a_body(hass, entry, monkeypatch):
    """PUT commands keep their body and content type."""
    await setup_camera(
        hass,
        entry,
        commands={
            "right": 'PUT http://10.0.0.5/ISAPI/PTZCtrl/channels/1/continuous <?xml '
            'version="1.0"?><PTZData><pan>60</pan></PTZData>',
        },
        stop_codes={},
    )
    session = FakeSession()
    use_session(monkeypatch, session)

    await hass.services.async_call(
        DOMAIN, "ptz", {"entity_id": "camera.front_door", "action": "right"}, blocking=True
    )

    assert session.requests[0]["method"] == "PUT"
    assert "<pan>60</pan>" in session.requests[0]["body"]
    assert session.requests[0]["headers"]["Content-Type"] == "application/xml"
