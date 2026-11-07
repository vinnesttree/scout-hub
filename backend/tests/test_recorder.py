"""Capture pipeline + live splitting, with local ffmpeg sources standing in for yt-dlp.

The per-frame OCR is stubbed (its accuracy depends on a real broadcast overlay);
everything else -- the pipes, the segmenter, reading closed segments while the
capture is still running, clipping from segments, joining at the end -- is real.
"""

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from sqlmodel import select

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)

# 0-5 s pre-show, matches at 5-40 s and 85-120 s, then 10 s of outro
MATCHES = [(5, 40), (85, 120)]
LENGTH = 130


def _make_broadcast(path: Path, fps: int = 5) -> None:
    from PIL import Image, ImageDraw

    frames = path.parent / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    idx = 0
    for t in range(LENGTH):
        img = Image.new("RGB", (320, 180), (10, 10, 10))
        if any(a <= t < b for a, b in MATCHES):
            # a bright block in the timer region marks "match running"
            ImageDraw.Draw(img).rectangle([140, 5, 180, 25], fill=(240, 240, 240))
        for _ in range(fps):
            img.save(frames / f"{idx:06d}.jpg", quality=85)
            idx += 1
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", str(fps),
         "-i", str(frames / "%06d.jpg"), "-f", "lavfi", "-i", f"sine=duration={LENGTH}",
         "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(path)],
        check=True,
    )
    shutil.rmtree(frames, ignore_errors=True)


@pytest.fixture(scope="module")
def broadcast(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("bc") / "broadcast.mp4"
    _make_broadcast(path)
    return path


@pytest.fixture
def stub_ocr(monkeypatch):
    from app.services import match_detect, ocr

    def fake_read_timer(frame_bgr, profile):
        h, w = frame_bgr.shape[:2]
        region = frame_bgr[int(0.02 * h):int(0.14 * h), int(0.44 * w):int(0.56 * w)]
        return 90 if region.mean() > 120 else None

    monkeypatch.setattr(match_detect, "read_timer", fake_read_timer)
    monkeypatch.setattr(match_detect, "read_match_label", lambda f, p: (False, None, None))
    monkeypatch.setattr(
        ocr, "_ocr_region",
        lambda frame, rect, digits_only: "50" if digits_only else "11111A 22222B",
    )


def _new_recording(tmp_path, **kw) -> int:
    from app.db import init_db, session_scope
    from app.models import Recording, RecordingStatus

    init_db()
    with session_scope() as s:
        rec = Recording(source_url="https://youtu.be/fake", archive_dir=str(tmp_path / "evt"),
                        status=RecordingStatus.pending, title="Test Stream", **kw)
        s.add(rec)
        s.flush()
        return rec.id


def _use_plan(monkeypatch, plan):
    from app.services import recorder as rec_mod

    monkeypatch.setattr(rec_mod, "_build_plan", lambda rec, resolved: plan)
    monkeypatch.setattr(rec_mod.stream_resolver, "probe", lambda *a, **k: None)


def _wait(pred, timeout=90.0, step=0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("timed out")


def test_matches_are_split_while_still_recording(tmp_path, monkeypatch, broadcast, stub_ocr):
    from app.db import session_scope
    from app.models import Match, MatchStatus, Recording, RecordingStatus
    from app.services import recorder as rec_mod
    from app.services.recorder import CapturePlan, segments_dir

    # feed the video at 8x real time through a pipe, like yt-dlp -o - would
    source = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-readrate", "8",
              "-i", str(broadcast), "-c", "copy", "-f", "mpegts", "-"]
    _use_plan(monkeypatch, CapturePlan(sources=[source], inputs=["-i", "{0}"]))
    rec_id = _new_recording(tmp_path)

    done: list[tuple[int, bool]] = []
    mgr = rec_mod.RecorderManager()
    mgr.set_completion_callback(lambda rid, needs: done.append((rid, needs)))
    mgr.start(rec_id)

    # the first match is catalogued AND clipped while the capture is still going
    def first_clipped_while_recording():
        with session_scope() as s:
            rec = s.get(Recording, rec_id)
            ms = s.exec(select(Match).where(Match.recording_id == rec_id)).all()
            clipped = [m for m in ms if m.status == MatchStatus.clipped]
            if clipped:
                return (rec.status, clipped[0].video_start_s, clipped[0].clip_path)
            return None

    status, start_s, clip = _wait(first_clipped_while_recording)
    assert status == RecordingStatus.recording, "match should be split before capture ends"
    assert abs(start_s - 5) <= 3
    assert Path(clip).exists()

    _wait(lambda: done)
    assert done == [(rec_id, False)]  # live split worked: no after-the-fact processing
    assert not mgr.is_running(rec_id)

    with session_scope() as s:
        rec = s.get(Recording, rec_id)
        assert rec.status == RecordingStatus.completed
        assert rec.split_status == "done"
        assert rec.analyzed_s and rec.analyzed_s > LENGTH - 5
        assert rec.file_path and rec.file_path.endswith(".mp4") and Path(rec.file_path).exists()
        assert abs(rec.duration_s - LENGTH) < 3
        assert not segments_dir(rec).exists()

        ms = sorted(s.exec(select(Match).where(Match.recording_id == rec_id)).all(),
                    key=lambda m: m.video_start_s)
        assert len(ms) == 2
        for m, (a, b) in zip(ms, MATCHES):
            assert abs(m.video_start_s - a) <= 3 and abs(m.video_end_s - b) <= 3
            assert m.status == MatchStatus.clipped and Path(m.clip_path).exists()
            assert Path(m.thumb_path).exists()
            assert m.detection_method.value == "ocr"
            assert m.provisional is False  # no event linked: nothing to wait for
            assert (m.red_score, m.blue_score) == (50, 50)
        assert [m.name for m in ms] == ["Match 1", "Match 2"]
        from app.crud import match_team_numbers
        assert match_team_numbers(s, ms[0]) == (["11111A", "22222B"], ["11111A", "22222B"])
        first = (ms[0].clip_path, ms[0].video_start_s, ms[0].video_end_s)

    # the clip covers the match plus the default padding (8 s before, 6 s after)
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
         "default=nw=1:nk=1", first[0]], capture_output=True, text=True).stdout)
    expected = (first[2] + 6) - max(0, first[1] - 8)
    assert abs(dur - expected) < 1.0


def test_separate_video_and_audio_pipes_are_merged(tmp_path, monkeypatch, stub_ocr):
    """The live-from-start shape: two producers on two pipes into one segmenter."""
    from app.db import session_scope
    from app.models import Recording, RecordingStatus
    from app.services import recorder as rec_mod
    from app.services.recorder import CapturePlan

    video = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc=duration=12:size=320x180:rate=10", "-c:v", "libx264", "-g", "20",
             "-pix_fmt", "yuv420p", "-f", "mpegts", "-"]
    audio = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=duration=12", "-c:a", "aac", "-f", "mpegts", "-"]
    _use_plan(monkeypatch, CapturePlan(sources=[video, audio], inputs=["-i", "{0}", "-i", "{1}"],
                                       maps=["-map", "0:v:0", "-map", "1:a:0?"]))
    rec_id = _new_recording(tmp_path)
    done = []
    mgr = rec_mod.RecorderManager()
    mgr.set_completion_callback(lambda rid, needs: done.append(rid))
    mgr.start(rec_id)
    _wait(lambda: done)

    with session_scope() as s:
        rec = s.get(Recording, rec_id)
        assert rec.status == RecordingStatus.completed, rec.error
        streams = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
             rec.file_path], capture_output=True, text=True).stdout.split()
        assert sorted(streams) == ["audio", "video"]
        assert abs(rec.duration_s - 12) < 2


def test_stop_finalises_a_live_capture(tmp_path, monkeypatch, stub_ocr):
    from app.db import session_scope
    from app.models import Recording, RecordingStatus
    from app.services import recorder as rec_mod
    from app.services.recorder import CapturePlan

    # an "endless" live stream, produced in real time like yt-dlp on a live URL
    live = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
            "-i", "testsrc=duration=600:size=320x180:rate=10", "-c:v", "libx264", "-g", "20",
            "-pix_fmt", "yuv420p", "-f", "mpegts", "-"]
    _use_plan(monkeypatch, CapturePlan(sources=[live], inputs=["-i", "{0}"]))
    rec_id = _new_recording(tmp_path)
    done = []
    mgr = rec_mod.RecorderManager()
    mgr.set_completion_callback(lambda rid, needs: done.append(rid))
    mgr.start(rec_id)

    from app.services.segments import SegmentStore
    from app.services.recorder import segments_dir_for
    store = SegmentStore(segments_dir_for(rec_id))
    _wait(lambda: store.refresh() or store.segments, timeout=40)

    mgr.stop(rec_id)
    _wait(lambda: done)
    with session_scope() as s:
        rec = s.get(Recording, rec_id)
        assert rec.status == RecordingStatus.completed, rec.error
        assert rec.duration_s and rec.duration_s >= 10
    with pytest.raises(RuntimeError):
        mgr.stop(rec_id)


def test_failed_source_marks_recording_failed(tmp_path, monkeypatch, stub_ocr):
    from app.db import session_scope
    from app.models import Recording, RecordingStatus
    from app.services import recorder as rec_mod
    from app.services.recorder import CapturePlan

    bad = ["sh", "-c", "echo 'ERROR: [youtube] x: Video unavailable' >&2; exit 1"]
    _use_plan(monkeypatch, CapturePlan(sources=[bad], inputs=["-i", "{0}"]))
    rec_id = _new_recording(tmp_path)
    done = []
    mgr = rec_mod.RecorderManager()
    mgr.set_completion_callback(lambda rid, needs: done.append(rid))
    mgr.start(rec_id)
    _wait(lambda: not mgr.is_running(rec_id))
    with session_scope() as s:
        rec = s.get(Recording, rec_id)
        assert rec.status == RecordingStatus.failed
        assert "Video unavailable" in (rec.error or "")
    assert done == []


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

    assert rec_id not in recover_orphans()
    with session_scope() as s:
        assert s.get(Recording, rec_id).status == RecordingStatus.interrupted


def test_recover_orphans_joins_segments_and_requests_processing(tmp_path):
    from app.db import init_db, session_scope
    from app.models import Recording, RecordingStatus
    from app.services.recorder import recover_orphans, segments_dir
    from app.services.segments import segmenter_output_args

    init_db()
    with session_scope() as s:
        rec = Recording(source_url="x", archive_dir=str(tmp_path / "evt"),
                        status=RecordingStatus.recording, split_status="running")
        s.add(rec)
        s.flush()
        rec_id = rec.id
        seg_dir = segments_dir(rec)
    seg_dir.mkdir(parents=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=duration=15:size=160x90:rate=10", "-c:v", "libx264", "-g", "10",
         "-f", "mpegts", str(tmp_path / "src.ts")], check=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(tmp_path / "src.ts"),
                    *segmenter_output_args(seg_dir)], check=True)

    assert rec_id in recover_orphans()
    with session_scope() as s:
        rec = s.get(Recording, rec_id)
        assert rec.status == RecordingStatus.completed
        assert Path(rec.file_path).exists()
        assert rec.split_status == "failed"
    assert not seg_dir.exists()


def test_linked_event_teams_replace_ocr_estimates_during_recording(
        tmp_path, monkeypatch, broadcast, stub_ocr):
    """With an event linked, a match is catalogued with OCR estimates, then
    upgraded to VEX Events teams -- all before the capture has finished."""
    from app.crud import match_team_numbers
    from app.db import session_scope
    from app.models import Event, Match, Recording, RecordingStatus
    from app.services import enrich, live_split
    from app.services import recorder as rec_mod
    from app.services.recorder import CapturePlan
    from app.services.vexevents import REAlliance, REMatch, RETeamRef

    def sched(i, red, blue):
        return REMatch(re_match_id=500 + i, name=f"Qualifier #{i}", round="qual", match_num=i,
                       instance=1, scheduled_at=None, started_at=None, division_id=1,
                       alliances=[REAlliance("red", None, [RETeamRef(t) for t in red]),
                                  REAlliance("blue", None, [RETeamRef(t) for t in blue])])

    schedule = [sched(1, ["11111A", "22222B"], ["33333C", "44444D"]),
                sched(2, ["55555E", "66666F"], ["77777G", "88888H"])]
    fetches = []
    monkeypatch.setattr(enrich, "fetch_schedule",
                        lambda rid: fetches.append(rid) or schedule)
    monkeypatch.setattr(live_split, "ENRICH_MIN_GAP_S", 0.0)

    source = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-readrate", "8",
              "-i", str(broadcast), "-c", "copy", "-f", "mpegts", "-"]
    _use_plan(monkeypatch, CapturePlan(sources=[source], inputs=["-i", "{0}"]))
    rec_id = _new_recording(tmp_path)
    with session_scope() as s:
        ev = Event(sku="RE-LIVE-26-1", re_event_id=4242, name="Live Test")
        s.add(ev)
        s.flush()
        s.get(Recording, rec_id).event_id = ev.id

    done = []
    mgr = rec_mod.RecorderManager()
    mgr.set_completion_callback(lambda rid, needs: done.append(rid))
    mgr.start(rec_id)

    def linked_while_recording():
        with session_scope() as s:
            rec = s.get(Recording, rec_id)
            m = s.exec(select(Match).where(Match.recording_id == rec_id,
                                           Match.re_match_id.is_not(None))).first()
            if m:
                return rec.status, m.name, match_team_numbers(s, m)
        return None

    status, name, teams = _wait(linked_while_recording)
    assert status == RecordingStatus.recording
    assert name == "Qualifier #1"
    assert teams == (["11111A", "22222B"], ["33333C", "44444D"])

    _wait(lambda: done)
    with session_scope() as s:
        ms = sorted(s.exec(select(Match).where(Match.recording_id == rec_id)).all(),
                    key=lambda m: m.video_start_s)
        assert len(ms) == 2
        # linked, teams from the API, but VEX Events has no score yet: still provisional
        assert ms[0].re_match_id == 501 and ms[0].provisional
        assert (ms[0].red_score, ms[0].blue_score) == (50, 50)   # OCR estimate kept
        # second match's OCR teams match nothing unclaimed: left for review
        assert ms[1].re_match_id is None and ms[1].provisional
        assert Path(ms[0].clip_path).name.startswith("Qualifier_1__11111A-22222B_vs_")
    assert fetches  # the schedule was consulted during the run
