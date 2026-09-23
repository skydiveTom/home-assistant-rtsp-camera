"""Tests for the camera definitions parsed by the Home Assistant integration."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.config import Settings
from app.storage import CameraStore

INTEGRATION_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "rtsp_cameras"
PACKAGE_NAME = "rtsp_cameras_under_test"


@pytest.fixture(scope="module")
def models() -> ModuleType:
    """Import the integration models without touching Home Assistant."""
    package = ModuleType(PACKAGE_NAME)
    package.__path__ = [str(INTEGRATION_DIR)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = package
    return importlib.import_module(f"{PACKAGE_NAME}.models")


def test_parse_cameras_from_the_documented_object(models: ModuleType) -> None:
    cameras = models.parse_cameras(
        {
            "version": 1,
            "cameras": [
                {
                    "id": "front",
                    "name": "Front",
                    "url": "rtsp://user:pass@camera.local/stream1",
                    "rtsp_transport": "udp",
                    "enabled": True,
                }
            ],
        }
    )

    assert list(cameras) == ["front"]
    camera = cameras["front"]
    assert camera.name == "Front"
    assert camera.rtsp_transport == "udp"
    assert camera.slug == "front"


def test_parse_cameras_accepts_plain_lists(models: ModuleType) -> None:
    cameras = models.parse_cameras([{"id": "a", "name": "A", "url": "rtsp://camera/a"}])

    assert list(cameras) == ["a"]


def test_parse_cameras_skips_disabled_and_invalid_entries(models: ModuleType) -> None:
    cameras = models.parse_cameras(
        {
            "cameras": [
                {"id": "off", "name": "Off", "url": "rtsp://camera/off", "enabled": False},
                {"id": "", "name": "Broken", "url": "rtsp://camera/x"},
                {"id": "bad-url", "name": "Bad", "url": "ftp://camera"},
                {"id": "no-url", "name": "No URL"},
                "not a mapping",
                {"id": "ok", "name": "Ok", "url": "https://camera/snapshot.jpg"},
            ]
        }
    )

    assert list(cameras) == ["ok"]


@pytest.mark.parametrize("raw", [None, "nonsense", 42, {"cameras": "nonsense"}])
def test_parse_cameras_tolerates_garbage(models: ModuleType, raw: object) -> None:
    assert models.parse_cameras(raw) == {}


def test_definition_defaults(models: ModuleType) -> None:
    camera = models.RtspCameraDefinition.from_dict({"id": "gate", "url": "rtsp://camera/g"})

    assert camera is not None
    assert camera.name == "gate"
    assert camera.rtsp_transport == "tcp"
    assert camera.model == "Generic RTSP camera"
    assert camera.snapshot_url is None


def test_definition_normalises_the_transport(models: ModuleType) -> None:
    camera = models.RtspCameraDefinition.from_dict(
        {"id": "gate", "name": "Gate", "url": "rtsp://camera/g", "rtsp_transport": "WEIRD"}
    )

    assert camera is not None
    assert camera.rtsp_transport == "tcp"


def test_addon_output_is_understood_by_the_integration(
    settings: Settings, models: ModuleType
) -> None:
    store = CameraStore(settings)
    store.load()
    store.add("Front door", "rtsp://user:pass@camera.local/stream1", "tcp")
    store.add("Gate", "rtsp://camera.local/2", "udp", enabled=False)

    raw = json.loads(settings.published_file.read_text(encoding="utf-8"))
    cameras = models.parse_cameras(raw)

    assert list(cameras) == ["front-door"]
    assert cameras["front-door"].name == "Front door"
    assert cameras["front-door"].url == "rtsp://user:pass@camera.local/stream1"
