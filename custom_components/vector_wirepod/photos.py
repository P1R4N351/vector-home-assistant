"""Photo storage for Vector snapshots. No Home Assistant imports.

Photos are stored as JPEG files in one private folder per robot under Home Assistant's
config directory (<config>/vector_wirepod/<serial>/<name>), outside www, and served authenticated
through the image entity. The newest `keep` are kept.
Blocking file work; call through hass.async_add_executor_job.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
DEFAULT_KEEP = 50
MAX_KEEP = 1000
NAME = re.compile(r"\d{8}T\d{6}Z-[a-z]{1,12}\.jpg")
REASON = re.compile(r"[a-z]{1,12}")


def photo_dir(config_dir: str, serial: str) -> Path:
    assert serial.isalnum(), "serial must be alphanumeric"
    return Path(config_dir) / "vector_wirepod" / serial.lower()


def is_jpeg(data: bytes) -> bool:
    return isinstance(data, (bytes, bytearray)) and len(data) > 64 and data[:2] == SOI and data[-2:] == EOI


def list_photos(directory: Path) -> list[Path]:
    """This robot's photos, oldest first. Only names this module wrote."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and NAME.fullmatch(p.name))


def latest(directory: Path) -> Path | None:
    photos = list_photos(directory)
    return photos[-1] if photos else None


def taken_at(path: Path) -> datetime | None:
    """When a photo was taken, from its name."""
    if not NAME.fullmatch(path.name):
        return None
    return datetime.strptime(path.name[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def reason_of(path: Path) -> str:
    return path.stem[17:] if NAME.fullmatch(path.name) else ""


def save_photo(directory: Path, frame: bytes, reason: str, when: datetime | None = None,
               keep: int = DEFAULT_KEEP) -> Path:
    """Write one photo and prune the folder to the newest `keep`. Returns the new file."""
    assert REASON.fullmatch(reason), "reason is a short lowercase word"
    assert 1 <= keep <= MAX_KEEP
    if not is_jpeg(frame):
        raise ValueError("not a complete JPEG frame")
    when = when or datetime.now(timezone.utc)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{when.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}-{reason}.jpg"
    tmp = path.with_suffix(".jpg.tmp")
    tmp.write_bytes(bytes(frame))
    tmp.replace(path)
    for old in list_photos(directory)[:-keep]:
        old.unlink(missing_ok=True)
    return path


MAX_FRAME_TRIES = 8


async def first_frame(attempt, tries: int, pause_s: float, sleep):
    """Ask `attempt()` for a frame up to `tries` times, pausing between tries. None when none came.

    A robot that has just woken gives no frames for its first seconds: on 2026-11-20 a single
    12 s wait started at the wake came back empty, and frames were flowing 18 s after it."""
    assert 1 <= tries <= MAX_FRAME_TRIES, "tries must be between 1 and MAX_FRAME_TRIES"
    assert pause_s >= 0, "the pause cannot be negative"
    for n in range(tries):
        frame = await attempt()
        if frame is not None:
            return frame
        if n + 1 < tries:
            await sleep(pause_s)
    return None
