"""Pytest configuration for the tests that run against a real Home Assistant.

These tests need ``pytest-homeassistant-custom-component`` (and therefore a
matching Home Assistant release) and are kept apart from the fast unit tests in
``rtsp_cameras/tests`` on purpose::

    py -3.14 -m venv .venv-ha
    .venv-ha\\Scripts\\python -m pip install pytest-homeassistant-custom-component
    .venv-ha\\Scripts\\python -m pytest -c ha_tests/pytest.ini ha_tests
"""

from __future__ import annotations

import shutil
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_turbojpeg_stub() -> None:
    """Provide a stand-in for the native PyTurboJPEG bindings when missing.

    ``homeassistant.components.camera`` imports ``turbojpeg`` at module level,
    which needs the native libjpeg-turbo library. Image scaling is not exercised
    by these tests, so a stub keeps the suite runnable everywhere.
    """
    try:
        import turbojpeg  # noqa: F401, PLC0415
    except ImportError:
        pass
    else:
        return

    module = types.ModuleType("turbojpeg")

    class TurboJPEG:  # noqa: D101
        """Placeholder that refuses to process images."""

        def decode_header(self, jpeg_buf: bytes) -> tuple[int, int, int, int]:
            """Refuse to decode images."""
            raise OSError("PyTurboJPEG is not installed in this environment")

        def scale(self, *args: object, **kwargs: object) -> bytes:
            """Refuse to scale images."""
            raise OSError("PyTurboJPEG is not installed in this environment")

    module.TurboJPEG = TurboJPEG
    sys.modules["turbojpeg"] = module


_install_turbojpeg_stub()

if sys.platform == "win32":
    # The Home Assistant test harness blocks every socket, keeping only AF_UNIX
    # in place for asyncio. Windows has no AF_UNIX socketpair, so even creating
    # the event loop would fail - keep the guard out of the way on this platform.
    import pytest_socket

    pytest_socket.disable_socket = lambda *args, **kwargs: None


def testing_config_dir() -> Path:
    """Return the configuration directory used by the Home Assistant harness."""
    import pytest_homeassistant_custom_component

    return Path(pytest_homeassistant_custom_component.__file__).parent / "testing_config"


@pytest.fixture(autouse=True)
def clean_camera_state():
    """Start every test with an empty camera file and a clean registry."""
    config_dir = testing_config_dir()
    for leftover in (config_dir / "rtsp_cameras", config_dir / ".storage"):
        if leftover.exists():
            shutil.rmtree(leftover, ignore_errors=True)
    yield
    shutil.rmtree(config_dir / "rtsp_cameras", ignore_errors=True)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(hass, enable_custom_integrations):
    """Make the integration of this repository visible to the test core."""
    source = REPO_ROOT / "custom_components" / "rtsp_cameras"
    target = Path(hass.config.path("custom_components", "rtsp_cameras"))
    if source.resolve() != target.resolve() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
    yield
