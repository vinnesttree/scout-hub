"""Upgrade provisional matches with VEX Events data, during and after capture.

The live splitter catalogues each match as soon as it has been played, with the
teams and score it could read off the overlay (rough OCR estimates). This module
ties those matches to the event's schedule and swaps in the authoritative data:

* **linking** a provisional match to a scheduled one uses whatever evidence is
  available: the match label OCR'd from the overlay ("QUALIFICATION 13"), the
  OCR'd team numbers, and the match's wall-clock time against the ``started``
  time VEX Events records for each match. No single weak signal is enough; the
  best candidate must clearly beat the runner-up.
* once linked, **teams** come from the schedule (published before matches are
  played) and the **score** once VEX Events reports the match as scored. Until
  then the OCR score stays, and the match stays marked provisional.

It is called repeatedly while a recording runs (data is filled in as the event
progresses), once more when capture ends, and from the "Refresh from VEX Events"
button. Matches a person has edited (``detection_method == manual``) are never
touched.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import Session, select

from app.crud import match_team_numbers, set_match_teams
from app.db import session_scope
from app.models import DetectionMethod, Event, Match, MatchRound, Recording
from app.services import clipper, settings_store
from app.services.aligner import align
from app.services.match_detect import MatchWindow
from app.services.vexevents import REMatch, VexEventsClient

log = logging.getLogger("scouthub.enrich")

# A candidate needs at least this much evidence, and must beat the runner-up by
# MIN_MARGIN, to be linked. Two matching team numbers, or a matching label, or a
# start time within a minute each reach MIN_SCORE on their own.
MIN_SCORE = 3.0
MIN_MARGIN = 1.5

NOTE_PROVISIONAL = (
    "Provisional: teams and score are on-screen (OCR) estimates; they are replaced "
    "with VEX Events data once the match is found there."
)
NOTE_AWAITING_SCORE = (
    "Teams from VEX Events. Score is an on-screen estimate until VEX Events posts it."
)
NOTE_BY_POSITION = (
    "Matched to the schedule by position only (no label, team or time evidence) "
    "— please verify."
)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite hands datetimes back naive; everything here is stored as UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Pure matching (unit-tested)
# --------------------------------------------------------------------------

@dataclass
class Evidence:
    """What is known about a detected match before it's tied to the schedule."""

    round: str | None = None            # MatchRound value read off the label
    match_num: int | None = None        # number read off the label
    red: list[str] = field(default_factory=list)   # OCR'd team numbers
    blue: list[str] = field(default_factory=list)
    wall_start: datetime | None = None  # wall-clock time the match appeared on stream


def score_candidate(ev: Evidence, rm: REMatch, *, offset_s: float = 0.0) -> float:
    """How strongly the evidence points at scheduled match ``rm``.

    ``offset_s`` is the typical (stream time - VEX Events start time) seen on
    matches already linked: it absorbs stream latency and clock skew.
    """
    score = 0.0

    if ev.match_num is not None and rm.match_num is not None:
        if rm.match_num == ev.match_num:
            if ev.round and ev.round == rm.round:
                score += 3.0
            elif not ev.round:
                score += 1.5
        elif ev.round and ev.round == rm.round:
            score -= 0.5        # label names a different match (OCR digits can misread)

    red_api = {t.number.upper() for t in (rm.alliance("red").teams if rm.alliance("red") else [])}
    blue_api = {t.number.upper() for t in (rm.alliance("blue").teams if rm.alliance("blue") else [])}
    red_ocr = {x.upper() for x in ev.red}
    blue_ocr = {x.upper() for x in ev.blue}
    direct = len(red_ocr & red_api) + len(blue_ocr & blue_api)
    swapped = len(red_ocr & blue_api) + len(blue_ocr & red_api)
    # Teams on the sides OCR read them, or all on the opposite sides (the profile's
    # red/blue panels are the wrong way round for this overlay): either is strong
    # evidence. A mix of both is weaker.
    score += 1.5 * max(direct, swapped) + 0.5 * min(direct, swapped)

    started = as_utc(rm.started_at)
    if ev.wall_start is not None and started is not None:
        dt = abs((as_utc(ev.wall_start) - started).total_seconds() - offset_s)
        if dt <= 60:
            score += 3.0
        elif dt <= 150:
            score += 1.5
        elif dt > 900:
            score -= 2.0
    return score


def pick_schedule_match(
    ev: Evidence,
    schedule: list[REMatch],
    *,
    claimed: set[int] = frozenset(),
    offset_s: float = 0.0,
) -> REMatch | None:
    """The scheduled match the evidence clearly identifies, or None."""
    scored = sorted(
        ((score_candidate(ev, rm, offset_s=offset_s), rm)
         for rm in schedule if rm.re_match_id not in claimed),
        key=lambda x: x[0],
        reverse=True,
    )
    if not scored or scored[0][0] < MIN_SCORE:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < MIN_MARGIN:
        return None
    return scored[0][1]


# --------------------------------------------------------------------------
# Database side
# --------------------------------------------------------------------------

def _wall_start(rec: Recording, m: Match) -> datetime | None:
    base = as_utc(rec.video_started_at)
    if base is None or m.video_start_s is None:
        return None
    return base + timedelta(seconds=m.video_start_s)


def _estimate_offset(rec: Recording, rows: list[Match], by_id: dict[int, REMatch]) -> float:
    diffs = []
    for m in rows:
        rm = by_id.get(m.re_match_id) if m.re_match_id else None
        wall = _wall_start(rec, m)
        started = as_utc(rm.started_at) if rm else None
        if wall is not None and started is not None:
            diffs.append((wall - started).total_seconds())
    return statistics.median(diffs) if diffs else 0.0


def _round(value: str) -> MatchRound:
    return MatchRound(value) if value in MatchRound._value2member_map_ else MatchRound.unknown


def apply_schedule_match(
    session: Session, rec: Recording, m: Match, rm: REMatch, *, note: str | None = None
) -> bool:
    """Copy VEX Events data for ``rm`` onto ``m``. Returns True if anything changed."""
    changed = False
    if m.re_match_id != rm.re_match_id:
        m.re_match_id = rm.re_match_id
        m.event_id = rec.event_id
        m.name = rm.name or m.name
        m.round = _round(rm.round)
        m.match_num = rm.match_num
        m.scheduled_at = rm.scheduled_at
        m.detection_method = DetectionMethod.api
        changed = True

    red, blue = rm.alliance("red"), rm.alliance("blue")
    api_red = [t.number for t in (red.teams if red else [])]
    api_blue = [t.number for t in (blue.teams if blue else [])]
    if api_red or api_blue:
        cur_red, cur_blue = match_team_numbers(session, m)
        if [x.upper() for x in api_red] != cur_red or [x.upper() for x in api_blue] != cur_blue:
            set_match_teams(session, m, red=api_red, blue=api_blue)
            changed = True

    has_score = bool(red and blue and red.score is not None and blue.score is not None)
    if has_score and (m.red_score, m.blue_score) != (red.score, blue.score):
        m.red_score, m.blue_score = red.score, blue.score
        changed = True

    provisional = not has_score
    new_note = note or (NOTE_AWAITING_SCORE if provisional else "")
    if m.provisional != provisional or m.review_note != new_note:
        m.provisional, m.review_note = provisional, new_note
        changed = True

    if changed:
        session.add(m)
        session.flush()
        rename_clip(session, rec, m)
    return changed


def clip_path_for(archive_dir: Path, m: Match, red: list[str], blue: list[str]) -> Path:
    """``matches/<name>__<red>_vs_<blue>.mp4``, made unique with the match id if
    another match already owns that name."""
    fname = clipper.clip_filename(m.name or f"match{m.id}", "-".join(red), "-".join(blue))
    path = archive_dir / "matches" / fname
    if path.exists() and str(path) != (m.clip_path or ""):
        path = path.with_name(f"{path.stem}_{m.id}{path.suffix}")
    return path


def rename_clip(session: Session, rec: Recording, m: Match) -> None:
    """Keep the clip's file name in step with the match's (new) name and teams."""
    if not m.clip_path or not Path(m.clip_path).exists():
        return
    red, blue = match_team_numbers(session, m)
    old = Path(m.clip_path)
    new = clip_path_for(Path(rec.archive_dir), m, red, blue)
    if new == old:
        return
    try:
        old.replace(new)
        m.clip_path = str(new)
        if m.thumb_path and Path(m.thumb_path).exists():
            thumb = new.with_suffix(".jpg")
            Path(m.thumb_path).replace(thumb)
            m.thumb_path = str(thumb)
        session.add(m)
    except OSError as exc:  # pragma: no cover - filesystem dependent
        log.warning("could not rename clip for match %s: %s", m.id, exc)


def pending(recording_id: int) -> bool:
    """Is there anything VEX Events could still fill in for this recording?"""
    with session_scope() as session:
        rows = session.exec(
            select(Match).where(
                Match.recording_id == recording_id,
                Match.detection_method != DetectionMethod.manual,
            )
        ).all()
        return any(m.provisional or m.re_match_id is None for m in rows)


def fetch_schedule(recording_id: int) -> list[REMatch] | None:
    """The linked event's matches, or None when the recording has no event.
    Raises :class:`VexEventsError` if the API can't be used."""
    with session_scope() as session:
        rec = session.get(Recording, recording_id)
        ev = session.get(Event, rec.event_id) if rec and rec.event_id else None
        if ev is None or not ev.re_event_id:
            return None
        token = settings_store.get_all(session)["vexevents_token"] or None
        re_event_id = ev.re_event_id
    return VexEventsClient(token=token).get_matches(re_event_id)


def refresh_recording(
    recording_id: int, *, schedule: list[REMatch] | None = None, final: bool = False
) -> str:
    """Link/refresh this recording's matches against VEX Events.

    ``final`` (capture over): if nothing at all could be linked by evidence,
    fall back to pairing matches with the schedule in order — what the
    after-the-fact pipeline does.
    """
    if schedule is None:
        schedule = fetch_schedule(recording_id)
        if schedule is None:
            return "no linked event"
    if not schedule:
        return "VEX Events has no matches for this event yet"
    by_id = {rm.re_match_id: rm for rm in schedule}

    linked = updated = 0
    with session_scope() as session:
        rec = session.get(Recording, recording_id)
        if rec is None:
            return "recording not found"
        rows = session.exec(
            select(Match).where(Match.recording_id == recording_id)
        ).all()
        rows.sort(key=lambda m: (m.video_start_s is None, m.video_start_s or 0.0))
        claimed = {m.re_match_id for m in rows if m.re_match_id}
        offset = _estimate_offset(rec, rows, by_id)

        for m in rows:
            if m.detection_method == DetectionMethod.manual:
                continue
            if m.re_match_id:
                rm = by_id.get(m.re_match_id)
                if rm and apply_schedule_match(session, rec, m, rm,
                                               note=m.review_note if m.review_note == NOTE_BY_POSITION else None):
                    updated += 1
                continue
            if not m.provisional:
                continue
            red, blue = match_team_numbers(session, m)
            ev = Evidence(
                round=m.round.value if m.round != MatchRound.unknown else None,
                match_num=m.match_num,
                red=red, blue=blue,
                wall_start=_wall_start(rec, m),
            )
            rm = pick_schedule_match(ev, schedule, claimed=claimed, offset_s=offset)
            if rm is not None:
                apply_schedule_match(session, rec, m, rm)
                claimed.add(rm.re_match_id)
                linked += 1
                # later matches get a better latency estimate
                offset = _estimate_offset(rec, rows, by_id)

        if final and not claimed:
            linked += _link_by_position(session, rec, rows, schedule)

    return f"{linked} linked, {updated} updated from VEX Events"


def _link_by_position(session: Session, rec: Recording, rows: list[Match],
                      schedule: list[REMatch]) -> int:
    todo = [m for m in rows if m.provisional and m.video_start_s is not None
            and m.video_end_s is not None and m.detection_method != DetectionMethod.manual]
    if not todo:
        return 0
    windows = [MatchWindow(m.video_start_s, m.video_end_s, m.detection_confidence or 0.5)
               for m in todo]
    by_window = {id(w): m for w, m in zip(windows, todo)}
    n = 0
    for pair in align(windows, schedule).matched:
        m = by_window[id(pair.window)]
        apply_schedule_match(session, rec, m, pair.re_match, note=NOTE_BY_POSITION)
        n += 1
    return n
