"""Manage long-lived yt-dlp / ffmpeg recording subprocesses."""

from __future__ import annotations

import logging
import re
import shlex
import signal
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.db import session_scope
from app.models import Recording, RecordingStatus, SourceType
from app.services import stream_resolver

log = logging.getLogger("scouthub.recorder")

_PROGRESS_RE = re.compile(r"(\[download\][^\r\n]*|frame=\s*\d+[^\r\n]*|size=[^\r\n]*)")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RecorderManager:
    def __init__(self) -> None:
        self._procs: dict[int, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._on_complete = None  # callback(recording_id) -> None

    def set_completion_callback(self, cb) -> None:
        self._on_complete = cb

    # -- lifecycle ------------------------------------------------------

    def running_ids(self) -> list[int]:
        with self._lock:
            return [rid for rid, p in self._procs.items() if p.poll() is None]

    def is_running(self, recording_id: int) -> bool:
        with self._lock:
            p = self._procs.get(recording_id)
        return bool(p and p.poll() is None)

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
                    # not meaningful for a VOD; keep the flag but note it
                    rec.progress = "archived video — downloading"

            archive_dir = Path(rec.archive_dir)
            (archive_dir / "full").mkdir(parents=True, exist_ok=True)
            cmd, out_path = _build_command(rec, resolved, archive_dir)
            rec.resolved_url = resolved.url
            rec.source_type = resolved.source_type
            rec.status = RecordingStatus.recording
            rec.started_at = _now()
            rec.error = None
            rec.progress = rec.progress or "starting…"
            rec.file_path = str(out_path) if out_path else None
            session.add(rec)

        log.info("recording %s: %s", recording_id, shlex.join(cmd))
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with self._lock:
            self._procs[recording_id] = proc
        with session_scope() as session:
            rec = session.get(Recording, recording_id)
            if rec:
                rec.pid = proc.pid
                session.add(rec)

        threading.Thread(
            target=self._monitor, args=(recording_id, proc), daemon=True
        ).start()

    def stop(self, recording_id: int) -> None:
        with self._lock:
            proc = self._procs.get(recording_id)
        if not proc or proc.poll() is not None:
            raise RuntimeError("recording is not running")
        # SIGINT lets yt-dlp / ffmpeg finalise the container cleanly.
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.terminate()

    # -- internals ----------------------------------------------------

    def _monitor(self, recording_id: int, proc: subprocess.Popen) -> None:
        last_line = ""
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            m = _PROGRESS_RE.search(line)
            if m:
                last_line = m.group(0)[:400]
                _update_progress(recording_id, last_line)
            elif "ERROR" in line or "error" in line.lower():
                last_line = line[:400]
        rc = proc.wait()
        with self._lock:
            self._procs.pop(recording_id, None)

        with session_scope() as session:
            rec = session.get(Recording, recording_id)
            if rec is None:
                return
            rec.ended_at = _now()
            rec.pid = None
            out_path = _final_output(rec)
            if out_path is not None:
                rec.file_path = str(out_path)
            # rc 0 = clean; SIGINT (130 / -2) after data still counts as a usable capture.
            clean_stop = rc in (0, 130, -2, 255)
            if clean_stop and out_path is not None and out_path.exists():
                rec.status = RecordingStatus.completed
                rec.duration_s = _probe_duration(out_path)
                rec.progress = "recording finished"
            else:
                rec.status = RecordingStatus.failed
                rec.error = last_line or f"exit code {rc}"
                rec.progress = "recording failed"
            session.add(rec)
            completed = rec.status == RecordingStatus.completed

        if completed and self._on_complete:
            try:
                self._on_complete(recording_id)
            except Exception:  # pragma: no cover
                log.exception("completion callback failed for recording %s", recording_id)


def _build_command(
    rec: Recording, resolved: stream_resolver.ResolvedStream, archive_dir: Path
) -> tuple[list[str], Path | None]:
    if resolved.source_type == SourceType.hls:
        ts = _now().strftime("%Y%m%d-%H%M%S")
        out = archive_dir / "full" / f"stream-{ts}.mp4"
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
            "-i", resolved.url,
            "-c", "copy", "-movflags", "+faststart",
            str(out),
        ]
        return cmd, out

    # yt-dlp path (youtube / twitch / generic), for both live streams and VODs
    out_tmpl = str(archive_dir / "full" / "%(title).150B-%(id)s.%(ext)s")
    cmd = ["yt-dlp", "--no-part", "--no-progress", "--newline", "-o", out_tmpl]

    # --live-from-start only applies to an actually-live stream. rec.is_live is
    # None when the probe couldn't run — then fall back to the user's checkbox.
    is_live = rec.is_live if rec.is_live is not None else resolved.supports_live_from_start
    if rec.live_from_start and is_live and resolved.supports_live_from_start:
        cmd.append("--live-from-start")

    if rec.sections:
        # e.g. "0-600" or "1:03:00-1:21:30"; download just that span of a VOD
        cmd += ["--download-sections", f"*{rec.sections.strip()}", "--force-keyframes-at-cuts"]

    cmd += ["--print", "after_move:filepath", resolved.url]
    return cmd, None


def _final_output(rec: Recording) -> Path | None:
    """Best guess at the finished file."""
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


def _probe_duration(path: Path) -> float | None:
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


def recover_orphans() -> None:
    """On startup mark any 'recording' rows without a live process as interrupted."""
    from sqlmodel import select

    with session_scope() as session:
        rows = session.exec(
            select(Recording).where(Recording.status == RecordingStatus.recording)
        ).all()
        for rec in rows:
            out = _final_output(rec)
            if out is not None:
                rec.status = RecordingStatus.completed
                rec.file_path = str(out)
                rec.duration_s = _probe_duration(out)
                rec.progress = "recovered after restart"
            else:
                rec.status = RecordingStatus.interrupted
                rec.progress = "interrupted by restart"
            rec.pid = None
            session.add(rec)


manager = RecorderManager()
