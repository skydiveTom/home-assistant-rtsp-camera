"""Tests for the Xiongmai encoder backend (``Simplify.Encode`` over DVRIP).

The Xiongmai family keeps the codec of its streams in a configuration document of its own
protocol, so the module is checked against a small DVRIP device that stands in for a camera
and records what it received. The endpoint tests then show that the panel reaches that
device without a setting: ``source`` picks the service, ``auto`` falls back to it when the
ONVIF read finds no media service.
"""

from __future__ import annotations

import asyncio
import copy
import errno
import json
import socket
import socketserver
import struct
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from app import dvrip, xm_media
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
#: The request types the family uses for a configuration document.
CONFIG_IDS = (MSG_CONFIG_REQUEST, 1040, 1044)


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


def avenc_group_document() -> dict[str, Any]:
    """Return the layout of the test camera: one group whose formats are per channel lists."""

    def fmt(size: str, gop: int, compression: str = "H265") -> dict[str, Any]:
        return {
            "Audio": {"BitRate": 64, "MaxVolume": 100, "SampleRate": 8000},
            "AudioEnable": 0,
            "Video": {
                "BitRate": 4096,
                "BitRateControl": 0,
                "Compression": compression,
                "FPS": 25,
                "GOP": gop,
                "Quality": 4,
                "Resolution": size,
                "VirtualGOP": 0,
            },
            "VideoEnable": 1,
        }

    def group(offset: int) -> dict[str, Any]:
        return {
            "EncodeStaticParam": [{"Level": 3, "Profile": 1}],
            "MainFormat": [
                fmt("1920*1080", 50 + offset),
                fmt("1280*720", 40 + offset),
                fmt("640*360", 30 + offset),
            ],
            "ExtraFormat": [
                fmt("640*360", 25 + offset),
                fmt("640*360", 25 + offset),
                fmt("640*360", 25 + offset),
            ],
            "SnapFormat": [fmt("640*360", 25), fmt("640*360", 25), fmt("640*360", 25)],
        }

    return {
        "DefinitionCfgTable": {"Table": []},
        "Encode": [group(0), group(1), group(2)],
        "EncodeEx": [{"Dst3irdFmt": 0, "Dst4rthFmt": 0}],
    }


def avenc_channel_document() -> dict[str, Any]:
    """Return the layout of the test camera: a list of channels, each with its blocks."""

    def channel(size: str, gop: int) -> dict[str, Any]:
        return {
            "MainFormat": {
                "Video": {
                    "Compression": "H265",
                    "Resolution": size,
                    "FPS": 25,
                    "GOP": gop,
                    "BitRate": 4096,
                }
            },
            "ExtraFormat": {
                "Video": {
                    "Compression": "H265",
                    "Resolution": "640*360",
                    "FPS": 25,
                    "GOP": 25,
                    "BitRate": 512,
                }
            },
            "SnapFormat": {"Video": {"Compression": "JPEG", "Resolution": "640*360"}},
            "EncodeStaticParam": {"IsTrans": 0, "Level": 3, "Profile": 1},
        }

    return {
        "DefinitionCfgTable": {"Table": []},
        "Encode": [
            channel("1920*1080", 50),
            channel("1280*720", 40),
            channel("640*360", 30),
        ],
        "EncodeEx": [{"Dst3irdFmt": 0, "Dst4rthFmt": 0}],
    }


def simplify_document(
    main: str = "H265", extra: str = "H265", channels: int = 1
) -> list[dict[str, Any]]:
    """Return the document of the newer layout: one entry per channel.

    The vendor app of the family reads and writes this one (``Simplify.Encode``), and the
    ``Video`` object of a stream names its codec exactly like the legacy document does.
    """

    def video(
        compression: str, resolution: str, bitrate: int, quality: int
    ) -> dict[str, Any]:
        return {
            "BitRate": bitrate,
            "BitRateControl": 0,
            "Compression": compression,
            "FPS": 25,
            "GOP": 50,
            "Quality": quality,
            "Resolution": resolution,
        }

    return [
        {
            "MainFormat": {
                "AudioEnable": 0,
                "Video": video(main, "1920*1080", 4096, 4),
                "VideoEnable": 1,
            },
            "ExtraFormat": {
                "AudioEnable": 0,
                "Video": video(extra, "640*360", 512, 3),
                "VideoEnable": 1,
            },
            "SnapFormat": {
                "AudioEnable": 0,
                "Video": video("JPEG", "1920*1080", 0, 4),
                "VideoEnable": 1,
            },
        }
        for _channel in range(channels)
    ]


class FakeXmCamera(socketserver.ThreadingTCPServer):
    """Fake Xiongmai device: it logs in and answers the encoder document."""

    allow_reuse_address = True
    #: A handler waiting on a socket the test left open must not keep the suite alive.
    daemon_threads = True
    block_on_close = False
    document: dict[str, Any] | list[Any] = {}
    requests: list[dict[str, Any]] = []
    logins: list[dict[str, Any]] = []
    #: The names of the configuration document this firmware knows: the document is
    #: answered under the name it was asked for, and any other name is refused.
    names: tuple[str, ...] = xm_media.CONFIG_NAMES
    #: A device that acknowledges a write and keeps its setting.
    keep_codec = False
    fail_login = False
    #: A device that takes a write only on this request type and with this codec word
    #: (``""`` means every write is taken).
    accept_message_id = MSG_CONFIG_REQUEST
    accept_word = ""
    #: When set, the answer carries these keys instead of the document.
    answer: dict[str, Any] | None = None

    def __init__(self) -> None:
        """Bind to a free port on localhost."""
        super().__init__(("127.0.0.1", 0), FakeXmHandler)
        self.port = self.server_address[1]

    @classmethod
    def writes(cls) -> list[dict[str, Any]]:
        """Return the writes the device received."""
        return [
            item
            for item in cls.requests
            if any(name in item["payload"] for name in xm_media.CONFIG_NAMES)
        ]


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
            if message_id not in CONFIG_IDS:
                self.request.sendall(reply(MSG_CONFIG_RESPONSE, {"Ret": 102}))
                continue
            name = payload.get("Name")
            if name not in FakeXmCamera.names:
                self.request.sendall(reply(MSG_CONFIG_RESPONSE, {"Ret": 607}))
                continue
            if name in payload:
                if _write_taken(message_id, payload) and not FakeXmCamera.keep_codec:
                    FakeXmCamera.document = copy.deepcopy(payload[name])
                self.request.sendall(reply(MSG_CONFIG_RESPONSE, {"Ret": 100}))
                continue
            answer: dict[str, Any] = {"Ret": 100, "Name": name}
            if FakeXmCamera.answer is not None:
                answer.update(copy.deepcopy(FakeXmCamera.answer))
            else:
                answer[name] = copy.deepcopy(FakeXmCamera.document)
            self.request.sendall(reply(MSG_CONFIG_RESPONSE, answer))


def _write_taken(message_id: int, payload: dict[str, Any]) -> bool:
    """Return True when the device of the test set takes this write."""
    if message_id != FakeXmCamera.accept_message_id:
        return False
    word = FakeXmCamera.accept_word
    return not word or word in json.dumps(payload)


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
    FakeXmCamera.names = xm_media.CONFIG_NAMES
    FakeXmCamera.keep_codec = False
    FakeXmCamera.fail_login = False
    FakeXmCamera.accept_message_id = MSG_CONFIG_REQUEST
    FakeXmCamera.accept_word = ""
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
def test_token_names_its_group_stream_and_entry() -> None:
    """A token of the vendor protocol round trips through its parser."""
    token = xm_media.token_of(2, "extra", 3)

    assert token == "xm:2:extra:3"
    assert xm_media.parse_token(token) == (2, "extra", 3)
    assert xm_media.parse_token("VideoEncoder_1") is None
    assert xm_media.parse_token("xm:1:snapshot:1") is None
    assert xm_media.parse_token("xm:1:main") is None
    assert xm_media.parse_token("xm:1:main:x") is None
    assert xm_media.parse_token(None) is None


def test_document_without_the_encoder_block_is_no_document() -> None:
    """Only an answer that carries a document is a document."""
    assert xm_media.document_of({"Ret": 100}) is None
    assert xm_media.document_of({"Ret": 100, "AVEnc": {}}, "AVEnc") is None
    assert xm_media.document_of(None) is None
    assert xm_media.document_of({"AVEnc": {"Channel": 0}}, "AVEnc") == {"Channel": 0}
    # The document of the newer layout is the list of the channels of the device.
    channels = [{"MainFormat": {"Video": {"Compression": "H265"}}}]
    assert xm_media.document_of({"Simplify.Encode": channels}) == channels
    assert xm_media.document_of({"Simplify.Encode": []}) is None


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
    assert tokens == ["xm:1:main:1", "xm:1:extra:1"]
    main, extra = report.configurations
    assert main["encoding"] == "H265"
    assert (main["width"], main["height"]) == (1280, 720)
    assert main["gop"] == 50
    assert main["bitrate"] == 4096
    assert extra["name"] == "Sub stream 1/1"
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
    assert tokens == ["xm:1:extra:2", "xm:1:main:1", "xm:1:third:3"]
    extra = report.configurations[0]
    assert extra["name"] == "Sub stream 1/2"
    assert (extra["width"], extra["height"]) == (640, 360)
    assert [item["encoding"] for item in report.configurations] == ["H265", "H265", "H264"]


def test_write_understands_the_encode_list_layout(xm_cam: int) -> None:
    """The write puts the codec word inside the entry the camera keeps it in."""
    FakeXmCamera.document = avenc_list_document()

    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra:2", encoding="H264"
        )
    )

    assert report.ok is True
    assert report.changed is True
    document = FakeXmCamera.writes()[0]["payload"][xm_media.CONFIG_NAME]
    assert document["Encode"][1]["Compression"] == "H264"
    assert document["Encode"][0]["Compression"] == "H265"
    assert document["EncodeEx"][1]["Compression"] == "H265"


def test_read_understands_the_simplify_encode_layout(xm_cam: int) -> None:
    """A document that is the list of the channels is read as one encoder per stream."""
    FakeXmCamera.document = simplify_document(main="H265", extra="H264", channels=2)

    report = asyncio.run(xm_media.async_read("127.0.0.1", port=xm_cam))

    assert report.ok is True
    tokens = [item["token"] for item in report.configurations]
    assert tokens == [
        "xm:1:main:1",
        "xm:1:extra:1",
        "xm:2:main:1",
        "xm:2:extra:1",
    ]
    assert report.configurations[1]["encoding"] == "H264"
    assert (report.configurations[1]["width"], report.configurations[1]["height"]) == (
        640,
        360,
    )
    assert report.configurations[1]["bitrate"] == 512


def test_write_understands_the_simplify_encode_layout(xm_cam: int) -> None:
    """The write of that layout changes one stream of one channel and nothing else."""
    FakeXmCamera.document = simplify_document(channels=2)

    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:2:extra:1", encoding="H264"
        )
    )

    assert report.ok is True
    assert report.changed is True
    document = FakeXmCamera.writes()[0]["payload"][xm_media.CONFIG_NAME]
    assert document[1]["ExtraFormat"]["Video"]["Compression"] == "H264"
    assert document[0]["ExtraFormat"]["Video"]["Compression"] == "H265"
    assert document[1]["MainFormat"]["Video"]["Compression"] == "H265"
    assert len(document) == 2


def test_read_falls_back_to_the_legacy_document(xm_cam: int) -> None:
    """A firmware that knows only the legacy name is read on the second attempt."""
    FakeXmCamera.names = ("AVEnc",)

    report = asyncio.run(xm_media.async_read("127.0.0.1", port=xm_cam))

    assert report.ok is True
    assert [item["token"] for item in report.configurations] == [
        "xm:1:main:1",
        "xm:1:extra:1",
    ]
    asked = [item["payload"]["Name"] for item in FakeXmCamera.requests]
    assert asked[:2] == [xm_media.CONFIG_NAME, "AVEnc"]


def test_write_answers_under_the_name_the_camera_knew(xm_cam: int) -> None:
    """The document goes back under the name it came from, not under the first name."""
    FakeXmCamera.names = ("AVEnc",)

    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra:1", encoding="H264"
        )
    )

    assert report.ok is True
    assert report.changed is True
    write = FakeXmCamera.writes()[0]["payload"]
    assert "AVEnc" in write
    assert write["AVEnc"]["ExtraFormat"]["Video"]["Compression"] == "H264"


def test_read_accepts_lower_case_field_names(xm_cam: int) -> None:
    """A firmware that writes its keys in lower case is read the same way."""
    FakeXmCamera.document = {
        "Encode": [
            {"compression": "H265", "resolution": "1280*720", "fps": 25, "gop": 50},
            {"compression": "H264", "resolution": "640*360", "fps": 15, "gop": 25},
        ]
    }

    report = asyncio.run(xm_media.async_read("127.0.0.1", port=xm_cam))

    assert report.ok is True
    assert [item["encoding"] for item in report.configurations] == ["H265", "H264"]
    assert report.configurations[1]["fps"] == 15
    assert (report.configurations[1]["width"], report.configurations[1]["height"]) == (640, 360)


def test_write_keeps_the_spelling_of_the_codec_key(xm_cam: int) -> None:
    """A lower case key is written in its own spelling, not duplicated."""
    FakeXmCamera.document = {
        "Encode": [
            {"compression": "H265", "resolution": "1280*720"},
            {"compression": "H265", "resolution": "640*360"},
        ]
    }

    asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra:2", encoding="H264"
        )
    )

    entry = FakeXmCamera.writes()[0]["payload"][xm_media.CONFIG_NAME]["Encode"][1]
    assert entry == {"compression": "H264", "resolution": "640*360"}


def test_read_finds_the_video_fields_one_level_down(xm_cam: int) -> None:
    """An entry that keeps its fields in a block of its own is read and written as well."""
    FakeXmCamera.document = {
        "Encode": [
            {"EncodeCfg": {"Compression": "H265", "Resolution": "1920*1080"}},
            {"EncodeCfg": {"Compression": "H265", "Resolution": "640*360"}},
        ]
    }

    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra:2", encoding="H264"
        )
    )

    assert report.ok is True
    assert report.changed is True
    document = FakeXmCamera.writes()[0]["payload"][xm_media.CONFIG_NAME]
    assert document["Encode"][1]["EncodeCfg"]["Compression"] == "H264"
    assert document["Encode"][0]["EncodeCfg"]["Compression"] == "H265"


def test_read_accepts_another_name_of_the_codec_field(xm_cam: int) -> None:
    """A firmware that calls the codec ``Codec`` is read and written the same way."""
    FakeXmCamera.document = {
        "Encode": [
            {
                "MainFormat": {"Video": {"Codec": "H265", "Resolution": "1920*1080"}},
                "ExtraFormat": {"Video": {"Codec": "H265", "Resolution": "640*360"}},
            }
        ]
    }

    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra:1", encoding="H264"
        )
    )

    assert report.ok is True
    assert report.changed is True
    document = FakeXmCamera.writes()[0]["payload"][xm_media.CONFIG_NAME]
    video = document["Encode"][0]["ExtraFormat"]["Video"]
    assert video == {"Codec": "H264", "Resolution": "640*360"}


def test_read_normalizes_the_dotted_codec(xm_cam: int) -> None:
    """The camera writes ``H.265``; the panel is offered the same word it writes."""
    FakeXmCamera.document = {
        "Encode": [
            {
                "MainFormat": {
                    "Video": {"Compression": "H.265", "Resolution": "1920*1080"}
                },
                "ExtraFormat": {
                    "Video": {"Compression": "H.265", "Resolution": "640*360"}
                },
            }
        ]
    }

    report = asyncio.run(xm_media.async_read("127.0.0.1", port=xm_cam))

    assert [item["encoding"] for item in report.configurations] == ["H265", "H265"]
    assert report.configurations[0]["raw_encoding"] == "H.265"
    assert "raw_encoding" not in report.to_dict()["configurations"][0]
    assert "path" not in report.to_dict()["configurations"][0]


def test_write_keeps_the_dotted_spelling_of_the_camera(xm_cam: int) -> None:
    """The write answers in the spelling the firmware knows, not in the panel's."""
    FakeXmCamera.document = {
        "Encode": [
            {
                "MainFormat": {
                    "Video": {"Compression": "H.265", "Resolution": "1920*1080"}
                },
                "ExtraFormat": {
                    "Video": {"Compression": "H.265", "Resolution": "640*360"}
                },
            }
        ]
    }

    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra:1", encoding="H264"
        )
    )

    assert report.ok is True
    assert report.changed is True
    document = FakeXmCamera.writes()[0]["payload"][xm_media.CONFIG_NAME]
    assert document["Encode"][0]["ExtraFormat"]["Video"]["Compression"] == "H.264"
    assert document["Encode"][0]["MainFormat"]["Video"]["Compression"] == "H.265"


def test_read_reports_the_shape_of_streams_without_a_codec(xm_cam: int) -> None:
    """Streams that name no codec come with the shape of the document, not with silence."""
    FakeXmCamera.document = {
        "Encode": [{"Resolution": "1920*1080"}, {"Resolution": "640*360"}]
    }

    report = asyncio.run(xm_media.async_read("127.0.0.1", port=xm_cam))

    assert report.ok is True
    assert report.to_dict()["shape"].startswith("Encode[2]{Resolution}")


def test_read_lists_every_entry_of_the_per_channel_lists(xm_cam: int) -> None:
    """Every entry of a list over the channels is offered, with its group and index."""
    FakeXmCamera.document = avenc_group_document()

    report = asyncio.run(xm_media.async_read("127.0.0.1", port=xm_cam))

    assert report.ok is True
    assert len(report.configurations) == 18
    first = report.configurations[0]
    assert first["token"] == "xm:1:main:1"
    assert (first["width"], first["height"]) == (1920, 1080)
    assert first["gop"] == 50
    by_token = {item["token"]: item for item in report.configurations}
    assert by_token["xm:1:main:2"]["gop"] == 40
    assert by_token["xm:2:main:1"]["gop"] == 51


def test_write_understands_the_per_channel_lists(xm_cam: int) -> None:
    """The codec word lands in the channel entry of the block that was read."""
    FakeXmCamera.document = avenc_group_document()

    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra:1", encoding="H264"
        )
    )

    assert report.ok is True
    assert report.changed is True
    document = FakeXmCamera.writes()[0]["payload"][xm_media.CONFIG_NAME]
    assert document["Encode"][0]["ExtraFormat"][0]["Video"]["Compression"] == "H264"
    assert document["Encode"][0]["MainFormat"][0]["Video"]["Compression"] == "H265"
    assert document["Encode"][0]["ExtraFormat"][1]["Video"]["Compression"] == "H265"
    assert document["Encode"][1]["ExtraFormat"][0]["Video"]["Compression"] == "H265"
    assert document["Encode"][0]["SnapFormat"][0]["Video"]["Compression"] == "H265"


def test_read_lists_every_channel_of_the_document(xm_cam: int) -> None:
    """A list of channels is listed channel by channel, with the same tokens."""
    FakeXmCamera.document = avenc_channel_document()

    report = asyncio.run(xm_media.async_read("127.0.0.1", port=xm_cam))

    assert report.ok is True
    tokens = [item["token"] for item in report.configurations]
    assert tokens == [
        "xm:1:main:1",
        "xm:1:extra:1",
        "xm:2:main:1",
        "xm:2:extra:1",
        "xm:3:main:1",
        "xm:3:extra:1",
    ]
    by_token = {item["token"]: item for item in report.configurations}
    main = by_token["xm:1:main:1"]
    assert (main["width"], main["height"]) == (1920, 1080)
    assert by_token["xm:2:main:1"]["gop"] == 40


def test_write_changes_only_the_entry_it_read(xm_cam: int) -> None:
    """The codec word lands in the block of that channel and nothing else changes."""
    FakeXmCamera.document = avenc_channel_document()

    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra:1", encoding="H264"
        )
    )

    assert report.ok is True
    assert report.changed is True
    document = FakeXmCamera.writes()[0]["payload"][xm_media.CONFIG_NAME]
    assert document["Encode"][0]["ExtraFormat"]["Video"]["Compression"] == "H264"
    assert document["Encode"][0]["MainFormat"]["Video"]["Compression"] == "H265"
    assert document["Encode"][1]["ExtraFormat"]["Video"]["Compression"] == "H265"
    assert document["Encode"][0]["SnapFormat"]["Video"]["Compression"] == "JPEG"


def test_read_lists_the_stream_of_the_url_first(xm_cam: int) -> None:
    """The stream the RTSP URL of the camera serves is the one on top."""
    report = asyncio.run(
        xm_media.async_read("127.0.0.1", port=xm_cam, preferred="extra")
    )

    assert [item["stream"] for item in report.configurations] == ["extra", "main"]


def test_read_asks_for_the_document_of_the_device(xm_cam: int) -> None:
    """The document is asked for the first channel and carries every entry anyway."""
    asyncio.run(xm_media.async_read("127.0.0.1", port=xm_cam))

    request = FakeXmCamera.requests[0]
    assert request["message_id"] == MSG_CONFIG_REQUEST
    assert request["payload"]["Name"] == xm_media.CONFIG_NAME
    assert request["payload"]["Channel"] == 0
    assert request["payload"]["SessionID"] == f"0x{DVRIP_SESSION:08X}"


def test_read_without_a_document_is_a_camera_without_encoders(xm_cam: int) -> None:
    """An answer that carries no document names the keys it did bring."""
    FakeXmCamera.answer = {"Ret": 100}

    report = asyncio.run(xm_media.async_read("127.0.0.1", port=xm_cam))

    assert report.ok is False
    assert report.error == "encoder_no_media"
    assert report.detail.startswith("no_encode")


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
        # What Linux puts into the same failure: the word "refused" is not in it, so the
        # socket names the failure and the text only follows (see ``dvrip.failure_word``).
        (
            "refused: [Errno 111] Connect call failed ('127.0.0.1', 49737)",
            "encoder_unreachable",
        ),
        ("something the family never said", "encoder_fault"),
    ],
)
def test_failures_are_named(detail: str, error: str) -> None:
    """Every failure of the transport gets the error code the panel translates."""
    assert xm_media._failure(detail).error == error
    assert xm_media._failure(detail).backend == "xm"


def test_a_closed_port_is_named_after_the_socket_not_after_the_platform() -> None:
    """A refusal reads the same on every platform, because the socket says which it is.

    The same closed port answers "[Errno 111] Connect call failed" on Linux and "No
    connection could be made because the target machine actively refused it" on Windows,
    and only the second sentence carries the word the encoder endpoints used to look for -
    which is why a run of the suite on Linux reported a camera that was simply not there
    as a fault of the device.
    """
    linux = ConnectionRefusedError(111, "Connect call failed ('127.0.0.1', 49737)")
    windows = ConnectionRefusedError(
        10061, "No connection could be made because the target machine actively refused it"
    )

    assert dvrip.failure_word(linux).startswith(dvrip.FAILURE_REFUSED)
    assert dvrip.failure_word(windows).startswith(dvrip.FAILURE_REFUSED)
    assert "Connect call failed" in dvrip.failure_word(linux), "the text is kept for the log"
    assert dvrip.failure_word(TimeoutError("timed out")) == dvrip.FAILURE_TIMEOUT
    assert dvrip.failure_word(OSError(errno.EHOSTUNREACH, "No route to host")).startswith(
        dvrip.FAILURE_UNREACHABLE
    )
    assert dvrip.failure_word(ValueError("broken answer")) == "broken answer"


# -------------------------------------------------------------------- writing
def test_write_switches_one_stream_and_reads_it_back(xm_cam: int) -> None:
    """The write changes one codec word and the re-read confirms it."""
    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra:1", encoding="H264"
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
    assert encodings == {"xm:1:main:1": "H265", "xm:1:extra:1": "H264"}


def test_write_keeps_everything_it_did_not_model(xm_cam: int) -> None:
    """The document goes back whole: only the codec word changes."""
    asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra:1", encoding="H264"
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
            "127.0.0.1", port=xm_cam, token="xm:1:extra:1", encoding="H265"
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
            "127.0.0.1", port=xm_cam, token="xm:1:extra:1", encoding="H266"
        )
    )

    assert report.error == "encoder_bad_encoding"
    assert FakeXmCamera.writes() == []


def test_write_reports_a_camera_that_keeps_its_codec(xm_cam: int) -> None:
    """A device that acknowledges the write and keeps its codec is not a success."""
    FakeXmCamera.keep_codec = True

    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra:1", encoding="H264"
        )
    )

    assert report.ok is False
    assert report.error == "encoder_fault"
    assert report.detail.startswith("codec_kept_")
    assert "kept_H265" in report.detail


def test_write_tries_the_pairs_a_camera_may_accept(xm_cam: int) -> None:
    """A device that takes only one request type and one spelling is still switched."""
    FakeXmCamera.document = avenc_channel_document()
    FakeXmCamera.accept_message_id = 1040
    FakeXmCamera.accept_word = "H264"

    report = asyncio.run(
        xm_media.async_write(
            "127.0.0.1", port=xm_cam, token="xm:1:extra:1", encoding="H264"
        )
    )

    assert report.ok is True
    assert report.changed is True
    assert report.detail == "write=1040:H264"
    document = FakeXmCamera.document
    assert document["Encode"][0]["ExtraFormat"]["Video"]["Compression"] == "H264"


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
    assert tokens == ["xm:1:extra:1", "xm:1:main:1"]


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
        json={"token": "xm:1:extra:1", "encoding": "H264"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["encoder"]["changed"] is True
    assert called == []
