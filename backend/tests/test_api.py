"""End-to-end-ish API tests that don't need ffmpeg or a network."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from conftest import sign_in


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        sign_in(c)
        yield c


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_settings_roundtrip_masks_secret(client):
    r = client.put("/api/settings", json={"robotevents_token": "supersecrettoken", "sample_fps": 2.0})
    body = r.json()
    assert body["sample_fps"] == 2.0
    assert body["robotevents_token"] == "••••••••"
    assert body["robotevents_token_set"] is True
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
