"""Manage long-lived capture pipelines (yt-dlp / ffmpeg) and their live splitter.

Every capture — a live stream or an archived video being downloaded — is
written as a series of short segments by ffmpeg's segment muxer instead of one
big file, because a growing MP4 can't be read until it's finished. The segments
can be read the moment they're closed, so :class:`~app.services.live_split.LiveSplitter`
finds, catalogues and clips each match while the capture is still running.

Pipeline shapes (all end in the same segmenter, see ``segments.segmenter_output_args``):

* HLS ``.m3u8``      ``ffmpeg -i <m3u8> … segments``
* YouTube/Twitch/…   ``yt-dlp -o - <url>`` | ``ffmpeg -i pipe:0 … segments``
  (yt-dlp merges video+audio itself and streams MPEG-TS / Matroska to stdout)
* YouTube live from the start
                     ``yt-dlp --live-from-start -f bv* -o -`` ┐
                     ``yt-dlp --live-from-start -f ba  -o -`` ┴ ``ffmpeg -i /dev/fd/A -i /dev/fd/B …``
  (yt-dlp can't merge its from-start DASH downloader's two formats to stdout,
  so video and audio come from two processes on separate pipes)

When capture ends the splitter drains the last segments, the segments are joined
into ``full/<title>.mp4`` (kept for re-processing / re-clipping) and removed.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import select

from app.db import session_scope
from app.models import Match, MatchStatus, Recording, RecordingStatus, SourceType
from app.services import clipper, settings_store, stream_resolver
from app.services.live_split import LiveSplitter
from app.services.segments import SegmentStore, segmenter_output_args

log = logging.getLogger("scouthub.recorder")

_PROGRESS_RE = re.compile(r"(\[download\][^\r\n]*|frame=\s*\d+[^\r\n]*|size=[^\r\n]*)")
_TIME_RE = re.compile(r"time=\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_SECTION_START_RE = re.compile(r"^\s*([\d:.]+)\s*-")

# yt-dlp format choice when writing to stdout. Without -f it would fall back to
# the best *single-file* format (often 360p on YouTube). H.264/AAC up to 1080p
# is preferred: it fits in the final MP4, and is cheaper to decode for OCR.
YTDLP_FORMAT = "bv*+ba/b"
YTDLP_SORT = "res:1080,vcodec:h264,acodec:aac"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def segments_dir(rec: Recording) -> Path:
    return Path(rec.archive_dir) / "full" / f".live-{rec.id}"


@dataclass
class CapturePlan:
    """``sources`` write a media stream to stdout (0, 1 or 2 of them);
    ``inputs`` are the segmenter's ffmpeg input options, with ``{0}``/``{1}``
    standing for the sources' pipes."""

    sources: list[list[str]]
    inputs: list[str]
    maps: list[str] = field(default_factory=list)


@dataclass
class Capture:
    sources: list[subprocess.Popen]
    sink: subprocess.Popen
    splitter: LiveSplitter
    capturing: bool = True      # False once the capture processes have exited


class RecorderManager:
    def __init__(self) -> None:
        self._active: dict[int, Capture] = {}
        self._lock = threading.Lock()
        self._on_complete = None  # callback(recording_id, needs_processing: bool)

    def set_completion_callback(self, cb) -> None:
        self._on_complete = cb

    # -- lifecycle ------------------------------------------------------

    def running_ids(self) -> list[int]:
        with self._lock:
            return list(self._active)

    def is_running(self, recording_id: int) -> bool:
        """Capturing, or still finishing the split / join after capture ended."""
        with self._lock:
            return recording_id in self._active

    def start(self, recording_id: int) -> None:
        if self.is_running(recording_id):
            raise RuntimeError("recording already running")
        with session_scope() as session:
            rec = session.get(Recording, recording_id)
            if rec is None:
                raise RuntimeError(f"recording {recording_id} not found")
            resolved = stream_resolver.classify(rec.source_url)

            # No-download metadata probe: tells us live vs archived VOD + a title.
            probe = stream_resolver.probe(rec.source_url)
            if probe is not None:
                rec.is_live = probe.is_live
                rec.title = probe.title or rec.title
                if probe.duration_s and probe.is_vod:
                    rec.duration_s = probe.duration_s
                if probe.is_vod and rec.live_from_start:
                    rec.progress = "archived video — downloading"

            seg_dir = segments_dir(rec)
            seg_dir.mkdir(parents=True, exist_ok=True)
            plan = _build_plan(rec, resolved)
            rec.resolved_url = resolved.url
            rec.source_type = resolved.source_type
            rec.status = RecordingStatus.recording
            rec.started_at = _now()
            rec.video_started_at = _video_start_time(rec, resolved, probe)
            rec.error = None
            rec.progress = rec.progress or "starting…"
            rec.file_path = None
            rec.split_status, rec.split_detail, rec.analyzed_s = "", "", None
            session.add(rec)

        capture = self._spawn(recording_id, plan, seg_dir)
        with self._lock:
            self._active[recording_id] = capture
        with session_scope() as session:
            rec = session.get(Recording, recording_id)
            if rec:
                rec.pid = capture.sink.pid
                session.add(rec)

        capture.splitter.start()
        threading.Thread(
            target=self._monitor, args=(recording_id, capture), daemon=True
        ).start()

    def stop(self, recording_id: int) -> None:
        with self._lock:
            cap = self._active.get(recording_id)
        if not cap or not cap.capturing:
            raise RuntimeError("recording is not capturing")
        # SIGINT lets yt-dlp / ffmpeg finish cleanly. Stopping the sources ends
        # the segmenter's input, so it closes the last segment by itself.
        for p in cap.sources or [cap.sink]:
            _signal(p, signal.SIGINT)
        try:
            cap.sink.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _signal(cap.sink, signal.SIGINT)
            try:
                cap.sink.wait(timeout=15)
            except subprocess.TimeoutExpired:
                for p in [*cap.sources, cap.sink]:
                    _signal(p, signal.SIGTERM)

    # -- internals ----------------------------------------------------

    def _spawn(self, recording_id: int, plan: CapturePlan, seg_dir: Path) -> Capture:
        sources: list[subprocess.Popen] = []
        read_fds: list[int] = []
        pass_fds: list[int] = []
        sink_stdin = subprocess.DEVNULL
        try:
            for cmd in plan.sources:
                log.info("recording %s source: %s", recording_id, shlex.join(cmd))
                r, w = os.pipe()
                sources.append(subprocess.Popen(
                    cmd, stdout=w, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                    text=True, bufsize=1, start_new_session=True,
                ))
                os.close(w)
                read_fds.append(r)
            if len(read_fds) == 1:
                sink_stdin = read_fds[0]
                fd_names = ["pipe:0"]
            else:
                pass_fds = read_fds
                fd_names = [f"/dev/fd/{fd}" for fd in read_fds]
            inputs = [a.format(*fd_names) for a in plan.inputs]
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-y",
                   *inputs, *plan.maps, *segmenter_output_args(seg_dir)]
            log.info("recording %s segmenter: %s", recording_id, shlex.join(cmd))
            sink = subprocess.Popen(
                cmd, stdin=sink_stdin, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, bufsize=1, pass_fds=pass_fds, start_new_session=True,
            )
        except Exception:
            for p in sources:
                _signal(p, signal.SIGTERM)
            raise
        finally:
            for fd in read_fds:   # the children hold their own copies
                os.close(fd)
        return Capture(sources, sink, LiveSplitter(recording_id, seg_dir))

    def _monitor(self, recording_id: int, cap: Capture) -> None:
        errors: list[str] = []          # from the segmenter
        source_errors: list[str] = []   # from yt-dlp: the real cause when both fail

        def read_source(p: subprocess.Popen) -> None:
            assert p.stderr is not None
            for line in p.stderr:
                line = line.strip()
                if "ERROR" in line or "error" in line.lower():
                    source_errors.append(line[:400])
                elif line.startswith("[download]"):
                    _update_progress(recording_id, line[:400])

        readers = [threading.Thread(target=read_source, args=(p,), daemon=True)
                   for p in cap.sources]
        for t in readers:
            t.start()

        assert cap.sink.stderr is not None
        for line in cap.sink.stderr:
            line = line.strip()
            if not line:
                continue
            m = _PROGRESS_RE.search(line)
            if m:
                _update_progress(recording_id, _sink_progress(m.group(0)))
            elif "error" in line.lower():
                errors.append(line[:400])
        sink_rc = cap.sink.wait()
        for p in cap.sources:
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                _signal(p, signal.SIGTERM)
                p.wait()
        for t in readers:
            t.join(timeout=5)
        cap.capturing = False

        store = SegmentStore(segments_dir_for(recording_id))
        store.refresh()
        usable = bool(store.segments) and store.end_s > 0
        source_rcs = [p.returncode for p in cap.sources]
        log.info("recording %s: capture ended (segmenter rc=%s, sources rc=%s, %.0fs captured)",
                 recording_id, sink_rc, source_rcs, store.end_s)

        with session_scope() as session:
            rec = session.get(Recording, recording_id)
            if rec is not None:
                rec.ended_at = _now()
                rec.pid = None
                if usable:
                    rec.status = RecordingStatus.finalizing
                    rec.duration_s = round(store.end_s, 2)
                    rec.progress = "capture ended — finishing the last matches"
                else:
                    rec.status = RecordingStatus.failed
                    last = (source_errors or errors or [None])[-1]
                    rec.error = last or (
                        f"capture produced no video (exit codes {source_rcs + [sink_rc]})")
                    rec.progress = "recording failed"
                session.add(rec)

        cap.splitter.capture_finished()
        cap.splitter.join()

        needs_processing = False
        if usable:
            try:
                needs_processing = self._finalize(recording_id, store, cap.splitter)
            except Exception as exc:
                log.exception("finalising recording %s failed", recording_id)
                with session_scope() as session:
                    rec = session.get(Recording, recording_id)
                    if rec:
                        rec.status = RecordingStatus.failed
                        rec.error = f"could not join the captured segments: {exc}"
                        rec.progress = "recording failed"
                        session.add(rec)
                usable = False
        else:
            store.remove()

        with self._lock:
            self._active.pop(recording_id, None)

        if usable and self._on_complete:
            try:
                self._on_complete(recording_id, needs_processing)
            except Exception:  # pragma: no cover
                log.exception("completion callback failed for recording %s", recording_id)

    def _finalize(self, recording_id: int, store: SegmentStore, splitter: LiveSplitter) -> bool:
        """Join the segments into the full recording. Returns True if the
        after-the-fact pipeline still has to run (live splitting failed)."""
        with session_scope() as session:
            rec = session.get(Recording, recording_id)
            keep_full = settings_store.get_all(session)["keep_full_recording"]
            has_clips = session.exec(
                select(Match).where(Match.recording_id == recording_id,
                                    Match.status == MatchStatus.clipped)
            ).first() is not None
            stem = _full_stem(rec)

        out: Path | None = None
        if keep_full or not splitter.ok or not has_clips:
            out = store.join(stem)
        store.remove()

        with session_scope() as session:
            rec = session.get(Recording, recording_id)
            rec.status = RecordingStatus.completed
            rec.file_path = str(out) if out else None
            rec.duration_s = (_probe_duration(out) if out else None) or rec.duration_s
            if not splitter.ok:
                rec.progress = "recording finished — live splitting failed, processing the full file"
            elif out is None:
                rec.progress = f"recording finished · {splitter.matches_made} match(es) · full recording not kept"
            else:
                rec.progress = f"recording finished · {splitter.matches_made} match(es)"
            session.add(rec)
        return not splitter.ok


def segments_dir_for(recording_id: int) -> Path:
    with session_scope() as session:
        rec = session.get(Recording, recording_id)
        return segments_dir(rec)


def _build_plan(rec: Recording, resolved: stream_resolver.ResolvedStream) -> CapturePlan:
    if resolved.source_type == SourceType.hls:
        return CapturePlan(sources=[], inputs=["-i", resolved.url])

    # yt-dlp path (youtube / twitch / generic), for both live streams and VODs
    base = ["yt-dlp", "--no-part", "--no-progress", "--newline", "-S", YTDLP_SORT]

    # --live-from-start only applies to an actually-live stream. rec.is_live is
    # None when the probe couldn't run — then fall back to the user's checkbox.
    is_live = rec.is_live if rec.is_live is not None else resolved.supports_live_from_start
    if (rec.live_from_start and is_live and resolved.supports_live_from_start
            and resolved.source_type == SourceType.youtube):
        tail = ["--live-from-start", "-o", "-", resolved.url]
        return CapturePlan(
            sources=[[*base, "-f", "bv*", *tail], [*base, "-f", "ba/b", *tail]],
            inputs=["-i", "{0}", "-i", "{1}"],
            maps=["-map", "0:v:0", "-map", "1:a:0?"],
        )

    cmd = [*base, "-f", YTDLP_FORMAT]
    if rec.sections:
        # e.g. "0-600" or "1:03:00-1:21:30"; download just that span of a VOD
        cmd += ["--download-sections", f"*{rec.sections.strip()}", "--force-keyframes-at-cuts"]
    cmd += ["-o", "-", resolved.url]
    return CapturePlan(sources=[cmd], inputs=["-i", "{0}"])


def _video_start_time(rec: Recording, resolved, probe) -> datetime | None:
    """Wall-clock time of t=0 in the capture, if it can be known."""
    from_start = (rec.live_from_start and resolved.supports_live_from_start
                  and resolved.source_type == SourceType.youtube)
    if probe is None:
        # raw HLS (no probe) is nearly always a live feed captured from now
        return _now() if resolved.source_type == SourceType.hls else None
    if probe.is_live and not from_start:
        return _now()
    if probe.release_timestamp and (probe.is_live or probe.was_live):
        start = datetime.fromtimestamp(probe.release_timestamp, timezone.utc)
        if rec.sections and not probe.is_live:
            m = _SECTION_START_RE.match(rec.sections)
            if m:
                start += timedelta(seconds=_parse_clock(m.group(1)))
        return start
    return None


def _parse_clock(text: str) -> float:
    secs = 0.0
    for part in text.split(":"):
        secs = secs * 60 + float(part or 0)
    return secs


def _full_stem(rec: Recording) -> Path:
    title = clipper.safe_name(rec.title or "stream")[:150]
    ts = (rec.started_at or _now()).strftime("%Y%m%d-%H%M%S")
    return Path(rec.archive_dir) / "full" / f"{title}-{ts}"


def _sink_progress(line: str) -> str:
    m = _TIME_RE.search(line)
    if m:
        secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        return f"captured {int(secs) // 3600}:{int(secs) % 3600 // 60:02d}:{int(secs) % 60:02d}"
    return line[:400]


def _signal(p: subprocess.Popen, sig: int) -> None:
    if p.poll() is not None:
        return
    try:
        os.killpg(p.pid, sig)   # the whole group: yt-dlp may run its own ffmpeg
    except (ProcessLookupError, PermissionError):
        try:
            p.send_signal(sig)
        except ProcessLookupError:
            pass


def _probe_duration(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return round(float(out.stdout.strip()), 2)
    except Exception:
        return None


def _update_progress(recording_id: int, text: str) -> None:
    with session_scope() as session:
        rec = session.get(Recording, recording_id)
        if rec and rec.status == RecordingStatus.recording:
            rec.progress = text
            session.add(rec)


def _legacy_output(rec: Recording) -> Path | None:
    """A finished single-file capture from before segmented recording."""
    if rec.file_path and Path(rec.file_path).exists():
        return Path(rec.file_path)
    full_dir = Path(rec.archive_dir) / "full"
    if not full_dir.is_dir():
        return None
    candidates = sorted(
        (p for p in full_dir.iterdir() if p.is_file() and p.suffix.lower() in
         {".mp4", ".mkv", ".webm", ".ts", ".m4a"}),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def recover_orphans() -> list[int]:
    """On startup, settle recordings a restart cut off mid-capture.

    Captured segments are joined into a full recording and the ids returned, so
    the caller can run the after-the-fact pipeline over them (the live
    splitter's state didn't survive the restart). Without segments the row is
    marked interrupted.
    """
    to_process: list[int] = []
    with session_scope() as session:
        rows = session.exec(
            select(Recording).where(Recording.status.in_(
                [RecordingStatus.recording, RecordingStatus.finalizing]))
        ).all()
        for rec in rows:
            rec.pid = None
            store = SegmentStore(segments_dir(rec))
            store.refresh()
            out: Path | None = None
            if store.segments:
                try:
                    out = store.join(_full_stem(rec))
                    store.remove()
                except Exception as exc:
                    log.warning("recovering recording %s: %s", rec.id, exc)
            if out is None and not store.segments:
                out = _legacy_output(rec)
            if out is not None:
                rec.status = RecordingStatus.completed
                rec.file_path = str(out)
                rec.duration_s = _probe_duration(out)
                rec.progress = "recovered after restart"
                if rec.split_status == "running":
                    rec.split_status = "failed"
                    rec.split_detail = "interrupted by restart; re-processing the full file"
                to_process.append(rec.id)
            else:
                rec.status = RecordingStatus.interrupted
                rec.progress = "interrupted by restart"
            session.add(rec)
    return to_process


manager = RecorderManager()
