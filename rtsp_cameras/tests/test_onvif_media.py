"""Tests for the ONVIF media support of the add-on (the codec of a stream).

The documents are the ones an ONVIF camera answers with, and every endpoint test talks to
a small server that plays such a device: it offers the media service, reports its encoder
configurations and accepts a change of the codec - or refuses one, the way a firmware that
only implements the read side does.
"""

from __future__ import annotations

import asyncio
import base64
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from starlette.testclient import TestClient

from app.onvif_media import (
    ENCODINGS,
    ERROR_BAD_ENCODING,
    ERROR_BAD_URL,
    ERROR_FAULT,
    ERROR_TIMEOUT,
    ERROR_TOKEN_REQUIRED,
    ERROR_TOKEN_UNKNOWN,
    ERROR_UNREACHABLE,
    async_read,
    async_set_encoding,
    parse_encoder_configurations,
    parse_media_url,
    parse_profiles,
    set_configuration_body,
)
from tests.helpers import add_camera

DEVICE_PATH = "/onvif/device_service"
MEDIA_PATH = "/onvif/media_service"


def soap(inner: str) -> bytes:
    """Wrap a body into the envelope a camera answers with."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f"<s:Body>{inner}</s:Body></s:Envelope>"
    ).encode()


def services_reply(port: int) -> bytes:
    """Return the ``GetServices`` answer: the device names its media service."""
    return soap(
        '<tds:GetServicesResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
        "<tds:Service>"
        "<tds:Namespace>http://www.onvif.org/ver10/device/wsdl</tds:Namespace>"
        f"<tds:XAddr>http://127.0.0.1:{port}{DEVICE_PATH}</tds:XAddr>"
        "</tds:Service>"
        "<tds:Service>"
        "<tds:Namespace>http://www.onvif.org/ver20/media/wsdl</tds:Namespace>"
        f"<tds:XAddr>http://127.0.0.1:{port}/onvif/media2_service</tds:XAddr>"
        "</tds:Service>"
        "<tds:Service>"
        "<tds:Namespace>http://www.onvif.org/ver10/media/wsdl</tds:Namespace>"
        f"<tds:XAddr>http://127.0.0.1:{port}{MEDIA_PATH}</tds:XAddr>"
        "</tds:Service>"
        "</tds:GetServicesResponse>"
    )


#: One encoder configuration, the way a camera reports it: the entries belong to the
#: schema, the element itself to the media service.
CONFIGURATION = """<trt:Configurations token="{token}">
      <tt:Name>{name}</tt:Name>
      <tt:UseCount>2</tt:UseCount>
      <tt:Encoding>{encoding}</tt:Encoding>
      <tt:Resolution><tt:Width>{width}</tt:Width><tt:Height>{height}</tt:Height></tt:Resolution>
      <tt:Quality>4</tt:Quality>
      <tt:RateControl>
        <tt:FrameRateLimit>25</tt:FrameRateLimit>
        <tt:EncodingInterval>1</tt:EncodingInterval>
        <tt:BitrateLimit>{bitrate}</tt:BitrateLimit>
      </tt:RateControl>
      {codec}
      <tt:Multicast>
        <tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>
        <tt:Port>0</tt:Port><tt:TTL>1</tt:TTL><tt:AutoStart>false</tt:AutoStart>
      </tt:Multicast>
      <tt:SessionTimeout>PT0S</tt:SessionTimeout>
    </trt:Configurations>"""

CONFIGURATIONS = {
    "V_ENC_000": {
        "name": "mainStream",
        "encoding": "H265",
        "width": 1280,
        "height": 720,
        "bitrate": 2048,
        "gop": 50,
    },
    "V_ENC_001": {
        "name": "subStream",
        "encoding": "H265",
        "width": 640,
        "height": 360,
        "bitrate": 512,
        "gop": 25,
    },
}


def configurations_reply(encodings: dict[str, str]) -> bytes:
    """Return the ``GetVideoEncoderConfigurations`` answer of a camera."""
    parts = []
    for token, values in CONFIGURATIONS.items():
        encoding = encodings.get(token, values["encoding"])
        codec = (
            "<tt:H264><tt:GovLength>{gop}</tt:GovLength>"
            "<tt:H264Profile>Main</tt:H264Profile></tt:H264>"
            if encoding == "H264"
            else "<tt:H265><tt:GovLength>{gop}</tt:GovLength>"
            "<tt:H265Profile>Main</tt:H265Profile></tt:H265>"
        ).format(gop=values["gop"])
        fields = {key: value for key, value in values.items() if key != "encoding"}
        parts.append(
            CONFIGURATION.format(codec=codec, encoding=encoding, token=token, **fields)
        )
    return soap(
        '<trt:GetVideoEncoderConfigurationsResponse '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        + "".join(parts)
        + "</trt:GetVideoEncoderConfigurationsResponse>"
    )


def profiles_reply() -> bytes:
    """Return the ``GetProfiles`` answer: every profile names its encoder."""
    return soap(
        '<trt:GetProfilesResponse xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        '<trt:Profiles token="Profile_1"><tt:Name>mainStream</tt:Name>'
        '<tt:VideoEncoderConfiguration token="V_ENC_000"/></trt:Profiles>'
        '<trt:Profiles token="Profile_2"><tt:Name>subStream</tt:Name>'
        '<tt:VideoEncoderConfiguration token="V_ENC_001"/></trt:Profiles>'
        "</trt:GetProfilesResponse>"
    )


def fault_reply(subcode: str, reason: str) -> bytes:
    """Return a SOAP fault, the way a device refuses an operation."""
    return soap(
        "<s:Fault><s:Code><s:Value>s:Sender</s:Value>"
        f"<s:Subcode><s:Value>{subcode}</s:Value></s:Subcode></s:Code>"
        f"<s:Reason><s:Text xml:lang=\"en\">{reason}</s:Text></s:Reason></s:Fault>"
    )



class OnvifHandler(BaseHTTPRequestHandler):
    """A fake ONVIF device: the device service and the media service of one camera."""

    received: list[dict[str, Any]] = []
    #: The codec every encoder of the device sends (empty = as built in).
    encodings: dict[str, str] = {}
    #: Refuse a request that carries a WS-Security header (an old firmware).
    refuse_security: bool = False
    #: Demand HTTP Basic credentials before anything is answered.
    require_auth: bool = False
    #: Refuse the write with this fault - ``None`` accepts it.
    write_fault: tuple[str, str] | None = None

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        """Record a SOAP request and answer like a camera."""
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        OnvifHandler.received.append(
            {
                "path": urlsplit(self.path).path,
                "body": body,
                "authorization": self.headers.get("Authorization"),
            }
        )
        if OnvifHandler.require_auth and not self._authorized():
            self._send(401, b"", {"WWW-Authenticate": 'Basic realm="onvif"'})
            return
        if OnvifHandler.refuse_security and "<wsse:Security" in body:
            self._send(400, fault_reply("s:NotAuthorized", "Sender not Authorized"))
            return
        status, payload, headers = self._reply(body)
        self._send(status, payload, headers)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        """This device serves no web interface."""
        self._send(404, b"not found")

    def _authorized(self) -> bool:
        """Accept exactly the credentials of the test camera, over Basic."""
        header = str(self.headers.get("Authorization") or "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):  # pragma: no cover - broken client
            return False
        return (user, password) == ("admin", "secret")

    def _reply(self, body: str) -> tuple[int, bytes, dict[str, str]]:
        """Answer one SOAP body of a camera that knows the read and the write side."""
        path = urlsplit(self.path).path
        if path == DEVICE_PATH and "GetServices" in body:
            return 200, services_reply(self.server.server_port), {}
        if path not in (DEVICE_PATH, MEDIA_PATH):
            return 404, b"not found", {}
        if "GetVideoEncoderConfigurations" in body:
            return 200, configurations_reply(OnvifHandler.encodings), {}
        if "GetProfiles" in body:
            return 200, profiles_reply(), {}
        if "SetVideoEncoderConfiguration" in body:
            if OnvifHandler.write_fault:
                return 500, fault_reply(*OnvifHandler.write_fault), {}
            token = re.search(r'token="([^"]+)"', body)
            encoding = re.search(r"<tt:Encoding>([A-Za-z0-9]+)</tt:Encoding>", body)
            if token and encoding:
                OnvifHandler.encodings[token.group(1)] = encoding.group(1)
            return (
                200,
                soap(
                    "<trt:SetVideoEncoderConfigurationResponse "
                    'xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>'
                ),
                {},
            )
        return 400, fault_reply("ter:ActionNotSupported", "Action not supported"), {}

    def _send(self, status: int, payload: bytes, headers: dict[str, str] | None = None) -> None:
        """Answer with a SOAP document."""
        self.send_response(status)
        self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        """Keep the test output clean."""


@pytest.fixture(name="onvif_cam")
def onvif_cam_fixture() -> Iterator[str]:
    """Run a fake ONVIF camera that reports two encoders and accepts a codec change."""
    OnvifHandler.received = []
    OnvifHandler.encodings = {}
    OnvifHandler.refuse_security = False
    OnvifHandler.require_auth = False
    OnvifHandler.write_fault = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), OnvifHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        OnvifHandler.encodings = {}
        OnvifHandler.refuse_security = False
        OnvifHandler.require_auth = False
        OnvifHandler.write_fault = None
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def received() -> list[dict[str, Any]]:
    """Return the requests the fake camera received."""
    return OnvifHandler.received


def test_media_url_is_taken_from_the_service_list() -> None:
    """GetServices names the media service - and the ver20 one is left alone."""
    assert parse_media_url(services_reply(8099)) == "http://127.0.0.1:8099/onvif/media_service"
    assert parse_media_url("not xml") is None


def test_configurations_are_parsed() -> None:
    """The codec, the picture and the rate of every encoder of the reply."""
    configurations = parse_encoder_configurations(configurations_reply({}))

    assert [item["token"] for item in configurations] == ["V_ENC_000", "V_ENC_001"]
    sub = configurations[1]
    assert sub["name"] == "subStream"
    assert sub["encoding"] == "H265"
    assert (sub["width"], sub["height"]) == (640, 360)
    assert (sub["fps"], sub["bitrate"], sub["gop"]) == (25.0, 512, 25)
    # the element is kept for the write, but never published
    assert "element" in sub


def test_profiles_name_their_encoder() -> None:
    """A profile tells which encoder its stream uses."""
    assert parse_profiles(profiles_reply()) == [
        {"token": "Profile_1", "name": "mainStream", "encoder": "V_ENC_000"},
        {"token": "Profile_2", "name": "subStream", "encoder": "V_ENC_001"},
    ]


def test_the_write_sends_the_document_back_with_one_word_changed() -> None:
    """Everything the device reported survives, only the codec changes."""
    sub = parse_encoder_configurations(configurations_reply({}))[1]

    body = set_configuration_body(sub, "h264")

    assert "<trt:SetVideoEncoderConfiguration>" in body
    assert "<trt:Configuration " in body
    assert 'token="V_ENC_001"' in body
    # the parameter is ``Configuration`` - the plural element is what a strict device refuses
    assert "<trt:Configurations" not in body
    assert "<tt:Encoding>H264</tt:Encoding>" in body
    assert "<tt:H264><tt:GovLength>25</tt:GovLength>" in body
    assert "H265" not in body
    # the parts this module does not model are kept: multicast and the session timeout
    assert "<tt:IPv4Address>0.0.0.0</tt:IPv4Address>" in body
    assert "<tt:SessionTimeout>PT0S</tt:SessionTimeout>" in body
    assert "<trt:ForcePersistence>true</trt:ForcePersistence>" in body


def test_the_encoders_of_a_camera_are_read(onvif_cam: str) -> None:
    """The device service names the media service, which lists the encoders."""
    report = asyncio.run(async_read(onvif_cam))

    assert report.ok, report.detail
    assert report.media_url == f"{onvif_cam}{MEDIA_PATH}"
    assert [item["encoding"] for item in report.configurations] == ["H265", "H265"]
    assert [profile["encoder"] for profile in report.profiles] == ["V_ENC_000", "V_ENC_001"]
    paths = [request["path"] for request in received()]
    assert paths[0] == DEVICE_PATH
    assert MEDIA_PATH in paths


def test_the_codec_of_one_encoder_is_switched(onvif_cam: str) -> None:
    """The write goes to the media service and the camera reports the new codec."""
    report = asyncio.run(async_set_encoding(onvif_cam, token="V_ENC_001", encoding="H264"))

    assert report.ok, report.detail
    assert report.changed is True
    assert OnvifHandler.encodings["V_ENC_001"] == "H264"
    written = next(item for item in report.configurations if item["token"] == "V_ENC_001")
    assert written["encoding"] == "H264"
    # the main stream was not touched
    untouched = next(item for item in report.configurations if item["token"] == "V_ENC_000")
    assert untouched["encoding"] == "H265"

    writes = [item for item in received() if "SetVideoEncoderConfiguration" in item["body"]]
    assert len(writes) == 1
    assert writes[0]["path"] == MEDIA_PATH


def test_a_camera_that_sends_the_codec_already_does_not_write(onvif_cam: str) -> None:
    """A second set of the same codec is answered, not sent."""
    first = asyncio.run(async_set_encoding(onvif_cam, token="V_ENC_001", encoding="H264"))
    second = asyncio.run(async_set_encoding(onvif_cam, token="V_ENC_001", encoding="H264"))

    assert first.changed is True
    assert second.ok is True
    assert second.changed is False
    writes = [item for item in received() if "SetVideoEncoderConfiguration" in item["body"]]
    assert len(writes) == 1


def test_a_device_that_refuses_the_write_is_reported(onvif_cam: str) -> None:
    """A firmware with a read only media service names the fault it answered."""
    OnvifHandler.write_fault = ("ter:ActionNotSupported", "Optional Action Not Implemented")

    report = asyncio.run(async_set_encoding(onvif_cam, token="V_ENC_001", encoding="H264"))

    assert report.ok is False
    assert report.error == ERROR_FAULT
    assert "ActionNotSupported" in report.detail
    assert "Optional Action Not Implemented" in report.detail
    assert OnvifHandler.encodings == {}


def test_the_ws_security_header_is_dropped_when_the_device_refuses_it(onvif_cam: str) -> None:
    """A firmware that wants plain HTTP authentication still answers."""
    OnvifHandler.refuse_security = True

    report = asyncio.run(async_read(onvif_cam, username="admin", password="secret"))

    assert report.ok, report.detail
    first, second = received()[0], received()[1]
    assert "<wsse:Security" in first["body"]
    assert "<wsse:Security" not in second["body"]


def test_a_basic_challenge_is_answered(onvif_cam: str) -> None:
    """The credentials of the camera are offered again when the device asks for them."""
    OnvifHandler.require_auth = True

    report = asyncio.run(async_read(onvif_cam, username="admin", password="secret"))

    assert report.ok, report.detail
    assert any(item["authorization"] for item in received())


def test_wrong_credentials_are_reported_as_such(onvif_cam: str) -> None:
    """A refused login is not an unreachable camera."""
    OnvifHandler.require_auth = True

    report = asyncio.run(async_read(onvif_cam, username="admin", password="wrong"))

    assert report.ok is False
    assert report.error == "encoder_auth"
    assert report.detail.startswith("HTTP 401")


def test_a_dead_host_and_a_bad_address_are_reported() -> None:
    """A camera that is not there costs one request, an address that cannot work none."""
    dead = asyncio.run(async_read("http://127.0.0.1:9", timeout=2))
    # a closed port is refused at once, a filtered one is a timeout - both are "not there"
    assert dead.error in (ERROR_UNREACHABLE, ERROR_TIMEOUT)
    assert asyncio.run(async_read("")).error == ERROR_BAD_URL
    assert asyncio.run(async_read("ftp://192.168.1.10")).error == ERROR_BAD_URL


def test_a_missing_or_unknown_encoder_is_reported(onvif_cam: str) -> None:
    """The caller fixes these, so no request asks the camera again."""
    missing = asyncio.run(async_set_encoding(onvif_cam, token="", encoding="H264"))
    unknown = asyncio.run(async_set_encoding(onvif_cam, token="V_ENC_042", encoding="H264"))
    wrong = asyncio.run(async_set_encoding(onvif_cam, token="V_ENC_001", encoding="VP9"))

    assert missing.error == ERROR_TOKEN_REQUIRED
    assert unknown.error == ERROR_TOKEN_UNKNOWN
    assert unknown.detail == "V_ENC_042"
    assert wrong.error == ERROR_BAD_ENCODING



# --------------------------------------------------------------- the endpoints
def test_the_panel_reads_the_encoders(client: TestClient, onvif_cam: str) -> None:
    """The camera editor asks for the encoders of a saved camera."""
    camera = add_camera(
        client,
        url="rtsp://admin:secret@192.168.1.10:554/user=admin&password=secret&channel=1"
        "&stream=1.sdp",
        ptz={
            "profile": "onvif",
            "base_url": onvif_cam,
            "username": "admin",
            "password": "secret",
        },
    )

    response = client.post(f"/api/cameras/{camera['id']}/encoder/inspect", json={})

    assert response.status_code == 200, response.text
    encoder = response.json()["encoder"]
    assert encoder["ok"] is True
    assert [item["token"] for item in encoder["configurations"]] == ["V_ENC_000", "V_ENC_001"]
    # the element behind a configuration never reaches the browser
    assert "element" not in encoder["configurations"][0]
    assert encoder["profiles"][1] == {"token": "Profile_2", "name": "subStream", "encoder": "V_ENC_001"}


def test_the_panel_switches_the_codec(client: TestClient, onvif_cam: str) -> None:
    """The endpoint reports what the camera sends after the change."""
    camera = add_camera(
        client,
        ptz={"profile": "onvif", "base_url": onvif_cam, "username": "admin", "password": "secret"},
    )

    response = client.post(
        f"/api/cameras/{camera['id']}/encoder",
        json={"token": "V_ENC_001", "encoding": "H264"},
    )

    assert response.status_code == 200, response.text
    encoder = response.json()["encoder"]
    assert encoder["changed"] is True
    sub = next(item for item in encoder["configurations"] if item["token"] == "V_ENC_001")
    assert sub["encoding"] == "H264"


def test_the_address_and_the_credentials_come_from_the_stream_url(
    client: TestClient, onvif_cam: str
) -> None:
    """A camera that was never set up for PTZ is still reachable through its URL."""
    OnvifHandler.require_auth = True
    camera = add_camera(
        client,
        url="rtsp://admin:secret@192.168.1.10:554/user=admin&password=secret&channel=1"
        "&stream=1.sdp",
    )

    response = client.post(
        f"/api/cameras/{camera['id']}/encoder/inspect", json={"base_url": onvif_cam}
    )

    assert response.status_code == 200, response.text
    assert response.json()["encoder"]["ok"] is True


def test_an_unknown_codec_is_refused_without_asking_the_camera(
    client: TestClient, onvif_cam: str
) -> None:
    """Nothing is sent to a device for a request that cannot work."""
    camera = add_camera(client, ptz={"profile": "onvif", "base_url": onvif_cam})

    response = client.post(
        f"/api/cameras/{camera['id']}/encoder", json={"token": "V_ENC_001", "encoding": "VP9"}
    )

    assert response.status_code == 400
    assert response.json()["error"] == ERROR_BAD_ENCODING
    assert response.json()["message"]
    assert received() == []


def test_an_unknown_camera_is_reported(client: TestClient) -> None:
    """A camera that was deleted in another tab is a 404, not a crash."""
    response = client.post("/api/cameras/gone/encoder/inspect", json={})

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_an_unreachable_camera_is_reported(client: TestClient) -> None:
    """A camera that does not answer is a gateway error with a translated message."""
    camera = add_camera(client, ptz={"profile": "onvif", "base_url": "http://127.0.0.1:9"})

    response = client.post(
        f"/api/cameras/{camera['id']}/encoder/inspect", json={"timeout": 1}
    )

    assert response.status_code == 502
    payload = response.json()
    assert payload["error"] in (ERROR_UNREACHABLE, ERROR_TIMEOUT)
    assert payload["message"] and payload["message"] != payload["error"]


def test_a_refused_write_carries_the_words_of_the_device(
    client: TestClient, onvif_cam: str
) -> None:
    """The panel shows the fault of the camera, not a generic failure."""
    OnvifHandler.write_fault = ("ter:ActionNotSupported", "Optional Action Not Implemented")
    camera = add_camera(client, ptz={"profile": "onvif", "base_url": onvif_cam})

    response = client.post(
        f"/api/cameras/{camera['id']}/encoder",
        json={"token": "V_ENC_001", "encoding": "H264"},
    )

    assert response.status_code == 502
    payload = response.json()
    assert payload["error"] == ERROR_FAULT
    assert "ActionNotSupported" in payload["detail"]
    # the message is translated and names the operation; the words go to ``detail``
    assert payload["message"] and payload["message"] != ERROR_FAULT


def test_the_panel_offers_the_codecs_the_endpoint_accepts() -> None:
    """The select of the editor and the validation of the endpoint agree."""
    script = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    choices = re.search(r"const ENCODER_CHOICES = \[([^\]]+)\]", script)

    assert choices is not None
    assert re.findall(r"'([A-Za-z0-9]+)'", choices.group(1)) == list(ENCODINGS)

