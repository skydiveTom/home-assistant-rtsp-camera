"""Tests for the PTZ support of the add-on.

The vendor templates are pure functions, so most of them are checked without any
network. The endpoint tests talk to a tiny HTTP server that stands in for the PTZ
interface of a camera and records what it received.
"""

from __future__ import annotations

import asyncio
import json
import socketserver
import struct
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.dvrip import (
    HEADER_FORMAT,
    HEADER_SIZE,
    LOGIN_OK,
    MSG_LOGIN_RESPONSE,
    MSG_PTZ_REQUEST,
    MSG_PTZ_RESPONSE,
    hash_password,
)
from app.ptz import (
    PTZ_PROFILES,
    async_send,
    build_command,
    configured_actions,
    normalize_ptz,
    public_config,
)
from app.ptz import (
    async_discover_onvif_token as discover_onvif_token,
)
from tests.helpers import add_camera

RTSP_URL = "rtsp://192.168.1.28:554/user=admin&password=&channel=1&stream=0.sdp"


class RecordingHandler(BaseHTTPRequestHandler):
    """Record every request and answer like a camera would."""

    received: list[dict[str, Any]] = []
    reply_body: bytes = b"ok"

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        """Record a GET request."""
        self._record()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        """Record a POST request."""
        self._record()

    def do_PUT(self) -> None:  # noqa: N802 - http.server API
        """Record a PUT request."""
        self._record()

    def _record(self) -> None:
        """Store method, path, query and body, then answer 200."""
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        parts = urlsplit(self.path)
        RecordingHandler.received.append(
            {
                "method": self.command,
                "path": parts.path,
                "query": {key: value[0] for key, value in parse_qs(parts.query).items()},
                "body": body,
                "content_type": self.headers.get("Content-Type"),
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(RecordingHandler.reply_body)

    def log_message(self, *args: Any) -> None:
        """Keep the test output clean."""


@pytest.fixture(name="ptz_cam")
def ptz_cam_fixture() -> Iterator[str]:
    """Run a fake camera HTTP server and yield its base URL."""
    RecordingHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

def test_profile_of_a_dvr_derives_the_base_url() -> None:
    """The Xiongmai profile fills the host from the RTSP URL."""
    config = normalize_ptz({"profile": "xiongmai"}, RTSP_URL)

    assert config is not None
    assert config["base_url"] == "http://192.168.1.28"
    assert config["channel"] == 1
    assert config["commands"]["left"].startswith("GET http://192.168.1.28/cgi-bin/ptz.cgi?")
    assert "code=DirectionLeft" in config["commands"]["left"]
    assert "{speed}" in config["commands"]["left"], "the speed stays dynamic"


def test_credentials_are_encoded_and_hidden() -> None:
    """A password with spaces must not break the URL, and the UI gets a mask."""
    config = normalize_ptz(
        {"profile": "foscam", "username": "admin", "password": "p w:1"},
        "rtsp://10.0.0.4:554/stream",
    )

    assert config is not None
    assert "usr=admin" in config["commands"]["up"]
    assert "pwd=p%20w%3A1" in config["commands"]["up"]
    masked = public_config(config)
    assert masked is not None
    assert "pwd=***" in masked["commands"]["up"]
    assert "p w" not in json.dumps(masked)


def test_custom_commands_win_over_the_profile() -> None:
    """An own command replaces the vendor preset for that action."""
    config = normalize_ptz(
        {
            "profile": "dahua",
            "commands": {"left": 'POST {base}/my/ptz {"dir":"left","s":{speed}}'},
            "base_url": "http://camera:8080",
        },
        RTSP_URL,
    )

    assert config is not None
    assert config["commands"]["left"] == 'POST http://camera:8080/my/ptz {"dir":"left","s":{speed}}'
    assert "code=Right" in config["commands"]["right"]


def test_commands_with_an_unknown_method_are_ignored() -> None:
    """Only GET/POST/PUT commands are accepted."""
    config = normalize_ptz(
        {"profile": "custom", "commands": {"left": "DELETE http://x/y", "up": "nonsense"}},
        RTSP_URL,
    )

    assert config is None, "nothing usable is configured"


def test_switched_off_ptz_is_not_published() -> None:
    """enabled=false removes the whole block."""
    assert normalize_ptz({"enabled": False, "profile": "dahua"}, RTSP_URL) is None
    assert normalize_ptz(None, RTSP_URL) is None


def test_stop_uses_the_direction_code_of_the_vendor() -> None:
    """Vendors want the direction on the stop command as well."""
    config = normalize_ptz({"profile": "xiongmai", "speed": 7}, RTSP_URL)

    assert config is not None
    command = build_command(config, "stop", direction="left")
    assert command is not None
    assert "action=stop" in command
    assert "code=DirectionLeft" in command


def test_speed_and_preset_are_rendered() -> None:
    """The speed can be overridden per call and presets are filled in."""
    config = normalize_ptz({"profile": "dahua", "speed": 3}, RTSP_URL)

    assert config is not None
    assert "arg2=8" in build_command(config, "up", speed=8)
    assert "arg2=4" in build_command(config, "preset", preset="4")


def test_credentials_and_channel_come_from_the_stream_url() -> None:
    """The login of the RTSP URL is reused, so it is not typed twice."""
    config = normalize_ptz({"profile": "dahua"}, RTSP_URL)

    assert config is not None
    assert config["username"] == "admin"
    assert config["password"] == ""
    assert config["channel"] == 1

    classic = normalize_ptz({"profile": "dahua"}, "rtsp://user:p%20w@10.0.0.4:554/s1?channel=2")
    assert classic is not None
    assert classic["username"] == "user"
    assert classic["password"] == "p w"
    assert classic["channel"] == 2

    foscam = normalize_ptz({"profile": "foscam"}, "rtsp://user:p%20w@10.0.0.4:554/s1")
    assert foscam is not None
    assert "usr=user&pwd=p%20w" in foscam["commands"]["up"]


def test_explicit_ptz_credentials_win_over_the_stream_url() -> None:
    """A user can still give different credentials for PTZ."""
    config = normalize_ptz(
        {"profile": "dahua", "username": "ptzuser", "password": "secret"}, RTSP_URL
    )

    assert config is not None
    assert config["username"] == "ptzuser"
    masked = public_config(config)
    assert masked is not None and masked["has_credentials"] is True
    assert "password" not in masked, "the API never hands out the password"

    foscam = normalize_ptz({"profile": "foscam", "password": "top secret"}, RTSP_URL)
    assert foscam is not None
    assert "pwd=top%20secret" in foscam["commands"]["up"]


def test_dvrip_profile_builds_the_payloads() -> None:
    """The DVRIP profile speaks the Xiongmai protocol on port 34567."""
    config = normalize_ptz({"profile": "xiongmai_dvrip", "speed": 5}, RTSP_URL)

    assert config is not None
    assert config["port"] == 34567
    assert build_command(config, "left") == (
        'DVRIP {"Command":"DirectionLeft","Step":5,"Channel":1}'
    )
    assert build_command(config, "left", speed=7) == (
        'DVRIP {"Command":"DirectionLeft","Step":7,"Channel":1}'
    )
    assert build_command(config, "stop", direction="left") == (
        'DVRIP {"Command":"DirectionLeft","Step":0,"Channel":1}'
    )
    assert build_command(config, "preset", preset="3") == (
        'DVRIP {"Command":"GotoPreset","Preset":3,"Channel":1}'
    )


def test_onvif_profile_builds_soap_commands() -> None:
    """The ONVIF profile posts SOAP to the PTZ service with the profile token."""
    config = normalize_ptz({"profile": "onvif", "token": "Profile_1"}, RTSP_URL)

    assert config is not None
    right = build_command(config, "right")
    assert right is not None
    assert right.startswith("POST http://192.168.1.28/onvif/ptz_service ")
    assert "<s:Envelope" in right
    assert "<tptz:ProfileToken>Profile_1</tptz:ProfileToken>" in right
    assert '<tptz:PanTilt x="0.5" y="0"/>' in right

    stop = build_command(config, "stop")
    assert stop is not None and "<tptz:Stop>" in stop
    preset = build_command(config, "preset", preset="4")
    assert preset is not None and "<tptz:PresetToken>4</tptz:PresetToken>" in preset
    home = build_command(config, "home")
    assert home is not None and "GotoHomePosition" in home


def test_every_profile_is_offered_and_usable() -> None:
    """Each vendor preset has at least a stop command and a description."""
    for key, profile in PTZ_PROFILES.items():
        assert profile["label"], key
        assert profile["description"], key
        if key == "custom":
            continue
        config = normalize_ptz({"profile": key, "token": "t"}, RTSP_URL)
        assert config is not None, key
        assert "stop" in config["commands"], key

    """The panel needs a fixed action order and every profile."""
    config = normalize_ptz({"profile": "hikvision"}, RTSP_URL)

    assert config is not None
    assert configured_actions(config) == [
        "up",
        "down",
        "left",
        "right",
        "zoom_in",
        "zoom_out",
        "home",
        "stop",
        "preset",
    ]
    assert set(PTZ_PROFILES) >= {"custom", "axis", "dahua", "hikvision", "foscam", "xiongmai"}


def test_hikvision_uses_a_body_and_a_preset_endpoint() -> None:
    """Hikvision needs PUT with an XML body."""
    config = normalize_ptz({"profile": "hikvision", "channel": 2}, RTSP_URL)

    assert config is not None
    command = build_command(config, "right")
    assert command is not None
    assert command.startswith("PUT http://192.168.1.28/ISAPI/PTZCtrl/channels/2/continuous ")
    assert "<pan>60</pan>" in command
    assert build_command(config, "preset", preset="3") == (
        "PUT http://192.168.1.28/ISAPI/PTZCtrl/channels/2/presets/3/goto"
    )


def received() -> list[dict[str, Any]]:
    """Return the requests the fake camera received."""
    return RecordingHandler.received


@pytest.fixture(name="soap_cam")
def soap_cam_fixture() -> Iterator[str]:
    """Run a fake ONVIF device that answers GetProfiles with a profile token."""
    RecordingHandler.reply_body = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
        b'<trt:GetProfilesResponse xmlns:trt="http://www.onvif.org/ver10/media/wsdl">'
        b"<trt:Profiles token=\"P1\"><trt:Name>mainStream</trt:Name></trt:Profiles>"
        b"<trt:ProfileToken>Profile_1</trt:ProfileToken>"
        b"</trt:GetProfilesResponse></s:Body></s:Envelope>"
    )
    RecordingHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        RecordingHandler.reply_body = b"ok"
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_onvif_token_is_discovered(soap_cam: str) -> None:
    """GetProfiles is posted to the media service and the token is returned."""
    token, error = asyncio.run(discover_onvif_token(soap_cam, "admin", "secret"))

    assert error is None, error
    assert token == "Profile_1"

    request = received()[0]
    assert request["method"] == "POST"
    assert request["path"] in ("/onvif/media_service", "/onvif/device_service", "/onvif/Media")
    assert "GetProfiles" in request["body"]
    assert "soap" in request["content_type"]


def test_onvif_discovery_reports_a_reply_without_token(ptz_cam: str) -> None:
    """A device answering without a token is reported as such."""
    token, error = asyncio.run(discover_onvif_token(ptz_cam))

    assert token is None
    assert error == "no_token_in_reply"


def test_onvif_discovery_reports_a_dead_host() -> None:
    """A base URL that does not answer is reported."""
    token, error = asyncio.run(discover_onvif_token("http://127.0.0.1:9", timeout=2))

    assert token is None
    assert error

def test_ptz_endpoint_sends_the_command(client: TestClient, ptz_cam: str) -> None:
    """The panel moves the camera through the API."""
    camera = add_camera(
        client,
        url=RTSP_URL,
        rtsp_transport="tcp",
        ptz={"profile": "xiongmai", "base_url": ptz_cam, "speed": 6},
    )

    response = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "left"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ptz"]["ok"] is True
    assert payload["ptz"]["status"] == 200

    assert len(received()) == 1
    request = received()[0]
    assert request["method"] == "GET"
    assert request["path"] == "/cgi-bin/ptz.cgi"
    assert request["query"]["action"] == "start"
    assert request["query"]["code"] == "DirectionLeft"
    assert request["query"]["arg2"] == "6"


def test_ptz_endpoint_stops_with_the_moved_direction(client: TestClient, ptz_cam: str) -> None:
    """Stopping tells the camera which direction to halt."""
    camera = add_camera(client, url=RTSP_URL, ptz={"profile": "xiongmai", "base_url": ptz_cam})

    client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "right"})
    response = client.post(
        f"/api/cameras/{camera['id']}/ptz",
        json={"action": "stop", "direction": "right"},
    )

    assert response.status_code == 200, response.text
    stop = received()[-1]
    assert stop["query"]["action"] == "stop"
    assert stop["query"]["code"] == "DirectionRight"


def test_ptz_endpoint_sends_a_body(client: TestClient, ptz_cam: str) -> None:
    """Hikvision style commands are PUT requests with an XML body."""
    camera = add_camera(
        client,
        url=RTSP_URL,
        ptz={"profile": "hikvision", "base_url": ptz_cam},
    )

    response = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "up"})

    assert response.status_code == 200, response.text
    request = received()[0]
    assert request["method"] == "PUT"
    assert request["path"] == "/ISAPI/PTZCtrl/channels/1/continuous"
    assert "<tilt>60</tilt>" in request["body"]
    assert request["content_type"] == "application/xml"


def test_ptz_endpoint_goes_to_a_preset(client: TestClient, ptz_cam: str) -> None:
    """Presets are sent with the number the panel picked."""
    camera = add_camera(
        client,
        url=RTSP_URL,
        ptz={"profile": "dahua", "base_url": ptz_cam, "presets": ["1=Drzwi", "2=Brama"]},
    )

    response = client.post(
        f"/api/cameras/{camera['id']}/ptz", json={"action": "preset", "preset": "2"}
    )

    assert response.status_code == 200, response.text
    assert received()[0]["query"]["code"] == "GotoPreset"
    assert received()[0]["query"]["arg2"] == "2"
    assert response.json()["camera"]["ptz"]["presets"] == [
        {"id": "1", "name": "Drzwi"},
        {"id": "2", "name": "Brama"},
    ]


def test_ptz_endpoint_reports_cameras_without_ptz(client: TestClient) -> None:
    """A camera without PTZ configuration answers with a clear error."""
    camera = add_camera(client, url=RTSP_URL)

    response = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "left"})

    assert response.status_code == 400
    assert response.json()["error"] == "ptz_not_configured"


def test_ptz_endpoint_rejects_unknown_actions(client: TestClient, ptz_cam: str) -> None:
    """Only known actions are executed."""
    camera = add_camera(client, url=RTSP_URL, ptz={"profile": "dahua", "base_url": ptz_cam})

    response = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "teleport"})

    assert response.status_code == 400
    assert response.json()["error"] == "ptz_action_required"
    assert received() == []


def test_ptz_endpoint_reports_a_broken_camera(client: TestClient) -> None:
    """An unreachable PTZ interface does not fake success."""
    camera = add_camera(
        client,
        url=RTSP_URL,
        ptz={"profile": "dahua", "base_url": "http://127.0.0.1:9"},
    )

    response = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "left"})

    assert response.status_code == 502
    assert response.json()["error"] == "ptz_failed"
    assert response.json()["detail"]


def test_ptz_profiles_are_published(client: TestClient) -> None:
    """The editor loads the vendor presets from the add-on."""
    response = client.get("/api/ptz/profiles")

    assert response.status_code == 200
    profiles = {item["id"]: item for item in response.json()["profiles"]}
    assert "custom" in profiles
    assert "code=DirectionLeft" in profiles["xiongmai"]["commands"]["left"]
    assert profiles["hikvision"]["commands"]["up"].startswith("PUT ")


def test_ptz_is_published_to_home_assistant(
    client: TestClient, settings: Settings, ptz_cam: str
) -> None:
    """The integration receives ready to use commands."""
    add_camera(client, url=RTSP_URL, ptz={"profile": "xiongmai", "base_url": ptz_cam})

    payload = json.loads(Path(settings.published_file).read_text(encoding="utf-8"))
    ptz = payload["cameras"][0]["ptz"]

    assert ptz["enabled"] is True
    assert ptz["profile"] == "xiongmai"
    assert ptz["stop_codes"]["left"] == "DirectionLeft"
    assert ptz["commands"]["left"].startswith(f"GET {ptz_cam}/cgi-bin/ptz.cgi?")

class DvripCamera(socketserver.ThreadingTCPServer):
    """Fake Xiongmai device that answers the DVRIP login and PTZ requests."""

    allow_reuse_address = True
    received: list[dict[str, Any]] = []
    login: dict[str, Any] = {}
    fail_login = False

    def __init__(self) -> None:
        """Bind to a free port on localhost."""
        super().__init__(("127.0.0.1", 0), DvripHandler)
        self.port = self.server_address[1]


class DvripHandler(socketserver.BaseRequestHandler):
    """Handle one connection: login, then the PTZ command."""

    def handle(self) -> None:
        """Answer the two messages the client sends."""
        _message_id, payload = read_message(self.request)
        DvripCamera.login = payload
        if DvripCamera.fail_login:
            self.request.sendall(reply(MSG_LOGIN_RESPONSE, {"Ret": 101}))
            return
        self.request.sendall(reply(MSG_LOGIN_RESPONSE, {"Ret": 100, "SessionID": 1234}))

        message_id, payload = read_message(self.request)
        DvripCamera.received.append({"message_id": message_id, "payload": payload})
        self.request.sendall(reply(MSG_PTZ_RESPONSE, {"Ret": 100}))


def read_message(sock: Any) -> tuple[int, dict[str, Any]]:
    """Read a complete DVRIP message from a socket."""
    header = b""
    while len(header) < HEADER_SIZE:
        chunk = sock.recv(HEADER_SIZE - len(header))
        if not chunk:
            raise AssertionError("connection closed")
        header += chunk
    _, _, _, _, _session, _, total, _current, message_id = struct.unpack(HEADER_FORMAT, header)
    body = b""
    while len(body) < total:
        chunk = sock.recv(total - len(body))
        if not chunk:
            raise AssertionError("connection closed")
        body += chunk
    return message_id, json.loads(body.decode("utf-8") or "{}")


def reply(message_id: int, payload: dict[str, Any]) -> bytes:
    """Build a DVRIP reply."""
    body = json.dumps(payload).encode("utf-8")
    return struct.pack(HEADER_FORMAT, 0xFF, 0, 0, 0, 1234, 1, len(body), 0, message_id) + body


@pytest.fixture(name="dvrip_cam")
def dvrip_cam_fixture() -> Iterator[int]:
    """Run a fake DVRIP device and yield its port."""
    DvripCamera.received = []
    DvripCamera.login = {}
    DvripCamera.fail_login = False
    server = DvripCamera()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dvrip_login_and_ptz_reach_the_dvr(dvrip_cam: int) -> None:
    """The add-on logs in with the RTSP credentials and sends the command."""
    url = "rtsp://admin:secret@127.0.0.1:554/user=admin&password=secret&channel=1&stream=0.sdp"
    config = normalize_ptz({"profile": "xiongmai_dvrip", "port": dvrip_cam}, url)
    assert config is not None

    command = build_command(config, "left", speed=6)
    assert command is not None
    host = urlsplit(config["base_url"]).hostname
    status, error = asyncio.run(
        async_send(
            command,
            host=host or "",
            port=dvrip_cam,
            username=str(config["username"]),
            password=str(config["password"]),
        )
    )

    assert error is None, error
    assert status == LOGIN_OK
    assert DvripCamera.login["UserName"] == "admin"
    assert DvripCamera.login["PassWord"] == hash_password("admin", "secret")

    assert len(DvripCamera.received) == 1
    sent = DvripCamera.received[0]
    assert sent["message_id"] == MSG_PTZ_REQUEST
    assert sent["payload"]["Name"] == "OPPTZControl"
    assert sent["payload"]["PTZControl"]["Command"] == "DirectionLeft"
    assert sent["payload"]["PTZControl"]["Parameter"]["Step"] == 6
    assert sent["payload"]["PTZControl"]["Parameter"]["Channel"] == 0
    assert sent["payload"]["SessionID"] == 1234


def test_dvrip_reports_a_refused_login(dvrip_cam: int) -> None:
    """Wrong credentials are reported, not silently ignored."""
    DvripCamera.fail_login = True
    config = normalize_ptz({"profile": "xiongmai_dvrip", "port": dvrip_cam}, RTSP_URL)
    assert config is not None
    command = build_command(config, "stop", direction="left")
    assert command is not None

    status, error = asyncio.run(
        async_send(command, host="127.0.0.1", port=dvrip_cam, username="admin", password="bad")
    )

    assert status is None
    assert error == "login_failed_101"

