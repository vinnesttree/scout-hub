"""Cut per-match clips + thumbnails from a full recording with ffmpeg."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(text: str) -> str:
    return _SAFE.sub("_", text.strip()).strip("_") or "match"


def clip_filename(match_name: str, red: str, blue: str) -> str:
    stem = safe_name(match_name)
    if red or blue:
        stem = f"{stem}__{safe_name(red)}_vs_{safe_name(blue)}"
    return stem + ".mp4"


def cut(
    source: Path,
    start_s: float,
    end_s: float,
    dest: Path,
    *,
    pad_start: float = 0.0,
    pad_end: float = 0.0,
    reencode: bool = True,
) -> Path:
    """Write ``dest`` covering ``[start_s-pad_start, end_s+pad_end]``.

    Re-encoding (default) gives frame-accurate cuts; ``reencode=False`` is a fast
    stream copy that snaps to keyframes.
    """
    src_start = max(0.0, start_s - pad_start)
    duration = max(0.5, (end_s + pad_end) - src_start)
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{src_start:.3f}",
           "-i", str(source), "-t", f"{duration:.3f}"]
    if reencode:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
    else:
        cmd += ["-c", "copy", "-movflags", "+faststart"]
    cmd.append(str(dest))

    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return dest


def thumbnail(source: Path, at_s: float, dest: Path, width: int = 640) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{max(0.0, at_s):.3f}", "-i", str(source), "-frames:v", "1",
         "-vf", f"scale={width}:-2", str(dest)],
        check=True, capture_output=True, text=True,
    )
    return dest
