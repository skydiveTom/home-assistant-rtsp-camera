"""Tests for the Xiongmai encoder backend (``AVEnc`` over DVRIP).

The Xiongmai family keeps the codec of its streams in a configuration document of its own
protocol, so the module is checked against a small DVRIP device that stands in for a camera
and records what it received. The endpoint tests then show that the panel reaches that
device without a setting: ``source`` picks the service, ``auto`` falls back to it when the
ONVIF read finds no media service.
"""

from __future__ import annotations

import asyncio
import copy
import json
import socket
import socketserver
import struct
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from app import xm_media
from app.dvrip import (
    MSG_CONFIG_REQUEST,
    MSG_CONFIG_RESPONSE,
    MSG_LOGIN_RESPONSE,
)
from tests.helpers import add_camera

#: The framing of the family, repeated here so the fake device does not depend on the
#: module it is testing (a broken ``pack`` in the client must show up as a failed test).
DVRIP_HEADER = "<BB2xIIBBHI"
DVRIP_HEADER_SIZE = struct.calcsize(DVRIP_HEADER)
DVRIP_SESSION = 0x2C


def avenc_document(main: str = "H265", extra: str = "H265") -> dict[str, Any]:
    """Return the encoder document of a camera, shaped like the vendor's own."""
    return {
        "Channel": 0,
        "MainFormat": {
            "Video": {
                "Compression": main,
                "Resolution": "1280*720",
                "FPS": 25,
                "GOP": 50,
                "BitRate": 4096,
                "Quality": 4,
            },
            "Audio": {"Compression": "G711A"},
        },
        "ExtraFormat": {
            "Video": {
                "Compression": extra,
                "Resolution": "640*360",
                "FPS": 25,
                "GOP": 25,
                "BitRate": 512,
                "Quality": 3,
            },
            "Audio": {"Compression": "G711A"},
        },
    }


def avenc_list_document(
    compressions: tuple[str, ...] = ("H265", "H265", "H264"),
) -> dict[str, Any]:
    """Return the newer layout of the document: one entry per stream under ``Encode``."""
    resolutions = ("1920*1080", "640*360", "1920*1080")
    return {
        "DefinitionCfgTable": {"Table": []},
        "Encode": [
            {
                "Compression": compression,
                "Resolution": resolutions[index],
                "FPS": 25,
                "GOP": 50,
                "BitRate": 4096,
                "Quality": 4,
            }
            for index, compression in enumerate(compressions)
        ],
        "EncodeEx": [{"Compression": compression} for compression in compressions],
    }


class FakeXmCamera(socketserver.ThreadingTCPServer):
    """Fake Xiongmai device: it logs in and answers the ``AVEnc`` document."""

    allow_reuse_address = True
    document: dict[str, Any] = {}
    requests: list[dict[str, Any]] = []
    logins: list[dict[str, Any]] = []
    #: A device that acknowledges a write and keeps its setting.
    keep_codec = False
    fail_login = False
    #: When set, the answer carries these keys instead of the document.
    answer: dict[str, Any] | None = None

    def __init__(self) -> None:
        """Bind to a free port on localhost."""
        super().__init__(("127.0.0.1", 0), FakeXmHandler)
        self.port = self.server_address[1]

    @classmethod
    def writes(cls) -> list[dict[str, Any]]:
        """Return the writes the device received."""
        return [item for item in cls.requests if xm_media.CONFIG_NAME in item["payload"]]


class FakeXmHandler(socketserver.BaseRequestHandler):
    """Answer the login and every configuration request of one connection."""

    def handle(self) -> None:
        """Serve the login, then the configuration requests of that session."""
        _message_id, _session, payload = read_message(self.request)
        FakeXmCamera.logins.append(payload)
        if FakeXmCamera.fail_login:
            self.request.sendall(reply(MSG_LOGIN_RESPONSE, {"Ret": 101}))
            return
        self.request.sendall(
            reply(MSG_LOGIN_RESPONSE, {"Ret": 100, "SessionID": f"0x{DVRIP_SESSION:08X}"})
        )

        while True:
            try:
                message_id, session, payload = read_message(self.request)
            except AssertionError:  # the client is done and disconnected
                return
            FakeXmCamera.requests.append(
                {"message_id": message_id, "session": session, "payload": payload}
            )
            if message_id != MSG_CONFIG_REQUEST:
                self.request.sendall(reply(MSG_CONFIG_RESPONSE, {"Ret": 102}))
                continue
            if payload.get("Name") != xm_media.CONFIG_NAME:
                self.request.sendall(reply(MSG_CONFIG_RESPONSE, {"Ret": 607}))
                continue
            if xm_media.CONFIG_NAME in payload:
                if not FakeXmCamera.keep_codec:
                    FakeXmCamera.document = copy.deepcopy(payload[xm_media.CONFIG_NAME])
                self.request.sendall(reply(MSG_CONFIG_RESPONSE, {"Ret": 100}))
                continue
            answer: dict[str, Any] = {"Ret": 100, "Name": xm_media.CONFIG_NAME}
            if FakeXmCamera.answer is not None:
                answer.update(copy.deepcopy(FakeXmCamera.answer))
            else:
                answer[xm_media.CONFIG_NAME] = copy.deepcopy(FakeXmCamera.document)
            self.request.sendall(reply(MSG_CONFIG_RESPONSE, answer))


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
    """Build a DVRIP reply, padded the way the devices pad their bodies."""
    body = json.dumps(payload).encode("utf-8")
    padding = b"\r\n"
    length = len(body) + len(padding)
    header = struct.pack(DVRIP_HEADER, 0xFF, 1, DVRIP_SESSION, 1, 0, 0, message_id, length)
    return header + body + padding


@pytest.fixture(name="xm_cam")
def xm_cam_fixture() -> Iterator[int]:
    """Run a fake Xiongmai device and yield its port."""
    FakeXmCamera.document = avenc_document()
    FakeXmCamera.requests = []
    FakeXmCamera.logins = []
    FakeXmCamera.keep_codec = False
    FakeXmCamera.fail_login = False
    FakeXmCamera.answer = None
    server = FakeXmCamera()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ------------------------------------------------------------------- parsing
def test_token_names_its_channel_and_stream() -> None:
    """A token of the vendor protocol round trips through its parser."""
    token = xm_media.token_of(2, "extra")

    assert token == "xm:2:extra"
    assert xm_media.parse_token(token) == (2, "extra")
    assert xm_media.parse_token("VideoEncoder_1") is None
    assert xm_media.parse_token("xm:1:snapshot") is None
    assert xm_media.parse_token(None) is None


def test_document_without_the_encoder_block_is_no_document() -> None:
    """Only an answer that carries a document is a document."""
    assert xm_media.document_of({"Ret": 100}) is None
    assert xm_media.document_of({"Ret": 100, "AVEnc": {}}) is None
    assert xm_media.document_of(None) is None
    assert xm_media.document_of({"AVEnc": {"Channel": 0}}) == {"Channel": 0}


# -------------------------------------------------------------------- reading
def test_read_reports_every_stream_of_the_camera(xm_cam: int) -> None:
    """The document is read over DVRIP and shows up as two encoders."""
    report = asyncio.run(
        xm_media.async_read("127.0.0.1", port=xm_cam, username="admin", password="secret")
    )

    assert report.ok is True
    assert report.backend == "xm"
    assert report.media_url == f"dvrip://127.0.0.1:{xm_cam}"
    tokens = [item["token"] for item in report.configurations]
    assert tokens == ["xm:1:main", "xm:1:extra"]
    main, extra = report.configurations
    assert main["encoding"] == "H265"
    assert (main["width"], main["height"]) == (1280, 720)
    assert main["gop"] == 50
    assert main["bitrate"] == 4096
    assert extra["name"] == "Sub stream"
    assert (extra["width"], extra["height"]) == (640, 360)
    assert report.to_dict()["backend"] == "xm"


def test_read_understands_the_encode_list_layout(xm_cam: int) -> None:
    """A firmware that keeps one entry per stream under ``Encode`` is read as well."""
    FakeXmCamera.document = avenc_list_document()

    report = asyncio.run(
        xm_media.async_read("127.0.0.1", port=xm_cam, preferred="extra")
    )

    assert report.ok is True
    tokens = [item["token"] for item in report.configurations]
    assert tokens == ["xm:1:extra", "xm:1:main", "xm:1:third"]
    extra = report.configurations[0]
    assert extra["name"] == "Sub stream"
    assert (extra["width"], extra["height"]) == (640, 360)
    assert [item["encoding"] for item in report.configurations] == ["H265", "H265", "H264"]


def test_write_understands_the_encode_list_layout(xm_cam: int) -> None:
    """The write puts the codec word inside the entry the camera keeps it in."""
    FakeXmCamera.document = avenc_list_document()

    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra", encoding="H264"
        )
    )

    assert report.ok is True
    assert report.changed is True
    document = FakeXmCamera.writes()[0]["payload"][xm_media.CONFIG_NAME]
    assert document["Encode"][1]["Compression"] == "H264"
    assert document["Encode"][0]["Compression"] == "H265"
    assert document["EncodeEx"][1]["Compression"] == "H265"


def test_read_lists_the_stream_of_the_url_first(xm_cam: int) -> None:
    """The stream the RTSP URL of the camera serves is the one on top."""
    report = asyncio.run(
        xm_media.async_read("127.0.0.1", port=xm_cam, preferred="extra")
    )

    assert [item["stream"] for item in report.configurations] == ["extra", "main"]


def test_read_asks_for_the_configured_channel(xm_cam: int) -> None:
    """The configuration commands of the family are per channel, numbered from zero."""
    asyncio.run(xm_media.async_read("127.0.0.1", port=xm_cam, channel=3))

    request = FakeXmCamera.requests[0]
    assert request["message_id"] == MSG_CONFIG_REQUEST
    assert request["payload"]["Name"] == xm_media.CONFIG_NAME
    assert request["payload"]["Channel"] == 2
    assert request["payload"]["SessionID"] == f"0x{DVRIP_SESSION:08X}"


def test_read_without_a_document_is_a_camera_without_encoders(xm_cam: int) -> None:
    """An answer that carries no document names the keys it did bring."""
    FakeXmCamera.answer = {"Ret": 100}

    report = asyncio.run(xm_media.async_read("127.0.0.1", port=xm_cam))

    assert report.ok is False
    assert report.error == "encoder_no_media"
    assert report.detail.startswith("no_avenc")


def test_read_names_the_shape_of_an_unknown_document(xm_cam: int) -> None:
    """A firmware that names its stream blocks differently is shown with its own shape."""
    FakeXmCamera.answer = {
        "Ret": 100,
        xm_media.CONFIG_NAME: {
            "Channel": 0,
            "Video": {"Compression": "H264"},
            "Extra": [{"FPS": 25}],
        },
    }

    report = asyncio.run(xm_media.async_read("127.0.0.1", port=xm_cam))

    assert report.ok is False
    assert report.error == "encoder_no_media"
    assert report.detail.startswith("no_streams:")
    assert "Video{Compression}" in report.detail
    assert "Extra[1]{FPS}" in report.detail
    assert "Channel" in report.detail


def test_read_reports_refused_credentials(xm_cam: int) -> None:
    """A device that refuses the login is an authentication failure."""
    FakeXmCamera.fail_login = True

    report = asyncio.run(
        xm_media.async_read("127.0.0.1", port=xm_cam, username="admin", password="wrong")
    )

    assert report.error == "encoder_auth"
    assert report.detail == "login_failed_101"


def test_read_reports_a_camera_that_is_not_there() -> None:
    """A device that does not answer is a failure of the transport, not of the document."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]

    report = asyncio.run(xm_media.async_read("127.0.0.1", port=closed_port, timeout=1))

    assert report.ok is False
    # Windows answers a closed port with a timeout in some sandboxes and with a refusal on
    # a real host: both are "the camera did not answer", and both are mapped to an error.
    assert report.error in ("encoder_unreachable", "encoder_timeout")


@pytest.mark.parametrize(
    ("detail", "error"),
    [
        ("timeout", "encoder_timeout"),
        ("no_host", "encoder_bad_url"),
        ("login_failed_203", "encoder_auth"),
        ("config_failed_607", "encoder_fault"),
        (
            "[WinError 10061] No connection could be made because the target machine "
            "actively refused it",
            "encoder_unreachable",
        ),
        ("something the family never said", "encoder_fault"),
    ],
)
def test_failures_are_named(detail: str, error: str) -> None:
    """Every failure of the transport gets the error code the panel translates."""
    assert xm_media._failure(detail).error == error
    assert xm_media._failure(detail).backend == "xm"


# -------------------------------------------------------------------- writing
def test_write_switches_one_stream_and_reads_it_back(xm_cam: int) -> None:
    """The write changes one codec word and the re-read confirms it."""
    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra", encoding="H264"
        )
    )

    assert report.ok is True
    assert report.changed is True
    writes = FakeXmCamera.writes()
    assert len(writes) == 1
    document = writes[0]["payload"][xm_media.CONFIG_NAME]
    assert document["ExtraFormat"]["Video"]["Compression"] == "H264"
    assert document["MainFormat"]["Video"]["Compression"] == "H265"
    encodings = {item["token"]: item["encoding"] for item in report.configurations}
    assert encodings == {"xm:1:main": "H265", "xm:1:extra": "H264"}


def test_write_keeps_everything_it_did_not_model(xm_cam: int) -> None:
    """The document goes back whole: only the codec word changes."""
    asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra", encoding="H264"
        )
    )

    document = FakeXmCamera.writes()[0]["payload"][xm_media.CONFIG_NAME]
    extra = document["ExtraFormat"]
    assert extra["Video"]["Resolution"] == "640*360"
    assert extra["Video"]["FPS"] == 25
    assert extra["Video"]["BitRate"] == 512
    assert extra["Audio"] == {"Compression": "G711A"}
    assert document["Channel"] == 0


def test_write_reports_a_codec_the_camera_already_sends(xm_cam: int) -> None:
    """Nothing is written when the camera already sends the codec."""
    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra", encoding="H265"
        )
    )

    assert report.ok is True
    assert report.changed is False
    assert FakeXmCamera.writes() == []


def test_write_refuses_an_unknown_token(xm_cam: int) -> None:
    """A token of another service is not answered with a write."""
    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="VideoEncoder_1", encoding="H264"
        )
    )

    assert report.error == "encoder_token_unknown"
    assert FakeXmCamera.writes() == []


def test_write_refuses_an_unknown_codec(xm_cam: int) -> None:
    """Only the two codecs the panel offers are written."""
    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra", encoding="H266"
        )
    )

    assert report.error == "encoder_bad_encoding"
    assert FakeXmCamera.writes() == []


def test_write_reports_a_camera_that_keeps_its_codec(xm_cam: int) -> None:
    """A device that acknowledges the write and keeps its codec is not a success."""
    FakeXmCamera.keep_codec = True

    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra", encoding="H264"
        )
    )

    assert report.ok is False
    assert report.error == "encoder_fault"
    assert report.detail == "codec_kept_H265"


# ------------------------------------------------------------------ endpoints
def xm_camera(client: TestClient, port: int) -> dict[str, Any]:
    """Create the camera of the test set: its sub stream is the one that plays."""
    return add_camera(
        client,
        name="Balkon 2",
        url=(
            "rtsp://user:pass@127.0.0.1:554/"
            "user=user&password=pass&channel=1&stream=1.sdp"
        ),
        ptz={"profile": "xiongmai_dvrip", "port": port, "channel": 1},
    )


def test_endpoint_reads_the_encoders_over_the_vendor_protocol(
    client: TestClient, xm_cam: int
) -> None:
    """``source: xm`` reads the document and lists the sub stream of the URL first."""
    camera = xm_camera(client, xm_cam)

    response = client.post(
        f"/api/cameras/{camera['id']}/encoder/inspect", json={"source": "xm"}
    )

    assert response.status_code == 200, response.text
    encoder = response.json()["encoder"]
    assert encoder["backend"] == "xm"
    tokens = [item["token"] for item in encoder["configurations"]]
    assert tokens == ["xm:1:extra", "xm:1:main"]


def test_endpoint_falls_back_to_the_vendor_protocol(
    client: TestClient, xm_cam: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A firmware without an ONVIF media service is read over its own protocol."""
    from app import main as app_main
    from app.onvif_media import EncoderReport

    async def no_media(*args: Any, **kwargs: Any) -> EncoderReport:
        return EncoderReport(error="encoder_no_media", detail="no_configurations (HTTP 200)")

    monkeypatch.setattr(app_main, "encoder_read", no_media)
    camera = xm_camera(client, xm_cam)

    response = client.post(f"/api/cameras/{camera['id']}/encoder/inspect", json={})

    assert response.status_code == 200, response.text
    assert response.json()["encoder"]["backend"] == "xm"


def test_endpoint_routes_a_vendor_token_to_the_vendor_protocol(
    client: TestClient, xm_cam: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token of the vendor protocol never reaches the ONVIF write."""
    from app import main as app_main
    from app.onvif_media import EncoderReport

    called: list[Any] = []

    async def onvif_write(*args: Any, **kwargs: Any) -> EncoderReport:
        called.append(kwargs.get("token"))
        return EncoderReport(error="encoder_token_unknown")

    monkeypatch.setattr(app_main, "encoder_write", onvif_write)
    camera = xm_camera(client, xm_cam)

    response = client.post(
        f"/api/cameras/{camera['id']}/encoder",
        json={"token": "xm:1:extra", "encoding": "H264"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["encoder"]["changed"] is True
    assert called == []
