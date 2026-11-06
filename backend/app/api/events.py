"""Event lookup + import from RobotEvents."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.auth import require_user
from app.db import get_session
from app.models import Event
from app.schemas import EventImportIn, EventOut, EventSearchOut
from app.services import settings_store
from app.services.robotevents import RobotEventsClient, RobotEventsError

router = APIRouter(prefix="/api/events", tags=["events"])


def _client(session: Session) -> RobotEventsClient:
    token = settings_store.get(session, "robotevents_token")
    return RobotEventsClient(token=token or None)


@router.get("", response_model=list[EventOut])
def list_events(session: Session = Depends(get_session)) -> list[Event]:
    return session.exec(select(Event).order_by(Event.created_at.desc())).all()


# Listing is public (the match library filters by event); the rest needs an account.
@router.get("/search", response_model=EventSearchOut, dependencies=[Depends(require_user)])
def search_event(sku: str, session: Session = Depends(get_session)) -> EventSearchOut:
    try:
        ev = _client(session).find_event(sku)
    except RobotEventsError as exc:
        raise HTTPException(400, str(exc))
    return EventSearchOut(
        sku=ev.sku,
        re_event_id=ev.re_event_id,
        name=ev.name,
        season=ev.season,
        program=ev.program,
        start_date=ev.start_date,
        divisions=ev.divisions,
    )


@router.post("/import", response_model=EventOut, dependencies=[Depends(require_user)])
def import_event(body: EventImportIn, session: Session = Depends(get_session)) -> Event:
    try:
        ev = _client(session).find_event(body.sku)
    except RobotEventsError as exc:
        raise HTTPException(400, str(exc))

    row = session.exec(select(Event).where(Event.sku == ev.sku)).first()
    if row is None:
        row = Event(sku=ev.sku)
    row.re_event_id = ev.re_event_id
    row.name = ev.name
    row.season = ev.season
    row.program = ev.program
    row.start_date = ev.start_date
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.delete("/{event_id}", status_code=204, dependencies=[Depends(require_user)])
def delete_event(event_id: int, session: Session = Depends(get_session)) -> None:
    row = session.get(Event, event_id)
    if row:
        session.delete(row)
        session.commit()
