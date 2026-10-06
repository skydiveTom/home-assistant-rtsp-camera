"""Offline tests for the PTZ services and buttons of the integration.

The add-on publishes ready to use HTTP commands; Home Assistant fills in the values
of the moment and sends them with its shared aiohttp session. The tests replace that
session, so no sockets are used (the CI harness blocks them).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
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
    DVRIP_TIMEOUT_SECONDS,
    PTZ_TIMEOUT_SECONDS,
)

CAMERAS_FILE = "rtsp_cameras/cameras.json"


class FakeResponse:
    """Stand-in for an aiohttp response of the camera."""

    def __init__(self, status: int = 200, body: str = "") -> None:
        self.status = status
        self.body = body

    async def text(self) -> str:
        """Return what the camera answered in the body."""
        return self.body

    async def __aenter__(self) -> FakeResponse:
        """Enter the response context."""
        return self

    async def __aexit__(self, *args: object) -> bool:
        """Leave the response context."""
        return False


class FakeSession:
    """Record the PTZ requests Home Assistant would send."""

    def __init__(self, status: int = 200, error: Exception | None = None, body: str = "") -> None:
        self.status = status
        self.error = error
        self.body = body
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
        return FakeResponse(self.status, self.body)


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


@pytest.fixture(autouse=True)
def clean_axis_memory() -> Iterator[None]:
    """Start every test without a remembered axis.

    The integration keeps the axis of the last move per camera, so a stop without a
    direction can name it. That memory lives in the module, which would let one test
    depend on the next.
    """
    ptz_module._LAST_DIRECTION.clear()
    yield
    ptz_module._LAST_DIRECTION.clear()


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
    # Which variant moves the camera: picked with the PTZ test mode of the add-on.
    assert state.attributes["ptz_profile"] == "dahua"


async def test_a_detected_variant_reaches_a_running_camera(hass, entry, monkeypatch):
    """A variant picked by the add-on takes over the commands of the entity.

    The PTZ test mode of the add-on republishes the camera file with the command set
    that answered; the entity of a running Home Assistant follows it without a
    restart - that is what "set as the main handling" means on this side.
    """
    camera = await setup_camera(hass, entry)
    session = FakeSession()
    use_session(monkeypatch, session)

    write_cameras_file(
        hass,
        {
            "id": "front_door",
            "name": "Front Door",
            "url": "rtsp://10.0.0.5:554/stream1",
            "rtsp_transport": "tcp",
            "enabled": True,
            "ptz": ptz_block(
                profile="onvif",
                commands={
                    "left": "POST http://10.0.0.5/onvif/ptz_service <tptz:ContinuousMove/>"
                },
                stop_codes={},
            ),
        },
    )
    await camera.coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get("camera.front_door")
    assert state is not None
    assert state.attributes["ptz_profile"] == "onvif"

    await hass.services.async_call(
        DOMAIN,
        "ptz",
        {"entity_id": "camera.front_door", "action": "left"},
        blocking=True,
    )
    assert session.requests[-1]["method"] == "POST"
    assert session.requests[-1]["url"] == "http://10.0.0.5/onvif/ptz_service"


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
    """move_mode: Stop stops what was moved and never sends a move of its own."""
    await setup_camera(hass, entry)
    session = FakeSession()
    use_session(monkeypatch, session)

    await hass.services.async_call(
        DOMAIN,
        "ptz",
        {"entity_id": "camera.front_door", "pan": "LEFT", "continuous_duration": 0},
        blocking=True,
    )
    session.requests.clear()

    await hass.services.async_call(
        DOMAIN,
        "ptz",
        {"entity_id": "camera.front_door", "move_mode": "Stop"},
        blocking=True,
    )

    assert len(session.requests) == 1
    assert "action=stop" in session.requests[0]["url"]
    assert "code=Left" in session.requests[0]["url"], "the axis that was moved"


async def test_a_stop_without_a_known_axis_is_refused(hass, entry, monkeypatch):
    """A stop that has no axis to name is refused instead of answered with a guess.

    Nothing was moved (and Home Assistant was restarted, in the worst case), so the
    direction of the stop is unknown. Filling in one - what the integration did until
    0.3.6 - does not stop a camera, it moves it: on a Xiongmai device every direction is
    a zero position and a stop that says "up" drives the camera to its top.
    """
    await setup_camera(hass, entry)
    session = FakeSession()
    use_session(monkeypatch, session)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "ptz",
            {"entity_id": "camera.front_door", "move_mode": "Stop"},
            blocking=True,
        )

    assert session.requests == [], "no command is sent, not even a wrong one"


async def test_the_stop_button_stops_the_axis_that_moved(hass, entry, monkeypatch):
    """The PTZ stop button of a dashboard names the axis of the last move."""
    await setup_camera(hass, entry)
    session = FakeSession()
    use_session(monkeypatch, session)

    await hass.services.async_call(
        DOMAIN,
        "ptz",
        {"entity_id": "camera.front_door", "tilt": "UP", "continuous_duration": 0},
        blocking=True,
    )
    session.requests.clear()

    await hass.services.async_call(
        "button", "press", {"entity_id": "button.front_door_ptz_stop"}, blocking=True
    )

    assert len(session.requests) == 1, "a stop and nothing else"
    assert "action=stop" in session.requests[0]["url"]
    assert "code=Up" in session.requests[0]["url"]


async def test_the_speed_of_each_axis_reaches_the_command(hass, entry, monkeypatch):
    """Both arrows can be tuned apart, and a speed of a call counts for both axes."""
    await setup_camera(
        hass,
        entry,
        speed=4,
        speed_vertical=2,
        speed_horizontal=7,
        commands={
            "up": "GET http://10.0.0.5/cgi-bin/ptz.cgi?action=start&code=Up&arg2={speed_vertical}",
            "left": "GET http://10.0.0.5/cgi-bin/ptz.cgi?action=start&code=Left"
            "&arg2={speed_horizontal}",
            "stop": "GET http://10.0.0.5/cgi-bin/ptz.cgi?action=stop&code={direction}",
        },
        stop_codes={"up": "Up", "left": "Left"},
    )
    session = FakeSession()
    use_session(monkeypatch, session)

    await hass.services.async_call(
        DOMAIN,
        "ptz",
        {"entity_id": "camera.front_door", "tilt": "UP", "continuous_duration": 0},
        blocking=True,
    )
    assert "arg2=2" in session.requests[0]["url"], "the tilt reads the vertical speed"

    session.requests.clear()
    await hass.services.async_call(
        DOMAIN,
        "ptz",
        {"entity_id": "camera.front_door", "pan": "LEFT", "continuous_duration": 0},
        blocking=True,
    )
    assert "arg2=7" in session.requests[0]["url"], "the pan reads the horizontal speed"

    session.requests.clear()
    await hass.services.async_call(
        DOMAIN,
        "ptz",
        {
            "entity_id": "camera.front_door",
            "pan": "LEFT",
            "speed": 1,
            "continuous_duration": 0,
        },
        blocking=True,
    )
    assert "arg2=8" in session.requests[0]["url"], "a speed of the call wins over both axes"


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


async def test_service_reports_a_web_page_instead_of_a_command(hass, entry, monkeypatch):
    """A command URL answered with a web page has not reached a PTZ interface.

    Xiongmai devices answer every path of their port 80 with ``200 OK`` and the page of
    their web interface, which used to look like a command that worked.
    """
    await setup_camera(hass, entry)
    use_session(
        monkeypatch,
        FakeSession(body="<!DOCTYPE html><html><body>NETSurveillance WEB</body></html>"),
    )

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "ptz",
            {"entity_id": "camera.front_door", "action": "up"},
            blocking=True,
        )

    assert "camera answered: <!DOCTYPE html>" in str(err.value)


async def test_service_reports_a_broken_camera(hass, entry, monkeypatch):
    """A camera answering with an error does not look like success."""
    await setup_camera(hass, entry)
    use_session(monkeypatch, FakeSession(status=500))

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "ptz", {"entity_id": "camera.front_door", "action": "stop"}, blocking=True
        )


async def test_service_reports_a_camera_that_answers_error(hass, entry, monkeypatch):
    """A camera that answers "200 OK" and "Error" has not moved - it is an error.

    Dahua and Xiongmai answer a rejected command like that, which used to look like a
    service call that worked.
    """
    await setup_camera(hass, entry)
    use_session(monkeypatch, FakeSession(body="Error"))

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "ptz",
            {"entity_id": "camera.front_door", "action": "right"},
            blocking=True,
        )

    assert "camera answered: Error" in str(err.value)


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


DVRIP_COMMANDS: dict[str, str] = {
    # What the bundled profile publishes since 0.3.10: a move carries the direction and the
    # speed of its axis and nothing else, and the stop is a *capture* - it stores the
    # position it is sent at and returns to it. No payload of this family halts a running
    # axis (a ``Stop`` of any shape is acknowledged and the axis travels on, the ``POINT``
    # object of 0.3.7 is refused with ``Ret: 118``), so a command may carry two payloads
    # and the sender sends them in order.
    "left": 'DVRIP {"Command":"DirectionLeft","Step":{speed},"Channel":{channel}}',
    # The stop names no direction, which is what a dashboard needs after a restart of Home
    # Assistant, and it ends where the camera stands when it is sent.
    "stop": 'DVRIP {"Command":"SetPreset","Preset":201,"Channel":{channel}} '
    'DVRIP {"Command":"GotoPreset","Preset":201,"Channel":{channel}}',
    "preset": 'DVRIP {"Command":"GotoPreset","Preset":{preset},"Channel":{channel}}',
}


async def test_dvrip_commands_go_through_the_tcp_client(hass, entry, monkeypatch):
    """A DVRIP command is sent over TCP 34567, not over HTTP."""
    calls: list[dict[str, Any]] = []

    async def fake_send(host, port, username, password, short, timeout=None):  # noqa: ANN001
        calls.append(
            {
                "host": host,
                "port": port,
                "username": username,
                "password": password,
                "short": short,
            }
        )
        return True, None

    monkeypatch.setattr("custom_components.rtsp_cameras.dvrip.async_send", fake_send)
    await setup_camera(
        hass,
        entry,
        base_url="http://10.0.0.5",
        commands=DVRIP_COMMANDS,
        stop_codes={"left": "DirectionLeft"},
        port=34567,
        channel=1,
        username="admin",
        password="secret",
    )
    session = FakeSession()
    use_session(monkeypatch, session)

    await hass.services.async_call(
        DOMAIN,
        "ptz",
        {"entity_id": "camera.front_door", "action": "left", "speed": 0.75},
        blocking=True,
    )

    assert session.requests == [], "no HTTP request for a DVRIP camera"
    assert len(calls) == 3, "the move and the two payloads of the automatic stop"

    move = calls[0]
    assert move["host"] == "10.0.0.5"
    assert move["port"] == 34567
    assert move["username"] == "admin"
    assert move["password"] == "secret"
    assert move["short"]["Command"] == "DirectionLeft"
    assert move["short"]["Step"] == 6, "0.75 of the camera scale (1-8)"
    assert move["short"]["Channel"] == 1

    # The stop is a *capture*: it stores the position it is sent at - the position of the
    # release - and returns to it. A dashboard that moves and stops therefore decides the
    # distance with the pause between its two commands.
    store = calls[1]
    assert store["short"]["Command"] == "SetPreset", "the stop stores where the camera is"
    assert store["short"]["Preset"] == 201, "the slot the profile reserves for the stop"
    assert store["short"]["Channel"] == 1

    stop = calls[2]
    assert stop["short"]["Command"] == "GotoPreset", "the stop of the bundled profile"
    assert stop["short"]["Preset"] == 201, "back to the position of the release"
    assert stop["short"]["Channel"] == 1


async def test_dvrip_stop_needs_no_direction(hass, entry, monkeypatch):
    """A stop of the bundled profile works on its own - it names no direction.

    The stop is the only one that may be sent without knowing which axis is moving, which
    is what a dashboard needs after a restart of Home Assistant: it stores the position the
    camera has at that moment and returns to it, whatever the camera did before.
    """
    calls: list[dict[str, Any]] = []

    async def fake_send(host, port, username, password, short, timeout=None):  # noqa: ANN001
        calls.append(short)
        return True, None

    monkeypatch.setattr("custom_components.rtsp_cameras.dvrip.async_send", fake_send)
    await setup_camera(hass, entry, commands=DVRIP_COMMANDS, stop_codes={"left": "DirectionLeft"})

    await hass.services.async_call(
        DOMAIN,
        "ptz",
        {"entity_id": "camera.front_door", "action": "stop"},
        blocking=True,
    )

    assert len(calls) == 2, "the capture of the position and the return to it"
    assert calls[0]["Command"] == "SetPreset"
    assert calls[0]["Preset"] == 201
    assert calls[1]["Command"] == "GotoPreset"
    assert calls[1]["Preset"] == 201


async def test_dvrip_failure_is_reported(hass, entry, monkeypatch):
    """A refused DVRIP login raises a readable error."""

    async def fake_send(host, port, username, password, short, timeout=None):  # noqa: ANN001
        return False, "login_failed_101"

    monkeypatch.setattr("custom_components.rtsp_cameras.dvrip.async_send", fake_send)
    await setup_camera(hass, entry, commands=DVRIP_COMMANDS)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "ptz", {"entity_id": "camera.front_door", "action": "left"}, blocking=True
        )


async def test_dvrip_waits_longer_than_a_vendor_cgi(hass, entry, monkeypatch):
    """The DVRIP transport gets the long deadline of this family.

    The device of the test set answers the ``GotoPreset`` of the stop - which drags the
    camera back to the position it stored - only once the axis arrived: measured 10 to 16 s
    after the command. With the deadline of a vendor CGI that working command was reported
    as a failure and the camera stayed where it was.
    """
    timeouts: list[float | None] = []

    async def fake_send(host, port, username, password, short, timeout=None):  # noqa: ANN001
        timeouts.append(timeout)
        return True, None

    monkeypatch.setattr("custom_components.rtsp_cameras.dvrip.async_send", fake_send)
    await setup_camera(hass, entry, commands=DVRIP_COMMANDS, stop_codes={"left": "DirectionLeft"})

    await hass.services.async_call(
        DOMAIN, "ptz", {"entity_id": "camera.front_door", "action": "left"}, blocking=True
    )

    assert timeouts == [DVRIP_TIMEOUT_SECONDS] * 3, "the move and the two payloads of the stop"
    assert DVRIP_TIMEOUT_SECONDS > PTZ_TIMEOUT_SECONDS, "a CGI keeps its short deadline"


async def test_onvif_commands_use_soap(hass, entry, monkeypatch):
    """An ONVIF command is POSTed as SOAP with the profile token."""
    await setup_camera(
        hass,
        entry,
        base_url="http://10.0.0.5",
        commands={
            "right": 'POST http://10.0.0.5/onvif/ptz_service <s:Envelope '
            'xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
            "<tptz:ContinuousMove xmlns:tptz=\"http://www.onvif.org/ver20/ptz/wsdl\">"
            "<tptz:ProfileToken>Profile_1</tptz:ProfileToken></tptz:ContinuousMove>"
            "</s:Body></s:Envelope>",
            "preset": 'POST http://10.0.0.5/onvif/ptz_service <s:Envelope '
            'xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
            "<tptz:GotoPreset xmlns:tptz=\"http://www.onvif.org/ver20/ptz/wsdl\">"
            "<tptz:PresetToken>{preset}</tptz:PresetToken></tptz:GotoPreset>"
            "</s:Body></s:Envelope>",
        },
        stop_codes={},
    )
    session = FakeSession()
    use_session(monkeypatch, session)

    await hass.services.async_call(
        DOMAIN,
        "ptz",
        {"entity_id": "camera.front_door", "move_mode": "GotoPreset", "preset": "3"},
        blocking=True,
    )

    assert session.requests[0]["method"] == "POST"
    assert session.requests[0]["headers"]["Content-Type"].startswith("application/soap+xml")
    assert "<tptz:PresetToken>3</tptz:PresetToken>" in session.requests[0]["body"]
