"""Trainable detector for the pre-match **team intro** key frame.

Most broadcasts show a full-screen card with both alliances' teams for a few
seconds before each match's countdown starts. That card looks the same from
match to match (only the team numbers change), so once a handful of examples
have been confirmed it can be recognised reliably and cheaply -- no OCR -- on
every sampled frame. Detection then uses it to

* split matches on streams with neither a usable clock nor a match label
  (a window starts where the intro ends), and
* read the teams off the intro card, where they are big and clear.

The workflow is review-first, nothing is trained on unconfirmed frames:

1. :func:`find_for_recording` looks for candidate intro frames in the stretch
   before each catalogued match of a recording. Frames are ranked by how much
   they look alike *across* matches (the card recurs), whether red and blue
   alliance colours are both on screen, and whether OCR finds team numbers on
   the left and right halves; frames where the match timer is already readable
   are ruled out (the intro comes before the countdown). The best frame per
   match plus a few alternatives are saved for review.
2. A person confirms or rejects each candidate (or picks one of the
   alternatives instead) in Admin -> Key frames.
3. :func:`train` builds the model from the confirmed frames only. Positives:
   the frames of each confirmed intro. Negatives: the other frames sampled
   around those intros, the frames of rejected candidates, and gameplay frames
   from inside the matches. The model is a nearest-example classifier over a
   compact appearance descriptor; its decision threshold and the reported
   precision / recall come from cross-validation that holds out whole intros.
"""

from __future__ import annotations

import json
import logging
import random
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("scouthub.keyframes")

FEATURE_VERSION = 1

# --- candidate search ----------------------------------------------------
LOOKBACK_S = 90.0     # how far before a match's start to look for its intro
AFTER_S = 10.0        # ...and how far after (label-based starts can be early)
SEARCH_FPS = 1.0
BACKGROUND_EVERY_S = 15.0  # gameplay negatives: one frame per this many seconds
TOP_K = 5             # frames per match that get OCR'd and kept as alternatives
ALT_MIN_SPACING_S = 3.0
RUN_SIM = 0.88        # frames this similar to the chosen one belong to the same intro
NEG_EXCLUDE_SIM = 0.80  # ...and frames this similar are never used as negatives
NEG_EXCLUDE_S = 3.0

# --- training --------------------------------------------------------------
MIN_CONFIRMED = 3
MAX_POS = 3000
MAX_NEG = 5000

STATUSES = ("candidate", "confirmed", "rejected")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ==========================================================================
# Frame descriptor
# ==========================================================================

def frame_features(img_bgr):
    """Unit-length appearance descriptor; ``a @ b`` is a similarity in [-1, 1].

    Three blocks, each mean-centred and normalised so none dominates: the
    coarse grey-level layout (32x18), a colour histogram (hue x sat x value),
    and where the edges / text are (16x9 grid of gradient energy).
    """
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    small = cv2.resize(img_bgr, (64, 36), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
    layout = cv2.resize(gray, (32, 18), interpolation=cv2.INTER_AREA).ravel()
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1, 2], None, [12, 4, 4], [0, 180, 0, 256, 0, 256]).ravel()
    hist = np.sqrt(hist / max(1.0, float(hist.sum())))
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    edges = cv2.resize(np.sqrt(gx * gx + gy * gy), (16, 9), interpolation=cv2.INTER_AREA).ravel()

    parts = []
    for vec, weight in ((layout, 1.0), (hist, 0.8), (edges, 1.0)):
        v = vec.astype(np.float32) - float(vec.mean())
        n = float(np.linalg.norm(v))
        parts.append(v * (weight / n) if n > 1e-6 else np.zeros_like(v))
    f = np.concatenate(parts)
    n = float(np.linalg.norm(f))
    return (f / n if n > 1e-6 else f).astype(np.float32)


def alliance_colors(img_bgr) -> float:
    """0..1: are strongly red *and* strongly blue areas both on screen?"""
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    hsv = cv2.cvtColor(cv2.resize(img_bgr, (64, 36), interpolation=cv2.INTER_AREA),
                       cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    vivid = (s > 100) & (v > 60)
    red = float(np.mean(vivid & ((h < 10) | (h > 170))))
    blue = float(np.mean(vivid & (h > 100) & (h < 130)))
    return min(1.0, min(red, blue) / 0.06)


def run_bounds(times, feats, k: int, *, sim: float = RUN_SIM) -> tuple[int, int]:
    """Indices ``[lo, hi]`` of the contiguous frames around ``k`` that look
    like frame ``k`` (the same intro card staying on screen)."""
    ref = feats[k]
    lo = k
    while lo - 1 >= 0 and float(feats[lo - 1] @ ref) >= sim:
        lo -= 1
    hi = k
    while hi + 1 < len(times) and float(feats[hi + 1] @ ref) >= sim:
        hi += 1
    return lo, hi


# ==========================================================================
# The model
# ==========================================================================

@dataclass
class KeyframeModel:
    """Nearest-example classifier: a frame's score is its similarity to the
    closest confirmed intro frame minus that to the closest non-intro frame."""

    pos: "object"   # (P, D) float32
    neg: "object"   # (N, D) float32
    threshold: float
    meta: dict = field(default_factory=dict)

    def scores(self, feats):
        import numpy as np  # noqa: PLC0415

        feats = np.atleast_2d(np.asarray(feats, dtype=np.float32))
        p = (feats @ self.pos.T).max(axis=1)
        n = (feats @ self.neg.T).max(axis=1) if len(self.neg) else np.zeros(len(feats), np.float32)
        return p - n

    def score(self, feat) -> float:
        return float(self.scores(feat)[0])

    def is_intro(self, img_bgr) -> bool:
        return self.score(frame_features(img_bgr)) >= self.threshold


def _models_dir() -> Path:
    from app.config import get_settings  # noqa: PLC0415

    return get_settings().data_dir / "models"


def model_paths() -> tuple[Path, Path]:
    d = _models_dir()
    return d / "keyframe_model.npz", d / "keyframe_model.json"


_cache_lock = threading.Lock()
_cache: tuple[float, KeyframeModel] | None = None


def load_model() -> KeyframeModel | None:
    """The trained model, or None. Re-read when the file changes."""
    global _cache
    import numpy as np  # noqa: PLC0415

    npz, meta_path = model_paths()
    if not npz.is_file():
        return None
    mtime = npz.stat().st_mtime
    with _cache_lock:
        if _cache and _cache[0] == mtime:
            return _cache[1]
        try:
            with np.load(npz) as data:
                if int(data["feature_version"]) != FEATURE_VERSION:
                    log.warning("key-frame model was trained on an older descriptor; retrain it")
                    return None
                model = KeyframeModel(
                    pos=data["pos"].astype(np.float32),
                    neg=data["neg"].astype(np.float32),
                    threshold=float(data["threshold"]),
                )
            model.meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
        except Exception:  # pragma: no cover - corrupt file
            log.exception("could not load the key-frame model")
            return None
        _cache = (mtime, model)
        return model


def active_model(cfg: dict) -> KeyframeModel | None:
    """The model detection should use under these settings (None = don't)."""
    if not cfg.get("keyframe_detection", True):
        return None
    return load_model()


def model_info() -> dict:
    _, meta_path = model_paths()
    m = load_model()
    if m is None:
        return {"trained": False}
    info = dict(m.meta) if m.meta else json.loads(meta_path.read_text() or "{}")
    info["trained"] = True
    return info


def delete_model() -> None:
    global _cache
    for p in model_paths():
        p.unlink(missing_ok=True)
    with _cache_lock:
        _cache = None


# ==========================================================================
# Candidate search
# ==========================================================================

@dataclass
class Anchor:
    start_s: float
    end_s: float
    match_id: int | None = None
    name: str = ""


@dataclass
class Candidate:
    anchor: Anchor
    t: float
    score: float
    run_start_s: float
    run_end_s: float
    # [{t, score, timer, tokens, file}] best first; includes the chosen frame
    alternatives: list[dict]
    times: "object"   # every frame sampled for this match (np.ndarray)
    feats: "object"   # their descriptors (n, D)


def search_range(anchors: list[Anchor], i: int) -> tuple[float, float]:
    a = anchors[i]
    lo = a.start_s - LOOKBACK_S
    if i > 0:
        lo = max(lo, anchors[i - 1].end_s + 2.0)
    lo = max(0.0, min(lo, a.start_s - 10.0))
    hi = min(a.start_s + AFTER_S, a.end_s)
    return lo, hi


def recurrence(per_anchor_feats: list) -> list:
    """For each frame: median over the *other* matches of its best similarity
    to any frame there. A card that recurs before every match scores high."""
    import numpy as np  # noqa: PLC0415

    out = []
    for i, fi in enumerate(per_anchor_feats):
        if len(fi) == 0:
            out.append(np.zeros(0, np.float32))
            continue
        best = [
            (fi @ fj.T).max(axis=1)
            for j, fj in enumerate(per_anchor_feats)
            if j != i and len(fj)
        ]
        out.append(np.median(np.stack(best), axis=0) if best else np.zeros(len(fi), np.float32))
    return out


def _ocr_tokens(img_bgr, rect) -> list[str]:
    from app.services import ocr  # noqa: PLC0415

    return ocr._candidate_tokens(ocr._ocr_region(img_bgr, rect, digits_only=False))


def find_candidates(
    video: Path,
    anchors: list[Anchor],
    profile,
    workdir: Path,
    *,
    use_ocr: bool = True,
    on_progress=None,
) -> tuple[list[Candidate], "object"]:
    """Candidate intro frames for each anchor (a catalogued match), plus the
    descriptors of gameplay frames from inside the matches (training negatives)."""
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    from app.services import match_detect, ocr  # noqa: PLC0415

    anchors = sorted(anchors, key=lambda a: a.start_s)
    workdir.mkdir(parents=True, exist_ok=True)
    frames_dir = workdir / "frames"

    # pass 1: descriptors of everything in each search range (+ gameplay)
    per_times, per_feats, per_color = [], [], []
    background = []
    for i, a in enumerate(anchors):
        lo, hi = search_range(anchors, i)
        times, feats, colors = [], [], []
        if hi > lo:
            for t, path in match_detect._sample_frames(video, SEARCH_FPS, frames_dir, start=lo, end=hi):
                img = cv2.imread(str(path))
                if img is None:
                    continue
                times.append(round(t, 2))
                feats.append(frame_features(img))
                colors.append(alliance_colors(img))
        per_times.append(np.asarray(times, np.float32))
        per_feats.append(np.stack(feats) if feats else np.zeros((0, 1), np.float32))
        per_color.append(np.asarray(colors, np.float32))
        g_lo, g_hi = a.start_s + 15.0, a.end_s - 5.0
        if g_hi - g_lo > 5:
            for _, path in match_detect._sample_frames(
                video, 1.0 / BACKGROUND_EVERY_S, frames_dir, start=g_lo, end=g_hi
            ):
                img = cv2.imread(str(path))
                if img is not None:
                    background.append(frame_features(img))
        if on_progress:
            on_progress(f"sampled {i + 1}/{len(anchors)} matches")

    rec = recurrence(per_feats)

    # pass 2: OCR the best few per match, rule out frames with a running clock
    candidates: list[Candidate] = []
    for i, a in enumerate(anchors):
        times, feats = per_times[i], per_feats[i]
        if len(times) == 0:
            continue
        prelim = 0.5 * rec[i] + 0.15 * per_color[i]
        picked: list[int] = []
        for k in np.argsort(-prelim):
            if all(abs(float(times[k]) - float(times[j])) >= ALT_MIN_SPACING_S for j in picked):
                picked.append(int(k))
            if len(picked) >= TOP_K:
                break
        alts = []
        for k in picked:
            t = float(times[k])
            dest = workdir / f"a{i}_{t:.2f}.jpg"
            img = None
            if ocr._grab_frame(video, t, dest):
                img = cv2.imread(str(dest))
            timer = tokens = None
            tok_score = 0.0
            if img is not None and use_ocr:
                timer = match_detect.read_timer(img, profile)
                left = _ocr_tokens(img, (0.0, 0.12, 0.5, 1.0))
                right = _ocr_tokens(img, (0.5, 0.12, 1.0, 1.0))
                tokens = left + [x for x in right if x not in left]
                tok_score = 0.5 * min(len(left), 2) / 2 + 0.5 * min(len(right), 2) / 2
            score = 0.5 * float(rec[i][k]) + 0.15 * float(per_color[i][k]) + 0.35 * tok_score
            if timer is not None:
                score -= 1.0  # the clock is up: this is the match, not the intro
            alts.append({
                "t": round(t, 2), "score": round(score, 3), "timer": timer,
                "tokens": tokens or [], "file": str(dest) if img is not None else None,
                "k": k,
            })
        alts = [x for x in alts if x["file"]]
        if not alts:
            continue
        alts.sort(key=lambda x: -x["score"])
        best = alts[0]
        lo, hi = run_bounds(times, feats, best["k"])
        candidates.append(Candidate(
            anchor=a, t=best["t"], score=best["score"],
            run_start_s=float(times[lo]), run_end_s=float(times[hi]),
            alternatives=[{k: v for k, v in x.items() if k != "k"} for x in alts],
            times=times, feats=feats,
        ))
        if on_progress:
            on_progress(f"ranked {i + 1}/{len(anchors)} matches")

    bg = np.stack(background) if background else np.zeros((0, 1), np.float32)
    return candidates, bg


# ==========================================================================
# Storage (DB rows + files under <data>/keyframes/rec_<id>/)
# ==========================================================================

def recording_dir(recording_id: int) -> Path:
    from app.config import get_settings  # noqa: PLC0415

    return get_settings().data_dir / "keyframes" / f"rec_{recording_id}"


def find_for_recording(recording_id: int, *, use_ocr: bool = True) -> str:
    """Job entry point: look for intro candidates before every catalogued match
    of a recording. Reviewed candidates are kept; unreviewed ones are replaced."""
    import numpy as np  # noqa: PLC0415
    from sqlmodel import select  # noqa: PLC0415

    from app.db import session_scope  # noqa: PLC0415
    from app.models import KeyFrame, Match, Recording, RecordingStatus  # noqa: PLC0415
    from app.services import settings_store  # noqa: PLC0415
    from app.services.overlay import load_profile  # noqa: PLC0415

    with session_scope() as session:
        rec = session.get(Recording, recording_id)
        if rec is None:
            raise RuntimeError(f"recording {recording_id} not found")
        if rec.status != RecordingStatus.completed:
            raise RuntimeError(f"recording {recording_id} is {rec.status.value}, not completed")
        if not rec.file_path or not Path(rec.file_path).exists():
            raise RuntimeError("the full recording file is missing on disk "
                               "(needed to look for key frames)")
        video = Path(rec.file_path)
        cfg = settings_store.get_all(session)
        anchors = [
            Anchor(m.video_start_s, m.video_end_s, m.id, m.name)
            for m in session.exec(select(Match).where(Match.recording_id == recording_id)).all()
            if m.video_start_s is not None and m.video_end_s is not None
        ]
        reviewed = session.exec(
            select(KeyFrame).where(KeyFrame.recording_id == recording_id,
                                   KeyFrame.status != "candidate")
        ).all()
        reviewed_starts = [k.anchor_start_s for k in reviewed]
    if not anchors:
        raise RuntimeError("this recording has no matches to look before; split or add "
                           "matches first")

    profile = load_profile(cfg["overlay_profile"])
    out_dir = recording_dir(recording_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="scouthub_kf_"))
    try:
        cands, background = find_candidates(video, anchors, profile, work, use_ocr=use_ocr)
        if len(background):
            np.savez_compressed(out_dir / "background.npz",
                                feats=background.astype(np.float16),
                                feature_version=FEATURE_VERSION)

        with session_scope() as session:
            for old in session.exec(
                select(KeyFrame).where(KeyFrame.recording_id == recording_id,
                                       KeyFrame.status == "candidate")
            ).all():
                _delete_files(old)
                session.delete(old)

        made = 0
        for c in cands:
            if any(abs(c.anchor.start_s - s) < 5 for s in reviewed_starts):
                continue  # already reviewed: keep the person's decision
            stem = f"m{int(c.anchor.start_s)}"
            alts = []
            for alt in c.alternatives:
                dest = out_dir / f"{stem}_{alt['t']:.2f}.jpg"
                _save_jpg(Path(alt["file"]), dest)
                alts.append({**alt, "file": dest.name})
            feats_path = out_dir / f"{stem}.npz"
            np.savez_compressed(feats_path, times=c.times, feats=c.feats.astype(np.float16),
                                feature_version=FEATURE_VERSION)
            with session_scope() as session:
                session.add(KeyFrame(
                    recording_id=recording_id, match_id=c.anchor.match_id,
                    match_name=c.anchor.name, anchor_start_s=c.anchor.start_s,
                    anchor_end_s=c.anchor.end_s, t=c.t, score=c.score,
                    run_start_s=c.run_start_s, run_end_s=c.run_end_s,
                    status="candidate", alternatives_json=json.dumps(alts),
                    features_file=feats_path.name,
                ))
            made += 1
        kept = len(reviewed)
        return (f"{made} key-frame candidate(s) to review from {len(anchors)} match(es)"
                + (f"; {kept} already reviewed kept" if kept else ""))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _save_jpg(src: Path, dest: Path) -> None:
    import cv2  # noqa: PLC0415

    img = cv2.imread(str(src))
    if img is None:
        shutil.copy2(src, dest)
        return
    h, w = img.shape[:2]
    if w > 960:
        img = cv2.resize(img, (960, int(h * 960 / w)), interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(dest), img, [cv2.IMWRITE_JPEG_QUALITY, 88])


def _delete_files(kf) -> None:
    d = recording_dir(kf.recording_id)
    for alt in json.loads(kf.alternatives_json or "[]"):
        if alt.get("file"):
            (d / alt["file"]).unlink(missing_ok=True)
    if kf.features_file:
        (d / kf.features_file).unlink(missing_ok=True)


def delete_for_recording(session, recording_id: int) -> None:
    from sqlmodel import select  # noqa: PLC0415

    from app.models import KeyFrame  # noqa: PLC0415

    for kf in session.exec(select(KeyFrame).where(KeyFrame.recording_id == recording_id)).all():
        session.delete(kf)
    shutil.rmtree(recording_dir(recording_id), ignore_errors=True)


def load_frames(kf) -> tuple["object", "object"]:
    import numpy as np  # noqa: PLC0415

    with np.load(recording_dir(kf.recording_id) / kf.features_file) as d:
        return d["times"].astype(np.float32), d["feats"].astype(np.float32)


def select_frame(kf, t: float) -> None:
    """Point a candidate at another of its alternative frames."""
    import numpy as np  # noqa: PLC0415

    alts = json.loads(kf.alternatives_json or "[]")
    match = [a for a in alts if abs(a["t"] - t) < 0.05]
    if not match:
        raise ValueError("t must be one of the candidate's alternative frames")
    times, feats = load_frames(kf)
    k = int(np.argmin(np.abs(times - match[0]["t"])))
    lo, hi = run_bounds(times, feats, k)
    kf.t = match[0]["t"]
    kf.score = match[0]["score"]
    kf.run_start_s, kf.run_end_s = float(times[lo]), float(times[hi])


# ==========================================================================
# Training
# ==========================================================================

@dataclass
class Example:
    """The labelled frames contributed by one reviewed candidate."""
    group: int
    pos: "object"
    neg: "object"


def examples_from(kf, times, feats) -> Example:
    import numpy as np  # noqa: PLC0415

    k = int(np.argmin(np.abs(times - kf.t)))
    lo, hi = run_bounds(times, feats, k)
    D = feats.shape[1]
    if kf.status == "confirmed":
        pos = feats[lo:hi + 1]
        sims = feats @ feats[k]
        far = (times < times[lo] - NEG_EXCLUDE_S) | (times > times[hi] + NEG_EXCLUDE_S)
        neg = feats[far & (sims < NEG_EXCLUDE_SIM)]
        return Example(kf.id, pos, neg)
    # rejected: the frames of the wrongly chosen "intro" are not intros
    return Example(kf.id, np.zeros((0, D), np.float32), feats[lo:hi + 1])


def _fit(pos, neg, seed: int = 0) -> tuple:
    import numpy as np  # noqa: PLC0415

    rng = np.random.default_rng(seed)
    if len(pos) > MAX_POS:
        pos = pos[rng.choice(len(pos), MAX_POS, replace=False)]
    if len(neg) > MAX_NEG:
        neg = neg[rng.choice(len(neg), MAX_NEG, replace=False)]
    return pos.astype(np.float32), neg.astype(np.float32)


def _best_threshold(scores, labels) -> tuple[float, dict]:
    import numpy as np  # noqa: PLC0415

    order = np.argsort(-scores)
    s, y = scores[order], labels[order]
    tp = np.cumsum(y)
    fp = np.cumsum(1 - y)
    P = max(1, int(y.sum()))
    best_f1, best_i = -1.0, 0
    for i in range(len(s)):
        if i + 1 < len(s) and s[i + 1] == s[i]:
            continue  # a threshold can't split equal scores
        prec = tp[i] / (tp[i] + fp[i])
        rec = tp[i] / P
        f1 = 0.0 if prec + rec == 0 else 2 * prec * rec / (prec + rec)
        if f1 > best_f1:
            best_f1, best_i = f1, i
    # put the cut halfway to the next lower score, not right on a training value
    thr = float(s[best_i]) if best_i + 1 >= len(s) else float((s[best_i] + s[best_i + 1]) / 2)
    pred = scores >= thr
    tp_, fp_ = int((pred & (labels == 1)).sum()), int((pred & (labels == 0)).sum())
    fn_ = int((~pred & (labels == 1)).sum())
    precision = tp_ / max(1, tp_ + fp_)
    recall = tp_ / max(1, tp_ + fn_)
    return thr, {
        "precision": round(precision, 4), "recall": round(recall, 4),
        "f1": round(0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall), 4),
        "false_positives": fp_, "missed": fn_,
    }


def train(examples: list[Example], background: list, *, seed: int = 0) -> KeyframeModel:
    """Fit the model; threshold and metrics from leave-intros-out cross-validation."""
    import numpy as np  # noqa: PLC0415

    confirmed = [e for e in examples if len(e.pos)]
    if len(confirmed) < MIN_CONFIRMED:
        raise ValueError(f"need at least {MIN_CONFIRMED} confirmed key frames to train "
                         f"(have {len(confirmed)})")
    D = confirmed[0].pos.shape[1]
    bg = np.concatenate([b for b in background if len(b)]) if any(len(b) for b in background) \
        else np.zeros((0, D), np.float32)

    # folds over reviewed candidates; gameplay frames spread across folds
    rng = random.Random(seed)
    groups = [e.group for e in examples]
    rng.shuffle(groups)
    n_folds = min(5, len(confirmed))
    # make sure every fold holds out at least one confirmed intro
    conf_ids = [g for g in groups if any(e.group == g and len(e.pos) for e in examples)]
    other_ids = [g for g in groups if g not in conf_ids]
    fold_of = {g: i % n_folds for i, g in enumerate(conf_ids)}
    fold_of.update({g: i % n_folds for i, g in enumerate(other_ids)})
    bg_fold = np.arange(len(bg)) % n_folds
    np.random.default_rng(seed).shuffle(bg_fold)

    all_scores, all_labels = [], []
    intros_found, intros_total = 0, 0
    for f in range(n_folds):
        tr = [e for e in examples if fold_of[e.group] != f]
        te = [e for e in examples if fold_of[e.group] == f]
        tr_pos = [e.pos for e in tr if len(e.pos)]
        if not tr_pos:
            continue
        pos, neg = _fit(
            np.concatenate(tr_pos),
            np.concatenate([e.neg for e in tr if len(e.neg)] + [bg[bg_fold != f]]),
            seed,
        )
        m = KeyframeModel(pos, neg, 0.0)
        test_neg = [e.neg for e in te if len(e.neg)] + [bg[bg_fold == f]]
        for e in te:
            if len(e.pos):
                s = m.scores(e.pos)
                all_scores.append(s)
                all_labels.append(np.ones(len(s)))
        tn = np.concatenate(test_neg) if test_neg else np.zeros((0, D), np.float32)
        if len(tn):
            all_scores.append(m.scores(tn))
            all_labels.append(np.zeros(len(tn)))
    scores = np.concatenate(all_scores)
    labels = np.concatenate(all_labels).astype(int)
    threshold, metrics = _best_threshold(scores, labels)

    # per intro: was any of its frames caught at that threshold (out of fold)?
    for f in range(n_folds):
        tr = [e for e in examples if fold_of[e.group] != f]
        tr_pos = [e.pos for e in tr if len(e.pos)]
        if not tr_pos:
            continue
        pos, neg = _fit(np.concatenate(tr_pos),
                        np.concatenate([e.neg for e in tr if len(e.neg)] + [bg[bg_fold != f]]), seed)
        m = KeyframeModel(pos, neg, threshold)
        for e in examples:
            if fold_of[e.group] == f and len(e.pos):
                intros_total += 1
                intros_found += int((m.scores(e.pos) >= threshold).any())

    pos, neg = _fit(
        np.concatenate([e.pos for e in confirmed]),
        np.concatenate([e.neg for e in examples if len(e.neg)] + [bg]),
        seed,
    )
    meta = {
        "trained_at": _now().isoformat(),
        "feature_version": FEATURE_VERSION,
        "threshold": round(threshold, 4),
        "confirmed": len(confirmed),
        "rejected": len(examples) - len(confirmed),
        "positive_frames": int(len(pos)),
        "negative_frames": int(len(neg)),
        "cv_folds": n_folds,
        "cv": {**metrics, "intros_found": intros_found, "intros_total": intros_total},
    }
    if metrics["f1"] < 0.8:
        meta["warning"] = ("held-out accuracy is low; review more key frames (especially "
                           "rejected near-misses) and train again")
    return KeyframeModel(pos, neg, threshold, meta)


CONFLICT_SIM = 0.95


def conflicting_rejections(examples: list[Example]) -> list[int]:
    """Rejected candidates whose frames are near-copies of confirmed intro
    frames: most likely a slip in review, and they pull the model both ways."""
    import numpy as np  # noqa: PLC0415

    pos = [e.pos for e in examples if len(e.pos)]
    if not pos:
        return []
    P = np.concatenate(pos)
    return [e.group for e in examples
            if not len(e.pos) and len(e.neg) and float((e.neg @ P.T).max()) >= CONFLICT_SIM]


def train_from_db(session) -> dict:
    """Train on every reviewed key frame in the database and save the model."""
    import numpy as np  # noqa: PLC0415
    from sqlmodel import select  # noqa: PLC0415

    from app.models import KeyFrame  # noqa: PLC0415

    rows = session.exec(select(KeyFrame).where(KeyFrame.status != "candidate")).all()
    examples, background, recs = [], [], set()
    for kf in rows:
        try:
            times, feats = load_frames(kf)
        except FileNotFoundError:
            log.warning("key frame %s: feature file missing, skipped", kf.id)
            continue
        examples.append(examples_from(kf, times, feats))
        recs.add(kf.recording_id)
    for rid in sorted(recs):
        p = recording_dir(rid) / "background.npz"
        if p.is_file():
            with np.load(p) as d:
                background.append(d["feats"].astype(np.float32))

    model = train(examples, background)
    model.meta["recordings"] = sorted(recs)
    conflicts = conflicting_rejections(examples)
    if conflicts:
        model.meta["conflicts"] = conflicts
        model.meta["warning"] = (
            f"{len(conflicts)} rejected key frame(s) look just like confirmed intros; if they "
            "are intros after all, confirm them and train again")
    npz, meta_path = model_paths()
    npz.parent.mkdir(parents=True, exist_ok=True)
    tmp = npz.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, pos=model.pos.astype(np.float16), neg=model.neg.astype(np.float16),
                        threshold=model.threshold, feature_version=FEATURE_VERSION)
    meta_path.write_text(json.dumps(model.meta, indent=2))
    tmp.replace(npz)
    log.info("key-frame model trained: %s", model.meta)
    return model_info()
