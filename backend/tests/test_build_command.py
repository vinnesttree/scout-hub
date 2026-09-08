"""_build_command: live streams vs archived VODs vs sections (no network)."""

from pathlib import Path

from app.models import Recording, SourceType
from app.services import stream_resolver
from app.services.recorder import _build_command

YT = stream_resolver.ResolvedStream(SourceType.youtube, "https://youtu.be/x", "YouTube",
                                    supports_live_from_start=True)


def _rec(**kw) -> Recording:
    base = dict(source_url="https://youtu.be/x", archive_dir="/tmp/a")
    base.update(kw)
    return Recording(**base)


def test_live_stream_gets_live_from_start():
    cmd, _ = _build_command(_rec(is_live=True, live_from_start=True), YT, Path("/tmp/a"))
    assert "--live-from-start" in cmd


def test_archived_vod_never_gets_live_from_start():
    cmd, _ = _build_command(_rec(is_live=False, live_from_start=True), YT, Path("/tmp/a"))
    assert "--live-from-start" not in cmd


def test_unknown_liveness_falls_back_to_flag():
    cmd, _ = _build_command(_rec(is_live=None, live_from_start=True), YT, Path("/tmp/a"))
    assert "--live-from-start" in cmd
    cmd2, _ = _build_command(_rec(is_live=None, live_from_start=False), YT, Path("/tmp/a"))
    assert "--live-from-start" not in cmd2


def test_sections_appended():
    cmd, _ = _build_command(_rec(is_live=False, sections="1:03:00-1:21:30"), YT, Path("/tmp/a"))
    i = cmd.index("--download-sections")
    assert cmd[i + 1] == "*1:03:00-1:21:30"
    assert "--force-keyframes-at-cuts" in cmd


def test_hls_still_uses_ffmpeg():
    hls = stream_resolver.ResolvedStream(SourceType.hls, "https://x/y.m3u8", "HLS")
    cmd, out = _build_command(_rec(), hls, Path("/tmp/a"))
    assert cmd[0] == "ffmpeg" and out is not None
