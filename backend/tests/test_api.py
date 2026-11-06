"""End-to-end-ish API tests that don't need ffmpeg or a network."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_settings_roundtrip_masks_secret(client):
    r = client.put("/api/settings", json={"vexevents_token": "supersecrettoken", "sample_fps": 2.0})
    body = r.json()
    assert body["sample_fps"] == 2.0
    assert body["vexevents_token"] == "••••••••"
    assert body["vexevents_token_set"] is True
    assert "supersecrettoken" not in str(body)


def test_folder_picker_lists_and_creates(client, archive_root: Path):
    (archive_root / "Worlds2026").mkdir(exist_ok=True)
    r = client.get("/api/fs/dirs")
    assert r.status_code == 200
    names = [e["name"] for e in r.json()["entries"]]
    assert "Worlds2026" in names

    r = client.post("/api/fs/dirs", json={"path": str(archive_root / "Worlds2026"), "name": "Div1"})
    assert r.status_code == 200
    assert "Div1" in [e["name"] for e in r.json()["entries"]]


def test_folder_picker_rejects_escape(client):
    r = client.get("/api/fs/dirs", params={"path": "/etc"})
    assert r.status_code == 400


def test_manual_match_flow_sort_and_filter_by_team(client, archive_root: Path):
    # a completed recording with no real file (we won't re-clip in this test)
    from app.db import session_scope
    from app.models import Recording, RecordingStatus

    with session_scope() as s:
        rec = Recording(
            source_url="https://youtu.be/x",
            archive_dir=str(archive_root),
            status=RecordingStatus.completed,
        )
        s.add(rec)
        s.flush()
        rec_id = rec.id

    m1 = client.post("/api/matches", json={
        "recording_id": rec_id, "name": "Qual 1", "round": "qual", "match_num": 1,
        "video_start_s": 10, "video_end_s": 120,
        "red_teams": ["1234A", "5678B"], "blue_teams": ["9999Z", "4242C"],
        "red_score": 50, "blue_score": 88,
    }).json()
    m2 = client.post("/api/matches", json={
        "recording_id": rec_id, "name": "Qual 2", "round": "qual", "match_num": 2,
        "video_start_s": 200, "video_end_s": 320,
        "red_teams": ["1234A", "0000X"], "blue_teams": ["1111Y", "2222W"],
        "red_score": 100, "blue_score": 20,
    }).json()

    assert m1["winner"] == "blue"
    assert m2["winner"] == "red"

    # filter by a team that plays both
    rows = client.get("/api/matches", params={"team": "1234A"}).json()
    assert {r["id"] for r in rows} == {m1["id"], m2["id"]}

    # filter by a team in only one
    rows = client.get("/api/matches", params={"team": "9999Z"}).json()
    assert [r["id"] for r in rows] == [m1["id"]]

    # sort by red_score desc
    rows = client.get("/api/matches", params={"sort": "red_score", "order": "desc"}).json()
    ids = [r["id"] for r in rows if r["id"] in (m1["id"], m2["id"])]
    assert ids == [m2["id"], m1["id"]]

    # patch a score
    patched = client.patch(f"/api/matches/{m1['id']}", json={"red_score": 95, "reclip": False}).json()
    assert patched["red_score"] == 95 and patched["winner"] == "red"


def test_legacy_robotevents_token_is_migrated(client):
    from app.db import session_scope
    from app.models import AppSetting
    from app.services import settings_store

    with session_scope() as s:
        for key in ("vexevents_token", "robotevents_token"):
            row = s.get(AppSetting, key)
            if row:
                s.delete(row)
        s.add(AppSetting(key="robotevents_token", value="oldtoken"))
    with session_scope() as s:
        assert settings_store.get(s, "vexevents_token") == "oldtoken"
    assert client.get("/api/settings").json()["vexevents_token_set"] is True


def test_create_recording_with_event_link_only(client, archive_root: Path, monkeypatch):
    """Link + event link is enough: the event is imported, the folder defaulted."""
    from app.api import recordings as rec_api
    from app.services.vexevents import REEvent, VexEventsClient

    calls = []

    def fake_find(self, q):
        calls.append(q)
        return REEvent(re_event_id=4242, sku="RE-V5RC-26-4058", name="Spring Fling: HS/MS",
                       season="2026-2027", program="V5RC", start_date=None, divisions=[])

    monkeypatch.setattr(VexEventsClient, "find_event", fake_find)
    started = []
    monkeypatch.setattr(rec_api.recorder_manager, "start", lambda rid: started.append(rid))

    link = "https://events.vex.com/robot-competitions/vex-robotics-competition/RE-V5RC-26-4058.html"
    r = client.post("/api/recordings", json={
        "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "event": link,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["event_id"] is not None
    assert body["archive_dir"] == str((archive_root / "Spring Fling HS MS (RE-V5RC-26-4058)").resolve())
    assert started == [body["id"]]
    ev = next(e for e in client.get("/api/events").json() if e["id"] == body["event_id"])
    assert ev["sku"] == "RE-V5RC-26-4058" and ev["re_event_id"] == 4242

    # Second recording for the same event reuses the stored event (no API call).
    r2 = client.post("/api/recordings", json={
        "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "event": "RE-V5RC-26-4058",
    })
    assert r2.status_code == 201 and r2.json()["event_id"] == body["event_id"]
    assert len(calls) == 1


def test_create_recording_link_only_without_event(client, archive_root: Path, monkeypatch):
    from app.api import recordings as rec_api

    monkeypatch.setattr(rec_api.recorder_manager, "start", lambda rid: None)
    r = client.post("/api/recordings", json={"source_url": "https://www.twitch.tv/somechannel"})
    assert r.status_code == 201, r.text
    assert r.json()["archive_dir"] == str((archive_root / "recordings").resolve())
    assert r.json()["event_id"] is None


def test_create_recording_bad_event_is_rejected(client, monkeypatch):
    from app.api import recordings as rec_api

    monkeypatch.setattr(rec_api.recorder_manager, "start", lambda rid: None)
    r = client.post("/api/recordings", json={
        "source_url": "https://www.twitch.tv/somechannel", "event": "not an event",
    })
    assert r.status_code == 400
    assert "event code" in r.json()["detail"]
