"""Exercises real ffmpeg frame-sampling + clipping + range streaming.

The per-frame timer OCR is stubbed (its accuracy depends on the real broadcast
overlay, not a synthetic fixture); everything downstream of it runs for real.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from sqlmodel import select

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def _make_video(path: Path, seconds: int, fps: int = 5) -> None:
    from PIL import Image, ImageDraw

    frames = path.parent / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    idx = 0
    for t in range(seconds):
        img = Image.new("RGB", (320, 180), (10, 10, 10))
        d = ImageDraw.Draw(img)
        # a bright block in the timer region so frames aren't identical
        if 5 <= t < seconds - 5:
            d.rectangle([140, 5, 180, 25], fill=(240, 240, 240))
            d.text((6, 150), "11111A 22222B  vs  33333C 44444D", fill=(200, 200, 200))
        for _ in range(fps):
            img.save(frames / f"{idx:06d}.jpg", quality=85)
            idx += 1
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", str(fps),
         "-i", str(frames / "%06d.jpg"), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    shutil.rmtree(frames, ignore_errors=True)


def test_detect_clip_and_stream(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.db import init_db, session_scope
    from app.main import app
    from app.models import Match, Recording, RecordingStatus
    from app.services import match_detect
    from app.services.overlay import load_profile

    video = tmp_path / "rec.mp4"
    _make_video(video, seconds=40)

    # Stub the OCR: report a counting-down timer for the "match" segment (t in [5, 35)).
    def fake_read_timer(frame_bgr, profile):
        h, w = frame_bgr.shape[:2]
        cx0, cx1 = int(0.44 * w), int(0.56 * w)
        cy0, cy1 = int(0.02 * h), int(0.14 * h)
        region = frame_bgr[cy0:cy1, cx0:cx1]
        return 90 if region.mean() > 120 else None

    monkeypatch.setattr(match_detect, "read_timer", fake_read_timer)

    windows = match_detect.detect_windows(video, load_profile("vex_default"), sample_fps=2.0)
    assert len(windows) == 1
    w = windows[0]
    assert w.start_s < 8 and w.end_s > 30

    # catalogue a match against a completed recording and clip it for real
    init_db()
    with session_scope() as s:
        rec = Recording(source_url="https://youtu.be/x", archive_dir=str(tmp_path),
                        file_path=str(video), status=RecordingStatus.completed)
        s.add(rec)
        s.flush()
        rec_id = rec.id

    with TestClient(app) as client:
        created = client.post("/api/matches", json={
            "recording_id": rec_id, "name": "Qual 1", "round": "qual", "match_num": 1,
            "video_start_s": w.start_s, "video_end_s": w.end_s,
            "red_teams": ["11111A", "22222B"], "blue_teams": ["33333C", "44444D"],
            "red_score": 50, "blue_score": 40,
        })
        assert created.status_code == 201, created.text
        match_id = created.json()["id"]
        assert created.json()["has_clip"] is True

        with session_scope() as s:
            m = s.get(Match, match_id)
            assert m.clip_path and Path(m.clip_path).exists()
            assert m.thumb_path and Path(m.thumb_path).exists()

        # range request
        r = client.get(f"/api/matches/{match_id}/clip", headers={"Range": "bytes=0-1023"})
        assert r.status_code == 206
        assert r.headers["Content-Range"].startswith("bytes 0-1023/")
        assert len(r.content) == 1024

        # thumbnail
        assert client.get(f"/api/matches/{match_id}/thumb").status_code == 200


def test_process_recording_ocr_only(tmp_path, monkeypatch):
    """The full job pipeline with no linked event: detect -> OCR -> clip -> catalogue."""
    from app.db import init_db, session_scope
    from app.models import Match, Recording, RecordingStatus
    from app.services import jobs, match_detect

    video = tmp_path / "rec.mp4"
    _make_video(video, seconds=40)

    def fake_read_timer(frame_bgr, profile):
        h, w = frame_bgr.shape[:2]
        region = frame_bgr[int(0.02 * h):int(0.14 * h), int(0.44 * w):int(0.56 * w)]
        return 90 if region.mean() > 120 else None

    monkeypatch.setattr(match_detect, "read_timer", fake_read_timer)

    init_db()
    with session_scope() as s:
        rec = Recording(source_url="https://youtu.be/x", archive_dir=str(tmp_path),
                        file_path=str(video), status=RecordingStatus.completed)
        s.add(rec)
        s.flush()
        rec_id = rec.id

    detail = jobs.process_recording(rec_id)
    assert "1 windows detected" in detail

    with session_scope() as s:
        matches = s.exec(select(Match).where(Match.recording_id == rec_id)).all()
        assert len(matches) == 1
        m = matches[0]
        assert m.detection_method.value == "ocr"
        assert m.clip_path and Path(m.clip_path).exists()

    # re-processing is idempotent (doesn't duplicate the auto match)
    jobs.process_recording(rec_id)
    with session_scope() as s:
        matches = s.exec(select(Match).where(Match.recording_id == rec_id)).all()
        assert len(matches) == 1
