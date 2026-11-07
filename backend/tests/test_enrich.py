"""Linking provisional (OCR-estimated) matches to the VEX Events schedule."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import select

from app.services.enrich import Evidence, pick_schedule_match, score_candidate
from app.services.vexevents import REAlliance, REMatch, RETeamRef

T0 = datetime(2026, 2, 14, 14, 0, tzinfo=timezone.utc)


def _rm(i, *, red=None, blue=None, round="qual", num=None, div=1, started=None,
        red_score=None, blue_score=None):
    return REMatch(
        re_match_id=div * 1000 + i,
        name=f"Qualifier #{num or i}",
        round=round,
        match_num=num or i,
        instance=1,
        scheduled_at=None,
        started_at=started,
        division_id=div,
        alliances=[
            REAlliance("red", red_score, [RETeamRef(t) for t in (red or [f"{i}1A", f"{i}2A"])]),
            REAlliance("blue", blue_score, [RETeamRef(t) for t in (blue or [f"{i}3B", f"{i}4B"])]),
        ],
    )


SCHED = [_rm(i, started=T0 + timedelta(minutes=4 * i)) for i in range(1, 9)]


def test_label_alone_identifies_the_match():
    assert pick_schedule_match(Evidence(round="qual", match_num=5), SCHED).re_match_id == 1005


def test_two_ocr_teams_identify_the_match():
    ev = Evidence(red=["31A", "32A"])
    assert pick_schedule_match(ev, SCHED).re_match_id == 1003


def test_one_team_is_not_enough():
    assert pick_schedule_match(Evidence(red=["31A"]), SCHED) is None


def test_alliances_read_the_wrong_way_round_still_count():
    ev = Evidence(red=["63B", "64B"], blue=["61A"])
    assert pick_schedule_match(ev, SCHED).re_match_id == 1006


def test_time_alone_with_latency_offset():
    # on stream 25 s after VEX Events' start time; offset learned from earlier matches
    ev = Evidence(wall_start=T0 + timedelta(minutes=16, seconds=25))
    assert pick_schedule_match(ev, SCHED, offset_s=25).re_match_id == 1004


def test_same_label_in_two_divisions_is_ambiguous_until_teams_break_the_tie():
    sched = [_rm(7, div=1, red=["1A", "2A"]), _rm(7, div=2, red=["9X", "8X"])]
    assert pick_schedule_match(Evidence(round="qual", match_num=7), sched) is None
    ev = Evidence(round="qual", match_num=7, red=["9X", "8X"])
    assert pick_schedule_match(ev, sched).re_match_id == 2007


def test_claimed_matches_are_skipped():
    ev = Evidence(round="qual", match_num=5)
    assert pick_schedule_match(ev, SCHED, claimed={1005}) is None


def test_far_off_time_counts_against():
    ev = Evidence(red=["21A", "22A"], wall_start=T0 + timedelta(hours=3))
    near = score_candidate(Evidence(red=["21A", "22A"]), SCHED[1])
    assert score_candidate(ev, SCHED[1]) < near


# --------------------------------------------------------------------------


def _setup(tmp_path, *, teams=(["21A", "99Z"], ["23B"]), label=(None, None)):
    """A recording linked to an event, with one provisional live-split match + clip."""
    from app.crud import set_match_teams
    from app.db import init_db, session_scope
    from app.models import (DetectionMethod, Event, Match, MatchRound, MatchStatus,
                            Recording, RecordingStatus)
    from app.services.enrich import NOTE_PROVISIONAL

    init_db()
    archive = tmp_path / "evt"
    (archive / "matches").mkdir(parents=True)
    clip = archive / "matches" / "Match_1__21A-99Z_vs_23B.mp4"
    clip.write_bytes(b"clip")
    clip.with_suffix(".jpg").write_bytes(b"jpg")
    with session_scope() as s:
        ev = Event(sku=f"RE-TEST-26-{tmp_path.name[-6:]}", re_event_id=55123, name="Test")
        s.add(ev)
        s.flush()
        rec = Recording(source_url="x", archive_dir=str(archive), event_id=ev.id,
                        status=RecordingStatus.recording, video_started_at=T0)
        s.add(rec)
        s.flush()
        m = Match(recording_id=rec.id, event_id=ev.id, name="Match 1",
                  round=MatchRound(label[0]) if label[0] else MatchRound.unknown,
                  match_num=label[1], video_start_s=8 * 60 + 20, video_end_s=8 * 60 + 140,
                  red_score=40, blue_score=41, detection_method=DetectionMethod.ocr,
                  status=MatchStatus.clipped, provisional=True, review_note=NOTE_PROVISIONAL,
                  clip_path=str(clip), thumb_path=str(clip.with_suffix(".jpg")))
        s.add(m)
        s.flush()
        set_match_teams(s, m, red=list(teams[0]), blue=list(teams[1]))
        return rec.id, m.id


def test_provisional_match_gets_api_teams_now_and_score_later(tmp_path):
    from app.crud import match_team_numbers
    from app.db import session_scope
    from app.models import Match
    from app.services.enrich import NOTE_AWAITING_SCORE, refresh_recording

    rec_id, match_id = _setup(tmp_path)

    # schedule is out (teams), match not scored yet
    unscored = [_rm(i) for i in range(1, 5)]
    assert refresh_recording(rec_id, schedule=unscored).startswith("1 linked")
    with session_scope() as s:
        m = s.get(Match, match_id)
        assert m.re_match_id == 1002 and m.name == "Qualifier #2"
        assert m.detection_method.value == "api"
        assert match_team_numbers(s, m) == (["21A", "22A"], ["23B", "24B"])
        assert (m.red_score, m.blue_score) == (40, 41)   # OCR estimate kept for now
        assert m.provisional is True and m.review_note == NOTE_AWAITING_SCORE
        # clip renamed to the real teams
        assert Path(m.clip_path).name == "Qualifier_2__21A-22A_vs_23B-24B.mp4"
        assert Path(m.clip_path).exists() and Path(m.thumb_path).exists()

    # VEX Events posts the score
    scored = [_rm(i, red_score=60 + i, blue_score=50) for i in range(1, 5)]
    assert refresh_recording(rec_id, schedule=scored) == "0 linked, 1 updated from VEX Events"
    with session_scope() as s:
        m = s.get(Match, match_id)
        assert (m.red_score, m.blue_score) == (62, 50)
        assert m.provisional is False and m.review_note == ""

    # nothing left to do: a further refresh is a no-op
    assert refresh_recording(rec_id, schedule=scored) == "0 linked, 0 updated from VEX Events"


def test_manual_edits_are_never_overwritten(tmp_path):
    from app.db import session_scope
    from app.models import DetectionMethod, Match
    from app.services.enrich import refresh_recording

    rec_id, match_id = _setup(tmp_path)
    with session_scope() as s:
        m = s.get(Match, match_id)
        m.detection_method = DetectionMethod.manual
        s.add(m)
    refresh_recording(rec_id, schedule=[_rm(i) for i in range(1, 5)])
    with session_scope() as s:
        assert s.get(Match, match_id).re_match_id is None


def test_unidentifiable_match_stays_provisional_then_falls_back_to_order(tmp_path):
    from app.db import session_scope
    from app.models import Match, Recording
    from app.services.enrich import NOTE_BY_POSITION, NOTE_PROVISIONAL, refresh_recording

    rec_id, match_id = _setup(tmp_path, teams=([], []))
    with session_scope() as s:   # no usable clock either
        rec = s.get(Recording, rec_id)
        rec.video_started_at = None
        s.add(rec)
    sched = [_rm(i) for i in range(1, 5)]
    refresh_recording(rec_id, schedule=sched)
    with session_scope() as s:
        m = s.get(Match, match_id)
        assert m.re_match_id is None and m.provisional and m.review_note == NOTE_PROVISIONAL

    refresh_recording(rec_id, schedule=sched, final=True)
    with session_scope() as s:
        m = s.get(Match, match_id)
        assert m.re_match_id == 1001
        assert m.review_note == NOTE_BY_POSITION


def test_label_read_off_the_overlay_links_the_match(tmp_path):
    from app.db import session_scope
    from app.models import Match
    from app.services.enrich import refresh_recording

    rec_id, match_id = _setup(tmp_path, teams=([], []), label=("qual", 3))
    with session_scope() as s:
        rec = s.get(Match, match_id).recording
        rec.video_started_at = None
        s.add(rec)
    refresh_recording(rec_id, schedule=[_rm(i) for i in range(1, 5)])
    with session_scope() as s:
        assert s.get(Match, match_id).re_match_id == 1003


def test_unscored_api_matches_have_no_score():
    import httpx
    import respx

    from app.services.vexevents import VexEventsClient

    base = "https://ve.test/api/v2"
    row = {"id": 1, "name": "Q1", "round": 2, "matchnum": 1, "instance": 1, "scored": False,
           "division": {"id": 1},
           "alliances": [{"color": "red", "score": 0, "teams": [{"team": {"id": 1, "name": "1A"}}]},
                         {"color": "blue", "score": 0, "teams": [{"team": {"id": 2, "name": "2B"}}]}]}
    with respx.mock:
        respx.get(f"{base}/events/7/divisions/1/matches").mock(
            return_value=httpx.Response(200, json={"data": [row], "meta": {}}))
        m = VexEventsClient(token="t", base_url=base).get_matches(7, division_id=1)[0]
    assert m.alliance("red").score is None and m.alliance("blue").score is None
    assert [t.number for t in m.alliance("red").teams] == ["1A"]


def test_refresh_endpoint(tmp_path):
    from fastapi.testclient import TestClient

    from app.db import session_scope
    from app.main import app
    from app.models import Recording, RecordingStatus
    from conftest import sign_in

    rec_id, _ = _setup(tmp_path)
    with session_scope() as s:
        plain = Recording(source_url="x", archive_dir=str(tmp_path), status=RecordingStatus.completed)
        s.add(plain)
        rec = s.get(Recording, rec_id)
        rec.status = RecordingStatus.completed
        s.add(rec)
        s.flush()
        plain_id = plain.id
    with TestClient(app) as client:
        assert client.post(f"/api/recordings/{rec_id}/refresh-api").status_code == 401
        sign_in(client)
        assert client.post(f"/api/recordings/{plain_id}/refresh-api").status_code == 409
        r = client.post(f"/api/recordings/{rec_id}/refresh-api")
        assert r.status_code == 200 and "job_id" in r.json()
        out = client.get(f"/api/recordings/{rec_id}").json()
        assert "split_status" in out and "analyzed_s" in out
        m = client.get("/api/matches", params={"recording_id": rec_id}).json()
        assert m[0]["provisional"] is True
