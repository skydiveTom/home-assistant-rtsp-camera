"""Shared fixtures for the add-on test suite."""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

TESTS_DIR = Path(__file__).resolve().parent
ADDON_DIR = TESTS_DIR.parent
REPO_ROOT = ADDON_DIR.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "rtsp_cameras"
FAKE_FFPROBE = TESTS_DIR / "fake_ffprobe.py"
FAKE_FFMPEG = TESTS_DIR / "fake_ffmpeg.py"

if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Provide a temporary replacement for /data, /config and /tmp."""
    for name in ("data", "config", "temp"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def integration_source(tmp_path: Path) -> Path:
    """Provide a writable copy of the bundled integration."""
    target = tmp_path / "integration_source"
    shutil.copytree(INTEGRATION_DIR, target)
    return target


def build_settings(
    workspace: Path,
    options: dict | None = None,
    integration_source: Path | None = None,
    **overrides: str,
) -> Settings:
    """Create settings that point at the temporary folders."""
    data_dir = workspace / "data"
    (data_dir / "options.json").write_text(json.dumps(options or {}), encoding="utf-8")
    environment = {
        "RTSP_ADDON_DATA_DIR": str(data_dir),
        "RTSP_ADDON_CONFIG_DIR": str(workspace / "config"),
        "RTSP_ADDON_TEMP_DIR": str(workspace / "temp"),
        "RTSP_ADDON_INTEGRATION_SOURCE": str(
            integration_source or workspace / "integration_source"
        ),
        "RTSP_ADDON_VERSION": "0.1.0",
        "RTSP_ADDON_INGRESS_PORT": "8099",
    }
    environment.update(overrides)
    return Settings.load(env=environment)


@pytest.fixture()
def settings(workspace: Path, integration_source: Path) -> Settings:
    """Add-on settings pointing at the temporary workspace."""
    return build_settings(workspace, integration_source=integration_source)


@pytest.fixture()
def fake_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ffmpeg and ffprobe with the fake scripts."""

    def fake_binary(name: str) -> list[str] | None:
        script = FAKE_FFPROBE if name == "ffprobe" else FAKE_FFMPEG
        return [sys.executable, str(script)]

    monkeypatch.setattr("app.ffmpeg.binary_command", fake_binary)


@pytest.fixture()
def no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Emulate a container without ffmpeg and ffprobe."""
    monkeypatch.setattr("app.ffmpeg.binary_command", lambda name: None)


@pytest.fixture()
def client(settings: Settings, fake_tools: None) -> Iterator[TestClient]:
    """A TestClient with working (fake) ffmpeg binaries."""
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture()
def bare_client(settings: Settings, no_tools: None) -> Iterator[TestClient]:
    """A TestClient for a container without ffmpeg."""
    with TestClient(create_app(settings)) as test_client:
        yield test_client
