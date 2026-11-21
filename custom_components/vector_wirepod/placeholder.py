"""Status images, so a still never passes for live video.

The image is always the same shape as the robot's own camera frame: Vector 1.0 sends 16:9 and
Vector 2.0 sends 4:3, and squeezing one into the other distorts the picture.

Blocking (Pillow); call through hass.async_add_executor_job.
"""

from __future__ import annotations

import io
import textwrap

from PIL import Image, ImageDraw, ImageFont

WIDTH = 640
DEFAULT_HEIGHT = 360          # used only until the first frame shows the camera's real shape
MIN_HEIGHT, MAX_HEIGHT = 160, 1280
LINE_PX = 30
WRAP_CHARS = 52


def canvas_size(size: tuple[int, int] | None) -> tuple[int, int]:
    """WIDTH wide and as tall as a frame of this shape; 16:9 when the shape is not known."""
    if size is None or len(size) != 2 or size[0] <= 0 or size[1] <= 0:
        return WIDTH, DEFAULT_HEIGHT
    height = round(WIDTH * size[1] / size[0])
    return WIDTH, max(MIN_HEIGHT, min(MAX_HEIGHT, height))


def render(lines: list[str], frame: bytes | None = None,
           size: tuple[int, int] | None = None) -> bytes:
    """A JPEG: the last frame darkened with a banner, or a dark card of the camera's shape.

    `size` is the camera's last known (width, height), used when there is no frame to draw on."""
    assert isinstance(lines, (list, tuple))
    img = None
    if frame is not None:
        try:
            src = Image.open(io.BytesIO(frame))
            img = src.convert("RGB").resize(canvas_size(src.size))
            img = Image.eval(img, lambda v: v * 45 // 100)
        except (OSError, ValueError):
            img = None
    if img is None:
        img = Image.new("RGB", canvas_size(size), (16, 16, 20))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=20)
    wrapped: list[str] = []
    for text in lines:
        wrapped.extend(textwrap.wrap(text, WRAP_CHARS) or [""])
    max_lines = max(3, (img.height - 40) // LINE_PX + 1)
    y = 20
    for text in wrapped[:max_lines]:
        draw.text((24, y), text, fill=(230, 230, 230), font=font)
        y += LINE_PX
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=80)
    return out.getvalue()
