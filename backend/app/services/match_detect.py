"""Find candidate match windows in a full recording.

Two signals, sampled per frame at a low rate:

1. the broadcast **match timer** (a decreasing ``M:SS``) -- the strongest cue when
   the stream has a real produced clock.
2. the **match-label** in the overlay header (``QUALIFICATION 13`` etc.) -- used
   when there's no usable countdown (e.g. a static Tournament Manager overlay,
   common on VEXU / regional streams). A window is a contiguous stretch where the
   label is visible; it's split when the match number changes.
3. the pre-match **team intro** card, once the intro detector has been trained
   (:mod:`app.services.keyframes`). Each window is tagged with the intro that
   precedes it (its teams are read off it), and on streams where neither of the
   signals above works, a match is taken to start where an intro ends.

Schedule alignment (:mod:`app.services.aligner`) makes the windows authoritative;
this step only has to be roughly right.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("scouthub.detect")

_TIMER_RE = re.compile(r"^\s*([0-2])[:.\s]([0-5]\d)\s*$")
_ROUND_WORDS = (
    "QUALIFICATION", "QUALIFIER", "QUALIFYING", "QUAL", "PRACTICE", "FINAL",
    "FINALS", "SEMIFINAL", "SEMIFINALS", "QUARTERFINAL", "QUARTERFINALS",
    "ELIMINATION", "ELIM", "MATCH", "ROUND", "R16", "OF", "RO16",
)
_ROUND_RE = re.compile(r"(" + "|".join(_ROUND_WORDS) + r")\s*#?\s*(\d{1,3})?")
# overlay round word -> MatchRound value (words not listed carry no round info)
_ROUND_WORD_MAP = {
    "QUALIFICATION": "qual", "QUALIFIER": "qual", "QUALIFYING": "qual", "QUAL": "qual",
    "PRACTICE": "practice",
    "QUARTERFINAL": "qf", "QUARTERFINALS": "qf",
    "SEMIFINAL": "sf", "SEMIFINALS": "sf",
    "FINAL": "final", "FINALS": "final",
    "R16": "r16", "RO16": "r16",
}

# A broadcast match window (incl. countdown into auton and the score reveal).
MIN_WINDOW_S = 25.0
MAX_WINDOW_S = 200.0
# Bridge short OCR dropouts within one match.
MAX_GAP_S = 12.0
# Intro-card frames this close together are one showing of the card.
INTRO_GAP_S = 6.0
# A window's intro is the last one shown up to this long before it starts
# (or, for label-based windows, which can start early, a little after).
INTRO_LOOKBACK_S = 90.0
INTRO_AFTER_S = 15.0
# Streams with only intros to go on: how long a match lasts after its intro.
INTRO_MATCH_LEN_S = 135.0


@dataclass
class MatchWindow:
    start_s: float
    end_s: float
    confidence: float
    n_reads: int = 0
    timer_max: int | None = None
    timer_min: int | None = None
    # Most common match number / round read off the overlay label inside the
    # window (filled in by :func:`annotate_window`), e.g. 13 / "qual".
    label_num: int | None = None
    label_round: str | None = None
    # When the team-intro card for this match was on screen (seconds), if seen.
    intro_t: float | None = None

    @property
    def duration(self) -> float:
        return self.end_s - self.start_s


@dataclass
class TimerSample:
    t: float
    seconds: int | None            # timer value in seconds, or None if no timer read
    overlay: bool = False          # match-label / header overlay visible this frame
    label_num: int | None = None   # match number parsed from the label, if any
    label_round: str | None = None  # round parsed from the label ("qual", "sf", ...)
    intro: bool = False            # trained intro detector: team-intro card on screen


# --------------------------------------------------------------------------
# Pure window assembly (unit-tested)
# --------------------------------------------------------------------------

def assemble_windows(
    samples: list[TimerSample],
    *,
    min_window_s: float = MIN_WINDOW_S,
    max_window_s: float = MAX_WINDOW_S,
    max_gap_s: float = MAX_GAP_S,
) -> list[MatchWindow]:
    samples = sorted(samples, key=lambda s: s.t)
    windows: list[MatchWindow] = []

    run: list[TimerSample] = []

    def flush() -> None:
        if not run:
            return
        hits = [s for s in run if s.seconds is not None]
        if not hits:
            run.clear()
            return
        start = hits[0].t
        end = hits[-1].t
        dur = end - start
        if dur < min_window_s:
            run.clear()
            return
        # Split anything absurdly long (two matches with no gap) at the biggest
        # upward jump in timer value (a reset back toward 0:15 / 2:00).
        pieces = _split_on_reset(hits, max_window_s)
        for piece in pieces:
            p_start, p_end = piece[0].t, piece[-1].t
            if p_end - p_start < min_window_s:
                continue
            vals = [s.seconds for s in piece]
            decreasing = sum(
                1 for a, b in zip(vals, vals[1:]) if b <= a
            )
            monotonic_frac = decreasing / max(1, len(vals) - 1)
            coverage = len(piece) / max(1.0, (p_end - p_start))
            confidence = round(min(1.0, 0.35 * monotonic_frac + 0.65 * min(1.0, coverage)), 3)
            windows.append(
                MatchWindow(
                    start_s=round(p_start, 2),
                    end_s=round(p_end, 2),
                    confidence=confidence,
                    n_reads=len(piece),
                    timer_max=max(vals),
                    timer_min=min(vals),
                )
            )
        run.clear()

    last_hit_t: float | None = None
    for s in samples:
        if s.seconds is None:
            if last_hit_t is not None and (s.t - last_hit_t) > max_gap_s:
                flush()
                last_hit_t = None
            continue
        if last_hit_t is not None and (s.t - last_hit_t) > max_gap_s:
            flush()
        run.append(s)
        last_hit_t = s.t
    flush()
    return windows


def _split_on_reset(hits: list[TimerSample], max_window_s: float) -> list[list[TimerSample]]:
    if hits[-1].t - hits[0].t <= max_window_s:
        return [hits]
    # find the largest positive jump (timer counting back up = new match)
    best_i, best_jump = None, 8
    for i in range(1, len(hits)):
        jump = (hits[i].seconds or 0) - (hits[i - 1].seconds or 0)
        if jump > best_jump:
            best_jump, best_i = jump, i
    if best_i is None:
        return [hits]
    return [hits[:best_i], hits[best_i:]]


def assemble_windows_from_labels(
    samples: list[TimerSample],
    *,
    min_window_s: float = MIN_WINDOW_S,
    max_window_s: float = 600.0,
    max_gap_s: float = 25.0,
) -> list[MatchWindow]:
    """Windows from 'overlay/match-label visible' runs, split on the number changing.

    For streams with no usable countdown clock. ``max_gap_s`` is looser here since
    the overlay is often cut in and out between camera angles.
    """
    samples = sorted(samples, key=lambda s: s.t)
    windows: list[MatchWindow] = []
    run: list[TimerSample] = []

    def flush() -> None:
        hits = [s for s in run if s.overlay]
        run.clear()
        if len(hits) < 2:
            return
        # split into sub-runs when the parsed match number *stably* changes.
        # OCR of the number is noisy, so: the current piece's number is the first
        # one seen in it, and a split only happens once a different value has been
        # read on two consecutive sampled frames.
        pieces: list[list[TimerSample]] = [[hits[0]]]
        cur_num: int | None = hits[0].label_num
        streak: tuple[int, list[TimerSample]] | None = None
        for s in hits[1:]:
            n = s.label_num
            if n is None or cur_num is None or n == cur_num:
                streak = None
                pieces[-1].append(s)
                if cur_num is None and n is not None:
                    cur_num = n
                continue
            # n differs from the established number
            if streak and streak[0] == n:
                streak[1].append(s)
                if len(streak[1]) >= 2:
                    pieces.append(streak[1])
                    cur_num = n
                    streak = None
            else:
                streak = (n, [s])
                pieces[-1].append(s)
        for piece in pieces:
            p_start, p_end = piece[0].t, piece[-1].t
            if not (min_window_s <= p_end - p_start <= max_window_s):
                continue
            coverage = len(piece) / max(1.0, p_end - p_start)
            nums = {x.label_num for x in piece if x.label_num is not None}
            conf = round(min(0.85, 0.25 + 0.5 * min(1.0, coverage) + (0.1 if len(nums) == 1 else 0)), 3)
            windows.append(
                MatchWindow(
                    start_s=round(p_start, 2),
                    end_s=round(p_end, 2),
                    confidence=conf,
                    n_reads=len(piece),
                )
            )

    last_t: float | None = None
    for s in samples:
        if not s.overlay:
            if last_t is not None and s.t - last_t > max_gap_s:
                flush()
                last_t = None
            continue
        if last_t is not None and s.t - last_t > max_gap_s:
            flush()
        run.append(s)
        last_t = s.t
    flush()
    return windows


def intro_runs(samples: list[TimerSample], *, max_gap_s: float = INTRO_GAP_S
               ) -> list[tuple[float, float, int]]:
    """``(first_t, last_t, n_frames)`` for each showing of the intro card."""
    runs: list[list[float]] = []
    for s in sorted(samples, key=lambda x: x.t):
        if not s.intro:
            continue
        if runs and s.t - runs[-1][-1] <= max_gap_s:
            runs[-1].append(s.t)
        else:
            runs.append([s.t])
    return [(r[0], r[-1], len(r)) for r in runs]


def _sample_step(samples: list[TimerSample]) -> float:
    ts = sorted(s.t for s in samples)
    gaps = sorted(b - a for a, b in zip(ts, ts[1:]) if b > a)
    return gaps[len(gaps) // 2] if gaps else 1.0


def assemble_windows_from_intros(
    samples: list[TimerSample],
    *,
    match_len_s: float = INTRO_MATCH_LEN_S,
    min_window_s: float = MIN_WINDOW_S,
) -> list[MatchWindow]:
    """Windows that start where an intro card ends and run ``match_len_s``
    (or until the next intro). For streams with no usable clock or label."""
    samples = sorted(samples, key=lambda s: s.t)
    if not samples:
        return []
    # at a dense sample rate, a single flagged frame is more likely a false hit
    min_frames = 2 if _sample_step(samples) <= 3.0 else 1
    runs = [r for r in intro_runs(samples) if r[2] >= min_frames]
    last_t = samples[-1].t
    out: list[MatchWindow] = []
    for i, (a, b, n) in enumerate(runs):
        start = b
        end = min(start + match_len_s, last_t)
        if i + 1 < len(runs):
            end = min(end, runs[i + 1][0] - 1.0)
        if end - start < min_window_s:
            continue
        out.append(MatchWindow(
            start_s=round(start, 2), end_s=round(end, 2), confidence=0.5,
            n_reads=n, intro_t=round((a + b) / 2, 2),
        ))
    return out


def _attach_intros(windows: list[MatchWindow], samples: list[TimerSample]) -> list[MatchWindow]:
    """Tag each window with the intro card shown just before it."""
    runs = intro_runs(samples)
    if not runs:
        return windows
    for w in windows:
        if w.intro_t is not None:
            continue
        near = [r for r in runs
                if w.start_s - INTRO_LOOKBACK_S <= r[1] <= w.start_s + INTRO_AFTER_S]
        if near:
            a, b, _ = max(near, key=lambda r: r[1])
            w.intro_t = round((a + b) / 2, 2)
    return windows


# --------------------------------------------------------------------------
# Frame sampling + OCR
# --------------------------------------------------------------------------

def probe_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _sample_frames(
    video: Path, fps: float, workdir: Path,
    *, start: float | None = None, end: float | None = None,
) -> list[tuple[float, Path]]:
    workdir.mkdir(parents=True, exist_ok=True)
    pattern = workdir / "f_%06d.jpg"
    for p in workdir.glob("f_*.jpg"):
        p.unlink()
    base = 0.0 if start is None else max(0.0, start)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if base > 0:
        cmd += ["-ss", f"{base:.3f}"]
    if end is not None and end > base:
        cmd += ["-t", f"{end - base:.3f}"]
    cmd += ["-i", str(video), "-vf", f"fps={fps},scale=960:-2", "-q:v", "4", str(pattern)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    frames = sorted(workdir.glob("f_*.jpg"))
    step = 1.0 / fps
    return [(base + i * step, p) for i, p in enumerate(frames)]


def _region_has_content(crop) -> bool:
    """Cheap gate: does this crop plausibly contain overlay text/graphics?

    Skips the (subprocess-heavy) OCR on the ~half of frames where the overlay
    region is just background.
    """
    try:
        import cv2  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return True
    if crop is None or crop.size == 0:
        return False
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    edges = cv2.Laplacian(g, cv2.CV_16S, ksize=3)
    return float(edges.var()) > 120.0


def read_timer(frame_bgr, profile) -> int | None:
    """OCR the timer region. Returns seconds remaining, or None."""
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
        import pytesseract  # noqa: PLC0415
    except Exception:  # pragma: no cover - deps missing
        return None
    from app.services.overlay import crop_norm

    crop = crop_norm(frame_bgr, profile.timer_region)
    if crop.size == 0 or not _region_has_content(crop):
        return None
    # small pad so a slightly-off profile region doesn't clip the edge digits
    crop = _pad(crop, 0.12)
    crop = cv2.resize(crop, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    if profile.timer_invert:
        gray = cv2.bitwise_not(gray)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # try both polarities (broadcast overlays vary); psm 7 then psm 8
    for img in (thresh, cv2.bitwise_not(thresh)):
        for psm in (7, 8):
            try:
                text = pytesseract.image_to_string(
                    img,
                    config=f"--psm {psm} -c tessedit_char_whitelist=0123456789:.",
                ).strip()
            except pytesseract.TesseractError:  # pragma: no cover - env dependent
                return None
            secs = _parse_timer(text)
            if secs is not None:
                return secs
    return None


def _pad(img, frac: float):
    import cv2  # noqa: PLC0415

    h, w = img.shape[:2]
    py, px = int(h * frac), int(w * frac)
    return cv2.copyMakeBorder(img, py, py, px, px, cv2.BORDER_REPLICATE)


def read_match_label(frame_bgr, profile) -> tuple[bool, int | None, str | None]:
    """OCR the round/number label.

    Returns ``(overlay_visible, match_number, round)``; ``round`` is a MatchRound
    value ("qual", "sf", ...) when the label names one.
    """
    try:
        import cv2  # noqa: PLC0415
        import pytesseract  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return False, None, None
    from app.services.overlay import crop_norm

    crop = crop_norm(frame_bgr, profile.match_label_region)
    if crop.size == 0 or not _region_has_content(crop):
        return False, None, None
    crop = cv2.resize(crop, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    for img in (thresh, cv2.bitwise_not(thresh)):
        try:
            text = pytesseract.image_to_string(img, config="--psm 6").upper()
        except pytesseract.TesseractError:  # pragma: no cover
            return False, None, None
        m = _ROUND_RE.search(text.replace("\n", " "))
        if m:
            num = int(m.group(2)) if m.group(2) else None
            return True, num, _ROUND_WORD_MAP.get(m.group(1))
        # a bare 1-3 digit number filling the region also counts as "label present"
        digits = re.findall(r"\b(\d{1,3})\b", text)
        if digits and len(re.sub(r"[^A-Z0-9]", "", text)) <= 6:
            return True, int(digits[0]), None
    return False, None, None


def tesseract_ready() -> tuple[bool, str]:
    """(ok, message) — is tesseract importable and does it have an English model?"""
    try:
        import pytesseract  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        return False, f"pytesseract not importable: {exc}"
    try:
        langs = set(pytesseract.get_languages(config=""))
    except Exception as exc:  # pragma: no cover
        return False, f"tesseract binary not found: {exc}"
    if "eng" not in langs:
        return False, "tesseract is installed but the 'eng' language data is missing"
    return True, "ok"


def _parse_timer(text: str) -> int | None:
    text = text.replace(" ", "")
    m = _TIMER_RE.match(text)
    if not m:
        # sometimes OCR drops the separator: "145" -> 1:45
        digits = re.sub(r"\D", "", text)
        if len(digits) == 3 and digits[0] in "012" and int(digits[1]) < 6:
            return int(digits[0]) * 60 + int(digits[1:])
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _scan(video: Path, profile, fps: float, workdir: Path,
          *, start: float | None = None, end: float | None = None,
          intro_model=None) -> list[TimerSample]:
    """Read every sampled frame. ``intro_model`` (a trained
    :class:`app.services.keyframes.KeyframeModel`) also flags intro-card frames."""
    import cv2  # noqa: PLC0415

    out: list[TimerSample] = []
    for t, path in _sample_frames(video, fps, workdir, start=start, end=end):
        img = cv2.imread(str(path))
        if img is None:
            out.append(TimerSample(t, None))
            continue
        # The clock and label are still read on intro frames: a model that
        # wrongly flags match frames must not be able to hide the timer.
        intro = bool(intro_model is not None and intro_model.is_intro(img))
        secs = read_timer(img, profile)
        overlay, label_num, label_round = read_match_label(img, profile)
        out.append(TimerSample(t, secs, overlay=overlay, label_num=label_num,
                               label_round=label_round, intro=intro))
    return out


def detect_windows(
    video: Path,
    profile,
    sample_fps: float = 1.0,
    *,
    workdir: Path | None = None,
    coarse_fps: float = 0.2,
    intro_model=None,
    intro_match_len_s: float = INTRO_MATCH_LEN_S,
) -> list[MatchWindow]:
    """Coarse pass over the whole video, then a fine pass around each rough window.

    Per-frame OCR spawns tesseract, so a full-rate scan of a multi-hour recording
    is too slow. The coarse pass (default every 5 s) finds roughly where matches
    are; the fine pass re-scans only ``[start-30, end+30]`` at ``sample_fps``
    (from 60 s before with a trained intro detector, to catch the intro card).
    """
    try:
        import cv2  # noqa: PLC0415  # noqa: F401
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("opencv-python-headless is required for match detection") from exc

    tmp = workdir or Path(tempfile.mkdtemp(prefix="scouthub_frames_"))
    try:
        choose = lambda samples: _choose_windows(  # noqa: E731
            samples, intro_match_len_s=intro_match_len_s)
        coarse = _scan(video, profile, min(coarse_fps, sample_fps), tmp, intro_model=intro_model)
        rough = choose(coarse)
        log.info("detect: coarse pass -> %d rough window(s) from %d frames",
                 len(rough), len(coarse))
        if not rough or sample_fps <= coarse_fps:
            return rough

        pre = 60 if intro_model is not None else 30
        refined: list[MatchWindow] = []
        for rw in rough:
            fine = _scan(video, profile, sample_fps, tmp,
                         start=rw.start_s - pre, end=rw.end_s + 30, intro_model=intro_model)
            for w in choose(fine):
                refined.append(w)
        refined.sort(key=lambda w: w.start_s)
        # de-dupe overlapping refined windows
        merged: list[MatchWindow] = []
        for w in refined:
            if merged and w.start_s <= merged[-1].end_s + 5:
                prev = merged[-1]
                merged[-1] = MatchWindow(
                    prev.start_s, max(prev.end_s, w.end_s),
                    max(prev.confidence, w.confidence),
                    prev.n_reads + w.n_reads,
                    intro_t=prev.intro_t if prev.intro_t is not None else w.intro_t,
                )
            else:
                merged.append(w)
        log.info("detect: fine pass -> %d window(s)", len(merged))
        return merged or rough
    finally:
        if workdir is None:
            for p in tmp.glob("f_*.jpg"):
                p.unlink(missing_ok=True)
            tmp.rmdir()


def annotate_window(w: MatchWindow, samples: list[TimerSample]) -> MatchWindow:
    """Fill ``label_num`` / ``label_round`` with the values most often read off
    the overlay label inside the window (OCR of a single frame is noisy)."""
    from collections import Counter  # noqa: PLC0415

    inside = [s for s in samples if w.start_s <= s.t <= w.end_s]
    nums = Counter(s.label_num for s in inside if s.label_num is not None)
    rounds = Counter(s.label_round for s in inside if s.label_round)
    w.label_num = nums.most_common(1)[0][0] if nums else None
    w.label_round = rounds.most_common(1)[0][0] if rounds else None
    return w


def _choose_windows(samples: list[TimerSample], *, verbose: bool = True,
                    intro_match_len_s: float = INTRO_MATCH_LEN_S) -> list[MatchWindow]:
    """Prefer the timer-countdown windows; fall back to label windows when the
    timer signal is weak but the overlay label is clearly present, and to
    intro-card windows when there is neither. Windows are tagged with their intro."""
    # the live splitter re-runs this after every segment; keep that out of INFO
    say = log.info if verbose else log.debug
    timer_hits = sum(1 for s in samples if s.seconds is not None)
    distinct_timer_vals = len({s.seconds for s in samples if s.seconds is not None})
    label_hits = sum(1 for s in samples if s.overlay)

    timer_windows = assemble_windows(samples)
    # a real countdown moves; >=4 distinct values across >=8 reads
    timer_usable = distinct_timer_vals >= 4 and timer_hits >= 8 and timer_windows

    if timer_usable:
        say("detect: %d windows (timer)", len(timer_windows))
        return _attach_intros(timer_windows, samples)

    label_windows = assemble_windows_from_labels(samples)
    if label_windows and (not timer_windows or label_hits > timer_hits):
        say(
            "detect: %d windows (match-label; timer signal weak: %d hits / %d distinct)",
            len(label_windows), timer_hits, distinct_timer_vals,
        )
        return _attach_intros(label_windows, samples)

    if not timer_windows:
        intro_windows = assemble_windows_from_intros(samples, match_len_s=intro_match_len_s)
        if intro_windows:
            say("detect: %d windows (team intro; no usable timer or match label)",
                len(intro_windows))
            return intro_windows

    say("detect: %d windows (timer, weak)", len(timer_windows))
    return _attach_intros(timer_windows, samples)
