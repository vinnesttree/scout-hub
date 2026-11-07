"""The processing pipeline: recording -> match windows -> clips + catalogued matches."""

from __future__ import annotations

import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from app.crud import set_match_teams
from app.db import session_scope
from app.models import (
    DetectionMethod,
    Event,
    Job,
    JobStatus,
    Match,
    MatchRound,
    MatchStatus,
    Recording,
    RecordingStatus,
)
from app.services import clipper, match_detect, ocr
from app.services.aligner import align
from app.services.overlay import load_profile
from app.services.vexevents import VexEventsClient, VexEventsError
from app.services import settings_store

log = logging.getLogger("scouthub.jobs")


def _now() -> datetime:
    return datetime.now(timezone.utc)


KIND_PROCESS = "process_recording"
KIND_REFRESH_API = "refresh_api"
KIND_FIND_KEYFRAMES = "find_keyframes"


def enqueue(recording_id: int, kind: str = KIND_PROCESS) -> int:
    with session_scope() as session:
        job = Job(kind=kind, recording_id=recording_id, status=JobStatus.queued)
        session.add(job)
        session.flush()
        return job.id


def enqueue_process(recording_id: int) -> int:
    return enqueue(recording_id, KIND_PROCESS)


def run_job(job_id: int) -> None:
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None or job.status not in (JobStatus.queued, JobStatus.failed):
            return
        job.status = JobStatus.running
        job.started_at = _now()
        job.error = None
        session.add(job)
        recording_id = job.recording_id
        kind = job.kind

    try:
        if kind == KIND_REFRESH_API:
            from app.services import enrich

            detail = enrich.refresh_recording(recording_id, final=True)
        elif kind == KIND_FIND_KEYFRAMES:
            from app.services import keyframes

            detail = keyframes.find_for_recording(recording_id)
        else:
            detail = process_recording(recording_id)
        status, error = JobStatus.done, None
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("job %s failed", job_id)
        detail, status, error = "", JobStatus.failed, str(exc)

    with session_scope() as session:
        job = session.get(Job, job_id)
        if job:
            job.status = status
            job.detail = detail
            job.error = error
            job.finished_at = _now()
            session.add(job)


# --------------------------------------------------------------------------


def process_recording(recording_id: int) -> str:
    with session_scope() as session:
        rec = session.get(Recording, recording_id)
        if rec is None:
            raise RuntimeError(f"recording {recording_id} not found")
        if rec.status != RecordingStatus.completed:
            raise RuntimeError(f"recording {recording_id} is {rec.status}, not completed")
        if not rec.file_path or not Path(rec.file_path).exists():
            raise RuntimeError("recording file is missing on disk")
        video = Path(rec.file_path)
        archive_dir = Path(rec.archive_dir)
        event_id = rec.event_id
        cfg = settings_store.get_all(session)

    profile = load_profile(cfg["overlay_profile"])
    from app.services import keyframes  # noqa: PLC0415

    workdir = Path(tempfile.mkdtemp(prefix="scouthub_proc_"))
    try:
        windows = match_detect.detect_windows(
            video, profile, sample_fps=float(cfg["sample_fps"]), workdir=workdir / "frames",
            intro_model=keyframes.active_model(cfg),
            intro_match_len_s=float(cfg["intro_match_length_s"]),
        )

        with session_scope() as session:
            _clear_auto_matches(session, recording_id)

        made = 0
        if event_id is not None:
            made = _catalog_with_schedule(
                recording_id, event_id, video, archive_dir, windows, profile, cfg, workdir
            )
        else:
            made = _catalog_ocr_only(
                recording_id, video, archive_dir, windows, profile, cfg, workdir
            )

        if not cfg["keep_full_recording"]:
            _maybe_delete_full(recording_id)

        return f"{len(windows)} windows detected, {made} matches catalogued"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _clear_auto_matches(session: Session, recording_id: int) -> None:
    rows = session.exec(
        select(Match).where(
            Match.recording_id == recording_id,
            Match.detection_method != DetectionMethod.manual,
        )
    ).all()
    for m in rows:
        for link in list(m.teams):
            session.delete(link)
        session.delete(m)
    session.flush()


def _catalog_with_schedule(
    recording_id, event_id, video, archive_dir, windows, profile, cfg, workdir
) -> int:
    try:
        with session_scope() as session:
            ev = session.get(Event, event_id)
            re_event_id = ev.re_event_id if ev else None
        client = VexEventsClient(token=cfg["vexevents_token"] or None)
        schedule = client.get_matches(re_event_id) if re_event_id else []
    except VexEventsError as exc:
        log.warning("schedule fetch failed (%s); falling back to OCR", exc)
        return _catalog_ocr_only(recording_id, video, archive_dir, windows, profile, cfg, workdir)

    if not schedule:
        return _catalog_ocr_only(recording_id, video, archive_dir, windows, profile, cfg, workdir)

    result = align(windows, schedule)
    made = 0
    for pair in result.pairs:
        rm = pair.re_match
        red = rm.alliance("red")
        blue = rm.alliance("blue")
        with session_scope() as session:
            match = Match(
                recording_id=recording_id,
                event_id=event_id,
                re_match_id=rm.re_match_id,
                name=rm.name,
                round=MatchRound(rm.round) if rm.round in MatchRound._value2member_map_ else MatchRound.unknown,
                match_num=rm.match_num,
                scheduled_at=rm.scheduled_at,
                red_score=red.score if red else None,
                blue_score=blue.score if blue else None,
                detection_method=DetectionMethod.api,
                status=MatchStatus.detected,
            )
            if pair.window is not None:
                match.video_start_s = pair.window.start_s
                match.video_end_s = pair.window.end_s
                match.detection_confidence = pair.confidence
            else:
                match.review_note = "scheduled match not located in recording"
            session.add(match)
            session.flush()
            set_match_teams(
                session, match,
                red=[t.number for t in (red.teams if red else [])],
                blue=[t.number for t in (blue.teams if blue else [])],
            )
            match_id = match.id
        if pair.window is not None:
            _make_clip(match_id, video, archive_dir, cfg, workdir)
        made += 1

    # windows with no scheduled match -> OCR so they aren't lost
    for w in result.unmatched_windows:
        _one_ocr_match(recording_id, None, video, archive_dir, w, profile, cfg, workdir,
                       note="window had no matching scheduled match")
        made += 1
    return made


def _catalog_ocr_only(recording_id, video, archive_dir, windows, profile, cfg, workdir) -> int:
    for i, w in enumerate(windows, start=1):
        _one_ocr_match(
            recording_id, None, video, archive_dir, w, profile, cfg, workdir,
            name=f"Match {i}",
        )
    return len(windows)


def _one_ocr_match(
    recording_id, event_id, video, archive_dir, window, profile, cfg, workdir, *,
    name: str | None = None, note: str = "",
) -> None:
    read = ocr.read_overlay(
        video, window.start_s, window.end_s, profile, workdir / f"ocr_{int(window.start_s)}",
        intro_t=window.intro_t,
    )
    with session_scope() as session:
        match = Match(
            recording_id=recording_id,
            event_id=event_id,
            name=name or "Match",
            round=MatchRound.unknown,
            video_start_s=window.start_s,
            video_end_s=window.end_s,
            detection_confidence=window.confidence,
            red_score=read.red_score,
            blue_score=read.blue_score,
            detection_method=DetectionMethod.ocr,
            status=MatchStatus.detected,
            review_note=note,
        )
        session.add(match)
        session.flush()
        set_match_teams(session, match, red=read.red_teams, blue=read.blue_teams)
        match_id = match.id
    _make_clip(match_id, video, archive_dir, cfg, workdir)


def _make_clip(match_id: int, video: Path, archive_dir: Path, cfg: dict, workdir: Path) -> None:
    from app.crud import match_team_numbers

    with session_scope() as session:
        match = session.get(Match, match_id)
        if not match or match.video_start_s is None or match.video_end_s is None:
            return
        red, blue = match_team_numbers(session, match)
        fname = clipper.clip_filename(match.name or f"match{match_id}",
                                     "-".join(red), "-".join(blue))
        clip_path = archive_dir / "matches" / fname
        thumb_path = archive_dir / "matches" / (clip_path.stem + ".jpg")
        start_s, end_s = match.video_start_s, match.video_end_s

    try:
        clipper.cut(
            video, start_s, end_s, clip_path,
            pad_start=float(cfg["clip_pad_start_s"]),
            pad_end=float(cfg["clip_pad_end_s"]),
        )
        clipper.thumbnail(video, (start_s + end_s) / 2, thumb_path)
        ok = True
    except Exception as exc:  # pragma: no cover
        log.exception("clip failed for match %s", match_id)
        ok, err = False, str(exc)

    with session_scope() as session:
        match = session.get(Match, match_id)
        if not match:
            return
        if ok:
            match.clip_path = str(clip_path)
            match.thumb_path = str(thumb_path)
            match.status = MatchStatus.clipped
        else:
            match.status = MatchStatus.failed
            match.review_note = (match.review_note + f" | clip error: {err}").strip(" |")
        session.add(match)


def _maybe_delete_full(recording_id: int) -> None:
    with session_scope() as session:
        rec = session.get(Recording, recording_id)
        if not rec or not rec.file_path:
            return
        has_clips = session.exec(
            select(Match).where(Match.recording_id == recording_id,
                                Match.status == MatchStatus.clipped)
        ).first()
        if not has_clips:
            return
        p = Path(rec.file_path)
        if p.exists():
            p.unlink()
        rec.file_path = None
        rec.progress = "full recording removed after clipping"
        session.add(rec)
