"""Match library: filter/sort by team, detail, manual edits, clip streaming."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlmodel import Session, select

from app.crud import match_team_numbers, set_match_teams
from app.db import get_session
from app.models import (
    Alliance,
    DetectionMethod,
    Match,
    MatchRound,
    MatchStatus,
    MatchTeam,
    Recording,
    Team,
)
from app.schemas import MatchCreate, MatchOut, MatchPatch, TeamOut
from app.services import clipper

router = APIRouter(prefix="/api/matches", tags=["matches"])

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _winner(red: int | None, blue: int | None) -> str | None:
    if red is None or blue is None:
        return None
    if red > blue:
        return "red"
    if blue > red:
        return "blue"
    return "tie"


def serialize(session: Session, m: Match) -> MatchOut:
    links = session.exec(select(MatchTeam).where(MatchTeam.match_id == m.id)).all()
    red_teams, blue_teams = [], []
    for link in sorted(links, key=lambda x: x.station):
        team = session.get(Team, link.team_id)
        if not team:
            continue
        entry = TeamOut(number=team.number, name=team.name, station=link.station)
        (red_teams if link.alliance == Alliance.red else blue_teams).append(entry)
    return MatchOut(
        id=m.id,
        recording_id=m.recording_id,
        event_id=m.event_id,
        name=m.name,
        round=m.round.value,
        match_num=m.match_num,
        scheduled_at=m.scheduled_at,
        video_start_s=m.video_start_s,
        video_end_s=m.video_end_s,
        detection_confidence=m.detection_confidence,
        detection_method=m.detection_method.value,
        status=m.status.value,
        review_note=m.review_note,
        red_score=m.red_score,
        blue_score=m.blue_score,
        red_teams=red_teams,
        blue_teams=blue_teams,
        has_clip=bool(m.clip_path and Path(m.clip_path).exists()),
        winner=_winner(m.red_score, m.blue_score),
    )


_SORT_KEYS = {"match_num", "scheduled_at", "created_at", "red_score", "blue_score", "team"}


@router.get("", response_model=list[MatchOut])
def list_matches(
    session: Session = Depends(get_session),
    team: str | None = Query(None, description="team number, e.g. 1234A"),
    event_id: int | None = None,
    recording_id: int | None = None,
    round: MatchRound | None = None,
    has_clip: bool | None = None,
    sort: str = "scheduled_at",
    order: str = "asc",
) -> list[MatchOut]:
    stmt = select(Match)
    if event_id is not None:
        stmt = stmt.where(Match.event_id == event_id)
    if recording_id is not None:
        stmt = stmt.where(Match.recording_id == recording_id)
    if round is not None:
        stmt = stmt.where(Match.round == round)
    if team:
        t = session.exec(select(Team).where(Team.number == team.strip().upper())).first()
        if not t:
            return []
        match_ids = [
            mt.match_id for mt in session.exec(
                select(MatchTeam).where(MatchTeam.team_id == t.id)
            ).all()
        ]
        if not match_ids:
            return []
        stmt = stmt.where(Match.id.in_(match_ids))

    rows = session.exec(stmt).all()
    out = [serialize(session, m) for m in rows]

    if has_clip is not None:
        out = [m for m in out if m.has_clip == has_clip]

    key = sort if sort in _SORT_KEYS else "scheduled_at"
    reverse = order.lower() == "desc"

    def sort_key(m: MatchOut):
        if key == "team":
            nums = [t.number for t in m.red_teams + m.blue_teams]
            return (min(nums) if nums else "~", m.match_num or 0)
        if key == "scheduled_at":
            return (m.scheduled_at is None, m.scheduled_at or _EPOCH, m.match_num or 0)
        if key in ("red_score", "blue_score"):
            v = getattr(m, key)
            return (v is None, v or 0)
        if key == "match_num":
            return (m.match_num is None, m.match_num or 0)
        return (m.id,)

    out.sort(key=sort_key, reverse=reverse)
    return out


@router.get("/{match_id}", response_model=MatchOut)
def get_match(match_id: int, session: Session = Depends(get_session)) -> MatchOut:
    m = session.get(Match, match_id)
    if not m:
        raise HTTPException(404, "match not found")
    return serialize(session, m)


@router.post("", response_model=MatchOut, status_code=201)
def create_match(body: MatchCreate, session: Session = Depends(get_session)) -> MatchOut:
    rec = session.get(Recording, body.recording_id)
    if not rec:
        raise HTTPException(404, "recording not found")
    m = Match(
        recording_id=rec.id,
        event_id=rec.event_id,
        name=body.name,
        round=body.round,
        match_num=body.match_num,
        video_start_s=body.video_start_s,
        video_end_s=body.video_end_s,
        red_score=body.red_score,
        blue_score=body.blue_score,
        detection_method=DetectionMethod.manual,
        status=MatchStatus.detected,
    )
    session.add(m)
    session.flush()
    set_match_teams(session, m, red=body.red_teams, blue=body.blue_teams)
    session.commit()
    _reclip(session, m.id, strict=False)
    session.refresh(m)
    return serialize(session, m)


@router.patch("/{match_id}", response_model=MatchOut)
def patch_match(match_id: int, body: MatchPatch, session: Session = Depends(get_session)) -> MatchOut:
    m = session.get(Match, match_id)
    if not m:
        raise HTTPException(404, "match not found")

    boundary_changed = False
    for field in ("name", "match_num", "red_score", "blue_score", "review_note"):
        val = getattr(body, field)
        if val is not None:
            setattr(m, field, val)
    if body.round is not None:
        m.round = body.round
    if body.video_start_s is not None and body.video_start_s != m.video_start_s:
        m.video_start_s, boundary_changed = body.video_start_s, True
    if body.video_end_s is not None and body.video_end_s != m.video_end_s:
        m.video_end_s, boundary_changed = body.video_end_s, True

    # any human edit pins the row so re-processing won't wipe it
    m.detection_method = DetectionMethod.manual
    session.add(m)

    if body.red_teams is not None or body.blue_teams is not None:
        red, blue = match_team_numbers(session, m)
        set_match_teams(
            session, m,
            red=body.red_teams if body.red_teams is not None else red,
            blue=body.blue_teams if body.blue_teams is not None else blue,
        )
    session.commit()

    if boundary_changed and body.reclip:
        _reclip(session, m.id, strict=False)
    session.refresh(m)
    return serialize(session, m)


@router.delete("/{match_id}", status_code=204)
def delete_match(
    match_id: int, delete_files: bool = False, session: Session = Depends(get_session)
) -> None:
    m = session.get(Match, match_id)
    if not m:
        return
    if delete_files:
        for f in (m.clip_path, m.thumb_path):
            if f and Path(f).exists():
                Path(f).unlink()
    for link in list(m.teams):
        session.delete(link)
    session.delete(m)
    session.commit()


@router.post("/{match_id}/reclip", response_model=MatchOut)
def reclip_match(match_id: int, session: Session = Depends(get_session)) -> MatchOut:
    m = session.get(Match, match_id)
    if not m:
        raise HTTPException(404, "match not found")
    _reclip(session, match_id, strict=True)
    session.refresh(m)
    return serialize(session, m)


def _reclip(session: Session, match_id: int, *, strict: bool) -> None:
    """Regenerate the clip + thumbnail for a match.

    ``strict=True`` surfaces problems as HTTP errors (explicit re-clip button);
    ``strict=False`` leaves the match as-is if the source file is gone or ffmpeg
    fails (called as a side effect of create/patch).
    """
    from app.services import settings_store

    m = session.get(Match, match_id)
    if not m or m.video_start_s is None or m.video_end_s is None or not m.recording_id:
        if strict:
            raise HTTPException(409, "match has no video boundaries to clip")
        return
    rec = session.get(Recording, m.recording_id)
    if not rec or not rec.file_path or not Path(rec.file_path).exists():
        if strict:
            raise HTTPException(409, "the full recording is no longer on disk; cannot re-clip")
        return
    cfg = settings_store.get_all(session)
    red, blue = match_team_numbers(session, m)
    archive_dir = Path(rec.archive_dir)
    fname = clipper.clip_filename(m.name or f"match{m.id}", "-".join(red), "-".join(blue))
    clip_path = archive_dir / "matches" / fname
    thumb_path = archive_dir / "matches" / (clip_path.stem + ".jpg")
    try:
        clipper.cut(
            Path(rec.file_path), m.video_start_s, m.video_end_s, clip_path,
            pad_start=float(cfg["clip_pad_start_s"]), pad_end=float(cfg["clip_pad_end_s"]),
        )
        clipper.thumbnail(Path(rec.file_path), (m.video_start_s + m.video_end_s) / 2, thumb_path)
    except Exception as exc:  # pragma: no cover
        m.status = MatchStatus.failed
        session.add(m)
        session.commit()
        if strict:
            raise HTTPException(500, f"clip failed: {exc}")
        return
    m.clip_path = str(clip_path)
    m.thumb_path = str(thumb_path)
    m.status = MatchStatus.clipped
    session.add(m)
    session.commit()


# --- media streaming --------------------------------------------

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


@router.get("/{match_id}/clip")
def stream_clip(match_id: int, request: Request, session: Session = Depends(get_session)):
    m = session.get(Match, match_id)
    if not m or not m.clip_path or not Path(m.clip_path).exists():
        raise HTTPException(404, "clip not available")
    path = Path(m.clip_path)
    file_size = path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type="video/mp4", filename=path.name)

    match = _RANGE_RE.match(range_header)
    if not match:
        raise HTTPException(416, "invalid range")
    start = int(match.group(1)) if match.group(1) else 0
    end = int(match.group(2)) if match.group(2) else file_size - 1
    end = min(end, file_size - 1)
    if start > end:
        raise HTTPException(416, "range not satisfiable")
    length = end - start + 1

    def iterfile():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    return StreamingResponse(iterfile(), status_code=206, headers=headers, media_type="video/mp4")


@router.get("/{match_id}/thumb")
def get_thumb(match_id: int, session: Session = Depends(get_session)):
    m = session.get(Match, match_id)
    if not m or not m.thumb_path or not Path(m.thumb_path).exists():
        raise HTTPException(404, "thumbnail not available")
    return FileResponse(m.thumb_path, media_type="image/jpeg")
