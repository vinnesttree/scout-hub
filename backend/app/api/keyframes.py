"""Team-intro key frames: find candidates, review them, train the detector."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session, func, select

from app.api.auth import require_user
from app.db import get_session
from app.models import KeyFrame, Recording, RecordingStatus
from app.schemas import KeyFrameAlt, KeyFrameBulk, KeyFrameOut, KeyFramePatch
from app.services import jobs as jobs_mod
from app.services import keyframes as kf_svc
from app.worker import worker

router = APIRouter(
    prefix="/api/keyframes", tags=["keyframes"], dependencies=[Depends(require_user)]
)


def _out(kf: KeyFrame) -> KeyFrameOut:
    alts = [KeyFrameAlt(**{k: a.get(k) for k in ("t", "score", "timer", "tokens")})
            for a in json.loads(kf.alternatives_json or "[]")]
    data = KeyFrameOut.model_validate(kf, from_attributes=True)
    data.alternatives = alts
    return data


def _get(session: Session, kf_id: int) -> KeyFrame:
    kf = session.get(KeyFrame, kf_id)
    if kf is None:
        raise HTTPException(404, "key frame not found")
    return kf


def _check_status(status: str) -> None:
    if status not in kf_svc.STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(kf_svc.STATUSES)}")


@router.get("", response_model=list[KeyFrameOut])
def list_keyframes(
    recording_id: int | None = None,
    status: str | None = None,
    session: Session = Depends(get_session),
) -> list[KeyFrameOut]:
    stmt = select(KeyFrame).order_by(KeyFrame.recording_id, KeyFrame.anchor_start_s)
    if recording_id is not None:
        stmt = stmt.where(KeyFrame.recording_id == recording_id)
    if status:
        _check_status(status)
        stmt = stmt.where(KeyFrame.status == status)
    return [_out(k) for k in session.exec(stmt).all()]


@router.get("/summary")
def summary(session: Session = Depends(get_session)) -> dict:
    """Review counts per recording and overall, plus the trained model's stats."""
    rows = session.exec(
        select(KeyFrame.recording_id, KeyFrame.status, func.count())
        .group_by(KeyFrame.recording_id, KeyFrame.status)
    ).all()
    per: dict[int, dict[str, int]] = {}
    totals = {s: 0 for s in kf_svc.STATUSES}
    for rid, status, n in rows:
        per.setdefault(rid, {s: 0 for s in kf_svc.STATUSES})[status] = n
        totals[status] += n
    return {
        "recordings": [{"recording_id": rid, **c} for rid, c in sorted(per.items())],
        "totals": totals,
        "min_confirmed": kf_svc.MIN_CONFIRMED,
        "model": kf_svc.model_info(),
    }


@router.post("/find", response_model=dict)
def find(recording_id: int, session: Session = Depends(get_session)) -> dict:
    """Queue a search for intro candidates before every match of a recording."""
    rec = session.get(Recording, recording_id)
    if rec is None:
        raise HTTPException(404, "recording not found")
    if rec.status != RecordingStatus.completed:
        raise HTTPException(409, f"recording is {rec.status.value}, not completed")
    if not rec.file_path:
        raise HTTPException(409, "the full recording was not kept, so there is nothing to search")
    job_id = worker.enqueue_and_submit(recording_id, jobs_mod.KIND_FIND_KEYFRAMES)
    return {"job_id": job_id}


@router.get("/{kf_id}/image")
def image(kf_id: int, t: float | None = None, session: Session = Depends(get_session)):
    """The chosen frame, or (``t``) one of the candidate's alternatives."""
    kf = _get(session, kf_id)
    want = kf.t if t is None else t
    for alt in json.loads(kf.alternatives_json or "[]"):
        if abs(alt["t"] - want) < 0.05 and alt.get("file"):
            path = kf_svc.recording_dir(kf.recording_id) / alt["file"]
            if path.is_file():
                return FileResponse(path, media_type="image/jpeg")
    raise HTTPException(404, "image not found")


@router.patch("/{kf_id}", response_model=KeyFrameOut)
def patch(kf_id: int, body: KeyFramePatch, session: Session = Depends(get_session)) -> KeyFrameOut:
    kf = _get(session, kf_id)
    if body.t is not None:
        try:
            kf_svc.select_frame(kf, body.t)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except FileNotFoundError:
            raise HTTPException(409, "this candidate's frame data is missing; find key frames again")
    if body.status is not None:
        _check_status(body.status)
        kf.status = body.status
    kf.reviewed_at = None if kf.status == "candidate" else datetime.now(timezone.utc)
    session.add(kf)
    session.commit()
    session.refresh(kf)
    return _out(kf)


@router.post("/bulk", response_model=dict)
def bulk(body: KeyFrameBulk, session: Session = Depends(get_session)) -> dict:
    _check_status(body.status)
    now = datetime.now(timezone.utc)
    n = 0
    for kf_id in body.ids:
        kf = session.get(KeyFrame, kf_id)
        if kf is None:
            continue
        kf.status = body.status
        kf.reviewed_at = None if body.status == "candidate" else now
        session.add(kf)
        n += 1
    session.commit()
    return {"updated": n}


@router.delete("", status_code=204)
def delete_for_recording(recording_id: int, session: Session = Depends(get_session)) -> None:
    kf_svc.delete_for_recording(session, recording_id)
    session.commit()


@router.post("/train", response_model=dict)
def train(session: Session = Depends(get_session)) -> dict:
    """Train the intro detector on every confirmed / rejected key frame."""
    try:
        return kf_svc.train_from_db(session)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/model", status_code=204)
def delete_model() -> None:
    kf_svc.delete_model()
