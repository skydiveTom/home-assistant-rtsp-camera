"""Tests for the camera store."""

from __future__ import annotations

import json

from app.config import Settings
from app.storage import CameraStore


def read_published(settings: Settings) -> dict:
    """Return the JSON document published for the integration."""
    return json.loads(settings.published_file.read_text(encoding="utf-8"))


def test_add_update_delete_round_trip(settings: Settings) -> None:
    store = CameraStore(settings)
    store.load()

    camera = store.add("Front door", "rtsp://user:pass@camera:554/stream", "tcp")
    assert camera.id == "front-door"
    assert settings.cameras_file.is_file()
    assert settings.published_file.is_file()

    published = read_published(settings)
    assert published["version"] == 1
    assert published["generator"] == "RTSP Camera Manager add-on"
    assert published["cameras"][0]["id"] == "front-door"
    assert "status" not in published["cameras"][0]

    updated = store.update(camera.id, name="Hall", enabled=False)
    assert updated is not None
    assert updated.name == "Hall"
    assert updated.enabled is False
    assert read_published(settings)["cameras"][0]["name"] == "Hall"

    assert store.delete(camera.id) is True
    assert store.count() == 0
    assert store.delete("missing") is False
    assert read_published(settings)["cameras"] == []


def test_duplicate_names_get_suffixes(settings: Settings) -> None:
    store = CameraStore(settings)
    store.load()

    first = store.add("Gate", "rtsp://camera/1")
    second = store.add("Gate", "rtsp://camera/2")
    third = store.add("gate", "rtsp://camera/3")

    assert [first.id, second.id, third.id] == ["gate", "gate-2", "gate-3"]
    assert store.unique_id("Gate", exclude="gate") == "gate"
    assert store.unique_id("Front door") == "front-door"


def test_load_imports_the_published_file(settings: Settings) -> None:
    published = settings.published_file
    published.parent.mkdir(parents=True, exist_ok=True)
    published.write_text(
        json.dumps({"cameras": [{"id": "yard", "name": "Yard", "url": "rtsp://camera/yard"}]}),
        encoding="utf-8",
    )

    store = CameraStore(settings)
    store.load()

    assert store.count() == 1
    imported = store.get("yard")
    assert imported is not None
    assert imported.name == "Yard"
    assert settings.cameras_file.is_file()


def test_load_skips_broken_files(settings: Settings) -> None:
    settings.cameras_file.write_text("{ not json", encoding="utf-8")

    store = CameraStore(settings)
    store.load()

    assert store.count() == 0


def test_save_reports_publish_errors(settings: Settings) -> None:
    store = CameraStore(settings)
    store.load()
    store.add("Front", "rtsp://camera/1")

    published = settings.published_file
    published.unlink()
    published.mkdir()

    assert store.save() is False
    assert store.publish_error == str(published)


def test_update_ignores_unknown_camera(settings: Settings) -> None:
    store = CameraStore(settings)
    store.load()

    assert store.update("missing", name="x") is None
