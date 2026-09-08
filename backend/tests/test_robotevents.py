import httpx
import respx

from app.services.robotevents import RobotEventsClient

BASE = "https://re.test/api/v2"


def _client():
    return RobotEventsClient(token="tok", base_url=BASE)


@respx.mock
def test_find_event_parses_fields():
    respx.get(f"{BASE}/events").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": 55123,
                        "sku": "RE-VRC-24-1234",
                        "name": "State Championship",
                        "start": "2026-02-14T00:00:00-05:00",
                        "season": {"id": 190, "name": "2025-2026: Push Back"},
                        "program": {"id": 1, "code": "VRC"},
                        "divisions": [{"id": 1, "name": "Science"}],
                    }
                ],
                "meta": {"current_page": 1, "last_page": 1},
            },
        )
    )
    ev = _client().find_event("RE-VRC-24-1234")
    assert ev.re_event_id == 55123
    assert ev.program == "VRC"
    assert ev.season.startswith("2025-2026")
    assert ev.start_date.year == 2026


@respx.mock
def test_get_matches_extracts_alliances_and_scores():
    respx.get(f"{BASE}/events/55123/matches").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": 900002,
                        "name": "Qualifier #2",
                        "round": 2,
                        "matchnum": 2,
                        "instance": 1,
                        "scheduled": "2026-02-14T09:10:00-05:00",
                        "started": None,
                        "division": {"id": 1},
                        "alliances": [
                            {"color": "red", "score": 88,
                             "teams": [{"team": {"id": 1, "name": "1234A"}},
                                       {"team": {"id": 2, "name": "5678B"}}]},
                            {"color": "blue", "score": 74,
                             "teams": [{"team": {"id": 3, "name": "9999Z"}},
                                       {"team": {"id": 4, "name": "4242C"}}]},
                        ],
                    },
                    {
                        "id": 900001,
                        "name": "Qualifier #1",
                        "round": 2,
                        "matchnum": 1,
                        "instance": 1,
                        "scheduled": "2026-02-14T09:00:00-05:00",
                        "started": None,
                        "division": {"id": 1},
                        "alliances": [
                            {"color": "red", "score": 10, "teams": [{"team": {"id": 5, "name": "1A"}}]},
                            {"color": "blue", "score": 20, "teams": [{"team": {"id": 6, "name": "2B"}}]},
                        ],
                    },
                ],
                "meta": {"current_page": 1, "last_page": 1},
            },
        )
    )
    matches = _client().get_matches(55123)
    assert [m.match_num for m in matches] == [1, 2]  # sorted by scheduled time
    q2 = matches[1]
    assert q2.alliance("red").score == 88
    assert [t.number for t in q2.alliance("red").teams] == ["1234A", "5678B"]
    assert q2.round == "qual"
