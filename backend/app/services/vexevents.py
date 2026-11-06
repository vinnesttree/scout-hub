"""Thin client for the VEX Events API v2 (https://events.vex.com/api/v2).

VEX Events replaced RobotEvents in 2026; the v2 API kept the same endpoints and
response shapes, only the host changed. Auth is a personal bearer token from an
events.vex.com account.

Endpoints used (per the official OpenAPI spec, swagger.yml "Public VEX Events
API" v1.0.21; servers: https://events.vex.com/api/v2):

- ``GET /events?sku[]=<sku>``                          -> PaginatedEvent
- ``GET /events/{id}``                                 -> Event (with ``divisions``)
- ``GET /events/{id}/divisions/{div}/matches``         -> PaginatedMatch
- ``GET /seasons?per_page=1``                          -> connection check

There is no event-wide matches endpoint; matches are listed per division.
Pagination: ``page`` / ``per_page`` (max 250), ``meta.current_page`` / ``meta.last_page``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from datetime import datetime

import httpx

from app.config import get_settings

# MatchObj.round, per the spec: 1 Practice, 2 Qualification, 3 Quarter-Finals,
# 4 Semi-Finals, 5 Finals, 6 Round of 16. 15 is "Top N" (VIQRC finals) and
# 16 "Round Robin"; anything else is kept as "unknown".
_ROUND_MAP = {
    1: "practice",
    2: "qual",
    3: "qf",
    4: "sf",
    5: "final",
    6: "r16",
    15: "final",
}
# Play order of the stages, used to sort matches that have no scheduled time.
_ROUND_ORDER = {"practice": 0, "qual": 1, "r16": 2, "qf": 3, "sf": 4, "final": 5, "unknown": 6}


class VexEventsError(RuntimeError):
    pass


# Event codes ("SKUs") look like RE-V5RC-26-4058, RE-VURC-25-1234, RE-VRC-24-1234.
_SKU_RE = re.compile(r"\b(RE-[A-Z0-9]+-\d{2}-\d+)\b", re.IGNORECASE)


def normalize_token(raw: str) -> str:
    """Clean up a pasted key: whitespace/newlines, surrounding quotes, and a
    leading "Bearer " (we add that ourselves)."""
    tok = (raw or "").strip().strip("'\"").strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    return "".join(tok.split())


def extract_sku(text: str) -> str | None:
    """Pull an event code out of a bare code or an event page link.

    Accepts e.g. ``RE-V5RC-26-4058``,
    ``https://events.vex.com/robot-competitions/vex-robotics-competition/RE-V5RC-26-4058.html``
    (any language prefix), or an old robotevents.com link.
    """
    m = _SKU_RE.search(text or "")
    return m.group(1).upper() if m else None


@dataclass
class RETeamRef:
    number: str
    name: str = ""
    re_team_id: int | None = None


@dataclass
class REAlliance:
    color: str                       # "red" | "blue"
    score: int | None
    teams: list[RETeamRef] = field(default_factory=list)


@dataclass
class REMatch:
    re_match_id: int
    name: str
    round: str
    match_num: int | None
    instance: int | None
    scheduled_at: datetime | None
    started_at: datetime | None
    division_id: int | None
    alliances: list[REAlliance] = field(default_factory=list)

    def alliance(self, color: str) -> REAlliance | None:
        return next((a for a in self.alliances if a.color == color), None)


@dataclass
class REEvent:
    re_event_id: int
    sku: str
    name: str
    season: str
    program: str
    start_date: datetime | None
    divisions: list[dict]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _get(client: httpx.Client, path: str, params: dict) -> httpx.Response:
    try:
        return client.get(path, params=params)
    except httpx.HTTPError as exc:
        raise VexEventsError(f"Couldn't reach VEX Events ({client.base_url}): {exc}") from exc


def _check(resp: httpx.Response) -> None:
    if resp.status_code == 401:
        raise VexEventsError("VEX Events rejected the API token (401).")
    if resp.status_code == 403:
        # VEX Events answers some rate-limited requests with 403 instead of 429.
        raise VexEventsError(
            "VEX Events refused the request (403): the token may be invalid, or the rate "
            "limit was hit. Check the token and try again shortly."
        )
    if resp.status_code == 429:
        raise VexEventsError("VEX Events rate limit hit (429). Try again shortly.")
    if resp.is_error:
        raise VexEventsError(f"VEX Events returned HTTP {resp.status_code}.")


def _play_order(m: REMatch) -> tuple:
    # Scheduled matches first, by time; unscheduled ones (often eliminations)
    # after, by stage. Uses timestamps so naive/aware datetimes never compare.
    ts = m.scheduled_at.timestamp() if m.scheduled_at else None
    return (
        ts is None,
        ts or 0.0,
        _ROUND_ORDER.get(m.round, 99),
        m.instance or 0,
        m.match_num or 0,
    )


class VexEventsClient:
    def __init__(self, token: str | None = None, base_url: str | None = None) -> None:
        cfg = get_settings()
        self.token = normalize_token(token or cfg.vexevents_token or "")
        self.base_url = (base_url or cfg.vexevents_base_url).rstrip("/")

    def _client(self) -> httpx.Client:
        if not self.token:
            raise VexEventsError(
                "No VEX Events API key configured. Set SCOUTHUB_VEXEVENTS_TOKEN (or "
                "VEX_EVENTS_API_KEY) in the environment, or enter it on the Settings page."
            )
        return httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
            timeout=30,
        )

    def _get_all(self, client: httpx.Client, path: str, params: dict) -> list[dict]:
        """Follow the API's page-based pagination."""
        results: list[dict] = []
        page = 1
        while True:
            q = {**params, "page": page, "per_page": 250}
            resp = _get(client, path, q)
            _check(resp)
            body = resp.json()
            results.extend(body.get("data", []))
            meta = body.get("meta", {})
            if not meta or meta.get("current_page", page) >= meta.get("last_page", page):
                break
            page += 1
        return results

    # -- public API --------------------------------------------------

    def ping(self) -> None:
        """Cheap authenticated call, used by Settings -> Test connection."""
        with self._client() as client:
            _check(_get(client, "/seasons", {"per_page": 1}))

    def find_event(self, sku_or_link: str) -> REEvent:
        """Look an event up by its code or by an events.vex.com event link."""
        sku = extract_sku(sku_or_link)
        if not sku:
            raise VexEventsError(
                "Couldn't find an event code in that. Paste the event's events.vex.com link "
                "or its code, e.g. RE-V5RC-26-4058."
            )
        with self._client() as client:
            rows = self._get_all(client, "/events", {"sku[]": sku})
        if not rows:
            raise VexEventsError(f"No event found on VEX Events for {sku}.")
        e = rows[0]
        return REEvent(
            re_event_id=e["id"],
            sku=e.get("sku", sku),
            name=e.get("name", ""),
            season=(e.get("season") or {}).get("name", ""),
            program=(e.get("program") or {}).get("code", ""),
            start_date=_parse_dt(e.get("start")),
            divisions=e.get("divisions", []) or [],
        )

    def get_matches(self, re_event_id: int, division_id: int | None = None) -> list[REMatch]:
        """All matches of an event (or of one division), in play order."""
        with self._client() as client:
            if division_id:
                division_ids = [division_id]
            else:
                resp = _get(client, f"/events/{re_event_id}", {})
                _check(resp)
                divisions = resp.json().get("divisions") or []
                division_ids = [d["id"] for d in divisions if d.get("id") is not None]
            rows: list[dict] = []
            for div in division_ids:
                rows.extend(
                    self._get_all(client, f"/events/{re_event_id}/divisions/{div}/matches", {})
                )
        matches = [self._match_from_row(r) for r in rows]
        matches.sort(key=_play_order)
        return matches

    def _match_from_row(self, r: dict) -> REMatch:
        alliances: list[REAlliance] = []
        for a in r.get("alliances", []):
            teams = [
                RETeamRef(
                    number=(t.get("team") or {}).get("name", "").strip(),
                    re_team_id=(t.get("team") or {}).get("id"),
                )
                for t in a.get("teams", [])
                if (t.get("team") or {}).get("name")
            ]
            alliances.append(
                REAlliance(
                    color=a.get("color", ""),
                    score=a.get("score"),
                    teams=teams,
                )
            )
        return REMatch(
            re_match_id=r["id"],
            name=r.get("name", ""),
            round=_ROUND_MAP.get(r.get("round"), "unknown"),
            match_num=r.get("matchnum"),
            instance=r.get("instance"),
            scheduled_at=_parse_dt(r.get("scheduled")),
            started_at=_parse_dt(r.get("started")),
            division_id=(r.get("division") or {}).get("id"),
            alliances=alliances,
        )
