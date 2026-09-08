"""Recorder subprocess lifecycle, using ffmpeg's test source instead of a real stream."""

import shutil
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_start_monitor_complete_and_callback(tmp_path, monkeypatch):
    from app.db import init_db, session_scope
    from app.models import Recording, RecordingStatus
    from app.services import recorder as rec_mod

    init_db()
    archive = tmp_path / "evt"
    (archive / "full").mkdir(parents=True)

    with session_scope() as s:
        rec = Recording(source_url="https://youtu.be/fake", archive_dir=str(archive),
                        status=RecordingStatus.pending)
        s.add(rec)
        s.flush()
        rec_id = rec.id

    out = archive / "full" / "capture.mp4"

    def fake_build(rec, resolved, archive_dir):
        return (
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=15",
             "-pix_fmt", "yuv420p", str(out)],
            out,
        )

    monkeypatch.setattr(rec_mod, "_build_command", fake_build)
    monkeypatch.setattr(rec_mod.stream_resolver, "probe", lambda *a, **k: None)

    seen: list[int] = []
    mgr = rec_mod.RecorderManager()
    mgr.set_completion_callback(lambda rid: seen.append(rid))

    mgr.start(rec_id)
    assert mgr.is_running(rec_id)

    # wait for the completion callback (fires after the monitor thread commits)
    for _ in range(150):
        if seen:
            break
        time.sleep(0.1)
    assert not mgr.is_running(rec_id)

    with session_scope() as s:
        rec = s.get(Recording, rec_id)
        assert rec.status == RecordingStatus.completed
        assert rec.file_path == str(out)
        assert Path(rec.file_path).exists()
        assert rec.duration_s and rec.duration_s > 1.0
    assert seen == [rec_id]


def test_recover_orphans_marks_interrupted(tmp_path):
    from app.db import init_db, session_scope
    from app.models import Recording, RecordingStatus
    from app.services.recorder import recover_orphans

    init_db()
    with session_scope() as s:
        rec = Recording(source_url="x", archive_dir=str(tmp_path / "nope"),
                        status=RecordingStatus.recording, pid=999999)
        s.add(rec)
        s.flush()
        rec_id = rec.id

    recover_orphans()
    with session_scope() as s:
        assert s.get(Recording, rec_id).status == RecordingStatus.interrupted
