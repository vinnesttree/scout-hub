"""Thin client for the RobotEvents API v2 (https://www.robotevents.com/api/v2).

Auth is a personal bearer token from a robotevents.com account (Account -> API).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import httpx

from app.config import get_settings

_ROUND_MAP = {
    1: "practice",
    2: "qual",
    3: "r16",
    4: "qf",
    5: "sf",
    6: "final",
    # some seasons expose these ids
    7: "r16",
    15: "final",
}


class RobotEventsError(RuntimeError):
    pass


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


class RobotEventsClient:
    def __init__(self, token: str | None = None, base_url: str | None = None) -> None:
        cfg = get_settings()
        self.token = (token or cfg.robotevents_token or "").strip()
        self.base_url = (base_url or cfg.robotevents_base_url).rstrip("/")

    def _client(self) -> httpx.Client:
        if not self.token:
            raise RobotEventsError("No RobotEvents API token configured (Settings page).")
        return httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
            timeout=30,
        )

    def _get_all(self, client: httpx.Client, path: str, params: dict) -> list[dict]:
        """Follow RobotEvents' page-based pagination."""
        results: list[dict] = []
        page = 1
        while True:
            q = {**params, "page": page, "per_page": 250}
            resp = client.get(path, params=q)
            if resp.status_code == 401:
                raise RobotEventsError("RobotEvents rejected the API token (401).")
            if resp.status_code == 429:
                raise RobotEventsError("RobotEvents rate limit hit (429). Try again shortly.")
            resp.raise_for_status()
            body = resp.json()
            results.extend(body.get("data", []))
            meta = body.get("meta", {})
            if not meta or meta.get("current_page", page) >= meta.get("last_page", page):
                break
            page += 1
        return results

    # -- public API --------------------------------------------------

    def find_event(self, sku: str) -> REEvent:
        sku = sku.strip()
        with self._client() as client:
            rows = self._get_all(client, "/events", {"sku[]": sku})
        if not rows:
            raise RobotEventsError(f"No event found for SKU {sku!r}.")
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
        params: dict = {}
        if division_id:
            params["division[]"] = division_id
        with self._client() as client:
            rows = self._get_all(client, f"/events/{re_event_id}/matches", params)
        matches = [self._match_from_row(r) for r in rows]
        matches.sort(key=lambda m: (m.scheduled_at or datetime.max, m.round, m.instance or 0,
                                    m.match_num or 0))
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
