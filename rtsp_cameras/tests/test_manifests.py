"""Consistency checks for the add-on and integration manifests."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ADDON_DIR = REPO_ROOT / "rtsp_cameras"
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "rtsp_cameras"
MIRROR_DIR = ADDON_DIR / "custom_components" / "rtsp_cameras"
LANGUAGES = ("en", "de", "es", "pl")


def read_json(path: Path) -> dict:
    """Read a JSON document."""
    return json.loads(path.read_text(encoding="utf-8"))


def read_yaml(path: Path) -> dict:
    """Read a YAML document."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def addon_config() -> dict:
    """The add-on manifest."""
    return read_yaml(ADDON_DIR / "config.yaml")


def test_addon_manifest_is_complete(addon_config: dict) -> None:
    required = (
        "name",
        "version",
        "slug",
        "description",
        "arch",
        "startup",
        "boot",
        "init",
        "map",
        "ingress",
        "ingress_port",
        "panel_icon",
        "panel_title",
        "watchdog",
        "options",
        "schema",
        "hassio_api",
        "homeassistant_api",
    )
    for key in required:
        assert key in addon_config, f"config.yaml misses {key}"

    # The Home Assistant base images ship s6-overlay as ENTRYPOINT. Without
    # "init: false" Supervisor puts Docker's own init (tini) in front of it and
    # the container dies with "s6-overlay-suexec: fatal: can only run as pid 1".
    assert addon_config["init"] is False

    assert addon_config["slug"] == "rtsp_cameras"
    assert addon_config["ingress"] is True
    assert addon_config["ingress_port"] == 8099
    assert addon_config["arch"] == ["aarch64", "amd64"]
    assert addon_config["hassio_api"] is True
    assert addon_config["homeassistant_api"] is True
    assert any("homeassistant_config" in str(entry) for entry in addon_config["map"])
    assert addon_config["watchdog"] == "http://[HOST]:[PORT:8099]/health"


def test_options_and_schema_match(addon_config: dict) -> None:
    assert set(addon_config["options"]) == set(addon_config["schema"])
    assert addon_config["options"]["language"] == "auto"
    assert set(addon_config["options"]) >= {
        "language",
        "default_rtsp_transport",
        "install_integration",
        "ha_restart_after_install",
        "health_check_interval",
        "test_timeout",
        "preview_mode",
        "preview_max_height",
        "preview_fps",
        "redact_credentials_in_logs",
    }


def test_addon_translations_cover_every_option(addon_config: dict) -> None:
    options = set(addon_config["options"])
    for language in LANGUAGES:
        data = read_yaml(ADDON_DIR / "translations" / f"{language}.yaml")
        assert set(data["configuration"]) == options, f"{language} lists other options"
        for key, value in data["configuration"].items():
            assert value.get("name"), f"{language}:{key} has no name"
            assert value.get("description"), f"{language}:{key} has no description"
        assert "8099/TCP" in data["network"]


def test_versions_are_in_sync(addon_config: dict) -> None:
    manifest = read_json(INTEGRATION_DIR / "manifest.json")
    version = addon_config["version"]

    assert manifest["version"] == version
    assert f'__version__ = "{version}"' in (ADDON_DIR / "app" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert f'ADDON_VERSION = "{version}"' in (ADDON_DIR / "app" / "config.py").read_text(
        encoding="utf-8"
    )


def test_integration_manifest_is_valid() -> None:
    manifest = read_json(INTEGRATION_DIR / "manifest.json")

    assert manifest["domain"] == "rtsp_cameras"
    assert manifest["config_flow"] is True
    assert manifest["dependencies"] == ["stream"]
    assert manifest["integration_type"] == "hub"
    assert manifest["codeowners"] == ["@skydiveTom"]
    assert manifest["documentation"].startswith("https://github.com/skydiveTom/")
    assert manifest["issue_tracker"].startswith("https://github.com/skydiveTom/")


def test_integration_copies_are_identical() -> None:
    source = sorted(
        path.relative_to(INTEGRATION_DIR)
        for path in INTEGRATION_DIR.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    mirror = sorted(
        path.relative_to(MIRROR_DIR)
        for path in MIRROR_DIR.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    assert source, "the integration source folder is empty"
    assert source == mirror, "run scripts/sync_integration.ps1"
    for relative in source:
        assert (INTEGRATION_DIR / relative).read_bytes() == (
            MIRROR_DIR / relative
        ).read_bytes(), f"{relative} differs between the two copies"


def test_prebuilt_image_matches_the_manifest(addon_config: dict) -> None:
    """The published image, the workflow and the manifest must agree."""
    workflow = read_yaml(REPO_ROOT / ".github" / "workflows" / "build-addon.yml")
    triggers = workflow.get("on") or workflow.get(True)

    image_name = workflow["env"]["IMAGE_NAME"]
    assert addon_config["image"] == f"ghcr.io/skydiveTom/{image_name}"
    assert "{arch}" not in addon_config["image"], "reference the multi-arch manifest"

    assert "main" in triggers["push"]["branches"]
    assert ".github/workflows/build-addon.yml" in triggers["push"]["paths"]

    prepare_steps = workflow["jobs"]["prepare"]["steps"]
    version_script = next(step["run"] for step in prepare_steps if step.get("id") == "version")
    matrix_script = next(step["run"] for step in prepare_steps if step.get("id") == "matrix")
    assert "rtsp_cameras/config.yaml" in version_script
    for token in ("amd64", "aarch64", "linux/amd64", "linux/arm64"):
        assert token in matrix_script, f"the build matrix misses {token}"

    build_step = next(
        step
        for step in workflow["jobs"]["build"]["steps"]
        if step.get("name", "").startswith("Build and push")
    )
    build_script = build_step["run"]
    for token in (
        "docker build",
        "docker push",
        '--platform "${PLATFORM}"',
        '--build-arg "BUILD_VERSION=${VERSION}"',
        '--build-arg "BUILD_ARCH=${ARCH}"',
        "${ARCH}-${IMAGE_NAME}:${VERSION}",
        "rtsp_cameras",
        "::error title=Docker build failed",
    ):
        assert token in build_script, f"the build step misses {token}"
    assert (REPO_ROOT / "rtsp_cameras" / "Dockerfile").is_file()

    manifest_script = next(
        step["run"] for step in workflow["jobs"]["manifest"]["steps"] if "run" in step
    )
    assert "buildx imagetools create" in manifest_script
    assert "amd64-${IMAGE_NAME}" in manifest_script
    assert "aarch64-${IMAGE_NAME}" in manifest_script
    assert ":${VERSION}" in manifest_script
    assert ":latest" in manifest_script

    actions = [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]
    assert not any("home-assistant/builder" in action for action in actions), (
        "the legacy builder action is deprecated"
    )


def test_addon_files_exist() -> None:
    for name in (
        ".dockerignore",
        "CHANGELOG.md",
        "DOCS.md",
        "Dockerfile",
        "README.md",
        "config.yaml",
        "icon.png",
        "logo.png",
        "requirements.txt",
        "run.sh",
    ):
        assert (ADDON_DIR / name).is_file(), f"missing {name}"


def test_icons_have_the_expected_size() -> None:
    def png_size(path: Path) -> tuple[int, int]:
        data = path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
        width, height = struct.unpack(">II", data[16:24])
        return width, height

    assert png_size(ADDON_DIR / "icon.png") == (128, 128)
    assert png_size(ADDON_DIR / "logo.png") == (250, 100)


def test_repository_metadata() -> None:
    repository = read_yaml(REPO_ROOT / "repository.yaml")
    hacs = read_json(REPO_ROOT / "hacs.json")

    assert repository["name"]
    assert repository["url"].endswith("home-assistant-rtsp-camera")
    assert repository["maintainer"]
    assert hacs["name"] == "RTSP Camera Manager"


def test_dockerfile_matches_the_configured_paths() -> None:
    from pathlib import PurePosixPath

    from app.config import DEFAULT_INGRESS_PORT, DEFAULT_INTEGRATION_SOURCE

    dockerfile = (ADDON_DIR / "Dockerfile").read_text(encoding="utf-8")
    integration_parent = str(PurePosixPath(DEFAULT_INTEGRATION_SOURCE).parent)

    assert integration_parent in dockerfile
    assert "ghcr.io/home-assistant/base-python" in dockerfile
    assert '"app"' in dockerfile
    assert DEFAULT_INGRESS_PORT == read_yaml(ADDON_DIR / "config.yaml")["ingress_port"]


def test_run_script_is_usable_inside_the_container() -> None:
    data = (ADDON_DIR / "run.sh").read_bytes()

    assert b"\r\n" not in data, "run.sh must use LF line endings"
    assert data.startswith(b"#!/usr/bin/env bash")
    assert b"python3 -m app" in data

    # The base images ship bashio as a wrapper that has to run the script, so that
    # bashio:: functions are available inside run.sh.
    dockerfile = (ADDON_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["bashio", "/run.sh"]' in dockerfile


def test_web_assets_are_present() -> None:
    static = ADDON_DIR / "app" / "static"
    for name in ("app.css", "app.js", "icon.svg", "hls.min.js"):
        assert (static / name).is_file(), f"missing static/{name}"

    assert (static / "hls.min.js").stat().st_size > 100_000
    assert (static / "app.js").stat().st_size > 10_000


def test_interface_template_uses_the_ingress_base_path() -> None:
    template = (ADDON_DIR / "app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert "{{ base_path }}/static/app.css" in template
    assert "{{ base_path }}/static/app.js" in template
    assert 'id="bootstrap"' in template
    assert 'data-i18n="app.title"' in template


def test_repository_files_keep_build_artefacts_out() -> None:
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".venv/", "__pycache__/", ".pytest_cache/"):
        assert pattern in ignore

    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attributes

