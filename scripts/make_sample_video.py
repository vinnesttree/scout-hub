#!/usr/bin/env python3
"""Render a synthetic 'broadcast' recording for testing the processing pipeline.

Layout roughly matches ``overlay_profiles/vex_default.json``: a big M:SS match
timer near the top-center, alliance panels along the bottom. Produces one match
window framed by a pre-show and a final-score screen.

Usage:
    python scripts/make_sample_video.py out.mp4 [--matches 2]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 720
FPS = 6
_FONT_CANDIDATES = [
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]


def _load(size: int):
    for f in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


_big = _load(60)      # match timer
_med = _load(44)      # scores
_small = _load(26)    # team numbers

MATCHES = [
    dict(name="Qualification 1", red=("2645A", "8823B"), blue=("4090C", "6210Z"),
         red_score=88, blue_score=74, length=105),
    dict(name="Qualification 2", red=("2645A", "9091X"), blue=("3388D", "7717G"),
         red_score=45, blue_score=131, length=105),
]

PRESHOW_S = 8
SCORE_S = 14
GAP_S = 30


def _text(draw, xy, s, font, fill="white", anchor="la"):
    draw.text(xy, s, font=font, fill=fill, anchor=anchor)


def frame_blank(label: str) -> Image.Image:
    img = Image.new("RGB", (W, H), (12, 18, 28))
    d = ImageDraw.Draw(img)
    _text(d, (W / 2, H / 2), label, _med, "#8899aa", anchor="mm")
    return img


def frame_match(m: dict, remaining: int, show_score: bool) -> Image.Image:
    """Layout deliberately matches overlay_profiles/vex_default.json."""
    img = Image.new("RGB", (W, H), (20, 22, 26))
    d = ImageDraw.Draw(img)

    # match timer -> timer_region [0.42, 0.01, 0.58, 0.13], centered at (0.5, 0.07)
    mm, ss = divmod(max(0, remaining), 60)
    d.rectangle([W * 0.40, H * 0.00, W * 0.60, H * 0.15], fill=(0, 0, 0))
    _text(d, (W * 0.50, H * 0.072), f"{mm}:{ss:02d}", _big, "white", anchor="mm")

    # alliance panels along the bottom third
    d.rectangle([W * 0.04, H * 0.79, W * 0.485, H * 0.99], fill=(110, 22, 26))
    d.rectangle([W * 0.515, H * 0.79, W * 0.96, H * 0.99], fill=(22, 42, 110))

    # team numbers -> red_teams_region [0.06..0.34], blue_teams_region [0.66..0.94]
    _text(d, (W * 0.07, H * 0.855), "  ".join(m["red"]), _small, "white", anchor="lm")
    _text(d, (W * 0.94, H * 0.855), "  ".join(m["blue"]), _small, "white", anchor="rm")

    if show_score:
        # score -> red_score_region ~[0.35..0.45], blue_score_region ~[0.55..0.65]
        _text(d, (W * 0.40, H * 0.855), str(m["red_score"]), _med, "white", anchor="mm")
        _text(d, (W * 0.60, H * 0.855), str(m["blue_score"]), _med, "white", anchor="mm")
    return img


def build(out: Path, n_matches: int) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sample_frames_"))
    idx = 0

    def dump(img: Image.Image, seconds: float) -> None:
        nonlocal idx
        for _ in range(int(seconds * FPS)):
            img.save(tmp / f"{idx:07d}.jpg", quality=90)
            idx += 1

    try:
        dump(frame_blank("PRE-SHOW"), PRESHOW_S)
        for i in range(n_matches):
            m = MATCHES[i % len(MATCHES)]
            length = m["length"]
            for sec in range(length, -1, -1):
                # ~1s per timer value
                for _ in range(FPS):
                    img = frame_match(m, sec, show_score=False)
                    img.save(tmp / f"{idx:07d}.jpg", quality=90)
                    idx += 1
            dump(frame_match(m, 0, show_score=True), SCORE_S)
            if i != n_matches - 1:
                dump(frame_blank("NEXT MATCH SOON"), GAP_S)
        dump(frame_blank("THANKS FOR WATCHING"), 6)

        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-framerate", str(FPS), "-i", str(tmp / "%07d.jpg"),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", str(out)],
            check=True,
        )
        print(f"wrote {out} ({idx} frames, ~{idx / FPS:.0f}s)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--matches", type=int, default=1)
    args = ap.parse_args()
    build(args.out, args.matches)
