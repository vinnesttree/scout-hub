"""_build_plan: live streams vs archived VODs vs sections (no network)."""

from datetime import datetime, timezone

from app.models import Recording, SourceType
from app.services import stream_resolver
from app.services.recorder import YTDLP_FORMAT, _build_plan, _video_start_time

YT = stream_resolver.ResolvedStream(SourceType.youtube, "https://youtu.be/x", "YouTube",
                                    supports_live_from_start=True)
TW = stream_resolver.ResolvedStream(SourceType.twitch, "https://twitch.tv/x", "Twitch",
                                    supports_live_from_start=True)


def _rec(**kw) -> Recording:
    base = dict(source_url="https://youtu.be/x", archive_dir="/tmp/a")
    base.update(kw)
    return Recording(**base)


def _all_args(plan):
    return [a for cmd in plan.sources for a in cmd]


def test_every_source_streams_to_stdout():
    for rec in (_rec(is_live=True), _rec(is_live=False), _rec(is_live=False, sections="0-60")):
        plan = _build_plan(rec, YT)
        for cmd in plan.sources:
            assert cmd[0] == "yt-dlp"
            assert cmd[cmd.index("-o") + 1] == "-"
            assert "-f" in cmd  # never yt-dlp's stdout default (best single file)


def test_live_stream_from_start_uses_separate_video_and_audio_pipes():
    plan = _build_plan(_rec(is_live=True, live_from_start=True), YT)
    assert len(plan.sources) == 2
    assert all("--live-from-start" in cmd for cmd in plan.sources)
    fmts = [cmd[cmd.index("-f") + 1] for cmd in plan.sources]
    assert fmts == ["bv*", "ba/b"]
    assert plan.inputs == ["-i", "{0}", "-i", "{1}"]
    assert "1:a:0?" in plan.maps


def test_archived_vod_never_gets_live_from_start():
    plan = _build_plan(_rec(is_live=False, live_from_start=True), YT)
    assert len(plan.sources) == 1
    assert "--live-from-start" not in plan.sources[0]
    assert plan.sources[0][plan.sources[0].index("-f") + 1] == YTDLP_FORMAT


def test_unknown_liveness_falls_back_to_flag():
    plan = _build_plan(_rec(is_live=None, live_from_start=True), YT)
    assert "--live-from-start" in _all_args(plan)
    plan2 = _build_plan(_rec(is_live=None, live_from_start=False), YT)
    assert "--live-from-start" not in _all_args(plan2)


def test_twitch_live_is_a_single_merged_pipe():
    plan = _build_plan(_rec(is_live=True, live_from_start=True), TW)
    assert len(plan.sources) == 1
    assert "--live-from-start" not in plan.sources[0]


def test_sections_appended():
    plan = _build_plan(_rec(is_live=False, sections="1:03:00-1:21:30"), YT)
    cmd = plan.sources[0]
    i = cmd.index("--download-sections")
    assert cmd[i + 1] == "*1:03:00-1:21:30"
    assert "--force-keyframes-at-cuts" in cmd


def test_hls_is_read_directly_by_the_segmenter():
    hls = stream_resolver.ResolvedStream(SourceType.hls, "https://x/y.m3u8", "HLS")
    plan = _build_plan(_rec(), hls)
    assert plan.sources == []
    assert plan.inputs == ["-i", "https://x/y.m3u8"]


def test_video_start_time():
    probe = stream_resolver.StreamProbe(is_live=False, was_live=True,
                                        release_timestamp=1_700_000_000.0)
    t = _video_start_time(_rec(sections="1:00:00-2:00:00"), YT, probe)
    assert t == datetime.fromtimestamp(1_700_000_000 + 3600, timezone.utc)

    live = stream_resolver.StreamProbe(is_live=True, was_live=False)
    before = datetime.now(timezone.utc)
    assert _video_start_time(_rec(live_from_start=False), YT, live) >= before

    uploaded = stream_resolver.StreamProbe(is_live=False, was_live=False,
                                           release_timestamp=1_700_000_000.0)
    assert _video_start_time(_rec(), YT, uploaded) is None
