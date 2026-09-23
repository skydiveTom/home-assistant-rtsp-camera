"""Tests for the camera model helpers."""

from __future__ import annotations

import pytest

from app.models import (
    Camera,
    has_credentials,
    redact_url,
    slugify,
    validate_stream_url,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Front door", "front-door"),
        ("Kamera Łazienka 2", "kamera-lazienka-2"),
        ("  ---  ", "camera"),
        ("", "camera"),
    ],
)
def test_slugify(value: str, expected: str) -> None:
    assert slugify(value) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("rtsp://192.168.1.10:554/stream1", None),
        ("rtsps://camera/stream", None),
        ("http://camera/mjpeg", None),
        ("", "url_required"),
        ("ftp://camera", "url_scheme"),
        ("rtsp://", "url_invalid"),
        ("rtsp://host/stream with space", "url_invalid"),
    ],
)
def test_validate_stream_url(url: str, expected: str | None) -> None:
    assert validate_stream_url(url) == expected


def test_redact_url_masks_credentials() -> None:
    assert redact_url("rtsp://user:secret@camera:554/stream") == "rtsp://***:***@camera:554/stream"
    assert redact_url("rtsp://user@camera/stream") == "rtsp://***@camera/stream"
    assert redact_url("rtsp://camera/stream") == "rtsp://camera/stream"
    assert has_credentials("rtsp://user@camera") is True
    assert has_credentials("rtsp://camera") is False


def test_camera_round_trip() -> None:
    camera = Camera.from_dict(
        {"id": "front", "name": "Front", "url": "rtsp://camera/1", "enabled": False}
    )
    assert camera is not None
    assert camera.entity_id == "camera.front"
    payload = camera.to_integration_dict()
    assert payload == {
        "id": "front",
        "name": "Front",
        "url": "rtsp://camera/1",
        "rtsp_transport": "tcp",
        "enabled": False,
    }
    assert "status" not in payload


def test_camera_rejects_invalid_data() -> None:
    assert Camera.from_dict({"id": "", "name": "x", "url": "rtsp://camera"}) is None
    assert Camera.from_dict({"id": "x", "name": "", "url": "rtsp://camera"}) is None
    assert Camera.from_dict({"id": "x", "name": "y", "url": "nope"}) is None
    assert Camera.from_dict("not a mapping") is None  # type: ignore[arg-type]


def test_camera_api_payload_masks_credentials() -> None:
    camera = Camera(id="front", name="Front door", url="rtsp://user:secret@camera:554/stream")
    payload = camera.to_api_dict()
    assert payload["url"].startswith("rtsp://user:secret@")
    assert payload["url_masked"] == "rtsp://***:***@camera:554/stream"
    assert payload["has_credentials"] is True
    assert payload["entity_id"] == "camera.front-door"
    assert payload["status"] == "unknown"
