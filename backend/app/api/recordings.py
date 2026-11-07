"""Recording lifecycle: create/start, stop, reprocess, list."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, func, select

from app.config import get_settings
from app.api.auth import require_user
from app.db import get_session
from app.models import Event, Match, Recording, RecordingStatus
from app.api.events import get_or_import_event
from app.schemas import RecordingCreate, RecordingOut
from app.services import stream_resolver
from app.services.recorder import manager as recorder_manager, segments_dir
from app.services import jobs as jobs_mod
from app.worker import worker

# Recordings are admin-area only: every endpoint needs a signed-in account.
router = APIRouter(
    prefix="/api/recordings", tags=["recordings"], dependencies=[Depends(require_user)]
)


def default_archive_dir(event: Event | None) -> str:
    """Folder (relative to the archive root) used when none is chosen:
    ``<Event name> (<SKU>)`` for a linked event, else ``recordings``."""
    if event is None:
        return "recordings"
    name = re.sub(r"[^\w\- .()&]+", " ", event.name or "").strip()
    name = re.sub(r"\s+", " ", name).strip(" .")[:80]
    return f"{name} ({event.sku})" if name else event.sku


def _to_out(session: Session, rec: Recording) -> RecordingOut:
    count = session.exec(
        select(func.count()).select_from(Match).where(Match.recording_id == rec.id)
    ).one()
    data = RecordingOut.model_validate(rec, from_attributes=True)
    data.match_count = int(count)
    return data


@router.get("", response_model=list[RecordingOut])
def list_recordings(session: Session = Depends(get_session)) -> list[RecordingOut]:
    rows = session.exec(select(Recording).order_by(Recording.created_at.desc())).all()
    return [_to_out(session, r) for r in rows]


@router.get("/{rec_id}", response_model=RecordingOut)
def get_recording(rec_id: int, session: Session = Depends(get_session)) -> RecordingOut:
    rec = session.get(Recording, rec_id)
    if not rec:
        raise HTTPException(404, "recording not found")
    return _to_out(session, rec)


@router.post("", response_model=RecordingOut, status_code=201)
def create_recording(body: RecordingCreate, session: Session = Depends(get_session)) -> RecordingOut:
    # validate the stream URL up front so the user gets an immediate error
    try:
        resolved = stream_resolver.classify(body.source_url)
    except stream_resolver.ResolveError as exc:
        raise HTTPException(400, str(exc))

    event_id = body.event_id
    event: Event | None = None
    if (body.event or "").strip():
        event = get_or_import_event(session, body.event.strip())
        event_id = event.id
    elif event_id is not None:
        event = session.get(Event, event_id)
        if event is None:
            raise HTTPException(400, f"event #{event_id} not found")

    sections = (body.sections or "").strip() or None
    if sections and not re.match(r"^(\d+:)?(\d+:)?\d+(\.\d+)?-(\d+:)?(\d+:)?\d+(\.\d+)?$", sections):
        raise HTTPException(400, "sections must look like '0-600' or '1:03:00-1:21:30'")

    root = get_settings().archive_root.resolve()
    archive_dir = Path((body.archive_dir or "").strip() or default_archive_dir(event))
    if not archive_dir.is_absolute():
        archive_dir = root / archive_dir
    archive_dir = archive_dir.resolve()
    if archive_dir != root and root not in archive_dir.parents:
        raise HTTPException(400, "archive_dir must be inside the archive root")
    archive_dir.mkdir(parents=True, exist_ok=True)

    rec = Recording(
        source_url=body.source_url,
        source_type=resolved.source_type,
        resolved_url=resolved.url,
        archive_dir=str(archive_dir),
        event_id=event_id,
        live_from_start=body.live_from_start,
        sections=sections,
        status=RecordingStatus.pending,
    )
    session.add(rec)
    session.commit()
    session.refresh(rec)

    try:
        recorder_manager.start(rec.id)
    except Exception as exc:  # pragma: no cover - defensive
        rec.status = RecordingStatus.failed
        rec.error = str(exc)
        session.add(rec)
        session.commit()
        raise HTTPException(500, f"could not start recorder: {exc}")

    session.refresh(rec)
    return _to_out(session, rec)


@router.post("/{rec_id}/stop", response_model=RecordingOut)
def stop_recording(rec_id: int, session: Session = Depends(get_session)) -> RecordingOut:
    rec = session.get(Recording, rec_id)
    if not rec:
        raise HTTPException(404, "recording not found")
    try:
        recorder_manager.stop(rec_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    session.refresh(rec)
    return _to_out(session, rec)


@router.post("/{rec_id}/process", response_model=dict)
def reprocess(rec_id: int, session: Session = Depends(get_session)) -> dict:
    rec = session.get(Recording, rec_id)
    if not rec:
        raise HTTPException(404, "recording not found")
    if rec.status != RecordingStatus.completed:
        raise HTTPException(409, f"recording is {rec.status.value}, not completed")
    job_id = worker.enqueue_and_submit(rec_id)
    return {"job_id": job_id}


@router.post("/{rec_id}/refresh-api", response_model=dict)
def refresh_from_api(rec_id: int, session: Session = Depends(get_session)) -> dict:
    """Re-check VEX Events for this recording's matches: link provisional ones
    and pull in teams/scores posted since. (Runs automatically while capturing.)"""
    rec = session.get(Recording, rec_id)
    if not rec:
        raise HTTPException(404, "recording not found")
    if rec.event_id is None:
        raise HTTPException(409, "recording has no linked event")
    if recorder_manager.is_running(rec_id):
        raise HTTPException(409, "the recording is still capturing; it refreshes on its own")
    job_id = worker.enqueue_and_submit(rec_id, jobs_mod.KIND_REFRESH_API)
    return {"job_id": job_id}


@router.delete("/{rec_id}", status_code=204)
def delete_recording(
    rec_id: int, delete_files: bool = False, session: Session = Depends(get_session)
) -> None:
    rec = session.get(Recording, rec_id)
    if not rec:
        return
    if recorder_manager.is_running(rec_id):
        raise HTTPException(409, "stop the recording first (and let it finish splitting)")
    matches = session.exec(select(Match).where(Match.recording_id == rec_id)).all()
    if delete_files:
        for m in matches:
            for f in (m.clip_path, m.thumb_path):
                if f and Path(f).exists():
                    Path(f).unlink()
        if rec.file_path and Path(rec.file_path).exists():
            Path(rec.file_path).unlink()
        shutil.rmtree(segments_dir(rec), ignore_errors=True)
    for m in matches:
        for link in list(m.teams):
            session.delete(link)
        session.delete(m)
    session.delete(rec)
    session.commit()
