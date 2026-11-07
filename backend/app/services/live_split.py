"""Split a capture into matches *while it is being recorded or downloaded*.

Runs in a thread next to the capture processes. Each time ffmpeg closes a
segment (see :mod:`app.services.segments`) the splitter samples its frames,
reads the match timer / match label off the overlay, and re-assembles match
windows with the same logic as the after-the-fact pipeline
(:func:`match_detect._choose_windows`). A window is acted on once it has
*settled* — the stream has moved far enough past its end that no later frame
could still extend it — and then, straight away:

1. teams and score are read off the overlay (rough OCR estimates),
2. a match row is catalogued (``provisional`` when an event is linked),
3. its clip + thumbnail are cut from the segments.

With a linked event, :mod:`app.services.enrich` is polled every so often to tie
the provisional matches to the VEX Events schedule and replace the estimates
with real teams and scores as they're published.

For an archived video, the download usually runs faster than real time; the
splitter just works through the segments as they arrive and keeps going after
the download finishes until it has caught up.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
from pathlib import Path

from app.crud import set_match_teams
from app.db import session_scope
from app.models import DetectionMethod, Match, MatchRound, MatchStatus, Recording
from app.services import enrich, match_detect, ocr, settings_store
from app.services.match_detect import MatchWindow, TimerSample
from app.services.overlay import load_profile
from app.services.segments import Segment, SegmentStore
from app.services.vexevents import VexEventsError

log = logging.getLogger("scouthub.live")

# Label windows bridge overlay dropouts of up to 25 s (timer windows: 12 s), so
# a window can still grow until the stream is that far past its last frame.
SETTLE_S = 25.0
ENRICH_INTERVAL_S = 90.0
ENRICH_BACKOFF_S = 300.0
# never refresh more often than this (fast archive downloads finish many matches quickly)
ENRICH_MIN_GAP_S = 20.0

_ROUND_NAMES = {
    "practice": "Practice", "qual": "Qualification", "r16": "Round of 16",
    "qf": "Quarterfinal", "sf": "Semifinal", "final": "Final",
}


class WindowTracker:
    """Incremental match-window assembly over a growing list of samples."""

    def __init__(self, *, step_s: float = 2.0, settle_s: float = SETTLE_S,
                 intro_match_len_s: float = match_detect.INTRO_MATCH_LEN_S) -> None:
        self.samples: list[TimerSample] = []
        self.emitted: list[MatchWindow] = []
        self.settle_s = settle_s + 2 * step_s
        self.intro_match_len_s = intro_match_len_s

    def add(self, samples: list[TimerSample]) -> None:
        self.samples.extend(samples)

    def ready(self, analyzed_until: float, *, final: bool = False) -> list[MatchWindow]:
        """Windows that can no longer change and haven't been handed out yet.
        ``final``: the capture is over, so every remaining window is settled."""
        out = []
        for w in match_detect._choose_windows(self.samples, verbose=False,
                                              intro_match_len_s=self.intro_match_len_s):
            if not final and w.end_s + self.settle_s > analyzed_until:
                continue
            if any(w.start_s < e.end_s + 5 and w.end_s > e.start_s - 5 for e in self.emitted):
                continue  # already catalogued (possibly from the other signal)
            match_detect.annotate_window(w, self.samples)
            self.emitted.append(w)
            out.append(w)
        return out


def match_name(w: MatchWindow, ordinal: int) -> str:
    if w.label_round in _ROUND_NAMES and w.label_num is not None:
        return f"{_ROUND_NAMES[w.label_round]} {w.label_num}"
    return f"Match {ordinal}"


class LiveSplitter:
    def __init__(self, recording_id: int, seg_dir: Path, *, poll_s: float = 2.0) -> None:
        self.recording_id = recording_id
        self.store = SegmentStore(seg_dir)
        self.poll_s = poll_s
        self.ok = True
        self.error: str | None = None
        self.matches_made = 0
        self._capture_done = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"live-split-{recording_id}", daemon=True
        )
        self._next_enrich = 0.0
        self._last_enrich = -ENRICH_MIN_GAP_S

    # -- control -------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def capture_finished(self) -> None:
        """Capture processes have exited: drain the remaining segments and stop."""
        self._capture_done.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    # -- main loop -----------------------------------------------------

    def _run(self) -> None:
        self._workdir = Path(tempfile.mkdtemp(prefix="scouthub_live_"))
        try:
            self._set(split_status="running", split_detail="waiting for video…", analyzed_s=0.0)
            self._loop()
            self._set(split_status="done",
                      split_detail=f"{self.matches_made} match(es) split while capturing")
        except Exception as exc:
            log.exception("live splitting failed for recording %s", self.recording_id)
            self.ok, self.error = False, str(exc)
            self._set(split_status="failed", split_detail=f"live splitting failed: {exc}")
        finally:
            shutil.rmtree(self._workdir, ignore_errors=True)

    def _loop(self) -> None:
        with session_scope() as session:
            cfg = settings_store.get_all(session)
            rec = session.get(Recording, self.recording_id)
            self.event_id = rec.event_id if rec else None
            self.archive_dir = Path(rec.archive_dir)
            # an archived video's full length is known from the probe; keep it
            self.track_duration = not (rec.is_live is False and rec.duration_s)
        self.cfg = cfg
        self.profile = load_profile(cfg["overlay_profile"])
        self.fps = max(0.05, float(cfg.get("live_sample_fps") or 0.5))
        from app.services import keyframes  # noqa: PLC0415

        self.intro_model = keyframes.active_model(cfg)
        tracker = WindowTracker(step_s=1.0 / self.fps,
                                intro_match_len_s=float(cfg.get("intro_match_length_s") or 135.0))

        while True:
            # read the flag *before* looking for segments: once it is set, the
            # segment list is complete, so one more pass picks up the last one
            done = self._capture_done.is_set()
            new = self.store.refresh()
            if new and self.track_duration:
                # length captured so far (the analysis may lag behind it)
                self._set(duration_s=round(self.store.end_s, 2))
            for seg in new:
                tracker.add(self._analyze(seg))
                for w in tracker.ready(seg.end_s):
                    self._catalogue(w)
                self._set(analyzed_s=round(seg.end_s, 2),
                          split_detail=self._progress_text(seg.end_s))
                self._maybe_enrich()
            if done and not new:
                break
            if not new:
                self._maybe_enrich()
                self._capture_done.wait(self.poll_s)

        for w in tracker.ready(self.store.end_s, final=True):
            self._catalogue(w)
        self._set(analyzed_s=round(self.store.end_s, 2))
        if self.event_id is not None:
            self._enrich(final=True)

    def _progress_text(self, analyzed: float) -> str:
        return (f"split {self.matches_made} match(es) so far · analysed "
                f"{_hms(analyzed)} of the recording")

    # -- per segment / per window -----------------------------------------

    def _analyze(self, seg: Segment) -> list[TimerSample]:
        samples = match_detect._scan(seg.path, self.profile, self.fps, self._workdir / "frames",
                                     intro_model=self.intro_model)
        for s in samples:
            s.t = round(seg.start_s + s.t, 3)
        return [s for s in samples if s.t < seg.end_s]

    def _catalogue(self, w: MatchWindow) -> None:
        self.matches_made += 1
        name = match_name(w, self.matches_made)
        read = ocr.read_overlay(
            None, w.start_s, w.end_s, self.profile,
            self._workdir / f"ocr_{int(w.start_s)}", grab=self.store.grab_frame,
            intro_t=w.intro_t,
        )
        provisional = self.event_id is not None
        with session_scope() as session:
            m = Match(
                recording_id=self.recording_id,
                event_id=self.event_id,
                name=name,
                round=MatchRound(w.label_round) if w.label_round else MatchRound.unknown,
                match_num=w.label_num,
                video_start_s=w.start_s,
                video_end_s=w.end_s,
                detection_confidence=w.confidence,
                red_score=read.red_score,
                blue_score=read.blue_score,
                detection_method=DetectionMethod.ocr,
                status=MatchStatus.detected,
                provisional=provisional,
                review_note=enrich.NOTE_PROVISIONAL if provisional else "",
            )
            session.add(m)
            session.flush()
            set_match_teams(session, m, red=read.red_teams, blue=read.blue_teams)
            clip_path = enrich.clip_path_for(self.archive_dir, m, read.red_teams, read.blue_teams)
            match_id = m.id
        log.info("recording %s: %s at %s-%s (%s vs %s)", self.recording_id, name,
                 _hms(w.start_s), _hms(w.end_s), read.red_teams, read.blue_teams)

        thumb_path = clip_path.with_suffix(".jpg")
        try:
            self.store.cut(w.start_s, w.end_s, clip_path,
                           pad_start=float(self.cfg["clip_pad_start_s"]),
                           pad_end=float(self.cfg["clip_pad_end_s"]))
            self.store.thumbnail((w.start_s + w.end_s) / 2, thumb_path)
            err = None
        except Exception as exc:
            log.exception("live clip failed for match %s", match_id)
            err = str(exc)
        with session_scope() as session:
            m = session.get(Match, match_id)
            if m is None:
                return
            if err is None:
                m.clip_path, m.thumb_path, m.status = str(clip_path), str(thumb_path), MatchStatus.clipped
            else:
                m.status = MatchStatus.failed
                m.review_note = (m.review_note + f" | clip error: {err}").strip(" |")
            session.add(m)
        # a freshly played match is the most likely thing VEX Events now knows about
        self._next_enrich = min(self._next_enrich, self._last_enrich + ENRICH_MIN_GAP_S)

    # -- VEX Events --------------------------------------------------

    def _maybe_enrich(self) -> None:
        if self.event_id is None or time.monotonic() < self._next_enrich:
            return
        if not enrich.pending(self.recording_id):
            self._next_enrich = time.monotonic() + ENRICH_INTERVAL_S
            return
        self._enrich(final=False)

    def _enrich(self, *, final: bool) -> None:
        self._last_enrich = time.monotonic()
        try:
            detail = enrich.refresh_recording(self.recording_id, final=final)
            log.info("recording %s: VEX Events refresh: %s", self.recording_id, detail)
            self._next_enrich = time.monotonic() + ENRICH_INTERVAL_S
        except VexEventsError as exc:
            log.warning("recording %s: VEX Events refresh failed: %s", self.recording_id, exc)
            self._next_enrich = time.monotonic() + ENRICH_BACKOFF_S

    # -- helpers -----------------------------------------------------

    def _set(self, **fields) -> None:
        with session_scope() as session:
            rec = session.get(Recording, self.recording_id)
            if rec is None:
                return
            for k, v in fields.items():
                setattr(rec, k, v)
            session.add(rec)


def _hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
