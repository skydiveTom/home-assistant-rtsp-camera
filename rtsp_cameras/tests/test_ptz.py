"""Tests for the PTZ support of the add-on.

The vendor templates are pure functions, so most of them are checked without any
network. The endpoint tests talk to a tiny HTTP server that stands in for the PTZ
interface of a camera and records what it received.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.ptz import (
    PTZ_PROFILES,
    build_command,
    configured_actions,
    normalize_ptz,
    public_config,
)
from tests.helpers import add_camera

RTSP_URL = "rtsp://192.168.1.28:554/user=admin&password=&channel=1&stream=0.sdp"


class RecordingHandler(BaseHTTPRequestHandler):
    """Record every request and answer like a camera would."""

    received: list[dict[str, Any]] = []

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
        self.wfile.write(b"ok")

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


def test_configured_actions_and_profiles_are_stable() -> None:
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
