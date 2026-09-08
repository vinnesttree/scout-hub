"""Small shared persistence helpers used by both the API and the job pipeline."""

from __future__ import annotations

from sqlmodel import Session, select

from app.models import Alliance, Match, MatchTeam, Team


def upsert_team(session: Session, number: str, *, name: str = "", re_team_id: int | None = None) -> Team:
    number = number.strip().upper()
    team = session.exec(select(Team).where(Team.number == number)).first()
    if team is None:
        team = Team(number=number, name=name, re_team_id=re_team_id)
        session.add(team)
        session.flush()
        return team
    changed = False
    if name and team.name != name:
        team.name, changed = name, True
    if re_team_id and team.re_team_id != re_team_id:
        team.re_team_id, changed = re_team_id, True
    if changed:
        session.add(team)
    return team


def set_match_teams(
    session: Session,
    match: Match,
    *,
    red: list[str],
    blue: list[str],
) -> None:
    """Replace the MatchTeam rows for ``match`` with the given alliance rosters."""
    for link in list(match.teams):
        session.delete(link)
    session.flush()
    for alliance, numbers in ((Alliance.red, red), (Alliance.blue, blue)):
        for station, number in enumerate(numbers, start=1):
            if not number:
                continue
            team = upsert_team(session, number)
            session.add(
                MatchTeam(
                    match_id=match.id,
                    team_id=team.id,
                    alliance=alliance,
                    station=station,
                )
            )
    session.flush()


def match_team_numbers(session: Session, match: Match) -> tuple[list[str], list[str]]:
    links = session.exec(select(MatchTeam).where(MatchTeam.match_id == match.id)).all()
    red, blue = [], []
    for link in sorted(links, key=lambda x: x.station):
        team = session.get(Team, link.team_id)
        if not team:
            continue
        (red if link.alliance == Alliance.red else blue).append(team.number)
    return red, blue
