"""Draw the brand images of the integration (and the add-on icon/logo).

Home Assistant serves local brand assets from ``custom_components/<domain>/brand``
when that folder exists (``Integration.has_branding``): ``icon.png`` (256x256),
``icon@2x.png`` (512x512) and their ``dark_*`` variants, which the integrations
dashboard uses for the light and the dark theme.

The artwork is rasterised with numpy and written with PyAV, so no image library has
to be installed - both come with the Home Assistant test environment::

    .venv-ha\\Scripts\\python scripts/make_brand_icons.py

The generated files are committed, so this script is only needed when the artwork
changes.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import av
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_BRAND = ROOT / "custom_components" / "rtsp_cameras" / "brand"
ADDON_DIR = ROOT / "rtsp_cameras"

SUPERSAMPLE = 4
GRID = 256  # the artwork is designed on a 256x256 grid
CYAN = (0x22, 0xD3, 0xEE)
DARK_BODY = (0x10, 0x1C, 0x26)
LIGHT_BODY = (0xE7, 0xEE, 0xF6)
INK = (0x0B, 0x14, 0x1C)


def canvas(size: int) -> np.ndarray:
    """Return a transparent RGBA canvas in supersampled resolution."""
    return np.zeros((size, size, 4), dtype=np.uint8)


def _grid(size: int) -> tuple[np.ndarray, np.ndarray]:
    """Return supersampled pixel coordinates."""
    return np.mgrid[0:size, 0:size]


def rounded_rect(
    size: int, x0: float, y0: float, x1: float, y1: float, radius: float
) -> np.ndarray:
    """Return a mask of a rounded rectangle (coordinates on the 256 grid)."""
    yy, xx = _grid(size)
    x0, y0, x1, y1, radius = (v * SUPERSAMPLE for v in (x0, y0, x1, y1, radius))
    inner_x = np.clip(xx, x0 + radius, x1 - radius)
    inner_y = np.clip(yy, y0 + radius, y1 - radius)
    return (np.hypot(xx - inner_x, yy - inner_y) <= radius) & (xx >= x0) & (xx <= x1) & (yy >= y0) & (
        yy <= y1
    )


def disc(size: int, cx: float, cy: float, radius: float) -> np.ndarray:
    """Return a mask of a filled circle."""
    yy, xx = _grid(size)
    return np.hypot(xx - cx * SUPERSAMPLE, yy - cy * SUPERSAMPLE) <= radius * SUPERSAMPLE


def arc(
    size: int, cx: float, cy: float, r_out: float, r_in: float, start: float, end: float
) -> np.ndarray:
    """Return a mask of a ring segment (degrees, counter clockwise from 3 o'clock)."""
    yy, xx = _grid(size)
    dx = xx - cx * SUPERSAMPLE
    dy = yy - cy * SUPERSAMPLE
    distance = np.hypot(dx, dy)
    angle = np.degrees(np.arctan2(-dy, dx)) % 360
    inside = (distance <= r_out * SUPERSAMPLE) & (distance >= r_in * SUPERSAMPLE)
    return inside & (angle >= start) & (angle <= end)


def triangle(size: int, points: list[tuple[float, float]]) -> np.ndarray:
    """Return a mask of a convex polygon (cross product test)."""
    yy, xx = _grid(size)
    mask = np.ones((size, size), dtype=bool)
    scaled = [(x * SUPERSAMPLE, y * SUPERSAMPLE) for x, y in points]
    for index, (x0, y0) in enumerate(scaled):
        x1, y1 = scaled[(index + 1) % len(scaled)]
        mask &= (x1 - x0) * (yy - y0) - (y1 - y0) * (xx - x0) >= 0
    return mask


def box(size: int, x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
    """Return a mask of a plain rectangle."""
    yy, xx = _grid(size)
    return (
        (xx >= x0 * SUPERSAMPLE)
        & (xx <= x1 * SUPERSAMPLE)
        & (yy >= y0 * SUPERSAMPLE)
        & (yy <= y1 * SUPERSAMPLE)
    )


def paint(layer: np.ndarray, mask: np.ndarray, color: tuple[int, int, int]) -> None:
    """Composite a colour onto a layer with the given mask."""
    layer[mask] = (color[0], color[1], color[2], 255)



def draw_glyph(body: tuple[int, int, int], canvas_size: int) -> np.ndarray:
    """Draw the camera with the rotation arc on a transparent canvas.

    ``canvas_size`` is the supersampled canvas; the design lives on the 256 grid, so
    the glyph fills a canvas of ``side * SUPERSAMPLE`` pixels.
    """
    scale = canvas_size / (GRID * SUPERSAMPLE)

    def s(value: float) -> float:
        return value * scale

    layer = canvas(canvas_size)
    # Body with the viewfinder bump on top, then lens, ring and pupil.
    paint(layer, rounded_rect(canvas_size, s(30), s(74), s(226), s(214), s(26)), body)
    paint(layer, rounded_rect(canvas_size, s(92), s(52), s(164), s(84), s(10)), body)
    paint(layer, disc(canvas_size, s(128), s(146), s(44)), CYAN)
    paint(layer, disc(canvas_size, s(128), s(146), s(30)), INK)
    paint(layer, disc(canvas_size, s(128), s(146), s(13)), CYAN)
    # A rotation arc with an arrow head in the upper right corner (PTZ).
    paint(layer, arc(canvas_size, s(128), s(146), s(118), s(98), -60, 45), CYAN)
    paint(
        layer,
        triangle(canvas_size, [(s(168), s(24)), (s(200), s(28)), (s(176), s(62))]),
        CYAN,
    )
    return layer


def downsample(layer: np.ndarray) -> np.ndarray:
    """Average the supersampled layer back to the requested size."""
    height, width, channels = layer.shape
    reduced = layer.reshape(
        height // SUPERSAMPLE, SUPERSAMPLE, width // SUPERSAMPLE, SUPERSAMPLE, channels
    )
    return reduced.mean(axis=(1, 3)).round().astype(np.uint8)


def write_png(path: Path, image: np.ndarray) -> None:
    """Write an RGBA numpy array as PNG (through PyAV, no image library needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = av.VideoFrame.from_ndarray(image, format="rgba")
    buffer = io.BytesIO()
    with av.open(buffer, mode="w", format="image2pipe") as container:
        stream = container.add_stream("png", rate=1)
        stream.width = image.shape[1]
        stream.height = image.shape[0]
        stream.pix_fmt = "rgba"
        container.mux(stream.encode(frame))
        container.mux(stream.encode(None))
    path.write_bytes(buffer.getvalue())


def main() -> int:
    """Write every brand image of the integration."""
    for factor, suffix in ((1, ""), (2, "@2x")):
        target_size = GRID * factor
        for variant, body in (("icon", DARK_BODY), ("dark_icon", LIGHT_BODY)):
            target = INTEGRATION_BRAND / f"{variant}{suffix}.png"
            layer = draw_glyph(body, target_size * SUPERSAMPLE)
            write_png(target, downsample(layer))
            print("wrote", target.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
