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
    sequence_timeout,
    unpack,
)
from app.ptz import (
    COMMAND_TIMEOUT,
    DEFAULT_SPEED,
    DVRIP_TIMEOUT,
    FIRST_STEP_SLOT,
    MAX_STEP_SLOTS,
    PROBE_STATUS_AUTH,
    PROBE_STATUS_NO_TOKEN,
    PROBE_STATUS_OK,
    PROBE_STATUS_TIMEOUT,
    PROBE_STATUS_UNREACHABLE,
    PROBE_STATUS_UNSUPPORTED,
    PTZ_PROFILES,
    _probe_outcome,
    apply_inversion,
    async_probe_ptz,
    async_run,
    async_send,
    build_command,
    configured_actions,
    crossing_of,
    crossing_rung,
    crossings_of,
    needs_direction,
    normalize_ptz,
    other_axis,
    preferred_profiles,
    probe_base_url,
    public_config,
    rung_position,
    split_userinfo,
    step_drag,
    step_target,
    stops_in_place,
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


def test_the_raw_placeholders_keep_a_password_as_it_is() -> None:
    """A CGI that compares the password itself gets the password, not an encoding of it.

    The snapshot API of the Xiongmai family of the test set answers 401 to
    ``password=Qwas123%24k`` and 200 to ``password=Qwas123$k``, so the encoded form that
    keeps a query string intact is exactly what that family refuses.
    """
    config = normalize_ptz(
        {
            "profile": "custom",
            "username": "borys",
            "password": "Qwas123$k",
            "commands": {
                "up": "GET {base}/webcapture.jpg?command=snap&channel={channel}"
                "&user={username_raw}&password={password_raw}",
                "stop": "GET {base}/webcapture.jpg?command=snap&channel={channel}"
                "&user={username}&password={password}",
            },
        },
        "rtsp://10.0.0.4:554/stream",
    )

    assert config is not None
    assert "user=borys&password=Qwas123$k" in config["commands"]["up"]
    assert "user=borys&password=Qwas123%24k" in config["commands"]["stop"]
    masked = public_config(config)
    assert masked is not None
    assert "Qwas123" not in json.dumps(masked), "the raw form is masked as well"


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


#: The stop the bundled Xiongmai profile builds: store the position, return to it.
#:
#: No payload of this family halts a moving axis - a ``Stop`` of any shape is acknowledged
#: and travelled on, ``Step: 0`` with a direction drives the axis to its *zero position*,
#: the ``POINT`` object of the vendor app was refused with ``Ret: 118`` (the defect of
#: 0.3.7), and an empty command name or ``StopTour`` is ignored (measured with
#: ``.smoke/jog_probe.py``). What the device does have is a *position*: ``SetPreset`` is
#: served immediately, a running axis included, and ``GotoPreset`` drives the camera back
#: to a stored position - so the stop captures where the camera stands when the key comes
#: up and returns to it. Held for half a second, one second and three seconds, the tilt of
#: the test device came to rest at ``vs top`` 1.9, 7.7 and 32.1 (see
#: ``.smoke/jog_probe.py``).
DVRIP_STOP = (
    'DVRIP {"Command":"SetPreset","Preset":201,"Channel":1} '
    'DVRIP {"Command":"GotoPreset","Preset":201,"Channel":1}'
)


def test_dvrip_profile_builds_the_payloads() -> None:
    """The DVRIP profile speaks the Xiongmai protocol on port 34567."""
    config = normalize_ptz({"profile": "xiongmai_dvrip", "speed": 5}, RTSP_URL)

    assert config is not None
    assert config["port"] == 34567
    # A move carries the direction and the speed of its axis - nothing else: the stop of
    # 0.3.10 captures the position it is sent at, so a move does not store a start position.
    assert build_command(config, "left") == (
        'DVRIP {"Command":"DirectionLeft","Step":5,"Channel":1}'
    )
    assert build_command(config, "left", speed=7) == (
        'DVRIP {"Command":"DirectionLeft","Step":7,"Channel":1}'
    )
    # The stop names no direction: it captures the position it is sent at, so it is the
    # same command for every axis (see ``test_a_stop_without_a_direction_is_not_guessed``).
    assert build_command(config, "stop", direction="left") == DVRIP_STOP
    assert build_command(config, "stop") == DVRIP_STOP
    assert build_command(config, "preset", preset="3") == (
        'DVRIP {"Command":"GotoPreset","Preset":3,"Channel":1}'
    )


def test_the_api_says_whether_a_stop_halts_a_move() -> None:
    """A panel has to know whether a stop ends a move, or drags the camera back to a position.

    Every direction command of the Xiongmai family drives an axis to its limit, and no payload
    of the family halts one on the way: a ``Stop`` of any shape, ``Step: 0``, ``Preset: -1``
    (the stop of ``dbuezas/icsee-ptz``), ``Preset: 65504`` and ``Pattern: "Stop"`` were all
    measured on the device, on the session of the move as well (``.smoke/dvrip-protocol.md``).
    The stop of the bundled profile is a *capture* for that reason, and a hold on such an axis
    has to walk the ladder of stored positions instead of starting a move it cannot end - so
    the panel is told which kind of stop the camera has.
    """
    dvrip = normalize_ptz({"profile": "xiongmai_dvrip"}, RTSP_URL)
    assert dvrip is not None
    assert stops_in_place(dvrip) is False
    masked = public_config(dvrip)
    assert masked is not None and masked["stops_in_place"] is False

    # A stop that stores a position cannot halt anything, whatever slot it names.
    other_slot = normalize_ptz(
        {
            "commands": {
                "stop": 'DVRIP {"Command":"SetPreset","Preset":7,"Channel":1} '
                'DVRIP {"Command":"GotoPreset","Preset":7,"Channel":1}'
            }
        },
        RTSP_URL,
    )
    assert other_slot is not None and stops_in_place(other_slot) is False

    # Every other transport stops an axis where it stands, and so does a plain DVRIP stop
    # that a user wrote by hand - the panel keeps the continuous move there.
    for profile in ("axis", "dahua", "onvif", "xiongmai"):
        config = normalize_ptz({"profile": profile}, RTSP_URL)
        assert config is not None, profile
        assert stops_in_place(config) is True, profile

    plain = normalize_ptz(
        {"commands": {"stop": 'DVRIP {"Command":"Stop","Step":0,"Channel":1}'}}, RTSP_URL
    )
    assert plain is not None and stops_in_place(plain) is True
    assert stops_in_place(None) is True, "a camera without a stop claims nothing"


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
                "up": 'DVRIP {"Command":"SetPreset","Preset":200,"Channel":1} '
                'DVRIP {"Command":"DirectionUp","Step":{speed_vertical},"Channel":1}',
                "left": 'DVRIP {"Command":"DirectionLeft","Step":{speed},"Channel":1,'
                '"Preset":7}',
            },
        },
        RTSP_URL,
    )

    assert config is not None
    assert config["commands"]["stop"] == DVRIP_STOP
    assert config["commands"]["down"] == (
        'DVRIP {"Command":"DirectionDown","Step":{speed_vertical},"Channel":1}'
    )
    # Up to 0.3.9 every move carried the ``SetPreset`` its stop returned to; the stop of
    # 0.3.10 captures the position itself, so the store goes away with the upgrade.
    assert config["commands"]["up"] == (
        'DVRIP {"Command":"DirectionUp","Step":{speed_vertical},"Channel":1}'
    )
    # A command this project never shipped is left alone.
    assert config["commands"]["left"] == (
        'DVRIP {"Command":"DirectionLeft","Step":{speed},"Channel":1,"Preset":7}'
    )


def test_the_stop_of_previous_releases_is_replaced_as_well() -> None:
    """The stops of 0.3.6 to 0.3.9 are templates of this project too.

    The short form ``{"Command":"Stop","Step":0}`` of 0.3.6 is ignored while a move runs,
    the ``POINT`` object of 0.3.7 is refused with ``Ret: 118`` (both measured on
    192.168.20.253, see the changelogs), and the ``GotoPreset`` of 0.3.8/0.3.9 returned a
    released arrow to the position of the *press* instead of the position of the release -
    so a camera that stored any of them is moved over to the capture of 0.3.10 like one
    that still carries the guessed template of 0.3.5.
    """
    for stop in (
        'DVRIP {"Command":"GotoPreset","Preset":200,"Channel":1}',
        'DVRIP {"Command":"Stop","Step":0,"Channel":1}',
        'DVRIP {"Name":"OPPTZControl","OPPTZControl":{"Command":"Stop","Parameter":'
        '{"POINT":{"bottom":0,"left":0,"right":0,"top":0},"Step":0,"Channel":1}}}',
    ):
        config = normalize_ptz(
            {"profile": "xiongmai_dvrip", "commands": {"stop": stop}}, RTSP_URL
        )

        assert config is not None
        assert config["commands"]["stop"] == DVRIP_STOP


def test_the_commands_of_the_test_camera_are_upgraded() -> None:
    """The commands a camera of the test set carries are the ones of 0.3.9.

    Copied out of ``/api/cameras/balkon-2`` of the running add-on, so the upgrade is
    checked against the real file and not only against the templates of this project: the
    moves carry the ``SetPreset`` of 0.3.8/0.3.9 and the stop is the ``GotoPreset`` of that
    slot, which returned a released arrow to the position of the *press*.
    """
    stored = {
        "up": 'DVRIP {"Command":"SetPreset","Preset":200,"Channel":1} '
        'DVRIP {"Command":"DirectionUp","Step":{speed_vertical},"Channel":1}',
        "down": 'DVRIP {"Command":"SetPreset","Preset":200,"Channel":1} '
        'DVRIP {"Command":"DirectionDown","Step":{speed_vertical},"Channel":1}',
        "left": 'DVRIP {"Command":"SetPreset","Preset":200,"Channel":1} '
        'DVRIP {"Command":"DirectionLeft","Step":{speed_horizontal},"Channel":1}',
        "right": 'DVRIP {"Command":"SetPreset","Preset":200,"Channel":1} '
        'DVRIP {"Command":"DirectionRight","Step":{speed_horizontal},"Channel":1}',
        "zoom_in": 'DVRIP {"Command":"SetPreset","Preset":200,"Channel":1} '
        'DVRIP {"Command":"ZoomTile","Step":{speed},"Channel":1}',
        "zoom_out": 'DVRIP {"Command":"SetPreset","Preset":200,"Channel":1} '
        'DVRIP {"Command":"ZoomWide","Step":{speed},"Channel":1}',
        "stop": 'DVRIP {"Command":"GotoPreset","Preset":200,"Channel":1}',
        "preset": 'DVRIP {"Command":"GotoPreset","Preset":{preset},"Channel":1}',
    }
    config = normalize_ptz({"profile": "xiongmai_dvrip", "commands": stored}, RTSP_URL)

    assert config is not None
    assert config["commands"]["up"] == (
        'DVRIP {"Command":"DirectionUp","Step":{speed_vertical},"Channel":1}'
    )
    assert config["commands"]["left"] == (
        'DVRIP {"Command":"DirectionLeft","Step":{speed_horizontal},"Channel":1}'
    )
    assert config["commands"]["zoom_out"] == 'DVRIP {"Command":"ZoomWide","Step":{speed},"Channel":1}'
    assert config["commands"]["stop"] == DVRIP_STOP
    # A preset of a user is not a template of this project and stays as it is.
    assert config["commands"]["preset"] == stored["preset"]


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
    """Handle one connection: the login, then every payload of the command.

    A command of this family may carry more than one payload, and the client sends all of
    them on the connection of their login - the session is what a device drives the axis of -
    so this device answers PTZ messages until the client closes its end of the connection.
    """

    def handle(self) -> None:
        """Answer the login and every PTZ message that follows on that connection."""
        _message_id, _session, payload = read_message(self.request)
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

        while True:
            try:
                message_id, session, payload = read_message(self.request)
            except AssertionError:  # the client is done with the command and disconnected
                return
            DvripCamera.received.append(
                {"message_id": message_id, "session": session, "payload": payload}
            )
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


def read_message(sock: Any) -> tuple[int, int, dict[str, Any]]:
    """Read a complete DVRIP message: message id, session and payload."""
    header = _read_exactly(sock, DVRIP_HEADER_SIZE)
    parsed = struct.unpack(DVRIP_HEADER, header)
    body = _read_exactly(sock, parsed[7]) if parsed[7] else b"{}"
    return parsed[6], parsed[2], json.loads(body.decode("utf-8"))


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

    # A move of the bundled profile is one payload since 0.3.10: the direction and the
    # speed of its axis, nothing else (the store belongs to the stop now).
    assert len(DvripCamera.received) == 1
    sent = DvripCamera.received[0]
    assert sent["message_id"] == MSG_PTZ_REQUEST
    assert sent["payload"]["Name"] == "OPPTZControl"
    assert sent["payload"]["OPPTZControl"]["Command"] == "DirectionLeft"
    assert sent["payload"]["OPPTZControl"]["Parameter"]["Step"] == 6
    assert sent["payload"]["OPPTZControl"]["Parameter"]["Channel"] == 0
    assert sent["payload"]["SessionID"] == f"0x{DVRIP_SESSION:08X}"


def test_dvrip_stop_captures_the_position_it_is_sent_at(dvrip_cam: int) -> None:
    """The stop of the bundled profile stores the position and returns to it.

    This family has no payload that halts a moving axis, so the stop of its profile is a
    *capture*: the ``SetPreset`` of the release is served immediately - a running axis
    included - and the ``GotoPreset`` behind it brings the camera back to the position it
    stored. Two payloads on **one session**, no direction, measured on the device (see the
    changelog of 0.3.10 and of 0.3.12).
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
    assert len(DvripCamera.received) == 2
    assert len(DvripCamera.logins) == 1, "both payloads ride the session of one login"
    assert [one["session"] for one in DvripCamera.received] == [DVRIP_SESSION] * 2
    assert [one["payload"]["SessionID"] for one in DvripCamera.received] == [
        f"0x{DVRIP_SESSION:08X}"
    ] * 2
    store = DvripCamera.received[0]
    assert store["payload"]["OPPTZControl"]["Command"] == "SetPreset"
    assert store["payload"]["OPPTZControl"]["Parameter"]["Preset"] == 201
    assert store["payload"]["OPPTZControl"]["Parameter"]["Channel"] == 0
    sent = DvripCamera.received[1]
    assert sent["payload"]["OPPTZControl"]["Command"] == "GotoPreset"
    assert sent["payload"]["OPPTZControl"]["Parameter"]["Preset"] == 201
    assert sent["payload"]["OPPTZControl"]["Parameter"]["Channel"] == 0


def test_dvrip_gives_every_payload_of_a_command_its_own_deadline() -> None:
    """The deadline of this transport belongs to an answer, not to the whole command.

    The stop of the bundled profile is two payloads, and the second one - the
    ``GotoPreset`` - is answered only once the axis arrived: measured 10 to 16 s on the
    device of the test set. A single deadline for the sequence would cut that answer off and
    report a command that works as a failure.
    """
    store = [{"Command": "SetPreset", "Preset": 201, "Channel": 1}]
    both = [*store, {"Command": "GotoPreset", "Preset": 201, "Channel": 1}]

    assert sequence_timeout(DVRIP_TIMEOUT, store) == DVRIP_TIMEOUT
    assert sequence_timeout(DVRIP_TIMEOUT, both) == DVRIP_TIMEOUT * 2
    assert sequence_timeout(DVRIP_TIMEOUT, []) == DVRIP_TIMEOUT, "a command without a payload"


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
    # The command is one login: a refused login type is asked again on a fresh connection,
    # and the accepted one carries both payloads of the stop.
    assert [login["LoginType"] for login in DvripCamera.logins] == [
        "DVRIP-Web",
        "DVRIP-Mobile",
    ]
    assert len(DvripCamera.received) == 2


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
    # The probe asks with the *stop* command of the profile, which keeps the camera where
    # it stands: the capture of the position first, the return to it behind it.
    assert DvripCamera.received[0]["payload"]["OPPTZControl"]["Command"] == "SetPreset"
    assert DvripCamera.received[0]["payload"]["OPPTZControl"]["Parameter"]["Preset"] == 201
    assert DvripCamera.received[1]["payload"]["OPPTZControl"]["Command"] == "GotoPreset"


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


DVRIP_URL = "rtsp://admin:secret@127.0.0.1:554/user=admin&password=secret&channel=1&stream=0.sdp"


def add_dvrip_camera(test_client: TestClient, port: int) -> dict:
    """Create a camera whose PTZ handling is the DVRIP device under test."""
    return add_camera(test_client, url=DVRIP_URL, ptz={"profile": "xiongmai_dvrip", "port": port})


def store_rungs(test_client: TestClient, camera_id: str, count: int) -> dict:
    """Store ``count`` rungs on one axis and return the answer of the last one."""
    answer: dict = {}
    for _ in range(count):
        response = test_client.post(
            f"/api/cameras/{camera_id}/ptz/steps", json={"axis": "vertical", "mode": "add"}
        )
        assert response.status_code == 200, response.text
        answer = response.json()
    return answer


def store_pan_rungs(test_client: TestClient, camera_id: str, count: int) -> dict:
    """Store ``count`` rungs of the pan ladder and return the answer of the last one."""
    answer: dict = {}
    for _ in range(count):
        response = test_client.post(
            f"/api/cameras/{camera_id}/ptz/steps", json={"axis": "horizontal", "mode": "add"}
        )
        assert response.status_code == 200, response.text
        answer = response.json()
    return answer


def forget_rung(test_client: TestClient, camera_id: str) -> None:
    """Move the camera outside its ladders, so the add-on forgets where it stands."""
    response = test_client.post(f"/api/cameras/{camera_id}/ptz", json={"action": "down"})
    assert response.status_code == 200, response.text


def test_the_panel_stores_a_step_position(client: TestClient, dvrip_cam: int) -> None:
    """The store button writes the position of the moment into the next free slot."""
    camera = add_dvrip_camera(client, dvrip_cam)

    response = client.post(
        f"/api/cameras/{camera['id']}/ptz/steps", json={"axis": "vertical", "mode": "add"}
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["slot"] == FIRST_STEP_SLOT
    assert data["steps"] == {"vertical": [FIRST_STEP_SLOT]}
    assert data["index"] == 0, "the camera stands on the rung that was just written"
    assert data["camera"]["ptz"]["steps"] == {"vertical": [FIRST_STEP_SLOT]}, (
        "the ladder is part of the camera the panel gets back"
    )
    sent = DvripCamera.received[0]["payload"]["OPPTZControl"]
    assert sent["Command"] == "SetPreset"
    assert sent["Parameter"]["Preset"] == FIRST_STEP_SLOT
    assert sent["Parameter"]["Channel"] == 0

    second = client.post(
        f"/api/cameras/{camera['id']}/ptz/steps", json={"axis": "vertical", "mode": "add"}
    ).json()
    assert second["slot"] == FIRST_STEP_SLOT + 1, "a second rung takes the next free slot"
    assert second["steps"] == {"vertical": [FIRST_STEP_SLOT, FIRST_STEP_SLOT + 1]}

    # The rungs of the other axis are a ladder of their own, and a preset is a whole
    # position: a slot the tilt already walks may not carry a pan rung as well.
    horizontal = client.post(
        f"/api/cameras/{camera['id']}/ptz/steps", json={"axis": "horizontal", "mode": "add"}
    ).json()
    assert horizontal["slot"] == FIRST_STEP_SLOT + 2, "a slot belongs to one axis"
    assert horizontal["steps"] == {
        "vertical": [FIRST_STEP_SLOT, FIRST_STEP_SLOT + 1],
        "horizontal": [FIRST_STEP_SLOT + 2],
    }


def test_a_step_walks_to_the_next_rung(client: TestClient, dvrip_cam: int) -> None:
    """A step of the panel is a ``GotoPreset``: a bounded move, no travel to a limit."""
    camera = add_dvrip_camera(client, dvrip_cam)
    store_rungs(client, camera["id"], 3)
    DvripCamera.received.clear()

    # Three rungs were stored, so the camera stands on the last one of them.
    down = client.post(
        f"/api/cameras/{camera['id']}/ptz",
        json={"action": "step", "direction": "down", "steps": 2},
    )

    assert down.status_code == 200, down.text
    assert down.json()["step"] == {
        "axis": "vertical",
        "slot": FIRST_STEP_SLOT,
        "index": 0,
        "rungs": 3,
        "arrow": "down",
        # The coordinates of the camera after the move: the rung it reached, and the crossing
        # rung of the other axis - a rung is a whole position, so it always writes both.
        "position": {
            "vertical": {
                "index": 0,
                "slot": FIRST_STEP_SLOT,
                "rungs": 3,
                "crossing": 1,
            }
        },
    }
    sent = DvripCamera.received[0]["payload"]["OPPTZControl"]
    assert sent["Command"] == "GotoPreset", "a step does not start a move"
    assert sent["Parameter"]["Preset"] == FIRST_STEP_SLOT

    # The rung the camera reached is remembered, so the next step starts from there.
    up = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "step", "direction": "up"})
    assert up.status_code == 200, up.text
    assert up.json()["step"]["index"] == 1
    assert up.json()["step"]["slot"] == FIRST_STEP_SLOT + 1


def test_a_continuous_move_forgets_the_rung(client: TestClient, dvrip_cam: int) -> None:
    """The ladder only counts while the add-on knows the rung the camera stands on."""
    camera = add_dvrip_camera(client, dvrip_cam)
    store_rungs(client, camera["id"], 3)

    moved = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "down"})
    assert moved.status_code == 200, moved.text

    step = client.post(
        f"/api/cameras/{camera['id']}/ptz", json={"action": "step", "direction": "up"}
    )

    assert step.status_code == 200, step.text
    assert step.json()["step"]["index"] == 2, (
        "a step without a known rung starts in the middle of the ladder and not at its "
        "first rung - a walk from the end would sweep the whole axis on the first press"
    )


def test_a_step_that_drags_the_other_axis_is_sent_and_reported(
    client: TestClient, dvrip_cam: int
) -> None:
    """A step that carries the other axis along is sent, and the answer says what it drags.

    A rung is a whole position: the tilt rungs of a camera are stored at one pan and its pan
    rungs at one tilt, so a step that switches axes carries the other axis back to the
    crossing of the two ladders. Measured on the test set, a ``right`` of three rungs followed
    by an ``up`` sent the pan back to the column the tilt rungs were stored at - the view the
    user had just panned to was gone.

    This family has no bounded *relative* move, so there is no tilt in place to offer: a rung
    can only be written where the camera already stands (see ``ptz.step_target``). The step is
    therefore sent, and the answer reports the drag instead of refusing the step - a refusal
    is what made both pairs of arrows dead in 0.7.8 (see the test below).
    """
    camera = add_dvrip_camera(client, dvrip_cam)
    store_rungs(client, camera["id"], 3)
    store_pan_rungs(client, camera["id"], 3)
    # Storing a rung says where the camera stands; pointing it somewhere else forgets that,
    # and the next step starts on the crossing of the two ladders.
    forget_rung(client, camera["id"])

    up = client.post(
        f"/api/cameras/{camera['id']}/ptz", json={"action": "step", "direction": "up", "steps": 9}
    )
    assert up.status_code == 200, up.text
    assert up.json()["step"]["index"] == 2, "the top rung of the tilt, off the crossing"
    assert "drag" not in up.json()["step"], "the tilt leaves the pan on its crossing"

    sent = len(DvripCamera.received)
    right = client.post(
        f"/api/cameras/{camera['id']}/ptz", json={"action": "step", "direction": "right"}
    )

    assert right.status_code == 200, right.text
    assert right.json()["step"]["drag"] == {"axis": "vertical", "rungs": -1}, (
        "the pan rung carries the tilt back to the middle rung of its ladder - one rung down "
        "- and the answer says so instead of leaving the user with a silent jump"
    )
    assert len(DvripCamera.received) == sent + 1, "the step is sent, not refused"

    again = client.post(
        f"/api/cameras/{camera['id']}/ptz", json={"action": "step", "direction": "right"}
    )
    assert "drag" not in again.json()["step"], (
        "the camera stands on the crossing of the tilt again, so the next pan rung drags nothing"
    )


def test_a_step_of_one_axis_never_locks_the_other_out(client: TestClient, dvrip_cam: int) -> None:
    """Both pairs of arrows stay usable, whichever pair was pressed last.

    A rung of one ladder carries the middle value of the other axis, so landing on it leaves
    that axis on the crossing - known, and walkable. 0.7.8 forgot the other axis instead and
    refused its next step as if the camera stood between its rungs; since every step recorded
    the axis it walked, the two pairs locked each other out. Measured on the test set with the
    ladders of ``balkon-2``: ``preset 222 -> up (200) -> right (409) -> right (409) -> up
    (200)`` - neither pair could be pressed twice in a row, and the arrows of an axis looked
    dead as soon as the other one had moved.
    """
    camera = add_dvrip_camera(client, dvrip_cam)
    store_rungs(client, camera["id"], 5)
    store_pan_rungs(client, camera["id"], 5)
    forget_rung(client, camera["id"])

    def step(direction: str) -> dict:
        answer = client.post(
            f"/api/cameras/{camera['id']}/ptz", json={"action": "step", "direction": direction}
        )
        assert answer.status_code == 200, f"{direction}: {answer.text}"
        return answer.json()["step"]

    assert step("up")["index"] == 3, "one rung above the crossing of both ladders"
    right = step("right")
    assert right["index"] == 3, "the pan steps although the tilt was stepped first"
    assert right["drag"] == {"axis": "vertical", "rungs": -1}, "the tilt is reported as dragged"
    assert step("right")["index"] == 4, "... and the next pan rung drags nothing"

    up = step("up")
    assert up["index"] == 3, "the tilt steps although the pan was stepped first"
    assert up["drag"] == {"axis": "horizontal", "rungs": -2}, (
        "the tilt rung brings the pan back to the column the tilt ladder is stored at"
    )
    assert step("down")["index"] == 2, "... and the tilt keeps walking from there"


def test_a_step_starts_from_the_crossing_the_camera_file_names(
    client: TestClient, dvrip_cam: int
) -> None:
    """The coordinates of a camera come from its file, not from the shape of a ladder.

    A ladder is stored along one axis at a fixed value of the other one, so a rung written
    later moves the *middle* of the list but not that place (see ``ptz.crossing_rung``). The
    add-on names the place in ``step_crossings``: with the middle used instead, the ``up``
    below would start two rungs above the pan rung the camera stands on, and it would report a
    drag that never happened - which is what the ladder of ``balkon-2`` did (its tilt ladder
    grew by a rung at the ground, and every step off the pan ladder jumped two rungs).
    """
    camera = add_dvrip_camera(client, dvrip_cam)
    store_rungs(client, camera["id"], 6)
    store_pan_rungs(client, camera["id"], 5)
    block = client.get(f"/api/cameras/{camera['id']}").json()["camera"]["ptz"]
    block["step_crossings"] = {"vertical": 3, "horizontal": 1}
    saved = client.put(f"/api/cameras/{camera['id']}", json={"ptz": block})
    assert saved.status_code == 200, saved.text
    assert saved.json()["camera"]["ptz"]["step_crossings"] == {"vertical": 3, "horizontal": 1}
    vertical = block["steps"]["vertical"]
    horizontal = block["steps"]["horizontal"]

    forget_rung(client, camera["id"])
    # A preset that is a rung of the pan ladder: the camera stands on it, and the tilt on the
    # crossing rung of its own ladder - the coordinate the next step starts from.
    preset = client.post(
        f"/api/cameras/{camera['id']}/ptz",
        json={"action": "preset", "preset": str(horizontal[1])},
    )
    assert preset.status_code == 200, preset.text
    assert preset.json()["position"] == {
        "vertical": {"index": 3, "slot": vertical[3], "rungs": 6, "crossing": 3},
        "horizontal": {"index": 1, "slot": horizontal[1], "rungs": 5, "crossing": 1},
    }, "a preset writes both coordinates: the rung it is, and the crossing of the other axis"

    up = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "step", "direction": "up"})
    assert up.status_code == 200, up.text
    assert up.json()["step"]["index"] == 4, "one rung above the crossing, not above the middle"
    assert "drag" not in up.json()["step"], "the pan stands on its crossing, so nothing drags"
    assert up.json()["step"]["position"] == {
        "vertical": {"index": 4, "slot": vertical[4], "rungs": 6, "crossing": 3},
        "horizontal": {"index": 1, "slot": horizontal[1], "rungs": 5, "crossing": 1},
    }

    right = client.post(
        f"/api/cameras/{camera['id']}/ptz", json={"action": "step", "direction": "right"}
    )
    assert right.json()["step"]["drag"] == {"axis": "vertical", "rungs": -1}, (
        "the pan rung carries the tilt back to the crossing named by the file"
    )
    assert right.json()["step"]["position"] == {
        "vertical": {"index": 3, "slot": vertical[3], "rungs": 6, "crossing": 3},
        "horizontal": {"index": 2, "slot": horizontal[2], "rungs": 5, "crossing": 1},
    }, "and both coordinates follow the move"


def test_a_continuous_move_forgets_both_coordinates(client: TestClient, dvrip_cam: int) -> None:
    """A move that this add-on cannot place leaves the camera between two rungs of each axis.

    Only a rung the camera is *at* can be walked on, and a direction command drives an axis to
    its limit: nothing of that move is a coordinate, so the answer of the move says the
    position is unknown instead of keeping a number that is no longer true.
    """
    camera = add_dvrip_camera(client, dvrip_cam)
    store_rungs(client, camera["id"], 3)

    moved = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "up"})

    assert moved.status_code == 200, moved.text
    assert moved.json()["position"] == {
        "vertical": {"index": None, "slot": None, "rungs": 3, "crossing": 1}
    }


def test_a_mirrored_axis_sends_the_other_command(client: TestClient, ptz_cam: str) -> None:
    """A camera that is mounted the other way round moves where the arrow of the picture points.

    ``invert_horizontal`` turns the pan around, so a ``right`` of the panel sends the command
    of ``left`` - and the stop names the axis that really moved, not the one the panel asked
    for: a stop of the wrong axis does not stop a camera, it moves it.
    """
    camera = add_camera(
        client,
        url=RTSP_URL,
        ptz={"profile": "dahua", "base_url": ptz_cam, "invert_horizontal": True},
    )

    moved = client.post(f"/api/cameras/{camera['id']}/ptz", json={"action": "right"})
    stopped = client.post(
        f"/api/cameras/{camera['id']}/ptz", json={"action": "stop", "direction": "right"}
    )

    assert moved.status_code == 200, moved.text
    assert "code=Left" in moved.json()["ptz"]["command"], "the mirrored right is a left"
    assert stopped.status_code == 200, stopped.text
    assert "code=Left" in stopped.json()["ptz"]["command"], (
        "the stop names the axis that really moved"
    )


def test_a_mirrored_axis_walks_its_ladder_the_other_way(
    client: TestClient, dvrip_cam: int
) -> None:
    """A mirrored axis walks its rungs backwards, so the arrow matches the picture.

    A rung is a position, so the ladder is stored in the order the camera is walked along it:
    turning the arrows of the axis around has to turn the walk around as well, or a ``up`` of
    the picture would still climb the ladder the other way.
    """
    camera = add_dvrip_camera(client, dvrip_cam)
    store_rungs(client, camera["id"], 3)
    mirrored = client.post(
        f"/api/cameras/{camera['id']}/ptz/invert", json={"axis": "vertical"}
    )
    assert mirrored.status_code == 200, mirrored.text
    assert mirrored.json()["inverted"] is True
    DvripCamera.received.clear()

    step = client.post(
        f"/api/cameras/{camera['id']}/ptz", json={"action": "step", "direction": "up"}
    )

    assert step.status_code == 200, step.text
    assert step.json()["step"]["axis"] == "vertical"
    assert step.json()["step"]["arrow"] == "down", "the mirrored up is a down"
    assert step.json()["step"]["index"] == 1, (
        "the camera stood on the top rung of its ladder and walks one rung down"
    )


def test_the_panel_mirrors_an_axis_on_the_camera(
    client: TestClient, settings: Settings, dvrip_cam: int
) -> None:
    """The mirror button of the pad flips the axis and keeps it on the camera.

    The flag is a property of the camera, not of the pad that happens to press it: the entity
    of Home Assistant and every automation follow it, so it is stored in the PTZ block - which
    is also what the camera file the integration reads is built from.
    """
    camera = add_dvrip_camera(client, dvrip_cam)

    first = client.post(f"/api/cameras/{camera['id']}/ptz/invert", json={"axis": "horizontal"})
    assert first.status_code == 200, first.text
    assert first.json()["inverted"] is True
    assert first.json()["camera"]["ptz"]["invert_horizontal"] is True
    assert first.json()["camera"]["ptz"]["invert_vertical"] is False

    # A body without the value flips the axis - that is what the button does.
    second = client.post(f"/api/cameras/{camera['id']}/ptz/invert", json={"axis": "horizontal"})
    assert second.json()["inverted"] is False

    # A body with it sets the flag, which is what a script that knows the bracket wants.
    set_value = client.post(
        f"/api/cameras/{camera['id']}/ptz/invert",
        json={"axis": "vertical", "inverted": True},
    )
    assert set_value.json()["inverted"] is True
    assert set_value.json()["camera"]["ptz"]["invert_vertical"] is True

    # The camera file the integration reads carries the flag as well.
    published = json.loads(settings.published_file.read_text(encoding="utf-8"))["cameras"]
    assert published[0]["ptz"]["invert_vertical"] is True
    assert published[0]["ptz"]["invert_horizontal"] is False


def test_the_mirror_endpoint_reports_an_unknown_axis(client: TestClient, dvrip_cam: int) -> None:
    """Only the two axes of a camera can be mirrored."""
    camera = add_dvrip_camera(client, dvrip_cam)

    response = client.post(f"/api/cameras/{camera['id']}/ptz/invert", json={"axis": "zoom"})

    assert response.status_code == 400, response.text
    assert response.json()["error"] == "ptz_axis_required"



def test_a_step_without_a_ladder_is_refused(client: TestClient, dvrip_cam: int) -> None:
    """A camera without rungs cannot step: the panel is told instead of moving nothing."""
    camera = add_dvrip_camera(client, dvrip_cam)

    response = client.post(
        f"/api/cameras/{camera['id']}/ptz", json={"action": "step", "direction": "up"}
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"] == "ptz_steps_missing"


def test_the_panel_drops_and_clears_the_ladder(client: TestClient, dvrip_cam: int) -> None:
    """A stored rung can be taken back, and a whole axis can be forgotten."""
    camera = add_dvrip_camera(client, dvrip_cam)
    store_rungs(client, camera["id"], 2)

    dropped = client.post(
        f"/api/cameras/{camera['id']}/ptz/steps", json={"axis": "vertical", "mode": "drop"}
    )
    assert dropped.status_code == 200, dropped.text
    assert dropped.json()["steps"] == {"vertical": [FIRST_STEP_SLOT]}

    last = client.post(
        f"/api/cameras/{camera['id']}/ptz/steps", json={"axis": "vertical", "mode": "drop"}
    )
    assert last.json()["steps"] == {}, "the last rung of an axis removes the ladder"

    store_rungs(client, camera["id"], 1)
    cleared = client.post(
        f"/api/cameras/{camera['id']}/ptz/steps", json={"axis": "vertical", "mode": "clear"}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["steps"] == {}
    assert cleared.json()["camera"]["ptz"]["steps"] == {}


def test_the_camera_editor_keeps_the_ladder(client: TestClient, dvrip_cam: int) -> None:
    """A save of the camera form has no field for the ladder and must not lose it."""
    camera = add_dvrip_camera(client, dvrip_cam)
    store_rungs(client, camera["id"], 1)

    saved = client.put(
        f"/api/cameras/{camera['id']}",
        json={"ptz": {"enabled": True, "profile": "xiongmai_dvrip", "port": dvrip_cam, "speed": 5}},
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["camera"]["ptz"]["steps"] == {"vertical": [FIRST_STEP_SLOT]}
    assert saved.json()["camera"]["ptz"]["speed"] == 5, "the new field of the form arrived"


def test_a_camera_without_a_store_cannot_grow_a_ladder(client: TestClient) -> None:
    """The store button of the panel is refused where the profile has no store command."""
    camera = add_camera(client, ptz={"profile": "dahua", "base_url": "http://10.0.0.5"})

    response = client.post(
        f"/api/cameras/{camera['id']}/ptz/steps", json={"axis": "vertical", "mode": "add"}
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"] == "ptz_store_missing"


def test_a_full_ladder_is_reported(client: TestClient, dvrip_cam: int) -> None:
    """A ladder has a last rung, and the panel is told instead of overwriting one."""
    camera = add_dvrip_camera(client, dvrip_cam)
    store_rungs(client, camera["id"], MAX_STEP_SLOTS)

    response = client.post(
        f"/api/cameras/{camera['id']}/ptz/steps", json={"axis": "vertical", "mode": "add"}
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"] == "ptz_steps_full"

# --------------------------------------------------------------------- steps
# A step by step arrow needs a *position*. This family drives an axis to its limit and
# nothing halts it on the way (measured: every shape of a ``Stop`` is acknowledged and
# ignored, ``Step: 0`` with a direction drives the axis to its zero position), so the only
# bounded move the device has is the ``GotoPreset`` of a slot a position was written into
# before - a **rung** of a ladder. These tests pin the rules of that ladder: who may store a
# rung, how a step walks it, and when it is lost.


def test_the_dvrip_profile_can_store_a_position(dvrip_cam: int) -> None:
    """The ``store`` action writes the position of the moment into the slot of the request."""
    config = normalize_ptz({"profile": "xiongmai_dvrip", "port": dvrip_cam}, RTSP_URL)
    assert config is not None
    assert "store" in configured_actions(config), "a ladder needs a slot a position can go to"

    command = build_command(config, "store", preset="212")
    assert command is not None
    status, error = asyncio.run(
        async_send(command, host="127.0.0.1", port=dvrip_cam, username="user", password="pass")
    )

    assert error is None, error
    assert status == LOGIN_OK
    sent = DvripCamera.received[0]["payload"]["OPPTZControl"]
    assert sent["Command"] == "SetPreset", "the store of the capture stop, not a move"
    assert sent["Parameter"]["Preset"] == 212
    assert sent["Parameter"]["Channel"] == 0


def test_a_profile_without_a_store_cannot_build_a_ladder() -> None:
    """The store button is offered only where the command exists."""
    config = normalize_ptz({"profile": "dahua", "base_url": "http://10.0.0.5"}, RTSP_URL)
    assert config is not None
    assert "store" not in configured_actions(config)


def test_a_ladder_survives_a_save_of_the_camera_editor() -> None:
    """The camera form has no field for the ladder, so saving the camera keeps it."""
    ladder = {"vertical": [210, 211], "horizontal": ["212"]}
    first = normalize_ptz({"profile": "xiongmai_dvrip", "steps": ladder}, RTSP_URL)
    assert first is not None
    assert first["steps"] == {"vertical": [210, 211], "horizontal": [212]}
    assert public_config(first)["steps"] == first["steps"], "the panel has to see the rungs"

    # What the camera editor sends: the fields it knows, no ``steps`` at all.
    saved = normalize_ptz({"profile": "xiongmai_dvrip", "speed": 6}, RTSP_URL, first)
    assert saved is not None
    assert saved["steps"] == first["steps"], "a form that does not speak of rungs keeps them"

    # The step endpoint sends the whole block, so an empty ladder clears the camera.
    cleared = normalize_ptz({"profile": "xiongmai_dvrip", "steps": {}}, RTSP_URL, first)
    assert cleared is not None
    assert cleared["steps"] == {}


def test_a_mirror_survives_a_save_of_the_camera_editor() -> None:
    """The flags of a mirrored axis are kept when the editor saves the camera.

    The mirror is a button of the preview pad, so a camera editor that does not speak about
    the flags at all - a hand written file, or the form of an older release - keeps what the
    camera carries, exactly like the ladder of step positions.
    """
    first = normalize_ptz({"profile": "xiongmai_dvrip", "invert_horizontal": True}, RTSP_URL)
    assert first is not None
    assert first["invert_horizontal"] is True
    assert first["invert_vertical"] is False
    assert public_config(first)["invert_horizontal"] is True, "the pad has to see the flag"
    assert public_config(first)["invert_vertical"] is False

    saved = normalize_ptz({"profile": "xiongmai_dvrip", "speed": 6}, RTSP_URL, first)
    assert saved is not None
    assert saved["invert_horizontal"] is True, "a block without the flag keeps it"
    assert saved["invert_vertical"] is False

    off = normalize_ptz(
        {"profile": "xiongmai_dvrip", "invert_horizontal": False}, RTSP_URL, first
    )
    assert off is not None
    assert off["invert_horizontal"] is False, "a flag that is sent wins over the camera"


def test_the_flags_turn_the_arrows_of_their_axis_around() -> None:
    """The mirror of an axis swaps its two arrows and leaves everything else alone."""
    config = normalize_ptz(
        {
            "profile": "xiongmai_dvrip",
            "invert_horizontal": "true",
            "invert_vertical": 1,
        },
        RTSP_URL,
    )
    assert config is not None

    assert apply_inversion(config, "left") == "right"
    assert apply_inversion(config, "right") == "left"
    assert apply_inversion(config, "up") == "down"
    assert apply_inversion(config, "down") == "up"
    for untouched in ("zoom_in", "zoom_out", "home", "stop", "preset", "store"):
        assert apply_inversion(config, untouched) == untouched
    assert apply_inversion(None, "left") == "left"
    assert apply_inversion({}, "left") == "left"


    """Garbage in a ladder is dropped: no step may ever point at a preset of the panel."""
    config = normalize_ptz(
        {
            "profile": "xiongmai_dvrip",
            "steps": {
                "vertical": [210, "211", 211, 3, 200, 201, 300, "x", 250],
                "horizontal": "not a list",
                "diagonal": [212],
                "sideways": [],
            },
        },
        RTSP_URL,
    )

    assert config is not None
    assert config["steps"] == {"vertical": [210, 211]}, (
        "the presets 3, 200 and 201 belong to the user and to this project, 300 and 250 are "
        "out of range, 211 is written twice, and a value that is not a number is dropped"
    )


def test_a_long_ladder_is_cut() -> None:
    """A ladder holds ``MAX_STEP_SLOTS`` rungs."""
    slots = list(range(FIRST_STEP_SLOT, FIRST_STEP_SLOT + MAX_STEP_SLOTS + 4))
    config = normalize_ptz({"profile": "xiongmai_dvrip", "steps": {"vertical": slots}}, RTSP_URL)

    assert config is not None
    assert config["steps"]["vertical"] == slots[:MAX_STEP_SLOTS]


def test_a_slot_of_one_axis_is_no_rung_of_the_other() -> None:
    """A preset is a whole position, so a slot named for two axes is kept once.

    The device keeps one namespace of presets: a pan rung stored under a slot the tilt
    already walks would put the camera at a rung of the *other* ladder. Exactly that
    happened to the two ladders of the test set, which ended up in the same slots.
    """
    config = normalize_ptz(
        {
            "profile": "xiongmai_dvrip",
            "steps": {"vertical": [210, 211], "horizontal": [211, 212, "212", 213]},
        },
        RTSP_URL,
    )

    assert config is not None
    assert config["steps"] == {"vertical": [210, 211], "horizontal": [212, 213]}


def test_a_step_walks_the_ladder_of_its_axis() -> None:
    """Up and right walk a ladder away from its first rung, down and left back towards it."""
    slots = [210, 211, 212]

    assert step_target(slots, 0, "up", 1) == (211, 1)
    assert step_target(slots, 1, "down", 2) == (210, 0)
    assert step_target(slots, 0, "right", 1) == (211, 1), "the pan ladder is walked the same way"
    assert step_target(slots, 0, "down", 1) == (210, 0), "a step never leaves the ladder"
    assert step_target(slots, 2, "up", 1) == (212, 2), "... also at the upper end"
    assert step_target(slots, 0, "up", 99) == (212, 2), "the count is cut at the end of it"
    assert step_target(slots, 0, "zoom_in", 1) is None, "only the four arrows walk a ladder"
    assert step_target([], 0, "up", 1) is None, "a camera without rungs has nothing to walk"


def test_a_step_without_a_known_rung_starts_in_the_middle_of_the_ladder() -> None:
    """An unknown rung starts in the middle and not at the end the arrow points away from.

    A walk that starts at that end sweeps half of the axis on its very first press: the
    camera dives to the limit of the ladder and climbs back from there, which is the
    uncontrolled travel the ladder of stored positions exists to avoid. The middle is the
    rung nearest the crossing of the two ladders, so a press of the axis that was not walked
    last stays where the camera is.
    """
    slots = [210, 211, 212, 213, 214]

    assert step_target(slots, None, "up", 1) == (213, 3), "one rung above the middle"
    assert step_target(slots, None, "down", 1) == (211, 1), "... one rung below it"
    assert step_target(slots, None, "right", 1) == (213, 3), "the pan ladder the same way"
    assert step_target(slots, None, "left", 2) == (210, 0), "and it stays on the ladder"
    assert step_target(slots, None, "up", 99) == (214, 4)


def test_the_two_ladders_of_a_camera_cross_at_their_middle_rung() -> None:
    """The crossing is what a walk of an unknown rung starts on, so it is the middle one."""
    assert crossing_rung([210, 211, 212]) == 1
    assert crossing_rung([210, 211, 212, 213]) == 2
    assert crossing_rung([210, 211]) == 1, "two rungs cross at the second one"
    assert crossing_rung([]) is None, "a ladder without rungs has no crossing"
    assert other_axis("vertical") == "horizontal"
    assert other_axis("horizontal") == "vertical"
    assert other_axis("zoom_in") is None


def test_the_crossing_of_a_ladder_is_what_the_camera_file_says() -> None:
    """A rung stored later moves the middle of a ladder, not the place it was built at.

    The tilt ladder of the test set was built on one pan and grew by a rung at the ground
    afterwards: the appended slot moved ``len(slots) // 2`` by one, so the guess pointed at a
    neighbour of the real crossing - every step off the pan ladder then jumped two rungs of
    the tilt, and every drag it reported was one rung too far. ``step_crossings`` of the PTZ
    block names the rung, and it wins over the shape of the list.
    """
    slots = [210, 211, 212, 213]

    assert crossing_rung(slots, 1) == 1, "the file wins over the middle of the list"
    assert crossing_rung(slots, 9) == 2, "a rung that does not exist falls back to the middle"
    assert crossing_rung(slots, "1") == 2, "only a number is a rung (a hand written file)"
    assert crossing_rung(slots) == 2, "without the key the middle is the best guess there is"

    config = {
        "steps": {"vertical": [210, 211, 212, 213], "horizontal": [217, 218, 219]},
        "step_crossings": {"vertical": 1, "horizontal": 2},
    }
    assert crossings_of(config) == {"vertical": 1, "horizontal": 2}
    assert crossing_of(config, "vertical") == 1
    assert crossing_of(config, "zoom") is None, "an axis without a ladder has no crossing"
    assert crossing_of({}, "vertical") is None
    assert crossings_of({"steps": {"vertical": [210]}, "step_crossings": {"vertical": 3}}) == {
        "vertical": 0
    }, "a crossing behind the end of its ladder is never used"

    assert public_config(normalize_ptz({**config, "profile": "xiongmai_dvrip"}, RTSP_URL))[
        "step_crossings"
    ] == {"vertical": 1, "horizontal": 2}, "the panel reads where the ladders cross"


def test_a_save_of_the_camera_editor_keeps_the_crossing() -> None:
    """The camera form has no field for the crossing, exactly as it has none for the rungs."""
    first = normalize_ptz(
        {
            "profile": "xiongmai_dvrip",
            "steps": {"vertical": [210, 211, 212], "horizontal": [217, 218]},
            "step_crossings": {"vertical": 2, "horizontal": 0},
        },
        RTSP_URL,
    )
    assert first is not None
    assert first["step_crossings"] == {"vertical": 2, "horizontal": 0}

    saved = normalize_ptz({"profile": "xiongmai_dvrip", "speed": 6}, RTSP_URL, first)
    assert saved is not None
    assert saved["step_crossings"] == first["step_crossings"], (
        "a form that does not speak of the crossing keeps it"
    )

    # A ladder that is gone cannot keep a crossing, and a rung that is dropped takes the name
    # with it - the rungs are gone from the end, so the place may be one of them.
    cleared = normalize_ptz(
        {"profile": "xiongmai_dvrip", "steps": {"horizontal": [217]}}, RTSP_URL, first
    )
    assert cleared is not None
    assert cleared["step_crossings"] == {"horizontal": 0}, (
        "the crossing of the tilt ladder is gone with its rungs, so both axes fall back"
    )
    assert crossings_of(cleared) == {"horizontal": 0}


def test_the_position_of_a_camera_is_both_coordinates() -> None:
    """The coordinates of a camera are its rung on *each* ladder, and they may be unknown."""
    ladders = {"vertical": [210, 211, 212, 213], "horizontal": [217, 218, 219]}
    crossings = {"vertical": 1, "horizontal": 2}

    assert rung_position({}, ladders, crossings) == {
        "vertical": {"index": None, "slot": None, "rungs": 4, "crossing": 1},
        "horizontal": {"index": None, "slot": None, "rungs": 3, "crossing": 2},
    }, "a camera that was never walked stands somewhere the add-on cannot name"

    known = rung_position({"vertical": 3, "horizontal": 2}, ladders, crossings)
    assert known["vertical"] == {"index": 3, "slot": 213, "rungs": 4, "crossing": 1}
    assert known["horizontal"] == {"index": 2, "slot": 219, "rungs": 3, "crossing": 2}

    assert rung_position({"vertical": 9}, ladders, crossings)["vertical"]["index"] is None, (
        "a rung that a shortened ladder no longer has reads as unknown"
    )
    assert rung_position({"vertical": True}, ladders, crossings)["vertical"]["index"] is None, (
        "a boolean is no coordinate"
    )


def test_a_step_drags_the_other_axis_only_off_its_crossing() -> None:
    """A step drags the other axis exactly while the camera stands off its crossing rung.

    Every rung of a ladder carries the *crossing* value of the other axis, so a walk of it
    keeps that axis where it stands while the camera is on the crossing - or on a rung of this
    ladder, which leaves the other axis there (see ``main._remember_rung``). Off the crossing
    the rung carries the other axis back, and the answer says how far.
    """
    ladders = {"vertical": [210, 211, 212], "horizontal": [213, 214, 215]}

    assert step_drag({}, ladders, "up") is None, (
        "an unknown rung starts on the crossing (see step_target), so it drags nothing"
    )
    assert step_drag({"vertical": 2, "horizontal": 1}, ladders, "up") is None, (
        "the pan stands on its crossing rung, which is the pan the tilt rungs were stored at"
    )
    assert step_drag({"vertical": 1, "horizontal": 1}, ladders, "right") is None, (
        "the crossing is shared by both ladders"
    )
    assert step_drag({"vertical": 2, "horizontal": 1}, ladders, "right") == ("vertical", -1), (
        "a pan rung carries the tilt back to the middle rung of its ladder"
    )
    assert step_drag({"vertical": 0, "horizontal": 2}, ladders, "up") == ("horizontal", -1)
    assert step_drag({"horizontal": 1}, ladders, "up") is None, "an unknown rung is the crossing"
    assert step_drag({"vertical": 1}, {"vertical": [210]}, "right") is None, (
        "an axis without a ladder of its own is not walked at all"
    )
    assert step_drag({"vertical": 1}, {}, "up") is None
    assert step_drag({"vertical": 1}, ladders, "zoom_in") is None, "a zoom is no step"
