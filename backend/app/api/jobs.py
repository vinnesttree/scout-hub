"""Read-only view of the background job queue."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.auth import require_user
from app.db import get_session
from app.models import Job
from app.schemas import JobOut

router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(require_user)])


@router.get("", response_model=list[JobOut])
def list_jobs(
    recording_id: int | None = None,
    limit: int = 50,
    session: Session = Depends(get_session),
) -> list[Job]:
    stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if recording_id is not None:
        stmt = stmt.where(Job.recording_id == recording_id)
    return session.exec(stmt).all()


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: int, session: Session = Depends(get_session)) -> Job:
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job
