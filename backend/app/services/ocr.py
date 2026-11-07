"""Best-effort OCR of team numbers + final score off the broadcast overlay.

Used only when a match has no VEX Events schedule to borrow from. Accuracy depends
heavily on the overlay layout, so results are marked ``detection_method="ocr"`` and
are meant to be corrected in the Match Detail screen. For VEXU (alphanumeric team
codes like ``BLRS1``) linking the VEX Events event is strongly preferred.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# VRC/VIQ style: a run of digits (with common OCR confusions) + an optional letter,
# not touching adjacent letters ("1234A", "62309X", "I234A" -> 1234A).
_TEAM_NUMERIC = re.compile(r"(?<![A-Za-z])([0-9ILOS|]{2,6})([A-Z]?)(?![A-Za-z0-9])")
# VEXU style: a whole-word mostly-letters code + optional trailing digits
# ("BLRS1", "GHST", "QCC2").
_TEAM_ALPHA = re.compile(r"\b([A-Z]{2,5}\d{0,2})\b")
# common single-glyph OCR confusions in the numeric part (digits misread as letters)
_DIGIT_FIX = str.maketrans({"I": "1", "L": "1", "|": "1", "O": "0", "S": "5"})
# overlay words that are never team codes
_STOPWORDS = {
    "RED", "BLUE", "TIE", "WON", "WIN", "WINS", "FINAL", "SCORE", "MATCH", "VS",
    "VEX", "VEXU", "VRC", "VIQ", "VIQRC", "VAIC", "AUTO", "AUTON", "DRIVER",
    "ALLIANCE", "WINNER", "QUAL", "QUALIFICATION", "PRACTICE", "ELIM", "ELIMINATION",
    "SEMIFINAL", "SEMIS", "QUARTERFINAL", "QUARTERS", "ROUND", "ROBIN", "FIELD",
    "POINTS", "PTS", "TEAM", "TEAMS", "TBD", "SAT", "SIT",
}


@dataclass
class OverlayRead:
    red_teams: list[str] = field(default_factory=list)
    blue_teams: list[str] = field(default_factory=list)
    red_score: int | None = None
    blue_score: int | None = None


def _grab_frame(video: Path, at_s: float, dest: Path) -> Path | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{max(0.0, at_s):.3f}", "-i", str(video), "-frames:v", "1",
         "-vf", "scale=1280:-2", str(dest)],
        capture_output=True, text=True,
    )
    return dest if r.returncode == 0 and dest.exists() else None


def _ocr_region(frame_bgr, rect, *, digits_only: bool) -> str:
    try:
        import cv2  # noqa: PLC0415
        import pytesseract  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return ""
    from app.services.overlay import crop_norm

    crop = crop_norm(frame_bgr, rect)
    if crop.size == 0:
        return ""
    crop = cv2.resize(crop, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.bilateralFilter(gray, 5, 40, 40)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    whitelist = "0123456789" if digits_only else "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    cfg = f"--psm 6 -c tessedit_char_whitelist={whitelist}"
    best = ""
    for img in (thresh, cv2.bitwise_not(thresh)):
        try:
            text = pytesseract.image_to_string(img, config=cfg)
        except pytesseract.TesseractError:  # pragma: no cover - env dependent
            return best
        if len(text.strip()) > len(best):
            best = text.strip()
    return best


def _candidate_tokens(text: str) -> list[str]:
    """Pull plausible team codes (VRC numeric *and* VEXU alpha) out of OCR text."""
    up = text.upper()
    out: list[str] = []
    seen: set[str] = set()

    for digits_raw, suffix in _TEAM_NUMERIC.findall(up):
        digits = digits_raw.translate(_DIGIT_FIX)
        if not re.fullmatch(r"\d{2,6}", digits):
            continue
        tok = f"{digits}{suffix}"
        if tok not in _STOPWORDS and tok not in seen:
            seen.add(tok)
            out.append(tok)

    for raw in _TEAM_ALPHA.findall(up):
        if raw in _STOPWORDS or raw in seen or raw.isalpha() and len(raw) < 3:
            continue
        seen.add(raw)
        out.append(raw)

    return out


def _vote_teams(samples: list[str]) -> list[str]:
    counter: Counter[str] = Counter()
    for text in samples:
        for tok in _candidate_tokens(text):
            counter[tok] += 1
    return [tok for tok, _ in counter.most_common(3)]


def _vote_score(samples: list[str]) -> int | None:
    counter: Counter[int] = Counter()
    for text in samples:
        for tok in re.findall(r"\d{1,3}", text):
            counter[int(tok)] += 1
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def read_overlay(
    video: Path | None,
    start_s: float,
    end_s: float,
    profile,
    workdir: Path,
    *,
    grab=None,
) -> OverlayRead:
    """OCR teams + score around a match window.

    ``grab(t, dest) -> Path | None`` extracts one frame at recording time ``t``;
    it defaults to seeking in ``video``. The live splitter passes one that reads
    from the capture's finished segments instead.
    """
    if grab is None:
        def grab(t: float, dest: Path) -> Path | None:
            return _grab_frame(video, t, dest)

    try:
        import cv2  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return OverlayRead()

    workdir.mkdir(parents=True, exist_ok=True)
    # Team numbers are clearest early in the match; the final score is revealed
    # around the buzzer and lingers, so sample on both sides of the detected end.
    team_ts = [start_s + off for off in (3, 6, 10, 15, 20)]
    score_ts = [end_s + off for off in (-6, -4, -2, 0, 2, 5)]

    red_team_texts, blue_team_texts = [], []
    red_score_texts, blue_score_texts = [], []

    for i, t in enumerate(team_ts):
        f = grab(t, workdir / f"team_{i}.jpg")
        if not f:
            continue
        img = cv2.imread(str(f))
        if img is None:
            continue
        red_team_texts.append(_ocr_region(img, profile.red_teams_region, digits_only=False))
        blue_team_texts.append(_ocr_region(img, profile.blue_teams_region, digits_only=False))

    for i, t in enumerate(score_ts):
        if t < start_s:
            continue
        f = grab(t, workdir / f"score_{i}.jpg")
        if not f:
            continue
        img = cv2.imread(str(f))
        if img is None:
            continue
        red_score_texts.append(_ocr_region(img, profile.red_score_region, digits_only=True))
        blue_score_texts.append(_ocr_region(img, profile.blue_score_region, digits_only=True))

    return OverlayRead(
        red_teams=_vote_teams(red_team_texts),
        blue_teams=_vote_teams(blue_team_texts),
        red_score=_vote_score(red_score_texts),
        blue_score=_vote_score(blue_score_texts),
    )
