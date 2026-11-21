"""Which Vector this is, for how it is shown. No Home Assistant imports.

Vector 1.0 was made by Anki and sends 16:9 camera frames. Vector 2.0 was made by Digital Dream
Labs and sends 4:3 frames. The frame shape is the evidence used when a frame has been seen.
Before that, the serial number is used: 2.0 serials begin "0dd" (observed: a robot
with such a serial is a 2.0 and sends 800x600). That prefix rule is a guess until a frame
confirms it, and a frame always wins.
"""

from __future__ import annotations

GEN_1 = "1.0"
GEN_2 = "2.0"
WIDE_RATIO = 1.5          # 16:9 is 1.78 and 4:3 is 1.33; anything wider than this is a 1.0 camera
V2_SERIAL_PREFIX = "0dd"


def generation(serial: str, frame_size: tuple[int, int] | None = None) -> str:
    if frame_size is not None and len(frame_size) == 2 and frame_size[0] > 0 and frame_size[1] > 0:
        return GEN_1 if frame_size[0] / frame_size[1] > WIDE_RATIO else GEN_2
    return GEN_2 if str(serial).lower().startswith(V2_SERIAL_PREFIX) else GEN_1


def maker_and_model(serial: str, frame_size: tuple[int, int] | None = None) -> tuple[str, str]:
    """(manufacturer, model) as shown on the device page."""
    if generation(serial, frame_size) == GEN_2:
        return "Digital Dream Labs", "Vector 2.0"
    return "Anki", "Vector"
