import pytest

from app.models import SourceType
from app.services import stream_resolver


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=abc123", SourceType.youtube),
        ("https://youtu.be/abc123", SourceType.youtube),
        ("https://www.twitch.tv/robotevents", SourceType.twitch),
        ("https://player.twitch.tv/?channel=robotevents", SourceType.twitch),
        ("https://cdn.example.com/live/stream.m3u8?token=xyz", SourceType.hls),
        ("https://some-broadcast.example.org/live", SourceType.other),
    ],
)
def test_classify(url, expected):
    assert stream_resolver.classify(url).source_type == expected


def test_youtube_supports_live_from_start():
    assert stream_resolver.classify("https://youtube.com/watch?v=x").supports_live_from_start


def test_hls_no_live_from_start():
    assert not stream_resolver.classify("https://x/y.m3u8").supports_live_from_start


def test_rejects_non_http():
    with pytest.raises(stream_resolver.ResolveError):
        stream_resolver.classify("rtmp://example.com/live")


def test_rejects_empty():
    with pytest.raises(stream_resolver.ResolveError):
        stream_resolver.classify("   ")
