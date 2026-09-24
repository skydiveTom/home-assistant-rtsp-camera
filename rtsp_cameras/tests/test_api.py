"""Tests for the HTTP API of the add-on."""

from __future__ import annotations

import json
import shutil

import pytest
from starlette.testclient import TestClient

from app.config import Settings
from tests.fake_ffmpeg import JPEG
from tests.helpers import add_camera


def test_index_renders_the_bootstrap(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "RTSP Camera Manager" in response.text
    assert 'id="bootstrap"' in response.text
    assert "/static/app.js" in response.text
    assert "/static/app.css" in response.text
    assert "camera-modal" in response.text
    assert "preview-modal" in response.text


def test_index_uses_the_ingress_base_path(client: TestClient) -> None:
    response = client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/token/"})

    assert "/api/hassio_ingress/token/static/app.css" in response.text
    assert '"base_path": "/api/hassio_ingress/token"' in response.text


def test_index_follows_the_requested_language(client: TestClient) -> None:
    assert 'lang="pl"' in client.get("/?lang=pl").text
    assert 'lang="de"' in client.get("/", headers={"Accept-Language": "de-DE,de;q=0.9"}).text
    assert 'lang="en"' in client.get("/", headers={"Accept-Language": "fr-FR"}).text


def test_health_endpoint(client: TestClient) -> None:
    payload = client.get("/health").json()

    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["cameras"] == 0
    assert payload["ffmpeg"] is True


def test_meta_reports_settings(client: TestClient, settings: Settings) -> None:
    payload = client.get("/api/meta").json()

    assert payload["settings"]["cameras_file"] == str(settings.cameras_file)
    assert payload["settings"]["ffmpeg_available"] is True
    assert payload["integration"]["installed"] is True
    assert payload["cameras"] == []


def test_create_camera_writes_both_files(client: TestClient, settings: Settings) -> None:
    camera = add_camera(client)

    assert camera["id"] == "front-door"
    assert camera["url_masked"] == "rtsp://***:***@192.168.1.10:554/stream1"
    assert camera["entity_id"] == "camera.front-door"
    assert camera["status"] == "unknown"

    assert settings.cameras_file.is_file()
    published = json.loads(settings.published_file.read_text(encoding="utf-8"))
    assert published["generator"] == "RTSP Camera Manager add-on"
    assert published["cameras"][0]["url"] == "rtsp://user:pass@192.168.1.10:554/stream1"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"url": "rtsp://camera/1"}, "name_required"),
        ({"name": "Gate"}, "url_required"),
        ({"name": "Gate", "url": "ftp://camera"}, "url_scheme"),
        ({"name": "Gate", "url": "rtsp://"}, "url_invalid"),
    ],
)
def test_create_camera_validates_input(client: TestClient, body: dict, code: str) -> None:
    response = client.post("/api/cameras", json=body)

    assert response.status_code == 400
    assert response.json()["error"] == code
    assert response.json()["ok"] is False


def test_update_and_delete_camera(client: TestClient) -> None:
    camera = add_camera(client)

    updated = client.put(
        f"/api/cameras/{camera['id']}",
        json={"name": "Hall", "enabled": False, "rtsp_transport": "udp"},
    )
    assert updated.status_code == 200
    payload = updated.json()["camera"]
    assert payload["name"] == "Hall"
    assert payload["enabled"] is False
    assert payload["rtsp_transport"] == "udp"

    deleted = client.delete(f"/api/cameras/{camera['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["cameras"] == []
    assert client.get(f"/api/cameras/{camera['id']}").status_code == 404


def test_update_camera_rejects_broken_urls(client: TestClient) -> None:
    camera = add_camera(client)

    response = client.put(f"/api/cameras/{camera['id']}", json={"url": "not-a-url"})

    assert response.status_code == 400
    assert response.json()["error"] == "url_scheme"


def test_update_camera_requires_a_name(client: TestClient) -> None:
    camera = add_camera(client)

    response = client.put(f"/api/cameras/{camera['id']}", json={"name": "   "})

    assert response.status_code == 400
    assert response.json()["error"] == "name_required"


def test_camera_endpoints_report_missing_cameras(client: TestClient) -> None:
    assert client.get("/api/cameras/nope").status_code == 404
    assert client.put("/api/cameras/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/cameras/nope").status_code == 404
    assert client.post("/api/cameras/nope/test").status_code == 404
    assert client.get("/api/cameras/nope/snapshot.jpg").status_code == 404
    assert client.get("/api/cameras/nope/mjpeg").status_code == 404


def test_probe_endpoint_tests_unsaved_urls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PROBE_MODE", "ok")

    payload = client.post("/api/probe", json={"url": "rtsp://camera/1"}).json()

    assert payload["probe"]["ok"] is True
    assert payload["probe"]["details"]["resolution"] == "1920x1080"
    assert payload["probe"]["details"]["codec"] == "h264"
    assert payload["camera"] is None


def test_probe_endpoint_validates_urls(client: TestClient) -> None:
    response = client.post("/api/probe", json={"url": "nope://camera"})

    assert response.status_code == 400
    assert response.json()["error"] == "url_scheme"


def test_camera_test_updates_the_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera = add_camera(client)

    monkeypatch.setenv("FAKE_PROBE_MODE", "ok")
    payload = client.post(f"/api/cameras/{camera['id']}/test").json()
    assert payload["camera"]["status"] == "online"
    assert payload["camera"]["last_probe"]["details"]["codec"] == "h264"

    monkeypatch.setenv("FAKE_PROBE_MODE", "fail")
    payload = client.post(f"/api/cameras/{camera['id']}/test").json()
    assert payload["camera"]["status"] == "offline"
    assert "secret" not in (payload["camera"]["last_error"] or "")

    listed = client.get("/api/cameras").json()["cameras"][0]
    assert listed["status"] == "offline"


def test_snapshot_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    camera = add_camera(client)

    monkeypatch.setenv("FAKE_FFMPEG_MODE", "snapshot")
    response = client.get(f"/api/cameras/{camera['id']}/snapshot.jpg")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == JPEG

    monkeypatch.setenv("FAKE_FFMPEG_MODE", "fail")
    assert client.get(f"/api/cameras/{camera['id']}/snapshot.jpg").status_code == 502


def test_mjpeg_endpoint_streams_frames(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera = add_camera(client)
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "mjpeg")
    monkeypatch.setenv("FAKE_FRAMES", "2")

    with client.stream("GET", f"/api/cameras/{camera['id']}/mjpeg") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("multipart/x-mixed-replace")
        first_chunk = next(response.iter_bytes())

    assert b"--rtspcamframe" in first_chunk
    assert JPEG in first_chunk


def test_mjpeg_endpoint_reports_broken_streams(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera = add_camera(client)
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "fail")

    response = client.get(f"/api/cameras/{camera['id']}/mjpeg")

    assert response.status_code == 502
    payload = response.json()
    assert payload["error"] == "stream_failed"
    assert "Connection refused" in payload["detail"], "ffmpeg's message must be visible"


def test_hls_endpoints(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    camera = add_camera(client)
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "hls")

    started = client.post(f"/api/cameras/{camera['id']}/hls/start")
    assert started.status_code == 200
    playlist = started.json()["playlist"]
    assert playlist == f"api/cameras/{camera['id']}/hls/index.m3u8"

    served = client.get(f"/{playlist}")
    assert served.status_code == 200
    assert b"#EXTM3U" in served.content

    segment = client.get(f"/api/cameras/{camera['id']}/hls/segment_000.ts")
    assert segment.status_code == 200
    assert segment.headers["content-type"] == "video/mp2t"

    assert client.get(f"/api/cameras/{camera['id']}/hls/missing.ts").status_code == 404
    assert client.get(f"/api/cameras/{camera['id']}/hls/bad!name.ts").status_code == 404
    assert client.get(f"/api/cameras/{camera['id']}/hls/%2e%2e%2fsecret").status_code == 404

    client.delete(f"/api/cameras/{camera['id']}")
    assert client.get(f"/{playlist}").status_code == 409


def test_integration_install_endpoint(client: TestClient, settings: Settings) -> None:
    status = client.get("/api/integration").json()["integration"]
    assert status["installed"] is True
    assert (settings.integration_target / "manifest.json").is_file()
    assert (settings.integration_target / "camera.py").is_file()

    installed = client.post("/api/integration/install", json={})

    assert installed.status_code == 200
    integration = installed.json()["integration"]
    assert integration["installed"] is True
    assert integration["needs_restart"] is True
    assert integration["version"] == integration["source_version"]


def test_integration_install_reports_a_missing_source(
    client: TestClient, settings: Settings
) -> None:
    shutil.rmtree(settings.integration_source)

    response = client.post("/api/integration/install", json={})

    assert response.status_code == 500
    assert response.json()["error"] == "source_missing"


def test_restart_requires_a_supervisor_token(client: TestClient) -> None:
    response = client.post("/api/ha/restart")

    assert response.status_code == 502
    assert response.json()["error"] == "restart_failed"


def test_endpoints_report_missing_ffmpeg(bare_client: TestClient) -> None:
    camera = add_camera(bare_client)

    assert bare_client.get("/health").json()["ffmpeg"] is False
    probe = bare_client.post("/api/probe", json={"url": "rtsp://camera/1"})
    assert probe.status_code == 503
    assert probe.json()["error"] == "ffmpeg_missing"
    assert bare_client.get(f"/api/cameras/{camera['id']}/snapshot.jpg").status_code == 503
    assert bare_client.get(f"/api/cameras/{camera['id']}/mjpeg").status_code == 503
    assert bare_client.post(f"/api/cameras/{camera['id']}/hls/start").status_code == 503

