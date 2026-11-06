import httpx
import respx

import pytest

from app.services.vexevents import VexEventsClient, VexEventsError, extract_sku

BASE = "https://ve.test/api/v2"


def _client():
    return VexEventsClient(token="tok", base_url=BASE)


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
    respx.get(f"{BASE}/events/55123").mock(
        return_value=httpx.Response(200, json={"id": 55123, "divisions": [{"id": 1, "name": "Div"}]})
    )
    respx.get(f"{BASE}/events/55123/divisions/1/matches").mock(
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


def test_default_base_url_is_vex_events():
    assert VexEventsClient(token="t").base_url == "https://events.vex.com/api/v2"


@pytest.mark.parametrize(
    "text, sku",
    [
        ("RE-V5RC-26-4058", "RE-V5RC-26-4058"),
        ("  re-v5rc-26-4058 ", "RE-V5RC-26-4058"),
        ("https://events.vex.com/robot-competitions/vex-robotics-competition/RE-V5RC-26-4058.html",
         "RE-V5RC-26-4058"),
        ("https://events.vex.com/de/RE-V5RC-26-4646.html", "RE-V5RC-26-4646"),
        ("https://events.vex.com/robot-competitions/college-competition/RE-VURC-25-1234.html#tab-results",
         "RE-VURC-25-1234"),
        ("https://www.robotevents.com/robot-competitions/vex-robotics-competition/RE-VRC-24-1234.html",
         "RE-VRC-24-1234"),
        ("State Championship", None),
        ("", None),
    ],
)
def test_extract_sku(text, sku):
    assert extract_sku(text) == sku


@respx.mock
def test_find_event_accepts_event_link():
    route = respx.get(f"{BASE}/events").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": 7, "sku": "RE-V5RC-26-4058", "name": "Yixing"}],
                  "meta": {"current_page": 1, "last_page": 1}},
        )
    )
    ev = _client().find_event(
        "https://events.vex.com/robot-competitions/vex-robotics-competition/RE-V5RC-26-4058.html"
    )
    assert ev.re_event_id == 7
    assert route.calls.last.request.url.params["sku[]"] == "RE-V5RC-26-4058"


def test_find_event_rejects_text_without_code():
    with pytest.raises(VexEventsError, match="event code"):
        _client().find_event("some event name")


@pytest.mark.parametrize("status, needle", [(401, "401"), (403, "403"), (429, "429"), (500, "500")])
@respx.mock
def test_http_errors_become_vexevents_errors(status, needle):
    respx.get(f"{BASE}/events").mock(return_value=httpx.Response(status))
    with pytest.raises(VexEventsError, match=needle):
        _client().find_event("RE-V5RC-26-4058")


@respx.mock
def test_network_failure_becomes_vexevents_error():
    respx.get(f"{BASE}/events").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(VexEventsError, match="reach VEX Events"):
        _client().find_event("RE-V5RC-26-4058")


@pytest.mark.parametrize("env_name", ["SCOUTHUB_VEXEVENTS_TOKEN", "VEX_EVENTS_API_KEY"])
def test_api_key_env_var_names(monkeypatch, env_name):
    from app.config import Settings

    monkeypatch.delenv("SCOUTHUB_VEXEVENTS_TOKEN", raising=False)
    monkeypatch.delenv("VEX_EVENTS_API_KEY", raising=False)
    monkeypatch.setenv(env_name, "abc123")
    assert Settings().vexevents_token == "abc123"


@pytest.mark.parametrize(
    "raw", ["abc123", "  abc123\n", '"abc123"', "Bearer abc123", "bearer  abc123 "]
)
def test_token_is_normalized(raw):
    from app.services.vexevents import normalize_token

    assert normalize_token(raw) == "abc123"


@respx.mock
def test_bearer_header_sent():
    route = respx.get(f"{BASE}/seasons").mock(return_value=httpx.Response(200, json={"data": []}))
    VexEventsClient(token="Bearer abc123", base_url=BASE).ping()
    assert route.calls.last.request.headers["Authorization"] == "Bearer abc123"


def _m(mid, rnd, num, sched=None, inst=1, div=1):
    return {"id": mid, "name": f"M{mid}", "round": rnd, "matchnum": num, "instance": inst,
            "scheduled": sched, "started": None, "division": {"id": div, "name": "D"},
            "alliances": []}


@pytest.mark.parametrize(
    "round_id, expected",
    [(1, "practice"), (2, "qual"), (3, "qf"), (4, "sf"), (5, "final"), (6, "r16"),
     (15, "final"), (16, "unknown"), (99, "unknown")],
)
def test_round_ids_follow_spec(round_id, expected):
    assert _client()._match_from_row(_m(1, round_id, 1)).round == expected


@respx.mock
def test_get_matches_walks_every_division_and_orders_unscheduled_elims_last():
    """Per the spec matches only exist under /events/{id}/divisions/{div}/matches."""
    respx.get(f"{BASE}/events/7").mock(return_value=httpx.Response(200, json={
        "id": 7, "divisions": [{"id": 1, "name": "Science"}, {"id": 2, "name": "Tech"}]}))
    d1 = respx.get(f"{BASE}/events/7/divisions/1/matches").mock(return_value=httpx.Response(200, json={
        "data": [_m(11, 5, 1), _m(12, 2, 1, "2026-02-14T09:00:00-05:00"), _m(13, 4, 1)],
        "meta": {"current_page": 1, "last_page": 1}}))
    d2 = respx.get(f"{BASE}/events/7/divisions/2/matches").mock(return_value=httpx.Response(200, json={
        "data": [_m(21, 2, 1, "2026-02-14T09:05:00-05:00", div=2)],
        "meta": {"current_page": 1, "last_page": 1}}))
    matches = _client().get_matches(7)
    assert d1.called and d2.called
    assert d1.calls.last.request.url.params["per_page"] == "250"
    assert [m.re_match_id for m in matches] == [12, 21, 13, 11]  # quals by time, then SF, F


@respx.mock
def test_get_matches_single_division_skips_event_lookup():
    ev = respx.get(f"{BASE}/events/7").mock(return_value=httpx.Response(500))
    respx.get(f"{BASE}/events/7/divisions/2/matches").mock(return_value=httpx.Response(200, json={
        "data": [_m(21, 2, 1)], "meta": {"current_page": 1, "last_page": 1}}))
    assert [m.re_match_id for m in _client().get_matches(7, division_id=2)] == [21]
    assert not ev.called


@respx.mock
def test_pagination_follows_last_page():
    route = respx.get(f"{BASE}/events/7/divisions/1/matches")
    route.side_effect = [
        httpx.Response(200, json={"data": [_m(1, 2, 1)], "meta": {"current_page": 1, "last_page": 2}}),
        httpx.Response(200, json={"data": [_m(2, 2, 2)], "meta": {"current_page": 2, "last_page": 2}}),
    ]
    assert len(_client().get_matches(7, division_id=1)) == 2
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2"]
