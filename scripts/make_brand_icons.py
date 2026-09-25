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
import math
import sys
from pathlib import Path

import av
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_BRAND = ROOT / "custom_components" / "rtsp_cameras" / "brand"
ADDON_DIR = ROOT / "rtsp_cameras"

SUPERSAMPLE = 4
#: The panel paints the glyph with ``--accent`` (index.html: .brand__mark).
ACCENT = (0x22, 0xD3, 0xEE)


def canvas(size: int) -> np.ndarray:
    """Return a transparent RGBA canvas in supersampled resolution."""
    return np.zeros((size, size, 4), dtype=np.uint8)


def _grid(size: int) -> tuple[np.ndarray, np.ndarray]:
    """Return supersampled pixel coordinates."""
    return np.mgrid[0:size, 0:size]


def stroke(outer: np.ndarray, inner: np.ndarray) -> np.ndarray:
    """Return the outline between two masks (outer minus inner)."""
    return outer & ~inner


def rounded_rect_mask(
    size: int, x0: float, y0: float, x1: float, y1: float, radius: float
) -> np.ndarray:
    """Return a mask of a rounded rectangle in supersampled pixels."""
    yy, xx = _grid(size)
    x0, y0, x1, y1, radius = (v * SUPERSAMPLE for v in (x0, y0, x1, y1, radius))
    inner_x = np.clip(xx, x0 + radius, x1 - radius)
    inner_y = np.clip(yy, y0 + radius, y1 - radius)
    inside = (np.hypot(xx - inner_x, yy - inner_y) <= radius) & (xx >= x0) & (xx <= x1)
    return inside & (yy >= y0) & (yy <= y1)


def disc_mask(size: int, cx: float, cy: float, radius: float) -> np.ndarray:
    """Return a mask of a filled circle in supersampled pixels."""
    yy, xx = _grid(size)
    return np.hypot(xx - cx * SUPERSAMPLE, yy - cy * SUPERSAMPLE) <= radius * SUPERSAMPLE


def arc_mask(
    size: int, cx: float, cy: float, radius: float, start: float, end: float, width: float
) -> np.ndarray:
    """Return the outline of a circular arc (degrees, counter clockwise from 3 o'clock)."""
    yy, xx = _grid(size)
    dx = xx - cx * SUPERSAMPLE
    dy = yy - cy * SUPERSAMPLE
    distance = np.hypot(dx, dy)
    angle = np.degrees(np.arctan2(-dy, dx))
    inside = (distance <= (radius + width / 2) * SUPERSAMPLE) & (
        distance >= (radius - width / 2) * SUPERSAMPLE
    )
    return inside & (angle >= start) & (angle <= end)


def segment_mask(
    size: int, x0: float, y0: float, x1: float, y1: float, width: float
) -> np.ndarray:
    """Return a mask of a thick line segment."""
    yy, xx = np.mgrid[0:size, 0:size]
    ax, ay = x0 * SUPERSAMPLE, y0 * SUPERSAMPLE
    bx, by = x1 * SUPERSAMPLE, y1 * SUPERSAMPLE
    px, py = xx - ax, yy - ay
    ex, ey = bx - ax, by - ay
    length_squared = ex * ex + ey * ey
    t = np.clip((px * ex + py * ey) / (length_squared or 1), 0.0, 1.0)
    distance = np.hypot(px - t * ex, py - t * ey)
    return distance <= (width / 2) * SUPERSAMPLE



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



def draw_glyph(color: tuple[int, int, int], canvas_size: int) -> np.ndarray:
    """Draw the brand mark of the add-on panel.

    This is the same artwork as ``.brand__mark`` in ``app/templates/index.html``: a
    stroked rounded rectangle as the body, the camera cone on the right, a filled lens
    and two signal arcs - scaled from the 32x32 view box of that SVG into the canvas.
    """
    final = canvas_size // SUPERSAMPLE
    scale = final * 0.92 / 32.0
    offset = final * 0.04
    stroke_width = 1.6 * scale  # stroke-width of .brand__mark

    def s(value: float) -> float:
        return offset + value * scale

    layer = canvas(canvas_size)

    def paint_mask(mask: np.ndarray, alpha: int = 255) -> None:
        layer[mask] = (color[0], color[1], color[2], alpha)

    # Body: rounded rectangle (x 2.5..22.5, y 9..23, corner radius 3), outline only.
    paint_mask(
        stroke(
            rounded_rect_mask(canvas_size, s(2.5), s(9), s(22.5), s(23), s(3)),
            rounded_rect_mask(
                canvas_size,
                s(2.5) + stroke_width,
                s(9) + stroke_width,
                s(22.5) - stroke_width,
                s(23) - stroke_width,
                max(0.5, s(3) - stroke_width),
            ),
        )
    )

    # Camera cone: the quad (22.5 14.5) (29 11) (29 21) (22.5 17.5), outline only.
    corners = [(s(22.5), s(14.5)), (s(29), s(11)), (s(29), s(21)), (s(22.5), s(17.5))]
    for index, (x0, y0) in enumerate(corners):
        x1, y1 = corners[(index + 1) % len(corners)]
        paint_mask(segment_mask(canvas_size, x0, y0, x1, y1, stroke_width))

    # Lens: filled circle, 85% opacity in the panel.
    paint_mask(disc_mask(canvas_size, s(8.5), s(16), s(2.6)), 217)

    # Two signal arcs, 55% opacity in the panel. The SVG chords ("M12.4 12.4 a5 5 0
    # 0 1 0 7.2" and "M15 10.4 a8 8 0 0 1 0 11.2") are vertical, so each arc centre
    # sits left of its chord and the arc bulges to the right.
    for chord_x, radius, chord in ((12.4, 5.0, 7.2), (15.0, 8.0, 11.2)):
        half_chord = chord / 2
        centre = chord_x - math.sqrt(radius**2 - half_chord**2)
        span = math.degrees(math.asin(half_chord / radius))
        paint_mask(
            arc_mask(canvas_size, s(centre), s(16), s(radius), -span, span, stroke_width),
            140,
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
    """Write every brand image of the integration.

    The panel glyph is drawn in both theme variants: the accent cyan of the add-on is
    readable on the light and on the dark theme, so the icon looks the same as inside
    the add-on.
    """
    for factor, suffix in ((1, ""), (2, "@2x")):
        target_size = 256 * factor
        for variant in ("icon", "dark_icon"):
            target = INTEGRATION_BRAND / f"{variant}{suffix}.png"
            write_png(target, downsample(draw_glyph(ACCENT, target_size * SUPERSAMPLE)))
            print("wrote", target.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
