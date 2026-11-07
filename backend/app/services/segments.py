"""Read a capture while it is still being written.

The recorder pipes the stream into ffmpeg's ``segment`` muxer, which writes
short self-contained Matroska files (``seg_000000.mkv``, ...) and appends a line
to ``segments.csv`` each time one is *closed*. The segment still being written
is never listed, so everything listed is safe to read: the live splitter samples
frames from them, OCRs teams off them and cuts match clips from them while the
capture carries on. When capture ends the segments are joined into the single
full recording.

Times in here are seconds from the start of the recording, i.e. the same
timeline as the joined full file.
"""

from __future__ import annotations

import csv
import itertools
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.services import clipper

log = logging.getLogger("scouthub.segments")

SEGMENT_LIST = "segments.csv"
SEGMENT_PATTERN = "seg_%06d.mkv"
SEGMENT_TIME_S = 10
_CONCAT_ARGS = ["-f", "concat", "-safe", "0"]


def segmenter_output_args(seg_dir: Path) -> list[str]:
    """ffmpeg output options that write ``seg_dir`` in the layout read here.

    Matroska holds any codec a stream may carry (H.264/VP9/AV1, AAC/Opus), and
    ``-reset_timestamps`` makes each segment start at 0 so it can be read alone.
    With ``-c copy`` ffmpeg can only split on keyframes, so segments run a
    little over ``SEGMENT_TIME_S``; the list records their real lengths.
    """
    return [
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(SEGMENT_TIME_S),
        "-segment_format", "matroska",
        "-reset_timestamps", "1",
        "-segment_list", str(seg_dir / SEGMENT_LIST),
        "-segment_list_type", "csv",
        str(seg_dir / SEGMENT_PATTERN),
    ]


@dataclass(frozen=True)
class Segment:
    index: int
    path: Path
    start_s: float      # position in the recording
    duration_s: float

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s


class SegmentStore:
    def __init__(self, seg_dir: Path) -> None:
        self.seg_dir = Path(seg_dir)
        self.segments: list[Segment] = []
        self._lists = itertools.count()

    # -- discovery ------------------------------------------------------

    def refresh(self) -> list[Segment]:
        """Pick up segments closed since the last call; returns the new ones."""
        listing = self.seg_dir / SEGMENT_LIST
        if not listing.exists():
            return []
        with listing.open(newline="") as fh:
            rows = [r for r in csv.reader(fh) if len(r) >= 3]
        new: list[Segment] = []
        for row in rows[len(self.segments):]:
            try:
                start, end = float(row[1]), float(row[2])
            except ValueError:
                # a line caught half-written; it will be complete next time
                break
            path = self.seg_dir / row[0]
            if not path.exists():
                break
            seg = Segment(len(self.segments), path, self.end_s, max(0.0, end - start))
            self.segments.append(seg)
            new.append(seg)
        return new

    @property
    def end_s(self) -> float:
        return self.segments[-1].end_s if self.segments else 0.0

    def covering(self, start_s: float, end_s: float) -> list[Segment]:
        return [s for s in self.segments if s.end_s > start_s and s.start_s < end_s]

    def at(self, t: float) -> Segment | None:
        for s in self.segments:
            if s.start_s <= t < s.end_s:
                return s
        return None

    # -- reading ---------------------------------------------------

    def _concat_list(self, segs: list[Segment]) -> Path:
        d = self.seg_dir / ".lists"
        d.mkdir(exist_ok=True)
        path = d / f"list_{next(self._lists)}.txt"
        # Each segment's own container length runs a little over the length the
        # segment list records (e.g. 10.39 s vs 10.0 s: the video starts a few
        # frames in, after B-frame delay). Left to probe those, the concat
        # demuxer adds the excess up -- ~0.4 s per segment, minutes over a long
        # stream. Stating the listed durations keeps the joined timeline equal
        # to the one the splitter used.
        path.write_text("".join(
            f"file '{_quote(s.path)}'\nduration {s.duration_s:.6f}\n" for s in segs
        ))
        return path

    def cut(self, start_s: float, end_s: float, dest: Path, *,
            pad_start: float = 0.0, pad_end: float = 0.0) -> Path:
        """Clip ``[start_s - pad_start, end_s + pad_end]`` out of the segments
        (re-encoded, frame-accurate), clamped to what has been captured."""
        a = max(0.0, start_s - pad_start)
        b = min(self.end_s, end_s + pad_end)
        segs = self.covering(a, b)
        if not segs or b <= a:
            raise RuntimeError(f"no captured video for {a:.1f}-{b:.1f}s")
        listing = self._concat_list(segs)
        try:
            local = a - segs[0].start_s
            return clipper.cut(listing, local, local + (b - a), dest, input_args=_CONCAT_ARGS)
        finally:
            listing.unlink(missing_ok=True)

    def grab_frame(self, t: float, dest: Path, width: int = 1280) -> Path | None:
        """One frame at recording time ``t`` (None if not captured yet)."""
        seg = self.at(max(0.0, t))
        if seg is None:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{t - seg.start_s:.3f}", "-i", str(seg.path), "-frames:v", "1",
             "-vf", f"scale={width}:-2", str(dest)],
            capture_output=True, text=True,
        )
        return dest if r.returncode == 0 and dest.exists() else None

    def thumbnail(self, t: float, dest: Path) -> Path:
        seg = self.at(max(0.0, t)) or (self.segments[-1] if self.segments else None)
        if seg is None:
            raise RuntimeError("nothing captured yet")
        return clipper.thumbnail(seg.path, min(max(0.0, t - seg.start_s), seg.duration_s), dest)

    # -- finishing ---------------------------------------------------

    def join(self, dest_stem: Path) -> Path:
        """Join every segment into one file: ``<stem>.mp4`` (stream copy), or
        ``<stem>.mkv`` if the codecs can't go in MP4 (e.g. VP9 + Opus)."""
        self.refresh()
        if not self.segments:
            raise RuntimeError("no segments to join")
        listing = self._concat_list(self.segments)
        errors = []
        try:
            for ext, extra in ((".mp4", ["-movflags", "+faststart"]), (".mkv", [])):
                dest = dest_stem.with_suffix(ext)
                r = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *_CONCAT_ARGS,
                     "-i", str(listing), "-map", "0", "-c", "copy", *extra, str(dest)],
                    capture_output=True, text=True,
                )
                if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                    return dest
                errors.append(f"{ext}: {r.stderr.strip()[-300:]}")
                dest.unlink(missing_ok=True)
        finally:
            listing.unlink(missing_ok=True)
        raise RuntimeError("could not join segments: " + " | ".join(errors))

    def remove(self) -> None:
        shutil.rmtree(self.seg_dir, ignore_errors=True)


def _quote(path: Path) -> str:
    # concat-demuxer quoting: a ' inside '...' is written as '\''
    return str(path.resolve()).replace("'", "'\\''")
