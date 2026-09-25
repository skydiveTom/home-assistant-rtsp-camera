"""Checks that Home Assistant finds the brand assets of the integration.

The integrations dashboard asks the ``brands`` integration for
``/api/brands/integration/rtsp_cameras/icon.png``; that endpoint serves local files
from ``custom_components/rtsp_cameras/brand`` when the integration has branding.
This test uses Home Assistant's own loader, so it fails as soon as the folder is
missing or renamed.
"""

from __future__ import annotations

import os
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_custom_components


async def test_home_assistant_finds_the_brand_assets(hass: HomeAssistant) -> None:
    """The loader reports branding and the icon sits where it is looked for."""
    components = await async_get_custom_components(hass)
    integration = components["rtsp_cameras"]

    brand_dir = Path(integration.file_path) / "brand"
    listing = sorted(os.listdir(integration.file_path))
    assert brand_dir.is_dir(), f"brand folder missing in {integration.file_path} ({listing})"
    assert integration.has_branding is True, f"loader sees {listing}"

    for name in ("icon.png", "icon@2x.png", "dark_icon.png", "dark_icon@2x.png"):
        assert (brand_dir / name).is_file(), f"{name} is missing"
