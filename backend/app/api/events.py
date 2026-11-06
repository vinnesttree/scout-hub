"""Event lookup + import from VEX Events (events.vex.com)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.auth import require_user
from app.db import get_session
from app.models import Event
from app.schemas import EventImportIn, EventOut, EventSearchOut
from app.services import settings_store
from app.services.vexevents import VexEventsClient, VexEventsError, extract_sku

router = APIRouter(prefix="/api/events", tags=["events"])


def _client(session: Session) -> VexEventsClient:
    token = settings_store.get(session, "vexevents_token")
    return VexEventsClient(token=token or None)


def get_or_import_event(session: Session, query: str) -> Event:
    """Return the local Event for an event code / events.vex.com link, importing
    it from VEX Events the first time it's seen. Raises HTTPException(400)."""
    sku = extract_sku(query)
    if sku:
        row = session.exec(select(Event).where(Event.sku == sku)).first()
        if row is not None and row.re_event_id:
            return row
    return _import(session, query)


def _import(session: Session, query: str) -> Event:
    try:
        ev = _client(session).find_event(query)
    except VexEventsError as exc:
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


@router.get("", response_model=list[EventOut])
def list_events(session: Session = Depends(get_session)) -> list[Event]:
    return session.exec(select(Event).order_by(Event.created_at.desc())).all()


# Listing is public (the match library filters by event); the rest needs an account.
@router.get("/search", response_model=EventSearchOut, dependencies=[Depends(require_user)])
def search_event(
    q: str | None = None, sku: str | None = None, session: Session = Depends(get_session)
) -> EventSearchOut:
    """Look up an event by its events.vex.com link or event code (``q``; ``sku`` is
    accepted as an alias)."""
    query = (q or sku or "").strip()
    if not query:
        raise HTTPException(400, "Paste an events.vex.com event link or event code.")
    try:
        ev = _client(session).find_event(query)
    except VexEventsError as exc:
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
    return _import(session, body.sku)


@router.delete("/{event_id}", status_code=204, dependencies=[Depends(require_user)])
def delete_event(event_id: int, session: Session = Depends(get_session)) -> None:
    row = session.get(Event, event_id)
    if row:
        session.delete(row)
        session.commit()
