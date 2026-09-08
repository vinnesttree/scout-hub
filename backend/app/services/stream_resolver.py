"""Classify a user-supplied stream URL into something recordable.

Scout Hub records three kinds of input:

* ``youtube`` / ``twitch`` -- handed to yt-dlp. Works for a *live* stream
  (optionally ``--live-from-start``) and for an *archived* video / finished
  livestream (a plain VOD download).
* ``hls``                  -- an ``.m3u8`` manifest, recorded with ffmpeg ``-c copy``

vexworlds.tv itself is Cloudflare-protected and simulcasts to YouTube/Twitch, so the
user pastes the underlying YouTube/Twitch link or an ``.m3u8`` captured from the page.
If a vexworlds.tv page URL is pasted we try to scrape an embedded stream URL, but that
is best-effort.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.models import SourceType

log = logging.getLogger("scouthub.resolver")

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
_TWITCH_HOSTS = {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}
_HLS_RE = re.compile(r"\.m3u8(\?|$)", re.IGNORECASE)
_EMBED_URL_RE = re.compile(
    r"""https?://[^\s"'<>]+?\.m3u8[^\s"'<>]*"""
    r"""|https?://www\.youtube\.com/embed/[\w-]+"""
    r"""|https?://player\.twitch\.tv/[^\s"'<>]+""",
    re.IGNORECASE,
)


@dataclass
class ResolvedStream:
    source_type: SourceType
    # URL to feed the downloader. For youtube/twitch this is a normal page URL;
    # for hls it is the manifest URL.
    url: str
    note: str = ""
    supports_live_from_start: bool = False


@dataclass
class StreamProbe:
    """Metadata from a no-download yt-dlp query."""

    is_live: bool
    was_live: bool          # a finished livestream, now a VOD (archived)
    title: str = ""
    duration_s: float | None = None
    webpage_url: str = ""

    @property
    def is_vod(self) -> bool:
        return not self.is_live


class ResolveError(RuntimeError):
    pass


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def classify(url: str) -> ResolvedStream:
    url = url.strip()
    if not url:
        raise ResolveError("empty URL")
    if not url.lower().startswith(("http://", "https://")):
        raise ResolveError("URL must start with http:// or https://")

    host = _host(url)

    if host in _YOUTUBE_HOSTS or host.endswith(".youtube.com"):
        return ResolvedStream(SourceType.youtube, url, "YouTube", supports_live_from_start=True)
    if host in _TWITCH_HOSTS or host.endswith(".twitch.tv"):
        return ResolvedStream(SourceType.twitch, url, "Twitch", supports_live_from_start=True)
    if _HLS_RE.search(url):
        return ResolvedStream(SourceType.hls, url, "HLS manifest")
    if "vexworlds.tv" in host:
        return _resolve_vexworlds(url)

    # Fall back to yt-dlp's generic extractor; many broadcast pages work.
    return ResolvedStream(SourceType.other, url, f"generic extractor ({host})")


def _resolve_vexworlds(url: str) -> ResolvedStream:
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ScoutHub/0.1",
        })
        resp.raise_for_status()
    except httpx.HTTPError as exc:  # pragma: no cover - network dependent
        raise ResolveError(
            "Could not load the vexworlds.tv page. Paste the underlying YouTube/Twitch "
            "link or an .m3u8 URL from the page's network tab instead."
        ) from exc

    match = _EMBED_URL_RE.search(resp.text)
    if not match:
        raise ResolveError(
            "No embedded stream found on that vexworlds.tv page. Paste the underlying "
            "YouTube/Twitch link or an .m3u8 URL instead."
        )
    embed = match.group(0)
    resolved = classify(embed)
    resolved.note = f"vexworlds.tv -> {resolved.note}"
    return resolved


def probe(url: str, *, timeout: float = 45.0) -> StreamProbe | None:
    """Ask yt-dlp for metadata without downloading. Returns None if unavailable
    (e.g. a raw .m3u8, or yt-dlp/network failure) — callers fall back to defaults.
    """
    resolved = classify(url)
    if resolved.source_type == SourceType.hls:
        return None
    try:
        out = subprocess.run(
            ["yt-dlp", "--skip-download", "--no-warnings", "--playlist-items", "1",
             "--print", "%(is_live)s\t%(live_status)s\t%(duration)s\t%(webpage_url)s\t%(title)s",
             resolved.url],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:  # pragma: no cover
        log.warning("probe failed for %s: %s", url, exc)
        return None
    if out.returncode != 0:
        log.warning("probe non-zero for %s: %s", url, out.stderr.strip()[:300])
        return None

    line = next((ln for ln in out.stdout.splitlines() if "\t" in ln), "")
    parts = line.split("\t")
    if len(parts) < 5:
        return None
    is_live_s, live_status, duration_s, webpage_url, title = parts[:5]
    try:
        duration = float(duration_s) if duration_s not in ("NA", "None", "") else None
    except ValueError:
        duration = None
    return StreamProbe(
        is_live=is_live_s.strip().lower() == "true",
        was_live=live_status.strip() in ("was_live", "post_live"),
        title=title.strip(),
        duration_s=duration,
        webpage_url=webpage_url.strip() or resolved.url,
    )
