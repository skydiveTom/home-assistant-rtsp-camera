"""Tests for the brand assets of the integration.

Home Assistant serves local brand images from ``custom_components/<domain>/brand``
(``Integration.has_branding`` is true when that folder exists) and the integrations
dashboard shows them next to the entry. A missing or wrongly sized file would only
show up in the browser, so it is guarded here.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "rtsp_cameras"
ADDON_MIRROR = REPO_ROOT / "rtsp_cameras" / "custom_components" / "rtsp_cameras"
BRAND_DIR = INTEGRATION_DIR / "brand"

#: Home Assistant looks for these names (see the brands integration).
EXPECTED = {
    "icon.png": 256,
    "icon@2x.png": 512,
    "dark_icon.png": 256,
    "dark_icon@2x.png": 512,
}

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def read_ihdr(path: Path) -> tuple[int, int, int, int]:
    """Return width, height, bit depth and colour type of a PNG."""
    data = path.read_bytes()
    assert data[:8] == PNG_MAGIC, f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    bit_depth, colour_type = data[24], data[25]
    return width, height, bit_depth, colour_type


def test_brand_folder_exists() -> None:
    """The folder is what tells Home Assistant that branding is available."""
    assert BRAND_DIR.is_dir(), "custom_components/rtsp_cameras/brand is missing"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_brand_image_is_valid(name: str) -> None:
    """Every image is a square RGBA PNG of the expected size."""
    path = BRAND_DIR / name
    assert path.is_file(), f"{name} is missing"

    width, height, bit_depth, colour_type = read_ihdr(path)
    expected = EXPECTED[name]
    assert (width, height) == (expected, expected), f"{name} must be {expected}x{expected}"
    assert bit_depth == 8, f"{name} must use 8 bit per channel"
    assert colour_type == 6, f"{name} must be RGBA (transparency for both themes)"
    assert path.stat().st_size > 1000, f"{name} looks empty"


def test_brand_images_are_shipped_with_the_addon() -> None:
    """The add-on image installs the integration, so the icons travel with it."""
    for name in EXPECTED:
        source = BRAND_DIR / name
        mirror = ADDON_MIRROR / "brand" / name
        assert mirror.is_file(), f"{name} is missing in the add-on copy"
        assert source.read_bytes() == mirror.read_bytes(), f"{name} differs between the copies"


def test_dark_icon_differs_from_the_light_one() -> None:
    """A dark theme needs its own artwork, not a fallback to the light icon."""
    assert (BRAND_DIR / "icon.png").read_bytes() != (BRAND_DIR / "dark_icon.png").read_bytes()
