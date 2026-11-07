"""Trainable team-intro (key frame) detection."""

import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

from app.services import keyframes, match_detect
from app.services.match_detect import TimerSample

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _samples(spec, step=1.0, end=None):
    """spec: list of (t0, t1, kind) with kind in {"intro", "timer"}."""
    end = end if end is not None else max(b for _, b, _ in spec) + 20
    out = []
    t = 0.0
    while t <= end:
        kind = next((k for a, b, k in spec if a <= t < b), None)
        if kind == "intro":
            out.append(TimerSample(t, None, intro=True))
        elif kind == "timer":
            out.append(TimerSample(t, max(0, 120 - int(t % 200)), overlay=False))
        else:
            out.append(TimerSample(t, None))
        t += step
    return out


# --- window assembly ----------------------------------------------------

def test_intro_runs_groups_nearby_frames():
    s = _samples([(10, 16, "intro"), (200, 204, "intro")])
    assert match_detect.intro_runs(s) == [(10.0, 15.0, 6), (200.0, 203.0, 4)]


def test_windows_from_intros_start_after_intro_and_stop_at_next():
    s = _samples([(10, 16, "intro"), (100, 106, "intro")], end=400)
    ws = match_detect.assemble_windows_from_intros(s, match_len_s=135)
    assert [(w.start_s, w.end_s) for w in ws] == [(15.0, 99.0), (105.0, 240.0)]
    assert ws[0].intro_t == 12.5


def test_windows_from_intros_ignore_single_frame_hits_at_dense_rates():
    s = _samples([(10, 11, "intro"), (100, 106, "intro")], end=400)
    ws = match_detect.assemble_windows_from_intros(s)
    assert [w.start_s for w in ws] == [105.0]
    # ...but at a sparse rate one frame is all an intro gets
    sparse = _samples([(10, 11, "intro")], step=5.0, end=300)
    assert len(match_detect.assemble_windows_from_intros(sparse)) == 1


def test_choose_windows_falls_back_to_intros_only_without_timer_or_label():
    intro_only = _samples([(10, 16, "intro"), (200, 206, "intro")], end=500)
    ws = match_detect._choose_windows(intro_only)
    assert [w.start_s for w in ws] == [15.0, 205.0]

    # with a working clock the timer windows win, tagged with their intro
    both = _samples([(10, 16, "intro"), (20, 140, "timer")], end=200)
    ws = match_detect._choose_windows(both)
    assert len(ws) == 1 and ws[0].start_s == 20.0
    assert ws[0].intro_t == 12.5


def test_intro_is_not_attached_when_too_far_before():
    s = _samples([(10, 16, "intro"), (200, 320, "timer")], end=360)
    ws = match_detect._choose_windows(s)
    assert len(ws) == 1 and ws[0].intro_t is None


# --- descriptor + model -------------------------------------------------

def _card(seed: int, intro: bool):
    """A synthetic intro card (fixed layout, varying text) or a gameplay frame."""
    sys.path.insert(0, str(SCRIPTS))
    import random  # noqa: PLC0415

    import cv2  # noqa: PLC0415
    import make_sample_video as msv  # noqa: PLC0415

    m = dict(msv.MATCHES[seed % len(msv.MATCHES)])
    if intro:
        m["red"] = (f"{1000 + seed * 37}A", f"{2000 + seed * 53}B")
        img = msv.frame_intro(m)
    else:
        img = msv.frame_match(m, 100 - seed, show_score=False, rng=random.Random(seed))
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


def test_features_are_unit_length_and_separate_intros_from_gameplay():
    a, b = keyframes.frame_features(_card(1, True)), keyframes.frame_features(_card(2, True))
    g = keyframes.frame_features(_card(3, False))
    assert abs(float(np.linalg.norm(a)) - 1) < 1e-4
    assert float(a @ b) > 0.9 > float(a @ g)
    assert keyframes.alliance_colors(_card(1, True)) == 1.0


def test_run_bounds_follows_the_same_card():
    feats = np.stack([keyframes.frame_features(_card(i, i in (2, 3, 4))) for i in range(7)])
    times = np.arange(7, dtype=np.float32)
    assert keyframes.run_bounds(times, feats, 3) == (2, 4)


def test_train_needs_enough_confirmed_examples():
    f = keyframes.frame_features(_card(1, True))[None]
    ex = [keyframes.Example(1, f, np.zeros((0, f.shape[1]), np.float32))]
    with pytest.raises(ValueError, match="at least"):
        keyframes.train(ex, [])


def test_train_learns_intro_cards():
    examples = []
    for g in range(6):
        pos = np.stack([keyframes.frame_features(_card(g * 10 + j, True)) for j in range(3)])
        neg = np.stack([keyframes.frame_features(_card(g * 10 + j, False)) for j in range(4)])
        examples.append(keyframes.Example(g, pos, neg))
    model = keyframes.train(examples, [])
    assert model.meta["cv"]["intros_found"] == 6
    assert model.meta["cv"]["f1"] >= 0.9
    # unseen frames
    assert model.is_intro(_card(97, True))
    assert not model.is_intro(_card(98, False))
    assert not model.is_intro(np.zeros((360, 640, 3), np.uint8))


# --- the whole loop on a rendered broadcast ------------------------------

needs_tools = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("tesseract") is None,
    reason="ffmpeg and tesseract needed",
)


@pytest.fixture(scope="module")
def broadcast(tmp_path_factory):
    sys.path.insert(0, str(SCRIPTS))
    import make_sample_video as msv  # noqa: PLC0415

    old = msv.FPS
    msv.FPS = 2  # fewer frames to render; one timer value per second still
    try:
        out = tmp_path_factory.mktemp("kf") / "broadcast.mp4"
        truth = msv.build(out, 3, match_length=30, gap_s=12)
    finally:
        msv.FPS = old
    return out, truth


@needs_tools
def test_find_review_train_via_api(broadcast):
    from fastapi.testclient import TestClient
    from sqlmodel import select

    from app.db import init_db, session_scope
    from app.main import app
    from app.models import KeyFrame, Match, Recording, RecordingStatus
    from conftest import sign_in

    video, truth = broadcast
    init_db()
    with session_scope() as s:
        rec = Recording(source_url="https://youtu.be/x", archive_dir=str(video.parent),
                        file_path=str(video), status=RecordingStatus.completed)
        s.add(rec)
        s.flush()
        rid = rec.id
        for g in truth:
            s.add(Match(recording_id=rid, name=g["name"], video_start_s=g["match_start"],
                        video_end_s=g["match_end"]))

    detail = keyframes.find_for_recording(rid)
    assert detail.startswith("3 key-frame candidate(s)")

    with TestClient(app) as client:
        sign_in(client)
        rows = client.get(f"/api/keyframes?recording_id={rid}").json()
        assert len(rows) == 3
        for row, g in zip(rows, truth):
            # the chosen frame is inside the real intro, never the match itself
            assert g["intro_start"] <= row["t"] < g["intro_end"], (row, g)
            assert row["run_start_s"] >= g["intro_start"] - 1
            assert row["run_end_s"] < g["intro_end"]
            assert row["status"] == "candidate"
            assert client.get(f"/api/keyframes/{row['id']}/image").headers[
                "content-type"] == "image/jpeg"
            # team numbers were read off the card (OCR may garble one of them)
            tokens = set(row["alternatives"][0]["tokens"])
            assert set(g["red"]) & tokens and set(g["blue"]) & tokens, (tokens, g)

        # nothing confirmed yet -> no training on unreviewed frames
        assert client.post("/api/keyframes/train").status_code == 400

        # pick an alternative frame (the one with the clock up), then reject it
        first = rows[0]
        timer_alt = next(a for a in first["alternatives"] if a["timer"] is not None)
        r = client.patch(f"/api/keyframes/{first['id']}", json={"t": timer_alt["t"]})
        assert r.status_code == 200 and r.json()["t"] == timer_alt["t"]
        assert client.patch(f"/api/keyframes/{first['id']}",
                            json={"t": 9999}).status_code == 400
        # ...and put it back on the intro and confirm all three
        client.patch(f"/api/keyframes/{first['id']}", json={"t": first["t"]})
        r = client.post("/api/keyframes/bulk",
                        json={"ids": [x["id"] for x in rows], "status": "confirmed"})
        assert r.json() == {"updated": 3}

        trained = client.post("/api/keyframes/train")
        assert trained.status_code == 200, trained.text
        info = trained.json()
        assert info["trained"] and info["confirmed"] == 3
        assert info["cv"]["intros_found"] == 3

        summary = client.get("/api/keyframes/summary").json()
        assert summary["totals"]["confirmed"] >= 3
        assert summary["model"]["trained"]

        # the trained model flags the intro frames during a normal scan
        import cv2  # noqa: PLC0415

        model = keyframes.load_model()

        hits = []
        for t, p in match_detect._sample_frames(video, 1.0, video.parent / "scan"):
            if model.is_intro(cv2.imread(str(p))):
                hits.append(t)
        for g in truth:
            assert any(g["intro_start"] <= t < g["intro_end"] for t in hits)
        assert all(any(g["intro_start"] - 1 <= t < g["intro_end"] + 1 for g in truth)
                   for t in hits), hits

        # deleting the recording's key frames removes rows + files
        assert client.delete(f"/api/keyframes?recording_id={rid}").status_code == 204
        with session_scope() as s:
            assert not s.exec(select(KeyFrame).where(KeyFrame.recording_id == rid)).all()
        assert not keyframes.recording_dir(rid).exists()

        assert client.delete("/api/keyframes/model").status_code == 204
        assert client.get("/api/keyframes/summary").json()["model"] == {"trained": False}


def test_overlay_profile_put_keeps_regions_not_sent(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app
    from conftest import sign_in

    with TestClient(app) as client:
        sign_in(client)
        prof = next(p for p in client.get("/api/overlay-profiles").json() if p["name"] == "vex_tm")
        assert prof["match_label_region"] == [0.2, 0.0, 0.34, 0.12]
        body = {k: v for k, v in prof.items()
                if k not in ("match_label_region", "intro_red_teams_region")}
        body["intro_blue_teams_region"] = [0.6, 0.2, 1.0, 0.9]
        out = client.put("/api/overlay-profiles/kf_test", json=body).json()
        assert out["intro_blue_teams_region"] == [0.6, 0.2, 1.0, 0.9]
        assert out["intro_red_teams_region"] == [0.0, 0.15, 0.5, 0.95]
        # a region left out of the body falls back to the default, not an error
        assert len(out["match_label_region"]) == 4


def test_rejecting_a_real_intro_is_flagged_as_a_conflict():
    feat = lambda seed, intro: keyframes.frame_features(_card(seed, intro))[None]  # noqa: E731
    D = feat(1, True).shape[1]
    empty = np.zeros((0, D), np.float32)
    examples = [
        keyframes.Example(1, feat(1, True), feat(2, False)),
        keyframes.Example(2, feat(3, True), empty),
        keyframes.Example(3, empty, feat(4, True)),    # a real intro, rejected by mistake
        keyframes.Example(4, empty, feat(5, False)),   # a correct rejection
    ]
    assert keyframes.conflicting_rejections(examples) == [3]
