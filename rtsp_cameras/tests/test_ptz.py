"""Tests for the PTZ support of the add-on.

The vendor templates are pure functions, so most of them are checked without any
network. The endpoint tests talk to a tiny HTTP server that stands in for the PTZ
interface of a camera and records what it received.
"""

from __future__ import annotations

import asyncio
import base64
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
    LOGIN_OK,
    MSG_LOGIN_RESPONSE,
    MSG_PTZ_REQUEST,
    MSG_PTZ_RESPONSE,
    PTZ_MESSAGE,
    hash_password,
    pack,
    ptz_payload,
    unpack,
)
from app.ptz import (
    COMMAND_TIMEOUT,
    DEFAULT_SPEED,
    DVRIP_TIMEOUT,
    PROBE_STATUS_AUTH,
    PROBE_STATUS_NO_TOKEN,
    PROBE_STATUS_OK,
    PROBE_STATUS_TIMEOUT,
    PROBE_STATUS_UNREACHABLE,
    PROBE_STATUS_UNSUPPORTED,
    PTZ_PROFILES,
    _probe_outcome,
    async_probe_ptz,
    async_run,
    async_send,
    build_command,
    configured_actions,
    needs_direction,
    normalize_ptz,
    preferred_profiles,
    probe_base_url,
    public_config,
    split_userinfo,
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
    #: Status every request is answered with (the PTZ test mode checks for 401 too).
    status_code: int = 200

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
        query = {key: value[0] for key, value in parse_qs(parts.query).items()}
        RecordingHandler.received.append(
            {
                "method": self.command,
                "path": parts.path,
                "query": query,
                "body": body,
                "content_type": self.headers.get("Content-Type"),
                "authorization": self.headers.get("Authorization"),
            }
        )
        self.send_response(RecordingHandler.status_code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(self.reply_for(query))

    def reply_for(self, query: dict[str, str]) -> bytes:
        """Return the body this fake camera answers with (a hook for test cameras)."""
        return RecordingHandler.reply_body

    def log_message(self, *args: Any) -> None:
        """Keep the test output clean."""


class XiongmaiHandler(RecordingHandler):
    """A camera that only understands the PTZ codes of its own family.

    Xiongmai devices answer a foreign CGI - the Dahua one for example - with ``200 OK``
    and ``Error`` in the body, so a stop command alone does not tell which family a
    device belongs to.
    """

    def reply_for(self, query: dict[str, str]) -> bytes:
        """Accept the Xiongmai codes and reject the codes of other families."""
        code = str(query.get("code") or "")
        return b"OK" if code.startswith("Direction") else b"Error"


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


@pytest.fixture(name="error_cam")
def error_cam_fixture() -> Iterator[str]:
    """Run a fake camera whose CGI reports failures with "200 OK" and "Error"."""
    RecordingHandler.received = []
    RecordingHandler.reply_body = b"Error"
    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        RecordingHandler.reply_body = b"ok"


@pytest.fixture(name="html_cam")
def html_cam_fixture() -> Iterator[str]:
    """Run a fake camera whose web interface answers every command with a page.

    This is what an Xiongmai device does on its port 80: the CGI paths do not exist,
    the web server answers ``200 OK`` and the page of its interface for each of them.
    """
    RecordingHandler.received = []
    RecordingHandler.reply_body = b"<!DOCTYPE html><html><body>NETSurveillance WEB</body></html>"
    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        RecordingHandler.reply_body = b"ok"


@pytest.fixture(name="xiongmai_cam")
def xiongmai_cam_fixture() -> Iterator[str]:
    """Run a fake Xiongmai camera that rejects the codes of other families."""
    RecordingHandler.received = []
    RecordingHandler.reply_body = b"ok"
    server = ThreadingHTTPServer(("127.0.0.1", 0), XiongmaiHandler)
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


def test_split_userinfo_moves_credentials_into_a_header() -> None:
    """A command URL with user information cannot be sent by urllib as it is.

    urllib reads ``user:password@host`` as host and port, so such a command used
    to end in ``InvalidURL: nonnumeric port`` instead of a request.
    """
    clean, authorization = split_userinfo(
        "http://admin:p%40ss@192.168.1.10:8080/cgi-bin/ptz.cgi?action=start&code=Left"
    )

    assert clean == "http://192.168.1.10:8080/cgi-bin/ptz.cgi?action=start&code=Left"
    expected = base64.b64encode(b"admin:p@ss").decode("ascii")
    assert authorization == f"Basic {expected}"


def test_split_userinfo_leaves_plain_urls_alone() -> None:
    """Nothing to do without credentials, and a broken URL stays readable."""
    url = "http://192.168.1.10/cgi-bin/ptz.cgi?action=stop"

    assert split_userinfo(url) == (url, None)
    assert split_userinfo("") == ("", None)
    # A port that is not a number is reported by the sender, not by this helper.
    assert split_userinfo("http://camera:abc/x") == ("http://camera:abc/x", None)
    # A URL without a host stays broken, but the password is still taken out of it.
    clean, authorization = split_userinfo("http://admin:secret@")
    assert clean == "http://"
    expected = base64.b64encode(b"admin:secret").decode("ascii")
    assert authorization == f"Basic {expected}"


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


#: The stop the bundled Xiongmai profile builds: a return to the preset a move stored.
#:
#: No payload of this family halts a moving axis - a ``Stop`` of any shape is acknowledged
#: and travelled on, ``Step: 0`` with a direction drives the axis to its *zero position*,
#: and the ``POINT`` object of the vendor app was refused with ``Ret: 118`` (the defect of
#: 0.3.7). The ``GotoPreset`` of the slot that the moves write does end a sweep; measured
#: on the device, see the changelog of 0.3.8 and ``.smoke/dvrip-protocol.md``.
DVRIP_STOP = 'DVRIP {"Command":"GotoPreset","Preset":200,"Channel":1}'


def test_dvrip_profile_builds_the_payloads() -> None:
    """The DVRIP profile speaks the Xiongmai protocol on port 34567."""
    config = normalize_ptz({"profile": "xiongmai_dvrip", "speed": 5}, RTSP_URL)

    assert config is not None
    assert config["port"] == 34567
    # A move stores the position it starts from first, because the stop is the return to
    # it: one template carries both commands, and the sender sends them in that order.
    assert build_command(config, "left") == (
        'DVRIP {"Command":"SetPreset","Preset":200,"Channel":1} '
        'DVRIP {"Command":"DirectionLeft","Step":5,"Channel":1}'
    )
    assert build_command(config, "left", speed=7) == (
        'DVRIP {"Command":"SetPreset","Preset":200,"Channel":1} '
        'DVRIP {"Command":"DirectionLeft","Step":7,"Channel":1}'
    )
    # The stop names no direction: it is the return to the stored position, so it is the
    # same command for every axis (see ``test_a_stop_without_a_direction_is_not_guessed``).
    assert build_command(config, "stop", direction="left") == DVRIP_STOP
    assert build_command(config, "stop") == DVRIP_STOP
    assert build_command(config, "preset", preset="3") == (
        'DVRIP {"Command":"GotoPreset","Preset":3,"Channel":1}'
    )


def test_axis_speeds_default_to_the_general_speed() -> None:
    """A camera without per axis speeds moves exactly as it did before."""
    config = normalize_ptz({"profile": "xiongmai_dvrip", "speed": 6}, RTSP_URL)

    assert config is not None
    assert config["speed_horizontal"] == 6
    assert config["speed_vertical"] == 6
    assert '"Step":6' in str(build_command(config, "up"))
    assert '"Step":6' in str(build_command(config, "left"))


def test_axis_speeds_are_stored_and_used_by_the_arrows() -> None:
    """Both arrows can be tuned apart: the tilt reads the vertical speed, the pan the
    horizontal one, and anything else the general speed."""
    config = normalize_ptz(
        {
            "profile": "xiongmai_dvrip",
            "speed": 4,
            "speed_vertical": 1,
            "speed_horizontal": 8,
        },
        RTSP_URL,
    )

    assert config is not None
    assert '"Step":1' in str(build_command(config, "down"))
    assert '"Step":1' in str(build_command(config, "up"))
    assert '"Step":8' in str(build_command(config, "left"))
    assert '"Step":8' in str(build_command(config, "right"))
    assert '"Step":4' in str(build_command(config, "zoom_in"))
    # A speed of the call is the speed of that whole move, so it wins over both axes.
    assert '"Step":2' in str(build_command(config, "down", speed=2))
    assert '"Step":2' in str(build_command(config, "left", speed=2))
    # An explicit per axis value of the call wins over the stored one.
    assert '"Step":3' in str(build_command(config, "down", speed_vertical=3))
    assert '"Step":6' in str(build_command(config, "left", speed_horizontal=6))


def test_out_of_range_axis_speeds_fall_back() -> None:
    """Whatever a file says, the camera gets a speed of the 1..8 the panel offers.

    An unusable value falls back to the default speed, exactly like an unusable general
    speed; an empty one takes the general speed of the camera.
    """
    config = normalize_ptz(
        {
            "profile": "xiongmai_dvrip",
            "speed": 3,
            "speed_vertical": 99,
            "speed_horizontal": "",
        },
        RTSP_URL,
    )

    assert config is not None
    assert config["speed_vertical"] == DEFAULT_SPEED
    assert config["speed_horizontal"] == 3


def test_a_stored_command_of_an_older_version_is_upgraded() -> None:
    """A camera configured before 0.3.6 gets the fixed commands without a second fill in.

    The commands live in the camera file, so a fix inside a bundled template would never
    reach a camera that was set up earlier. A stored command that is *exactly* an old
    template of this project is therefore replaced - a hand written one is not.
    """
    config = normalize_ptz(
        {
            "profile": "xiongmai_dvrip",
            "speed": 5,
            "commands": {
                "stop": 'DVRIP {"Command":"{direction}","Step":0,"Channel":1}',
                "down": 'DVRIP {"Command":"DirectionDown","Step":{speed},"Channel":1}',
                "left": 'DVRIP {"Command":"DirectionLeft","Step":{speed},"Channel":1,'
                '"Preset":7}',
            },
        },
        RTSP_URL,
    )

    assert config is not None
    assert config["commands"]["stop"] == DVRIP_STOP
    assert config["commands"]["down"] == (
        'DVRIP {"Command":"SetPreset","Preset":200,"Channel":1} '
        'DVRIP {"Command":"DirectionDown","Step":{speed_vertical},"Channel":1}'
    )
    # A command this project never shipped is left alone.
    assert config["commands"]["left"] == (
        'DVRIP {"Command":"DirectionLeft","Step":{speed},"Channel":1,"Preset":7}'
    )


def test_the_stop_of_previous_releases_is_replaced_as_well() -> None:
    """The stops of 0.3.6 and 0.3.7 are templates of this project too.

    The short form ``{"Command":"Stop","Step":0}`` of 0.3.6 is ignored while a move runs,
    and the ``POINT`` object of 0.3.7 is refused with ``Ret: 118`` (both measured on
    192.168.20.253, see the changelogs), so a camera that stored either of them is moved
    over to the ``GotoPreset`` return like one that still carries the guessed template of
    0.3.5.
    """
    for stop in (
        'DVRIP {"Command":"Stop","Step":0,"Channel":1}',
        'DVRIP {"Name":"OPPTZControl","OPPTZControl":{"Command":"Stop","Parameter":'
        '{"POINT":{"bottom":0,"left":0,"right":0,"top":0},"Step":0,"Channel":1}}}',
    ):
        config = normalize_ptz(
            {"profile": "xiongmai_dvrip", "commands": {"stop": stop}}, RTSP_URL
        )

        assert config is not None
        assert config["commands"]["stop"] == DVRIP_STOP


def test_a_stop_without_a_direction_is_not_guessed() -> None:
    """A stop that cannot name its axis is refused instead of moving the camera.

    Dahua and the Xiongmai HTTP CGI need the direction of the movement on their stop
    command. Filling in the first code of the profile - what this project did until
    0.3.6 - is not a stop: on a Xiongmai device every direction *is* a zero position, so
    a stop that says "up" drove the camera to its top instead of standing still
    (measured on 192.168.20.253, see the changelog of 0.3.6).
    """
    config = normalize_ptz({"profile": "xiongmai", "speed": 5}, RTSP_URL)

    assert config is not None
    assert build_command(config, "stop", direction="left") is not None
    assert build_command(config, "stop") is None
    assert needs_direction(config, "stop") is True
    assert needs_direction(config, "left") is False


def test_dvrip_names_the_nested_object_after_the_message() -> None:
    """The device looks the structure up by the name the payload carries twice.

    ``MNetSDK::CProtocolNetIP::NewPTZControlPTL`` of the vendor SDK (0xF1918C of
    ``libFunSDK.so``) loads ``OPPTZControl`` for the value of ``Name`` *and* for the
    key of the nested object (the ``adrp``/``add`` pairs at 0xF197E8 and 0xF1981C both
    point at 0x58D3A9), and the Java layer of that SDK keeps it as one constant
    (``OPPTZControlBean.OPPTZCONTROL_JSONNAME``). ``PTZControl`` - what this code sent
    before - is no member of a device, so the device answered ``Ret: 100`` and stayed
    still, which is exactly what the live camera on 192.168.20.253 did.
    """
    payload = ptz_payload({"Command": "DirectionLeft", "Step": 5, "Channel": 1})

    assert set(payload) == {"Name", PTZ_MESSAGE}, "the name is the only key next to Name"
    assert payload["Name"] == PTZ_MESSAGE == "OPPTZControl"
    assert payload["OPPTZControl"]["Command"] == "DirectionLeft"
    assert payload["OPPTZControl"]["Parameter"]["Channel"] == 0
    assert payload["OPPTZControl"]["Parameter"]["Step"] == 5


def test_dvrip_never_sends_a_negative_preset() -> None:
    """A negative Preset makes a device drop the command; the template uses 0.

    Measured on the live camera (192.168.20.253) with the nested key of ``PTZ_MESSAGE``
    in place and fresh frames of the stream while the command was held: ``Preset: -1``
    left every frame unchanged, ``Preset: 0`` panned the camera (a mean difference of
    47..54 grey levels between two frames 0.7 s apart, against 7.3..7.9 for a still
    camera). ``OPPTZControlBean$Parameter`` of the vendor SDK carries an ``int Preset``
    as well, whose default is 0.
    """
    payload = ptz_payload({"Command": "DirectionLeft", "Step": 5, "Channel": 1})
    assert payload["OPPTZControl"]["Parameter"]["Preset"] == 0

    preset = ptz_payload({"Command": "GotoPreset", "Preset": 3, "Channel": 1})
    assert preset["OPPTZControl"]["Parameter"]["Preset"] == 3


def test_dvrip_sends_a_hand_written_payload_unchanged() -> None:
    """A payload captured from the vendor app is what a custom profile is for."""
    captured = {
        "Name": "OPPTZControl",
        "OPPTZControl": {
            "Command": "DirectionLeft",
            "Parameter": {"Channel": 1, "Step": 4},
        },
        # the real session of the login is filled in by the client, never by hand
        "SessionID": "0x0000000001",
    }

    assert ptz_payload(dict(captured)) == {
        "Name": "OPPTZControl",
        "OPPTZControl": {
            "Command": "DirectionLeft",
            "Parameter": {"Channel": 1, "Step": 4},
        },
    }


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


def test_ptz_endpoint_with_credentials_inside_the_command_url(
    client: TestClient, ptz_cam: str
) -> None:
    """A command URL carrying user information must be sent, not rejected.

    Such a URL used to end in the generic HTTP 500 of the endpoint, because
    urllib cannot split ``user:password@host``. The credentials now travel in an
    ``Authorization`` header and the URL reaches the camera without them.
    """
    host = ptz_cam.replace("http://", "http://admin:secret@", 1)
    camera = add_camera(
        client,
        url=RTSP_URL,
        ptz={
            "profile": "custom",
            "commands": {"left": f"GET {host}/cgi-bin/ptz.cgi?action=start&code=DirectionLeft"},
        },
    )

    response = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "left"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ptz"]["ok"] is True
    assert payload["ptz"]["status"] == 200
    assert "secret" not in json.dumps(payload), "the password must not be echoed"

    request = received()[0]
    assert request["path"] == "/cgi-bin/ptz.cgi"
    assert request["query"]["action"] == "start"
    token = base64.b64encode(b"admin:secret").decode("ascii")
    assert request["authorization"] == f"Basic {token}"


def test_ptz_endpoint_reports_a_command_url_that_cannot_be_sent(
    client: TestClient,
) -> None:
    """A send that cannot even be prepared is a clear error, not a crash.

    ``urllib`` refuses ``host:port`` when the port is not a number; that used to
    escape as an unhandled ``InvalidURL`` and a generic HTTP 500.
    """
    camera = add_camera(
        client,
        url=RTSP_URL,
        ptz={
            "profile": "custom",
            "commands": {"left": "GET http://camera:not_a_port/x"},
        },
    )

    response = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "left"})

    assert response.status_code == 502, response.text
    payload = response.json()
    assert payload["error"] == "ptz_failed"
    assert "port" in payload["detail"].lower()
    assert "<html>" not in response.text, "not the HTML error page of the web server"


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


def test_ptz_endpoint_remembers_the_axis_a_stop_has_to_name(
    client: TestClient, ptz_cam: str
) -> None:
    """A stop without a direction stops what was moved last, not a guessed axis."""
    camera = add_camera(client, url=RTSP_URL, ptz={"profile": "xiongmai", "base_url": ptz_cam})

    client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "down"})
    response = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "stop"})

    assert response.status_code == 200, response.text
    stop = received()[-1]
    assert stop["query"]["action"] == "stop"
    assert stop["query"]["code"] == "DirectionDown"


def test_ptz_endpoint_refuses_a_stop_without_any_direction(
    client: TestClient, ptz_cam: str
) -> None:
    """A stop that has no axis to name is refused instead of answered with a guess.

    Nothing was moved yet, so the axis of the movement is unknown - and picking one is
    not an option: on a Xiongmai device a stop that says "up" drives the camera to its
    top (see ``test_a_stop_without_a_direction_is_not_guessed``).
    """
    camera = add_camera(client, url=RTSP_URL, ptz={"profile": "xiongmai", "base_url": ptz_cam})

    response = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "stop"})

    assert response.status_code == 400, response.text
    assert response.json()["error"] == "ptz_direction_required"


def test_ptz_endpoint_sends_the_speed_of_the_axis(client: TestClient, ptz_cam: str) -> None:
    """The panel sends the speed of the arrow that was pressed."""
    camera = add_camera(
        client,
        url=RTSP_URL,
        ptz={
            "profile": "custom",
            "base_url": ptz_cam,
            "speed": 4,
            "speed_vertical": 2,
            "speed_horizontal": 7,
            "commands": {
                "down": "GET {base}/cgi-bin/ptz.cgi?action=start&code=Down&arg2={speed_vertical}",
                "left": "GET {base}/cgi-bin/ptz.cgi?action=start&code=Left&arg2={speed_horizontal}",
            },
        },
    )

    down = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "down"})
    left = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "left"})
    override = client.post(
        f"/api/cameras/{camera['id']}/ptz", json={"action": "left", "speed_horizontal": 5}
    )

    assert down.status_code == 200, down.text
    assert left.status_code == 200, left.text
    assert down.json()["ptz"]["command"].endswith("arg2=2")
    assert left.json()["ptz"]["command"].endswith("arg2=7")
    assert override.json()["ptz"]["command"].endswith("arg2=5")


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


def test_ptz_endpoint_reports_a_camera_that_answers_error(
    client: TestClient, error_cam: str
) -> None:
    """A camera that answers "200 OK" and "Error" did not move - it is reported."""
    camera = add_camera(client, url=RTSP_URL, ptz={"profile": "dahua", "base_url": error_cam})

    response = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "right"})

    assert response.status_code == 502
    assert response.json()["error"] == "ptz_failed"
    assert "Error" in response.json()["detail"]


def test_ptz_endpoint_reports_a_web_page(client: TestClient, html_cam: str) -> None:
    """A command URL answered with the web page of the device did not reach a CGI."""
    camera = add_camera(client, url=RTSP_URL, ptz={"profile": "dahua", "base_url": html_cam})

    response = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "up"})

    assert response.status_code == 502
    assert response.json()["error"] == "ptz_failed"
    assert "DOCTYPE html" in response.json()["detail"]


def test_probe_does_not_keep_a_web_page(client: TestClient, html_cam: str) -> None:
    """No variant answered a command, so none of them is stored as the main handling.

    A device whose port 80 is its web interface answers all seven transports with
    ``200 OK`` and a page. Picking the first of them would publish commands that never
    move the camera - the test mode has to report that nothing understood it.
    """
    camera = add_camera(client, url=RTSP_URL, ptz={"profile": "dahua", "base_url": html_cam})

    response = client.post(
        "/api/ptz/probe",
        json={"camera_id": camera["id"], "base_url": html_cam, "timeout": 2},
    )
    data = response.json()

    assert response.status_code == 200, response.text
    assert data["probe"]["ok"] is False
    assert data["probe"]["profile"] is None
    assert data["applied"] is False
    statuses = {item["profile"]: item["status"] for item in data["probe"]["results"]}
    assert statuses["dahua"] == PROBE_STATUS_UNSUPPORTED
    assert statuses["xiongmai"] == PROBE_STATUS_UNSUPPORTED


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

#: The framing the devices use, written out here so that the test does not simply
#: follow the implementation: a header of 20 bytes with the message type as a 16 bit
#: value at offset 14 and the payload length as a 32 bit value at offset 16, the
#: payload as plain JSON.
DVRIP_HEADER = "<BB2xIIBBHI"
DVRIP_HEADER_SIZE = struct.calcsize(DVRIP_HEADER)
DVRIP_SESSION = 0x1234


class DvripCamera(socketserver.ThreadingTCPServer):
    """Fake Xiongmai device that answers the DVRIP login and PTZ requests."""

    allow_reuse_address = True
    received: list[dict[str, Any]] = []
    logins: list[dict[str, Any]] = []
    login: dict[str, Any] = {}
    fail_login = False
    reject_login_types: dict[str, int] = {}

    def __init__(self) -> None:
        """Bind to a free port on localhost."""
        super().__init__(("127.0.0.1", 0), DvripHandler)
        self.port = self.server_address[1]


class DvripHandler(socketserver.BaseRequestHandler):
    """Handle one connection: login, then the PTZ command."""

    def handle(self) -> None:
        """Answer the two messages the client sends."""
        _message_id, payload = read_message(self.request)
        DvripCamera.logins.append(payload)
        DvripCamera.login = payload
        rejection = DvripCamera.reject_login_types.get(str(payload.get("LoginType")))
        if rejection is not None:
            self.request.sendall(reply(MSG_LOGIN_RESPONSE, {"Ret": rejection}))
            return
        if DvripCamera.fail_login:
            self.request.sendall(reply(MSG_LOGIN_RESPONSE, {"Ret": 101}))
            return
        self.request.sendall(
            reply(MSG_LOGIN_RESPONSE, {"Ret": 100, "SessionID": f"0x{DVRIP_SESSION:08X}"})
        )

        message_id, payload = read_message(self.request)
        DvripCamera.received.append({"message_id": message_id, "payload": payload})
        self.request.sendall(reply(MSG_PTZ_RESPONSE, {"Ret": 100}))


def _read_exactly(sock: Any, length: int) -> bytes:
    """Read exactly ``length`` bytes from a socket."""
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise AssertionError("connection closed")
        data += chunk
    return data


def read_message(sock: Any) -> tuple[int, dict[str, Any]]:
    """Read a complete DVRIP message from a socket."""
    header = _read_exactly(sock, DVRIP_HEADER_SIZE)
    parsed = struct.unpack(DVRIP_HEADER, header)
    body = _read_exactly(sock, parsed[7]) if parsed[7] else b"{}"
    return parsed[6], json.loads(body.decode("utf-8"))


def reply(message_id: int, payload: dict[str, Any]) -> bytes:
    """Build a DVRIP reply; devices pad the payload, the client has to tolerate it."""
    body = json.dumps(payload).encode("utf-8")
    padding = b"\r\n"
    length = len(body) + len(padding)
    header = struct.pack(DVRIP_HEADER, 0xFF, 1, DVRIP_SESSION, 1, 0, 0, message_id, length)
    return header + body + padding


@pytest.fixture(name="dvrip_cam")
def dvrip_cam_fixture() -> Iterator[int]:
    """Run a fake DVRIP device and yield its port."""
    DvripCamera.received = []
    DvripCamera.logins = []
    DvripCamera.login = {}
    DvripCamera.fail_login = False
    DvripCamera.reject_login_types = {}
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
    assert DvripCamera.login["EncryptType"] == "MD5"
    assert DvripCamera.login["LoginType"] == "DVRIP-Web"
    assert DvripCamera.login["PassWord"] == hash_password("secret")

    # A move of the bundled profile stores the position it starts from first: two commands
    # over two connections, and the store goes out before the move.
    assert len(DvripCamera.received) == 2
    store = DvripCamera.received[0]
    assert store["message_id"] == MSG_PTZ_REQUEST
    assert store["payload"]["Name"] == "OPPTZControl"
    assert store["payload"]["OPPTZControl"]["Command"] == "SetPreset"
    assert store["payload"]["OPPTZControl"]["Parameter"]["Preset"] == 200
    assert store["payload"]["OPPTZControl"]["Parameter"]["Channel"] == 0
    assert store["payload"]["SessionID"] == f"0x{DVRIP_SESSION:08X}"

    sent = DvripCamera.received[1]
    assert sent["message_id"] == MSG_PTZ_REQUEST
    assert sent["payload"]["Name"] == "OPPTZControl"
    assert sent["payload"]["OPPTZControl"]["Command"] == "DirectionLeft"
    assert sent["payload"]["OPPTZControl"]["Parameter"]["Step"] == 6
    assert sent["payload"]["OPPTZControl"]["Parameter"]["Channel"] == 0
    assert sent["payload"]["SessionID"] == f"0x{DVRIP_SESSION:08X}"


def test_dvrip_stop_returns_to_the_stored_preset(dvrip_cam: int) -> None:
    """The stop of the bundled profile is the ``GotoPreset`` of the slot a move wrote.

    This family has no payload that halts a moving axis, so the stop of its profile is the
    return to the position the move stored - one command, no direction, measured on the
    device (see the changelog of 0.3.8).
    """
    url = "rtsp://admin:secret@127.0.0.1:554/user=admin&password=secret&channel=1&stream=0.sdp"
    config = normalize_ptz({"profile": "xiongmai_dvrip", "port": dvrip_cam}, url)
    assert config is not None

    command = build_command(config, "stop", direction="left")
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
    assert len(DvripCamera.received) == 1
    sent = DvripCamera.received[0]
    assert sent["payload"]["OPPTZControl"]["Command"] == "GotoPreset"
    assert sent["payload"]["OPPTZControl"]["Parameter"]["Preset"] == 200
    assert sent["payload"]["OPPTZControl"]["Parameter"]["Channel"] == 0


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
    assert [login["LoginType"] for login in DvripCamera.logins] == ["DVRIP-Web"], (
        "a refused password is not worth another login type"
    )


def test_dvrip_gets_the_long_deadline_of_this_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DVRIP command waits longer than a vendor CGI, an HTTP command keeps its deadline.

    The device of the test set answers the ``GotoPreset`` that ends a move - the stop of the
    bundled profile, which drags the camera back - only once the axis arrived: measured 10 to
    16 s after the command. A deadline of ``COMMAND_TIMEOUT`` reported that working command
    as a failure, so the transport gets ``DVRIP_TIMEOUT`` (see the changelog of 0.3.9).
    """
    seen: list[float] = []

    async def fake_send_detail(command: str, timeout: float = 0.0, **_kwargs: Any) -> tuple:
        seen.append(timeout)
        return LOGIN_OK, None, ""

    monkeypatch.setattr("app.ptz.async_send_detail", fake_send_detail)

    dvrip = normalize_ptz({"profile": "xiongmai_dvrip", "port": 34567}, RTSP_URL)
    assert dvrip is not None
    assert asyncio.run(async_run(dvrip, "stop")).ok
    assert seen == [DVRIP_TIMEOUT]

    seen.clear()
    cgi = normalize_ptz({"profile": "dahua"}, RTSP_URL)
    assert cgi is not None
    assert asyncio.run(async_run(cgi, "left")).ok
    assert seen == [COMMAND_TIMEOUT], "a vendor CGI is answered at once"
    assert DVRIP_TIMEOUT > COMMAND_TIMEOUT


def test_dvrip_login_hash_matches_the_sdk() -> None:
    """``PassWord`` is the eight character hash of the vendor SDK.

    ``XMMD5Encrypt`` of the FunSDK adds the two bytes of every MD5 pair, takes the
    sum modulo 62 and writes it as ``0-9A-Za-z``. The user name is not part of it.
    """
    assert hash_password("") == "tlJwpbo6"
    assert hash_password("admin") == "6QNMIQGe"
    assert hash_password("secret") == "awAU3E4X"
    assert len(hash_password("secret")) == 8


def test_dvrip_header_follows_the_sdk_layout() -> None:
    """The header carries the type at offset 14 and the payload length at offset 16."""
    body = b'{"Ret":100}'
    message = pack(MSG_LOGIN_RESPONSE, {"Ret": 100})

    assert message[:2] == b"\xff\x01", "magic and version of the header"
    assert message[2:4] == b"\x00\x00", "reserved bytes"
    assert struct.unpack_from("<II", message, 4) == (0, 0), "session and sequence"
    assert struct.unpack_from("<H", message, 14)[0] == MSG_LOGIN_RESPONSE
    assert struct.unpack_from("<I", message, 16)[0] == len(body)
    assert len(message) == 20 + len(body), "no padding behind the JSON"
    assert message[20:] == body


def test_dvrip_answers_with_padding_are_parsed() -> None:
    """Devices pad their answers; the payload has to survive it."""
    body = b'{"Ret":100,"SessionID":"0x0000002C"}'
    header = struct.pack(DVRIP_HEADER, 0xFF, 1, 0x2C, 1, 0, 0, MSG_LOGIN_RESPONSE, len(body))

    for padding in (b"", b"\r\n", b"\x00", b"\x00\x00"):
        message_id, session, payload = unpack(header + body + padding)
        assert message_id == MSG_LOGIN_RESPONSE
        assert session == 0x2C
        assert payload == {"Ret": 100, "SessionID": "0x0000002C"}


def test_dvrip_asks_again_with_another_login_type(dvrip_cam: int) -> None:
    """A device that dislikes the login type is asked with the next one."""
    DvripCamera.reject_login_types = {"DVRIP-Web": 102}
    config = normalize_ptz({"profile": "xiongmai_dvrip", "port": dvrip_cam}, RTSP_URL)
    assert config is not None
    command = build_command(config, "stop", direction="left")
    assert command is not None

    status, error = asyncio.run(
        async_send(
            command,
            host="127.0.0.1",
            port=dvrip_cam,
            username="admin",
            password="secret",
        )
    )

    assert error is None, error
    assert status == LOGIN_OK
    assert [login["LoginType"] for login in DvripCamera.logins] == [
        "DVRIP-Web",
        "DVRIP-Mobile",
    ]


# ----------------------------------------------------------- PTZ test mode
@pytest.fixture(name="locked_cam")
def locked_cam_fixture() -> Iterator[str]:
    """Run a fake camera that answers every request with '401 Unauthorized'."""
    RecordingHandler.received = []
    RecordingHandler.status_code = 401
    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        RecordingHandler.status_code = 200
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_probe_base_url_accepts_what_a_user_types() -> None:
    """An IP, a host:port and a full URL all become an HTTP base URL."""
    assert probe_base_url("192.168.1.108") == "http://192.168.1.108"
    assert probe_base_url("  192.168.1.108  ") == "http://192.168.1.108"
    assert probe_base_url("camera.fritz.box:8080") == "http://camera.fritz.box:8080"
    assert (
        probe_base_url("https://192.168.1.108/onvif/device_service")
        == "https://192.168.1.108"
    )
    assert probe_base_url("192.168.1.108", port=8000) == "http://192.168.1.108:8000"
    assert probe_base_url("192.168.1.108:8080", port=8000) == "http://192.168.1.108:8000"
    assert probe_base_url("") == ""
    assert probe_base_url("", stream_url=RTSP_URL) == "http://192.168.1.28"


def test_the_stream_url_names_the_family_of_a_device() -> None:
    """The shape of a stream URL is used as the hint for the PTZ test mode."""
    # DVRIP is asked before the HTTP codes of the same family: the answer of the device
    # proves more than a 200 OK of a web server (see PROBE_ORDER).
    assert preferred_profiles(RTSP_URL) == ("xiongmai_dvrip", "xiongmai")
    assert preferred_profiles(
        "rtsp://admin:secret@192.168.1.64:554/cam/realmonitor?channel=1&subtype=0"
    ) == ("dahua",)
    assert preferred_profiles("rtsp://admin:secret@192.168.1.64:554/Streaming/Channels/101") == (
        "hikvision",
    )
    assert preferred_profiles("rtsp://192.168.1.28:554/stream1") == ()
    assert preferred_profiles("") == ()


@pytest.mark.parametrize(
    ("status", "error", "snippet", "expected"),
    [
        (200, None, "ok", PROBE_STATUS_OK),
        (204, None, "", PROBE_STATUS_OK),
        # Vendor CGIs that report the failure with "200 OK" in the body
        (200, None, "result=-1", PROBE_STATUS_AUTH),
        (200, None, "result=-3", PROBE_STATUS_UNSUPPORTED),
        (200, None, "<s:Fault>NotAuthorized</s:Fault>", PROBE_STATUS_AUTH),
        (401, "HTTP 401", "", PROBE_STATUS_AUTH),
        (403, "HTTP 403", "", PROBE_STATUS_AUTH),
        (404, "HTTP 404", "", PROBE_STATUS_UNSUPPORTED),
        (501, "HTTP 501", "", PROBE_STATUS_UNSUPPORTED),
        (500, "HTTP 500", "", "error"),
        (None, "[WinError 10061] connection refused", "", PROBE_STATUS_UNREACHABLE),
        (None, "timeout", "", "timeout"),
        (None, "login_failed_101", "", PROBE_STATUS_AUTH),
        (None, "unexpected_reply_1001", "", PROBE_STATUS_UNSUPPORTED),
        # ... and with nothing but a word: cheap cameras answer "200 OK" and "Error"
        # when a code or a channel is wrong, which moves nothing.
        (200, None, "Error", PROBE_STATUS_UNSUPPORTED),
        (200, None, "  failed  ", PROBE_STATUS_UNSUPPORTED),
        # ... and with a web page: the command URL does not exist, the device served the
        # page of its web interface (an Xiongmai device answers like that on port 80).
        (
            200,
            None,
            "<!DOCTYPE html><html><body>NETSurveillance WEB</body></html>",
            PROBE_STATUS_UNSUPPORTED,
        ),
    ],
)
def test_probe_outcome_maps_every_answer(
    status: int | None, error: str | None, snippet: str, expected: str
) -> None:
    """Every answer of a camera is translated into the vocabulary of the test mode."""
    assert _probe_outcome(status, error, snippet)[0] == expected


def test_probe_keeps_the_variant_that_answers(
    settings: Settings, client: TestClient, ptz_cam: str
) -> None:
    """The test mode stores the command set that accepted the stop command."""
    camera = add_camera(
        client,
        url=RTSP_URL,
        rtsp_transport="tcp",
        ptz={
            "enabled": True,
            "profile": "custom",
            "base_url": ptz_cam,
            "commands": {"stop": f"GET {ptz_cam}/custom/stop"},
            "presets": [{"id": "1", "name": "Gate"}],
        },
    )

    response = client.post(
        "/api/ptz/probe",
        json={"camera_id": camera["id"], "base_url": ptz_cam, "timeout": 2},
    )
    data = response.json()

    assert response.status_code == 200, response.text
    assert data["probe"]["ok"] is True
    # ONVIF has no profile token here, DVRIP has no listener on the default port and the
    # stream URL is the Xiongmai shape, so the hinted Xiongmai CGI wins the test.
    assert data["probe"]["profile"] == "xiongmai"
    assert data["applied"] is True

    statuses = {item["profile"]: item["status"] for item in data["probe"]["results"]}
    assert statuses["onvif"] == PROBE_STATUS_NO_TOKEN
    assert statuses["xiongmai"] == PROBE_STATUS_OK

    # The winner is the main handling now - with the commands of its profile and the
    # presets of the camera - and it reaches Home Assistant through the camera file.
    stored = data["camera"]["ptz"]
    assert stored["profile"] == "xiongmai"
    assert stored["actions"] == list(PTZ_PROFILES["xiongmai"]["commands"])
    assert stored["presets"] == [{"id": "1", "name": "Gate"}]
    assert "action=stop" in stored["commands"]["stop"]
    # The stored command keeps the placeholders: which direction has to be stopped is
    # only known when the command is sent.
    assert "code={direction}" in stored["commands"]["stop"]

    published = json.loads(Path(settings.published_file).read_text(encoding="utf-8"))
    assert published["cameras"][0]["ptz"]["profile"] == "xiongmai"

    # The probe really sent the stop command of the variant that won the test. The
    # direction is part of the stop of the Xiongmai HTTP CGI, the probe names one so
    # that the request is a valid stop.
    stops = [request for request in received() if request["query"].get("action") == "stop"]
    assert stops
    assert any(request["query"].get("code") == "DirectionLeft" for request in stops)


def test_probe_prefers_a_real_dvrip_answer_over_an_http_200(
    client: TestClient, ptz_cam: str, dvrip_cam: int
) -> None:
    """A web server saying ``200 OK`` loses against the PTZ subsystem of the device.

    The Xiongmai camera of the test set answers *every* command on its own
    ``/cgi-bin/ptz.cgi`` with ``200 OK`` - even a command name that does not exist - and
    does not move. The HTTP variant therefore used to win the test mode and was stored as
    the main PTZ handling. DVRIP is asked first now: only it proves that a device
    understood the command, because the device has to accept the login and answer the PTZ
    request out of its PTZ subsystem.
    """
    camera = add_camera(client, url=RTSP_URL, ptz={"profile": "xiongmai", "base_url": ptz_cam})

    response = client.post(
        "/api/ptz/probe",
        json={"camera_id": camera["id"], "base_url": ptz_cam, "port": dvrip_cam, "timeout": 2},
    )
    data = response.json()

    assert response.status_code == 200, response.text
    statuses = {item["profile"]: item["status"] for item in data["probe"]["results"]}
    assert statuses["xiongmai"] == PROBE_STATUS_OK
    assert statuses["xiongmai_dvrip"] == PROBE_STATUS_OK
    assert data["probe"]["profile"] == "xiongmai_dvrip"
    assert data["applied"] is True
    assert data["camera"]["ptz"]["profile"] == "xiongmai_dvrip"
    assert data["camera"]["ptz"]["commands"]["left"].startswith("DVRIP ")
    # The stop of this profile needs no direction: it is the return to the preset that the
    # move of the probe stored.
    assert DvripCamera.received[-1]["payload"]["OPPTZControl"]["Command"] == "GotoPreset"


def test_probe_prefers_onvif_when_the_token_is_there(
    client: TestClient, soap_cam: str
) -> None:
    """A device with an ONVIF PTZ service wins - and its token is filled in."""
    camera = add_camera(client, url=RTSP_URL)

    response = client.post(
        "/api/ptz/probe", json={"camera_id": camera["id"], "base_url": soap_cam}
    )
    data = response.json()

    assert response.status_code == 200, response.text
    assert data["probe"]["profile"] == "onvif"
    assert data["probe"]["token"] == "Profile_1"
    # ONVIF is asked first and alone: the other variants need no request at all.
    assert [item["profile"] for item in data["probe"]["results"]] == ["onvif"]
    assert data["camera"]["ptz"]["token"] == "Profile_1"
    assert (
        "<tptz:ProfileToken>Profile_1</tptz:ProfileToken>"
        in data["camera"]["ptz"]["commands"]["stop"]
    )


def test_probe_reports_missing_credentials(client: TestClient, locked_cam: str) -> None:
    """A device that refuses the credentials is never made the main handling."""
    camera = add_camera(
        client, url=RTSP_URL, ptz={"profile": "xiongmai", "base_url": locked_cam}
    )

    response = client.post(
        "/api/ptz/probe",
        json={"camera_id": camera["id"], "base_url": locked_cam, "timeout": 2},
    )
    data = response.json()

    assert response.status_code == 200, response.text
    assert data["probe"]["ok"] is False
    # The variant exists, it only refuses the credentials - and that is reported.
    assert data["probe"]["profile"] == "onvif"
    assert data["probe"]["status"] == PROBE_STATUS_AUTH
    assert data["applied"] is False
    stored = client.get(f"/api/cameras/{camera['id']}").json()["camera"]["ptz"]
    assert stored["profile"] == "xiongmai"


def test_probe_reports_an_address_that_does_not_answer(client: TestClient) -> None:
    """Nothing answers - the test says so and leaves the camera as it is."""
    camera = add_camera(client, url=RTSP_URL)

    response = client.post(
        "/api/ptz/probe",
        json={"camera_id": camera["id"], "base_url": "http://127.0.0.1:9", "timeout": 2},
    )
    data = response.json()

    assert response.status_code == 200, response.text
    assert data["probe"]["ok"] is False
    assert data["probe"]["profile"] is None
    assert data["applied"] is False
    # A closed port answers with "connection refused" or swallows the request.
    assert {item["status"] for item in data["probe"]["results"]} <= {
        PROBE_STATUS_UNREACHABLE,
        PROBE_STATUS_TIMEOUT,
    }
    assert client.get(f"/api/cameras/{camera['id']}").json()["camera"]["ptz"] is None


def test_probe_takes_the_address_from_the_camera(client: TestClient, ptz_cam: str) -> None:
    """Without an address the host of the RTSP URL is used - the IP is typed once."""
    port = urlsplit(ptz_cam).port
    camera = add_camera(client, url=f"rtsp://127.0.0.1:{port}/stream1")

    response = client.post(
        "/api/ptz/probe",
        json={"camera_id": camera["id"], "http_port": port, "timeout": 2},
    )
    data = response.json()

    assert response.status_code == 200, response.text
    assert data["probe"]["base_url"] == f"http://127.0.0.1:{port}"
    assert data["probe"]["profile"] == "dahua"
    assert data["applied"] is True


def test_probe_can_run_without_applying(client: TestClient, ptz_cam: str) -> None:
    """``apply: false`` reports the winner and leaves the stored commands alone."""
    camera = add_camera(client, url=RTSP_URL, ptz={"profile": "dahua", "base_url": ptz_cam})

    response = client.post(
        "/api/ptz/probe",
        json={"camera_id": camera["id"], "base_url": ptz_cam, "apply": False},
    )
    data = response.json()

    assert data["probe"]["profile"] == "xiongmai"
    assert data["applied"] is False
    assert data["camera"] is None
    stored = client.get(f"/api/cameras/{camera['id']}").json()["camera"]["ptz"]
    assert stored["profile"] == "dahua"


def test_probe_needs_an_address(client: TestClient) -> None:
    """A test without any address to ask is refused."""
    response = client.post("/api/ptz/probe", json={"base_url": ""})

    assert response.status_code == 400
    assert response.json()["error"] == "ptz_base_url_required"


def test_probe_reports_an_unknown_camera(client: TestClient, ptz_cam: str) -> None:
    """A camera id that does not exist is reported instead of silently ignored."""
    response = client.post(
        "/api/ptz/probe", json={"camera_id": "nope", "base_url": ptz_cam}
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_probe_finds_a_dvrip_port(client: TestClient, dvrip_cam: int) -> None:
    """A DVR without any HTTP interface is detected on its DVRIP port."""
    camera = add_camera(
        client,
        url="rtsp://user:pass@127.0.0.1:554/user=user&password=pass&channel=1&stream=0.sdp",
    )

    response = client.post(
        "/api/ptz/probe",
        json={
            "camera_id": camera["id"],
            # The HTTP interface of this DVR does not exist, its DVRIP port does.
            "base_url": "127.0.0.1:9",
            "port": dvrip_cam,
            "timeout": 2,
        },
    )
    data = response.json()

    assert response.status_code == 200, response.text
    assert data["probe"]["profile"] == "xiongmai_dvrip"
    assert data["applied"] is True
    assert data["camera"]["ptz"]["profile"] == "xiongmai_dvrip"
    assert data["camera"]["ptz"]["port"] == dvrip_cam
    assert DvripCamera.login["UserName"] == "user"
    assert DvripCamera.received[0]["payload"]["OPPTZControl"]["Command"] == "GotoPreset"


def test_probe_finds_the_variant_a_xiongmai_url_points_at(
    client: TestClient, xiongmai_cam: str
) -> None:
    """Foreign codes are answered with "Error", the codes of the device with "OK".

    This is the case that stored Dahua commands in a Xiongmai camera: the stop command
    of the wrong family was answered with "200 OK" and "Error" instead of being
    refused, so the wrong commands looked like a camera that works.
    """
    camera = add_camera(
        client,
        url="rtsp://borys:secret@127.0.0.1:554/user=borys&password=secret&channel=1&stream=0.sdp",
        ptz={"enabled": True, "profile": "custom", "base_url": xiongmai_cam},
    )

    response = client.post(
        "/api/ptz/probe",
        json={"camera_id": camera["id"], "base_url": xiongmai_cam, "timeout": 2},
    )
    data = response.json()

    assert response.status_code == 200, response.text
    assert data["probe"]["profile"] == "xiongmai"
    assert data["applied"] is True
    assert data["camera"]["ptz"]["profile"] == "xiongmai"
    statuses = {item["profile"]: item["status"] for item in data["probe"]["results"]}
    assert statuses["xiongmai"] == PROBE_STATUS_OK
    # "200 OK" with "Error" in the body is not a camera that moves.
    assert statuses["dahua"] == PROBE_STATUS_UNSUPPORTED


def test_probe_asks_every_variant_in_order(ptz_cam: str) -> None:
    """The report carries one result per variant, in the order of the test."""
    report = asyncio.run(
        async_probe_ptz(ptz_cam, timeout=2, stream_url="rtsp://192.168.1.28:554/stream1")
    )

    assert report["ok"] is True
    assert report["profile"] == "dahua"
    assert report["tested"] == len(report["results"])
    # ONVIF, then the transport whose answer comes out of the device itself (DVRIP), then
    # the vendor CGIs in the order they are tried in.
    assert [item["profile"] for item in report["results"]] == [
        "onvif",
        "xiongmai_dvrip",
        "dahua",
        "hikvision",
        "axis",
        "foscam",
        "xiongmai",
    ]
    assert all(item["label"] for item in report["results"])


def test_probe_asks_the_variant_of_the_stream_url_first(ptz_cam: str) -> None:
    """A URL that names the family of a device decides which variant is asked first.

    The fake camera answers every command with "ok", so without the hint the first
    answering entry of PROBE_ORDER (Dahua) would win - which is how the commands of the
    wrong family ended up in a camera that does not understand them. The Xiongmai URL asks
    DVRIP before the Xiongmai HTTP codes; only the CGI of the same fake camera answers, so
    it is the one that is stored.
    """
    report = asyncio.run(async_probe_ptz(ptz_cam, timeout=2, stream_url=RTSP_URL))

    assert report["profile"] == "xiongmai"
    assert [item["profile"] for item in report["results"]] == [
        "onvif",
        "xiongmai_dvrip",
        "xiongmai",
        "dahua",
        "hikvision",
        "axis",
        "foscam",
    ]


def test_probe_without_an_address_asks_nothing() -> None:
    """An empty address is reported instead of asking the network."""
    report = asyncio.run(async_probe_ptz(""))

    assert report["ok"] is False
    assert report["profile"] is None
    assert report["results"] == []



