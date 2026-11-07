#!/usr/bin/env python3
"""Render a synthetic 'broadcast' recording for testing the processing pipeline.

Layout roughly matches ``overlay_profiles/vex_default.json``: a big M:SS match
timer near the top-center, alliance panels along the bottom. Each match is
preceded by a full-screen **team intro card** (red alliance on the left, blue on
the right, shown for a few seconds before the countdown starts) -- the key frame
the trainable intro detector learns -- and followed by a final-score screen.

Usage:
    python scripts/make_sample_video.py out.mp4 [--matches 2] [--match-length 105]

``build()`` returns the ground truth (where each intro and match is) so tests
can check detection against it.
"""

from __future__ import annotations

import argparse
import random
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
_intro = _load(72)    # team numbers on the intro card

MATCHES = [
    dict(name="Qualification 1", red=("2645A", "8823B"), blue=("4090C", "6210Z"),
         red_score=88, blue_score=74, length=105),
    dict(name="Qualification 2", red=("2645A", "9091X"), blue=("3388D", "7717G"),
         red_score=45, blue_score=131, length=105),
    dict(name="Qualification 3", red=("1104K", "5150R"), blue=("8823B", "3388D"),
         red_score=102, blue_score=97, length=105),
    dict(name="Qualification 4", red=("7717G", "6210Z"), blue=("9091X", "1104K"),
         red_score=60, blue_score=60, length=105),
]

PRESHOW_S = 8
INTRO_S = 6
SCORE_S = 14
GAP_S = 30


def _text(draw, xy, s, font, fill="white", anchor="la"):
    draw.text(xy, s, font=font, fill=fill, anchor=anchor)


def frame_blank(label: str) -> Image.Image:
    img = Image.new("RGB", (W, H), (12, 18, 28))
    d = ImageDraw.Draw(img)
    _text(d, (W / 2, H / 2), label, _med, "#8899aa", anchor="mm")
    return img


def frame_intro(m: dict) -> Image.Image:
    """Full-screen pre-match card: match name on top, alliances side by side."""
    img = Image.new("RGB", (W, H), (14, 14, 18))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, H * 0.16], fill=(30, 30, 38))
    _text(d, (W / 2, H * 0.08), m["name"].upper(), _med, "white", anchor="mm")
    d.rectangle([W * 0.03, H * 0.22, W * 0.47, H * 0.92], fill=(150, 24, 30))
    d.rectangle([W * 0.53, H * 0.22, W * 0.97, H * 0.92], fill=(24, 52, 150))
    for i, team in enumerate(m["red"]):
        _text(d, (W * 0.25, H * (0.42 + 0.22 * i)), team, _intro, "white", anchor="mm")
    for i, team in enumerate(m["blue"]):
        _text(d, (W * 0.75, H * (0.42 + 0.22 * i)), team, _intro, "white", anchor="mm")
    _text(d, (W / 2, H * 0.57), "VS", _med, "#cccccc", anchor="mm")
    return img


def frame_match(m: dict, remaining: int, show_score: bool, rng: random.Random | None = None) -> Image.Image:
    """Layout deliberately matches overlay_profiles/vex_default.json."""
    img = Image.new("RGB", (W, H), (20, 22, 26))
    d = ImageDraw.Draw(img)

    # "robots" moving on the field, so gameplay frames aren't all identical
    if rng is not None:
        for _ in range(4):
            x, y = rng.uniform(0.1, 0.85) * W, rng.uniform(0.25, 0.65) * H
            col = rng.choice([(140, 40, 40), (40, 60, 140), (90, 90, 90)])
            d.rectangle([x, y, x + 70, y + 70], fill=col)

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


def build(out: Path, n_matches: int, *, match_length: int | None = None,
          gap_s: float = GAP_S, seed: int = 7) -> list[dict]:
    """Write the video; return per-match ground truth (seconds into the video):
    ``intro_start``, ``intro_end``, ``match_start`` (countdown begins),
    ``match_end`` (timer hits 0), plus the match's teams."""
    tmp = Path(tempfile.mkdtemp(prefix="sample_frames_"))
    rng = random.Random(seed)
    idx = 0
    truth: list[dict] = []

    def now() -> float:
        return idx / FPS

    def dump(img: Image.Image, seconds: float) -> None:
        nonlocal idx
        for _ in range(int(seconds * FPS)):
            img.save(tmp / f"{idx:07d}.jpg", quality=90)
            idx += 1

    try:
        dump(frame_blank("PRE-SHOW"), PRESHOW_S)
        for i in range(n_matches):
            m = MATCHES[i % len(MATCHES)]
            length = match_length or m["length"]
            gt = {"name": m["name"], "red": list(m["red"]), "blue": list(m["blue"]),
                  "intro_start": now()}
            dump(frame_intro(m), INTRO_S)
            gt["intro_end"] = gt["match_start"] = now()
            for sec in range(length, -1, -1):
                # ~1s per timer value; re-roll the robots twice a second
                for k in range(FPS):
                    if k % 3 == 0:
                        img = frame_match(m, sec, show_score=False, rng=rng)
                    img.save(tmp / f"{idx:07d}.jpg", quality=90)
                    idx += 1
            gt["match_end"] = now()
            truth.append(gt)
            dump(frame_match(m, 0, show_score=True), SCORE_S)
            if i != n_matches - 1:
                dump(frame_blank("NEXT MATCH SOON"), gap_s)
        dump(frame_blank("THANKS FOR WATCHING"), 6)

        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-framerate", str(FPS), "-i", str(tmp / "%07d.jpg"),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", str(out)],
            check=True,
        )
        print(f"wrote {out} ({idx} frames, ~{idx / FPS:.0f}s)")
        return truth
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--matches", type=int, default=1)
    ap.add_argument("--match-length", type=int, default=None,
                    help="seconds on the match clock (default 105)")
    args = ap.parse_args()
    build(args.out, args.matches, match_length=args.match_length)
