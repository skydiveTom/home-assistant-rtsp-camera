"""Checks that Home Assistant finds the brand assets of the integration.

The integrations dashboard asks the ``brands`` integration for
``/api/brands/integration/rtsp_cameras/icon.png``; that endpoint serves local files
from ``custom_components/rtsp_cameras/brand`` when the integration reports branding.
This test walks the same path Home Assistant does, so a renamed or missing folder
fails here instead of in the browser.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_custom_components

import custom_components.rtsp_cameras as integration_module

IMAGES = ("icon.png", "icon@2x.png", "dark_icon.png", "dark_icon@2x.png")


async def test_home_assistant_finds_the_brand_assets(hass: HomeAssistant) -> None:
    """The brand folder is where the brands view looks for it."""
    brand_dir = Path(integration_module.__file__).parent / "brand"

    components = await async_get_custom_components(hass)
    integration = components.get("rtsp_cameras")
    if integration is not None and hasattr(integration, "has_branding"):
        # Home Assistant decides from its own metadata whether branding exists.
        assert integration.has_branding is True
        brand_dir = Path(integration.file_path) / "brand"

    assert brand_dir.is_dir(), f"brand folder missing in {brand_dir}"
    for name in IMAGES:
        assert (brand_dir / name).is_file(), f"{name} is missing in {brand_dir}"
