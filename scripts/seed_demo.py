#!/usr/bin/env python3
"""Populate the database from a local video + (optionally) a RobotEvents event,
so the UI can be explored without capturing a live stream.

    python scripts/seed_demo.py /path/to/recording.mp4 --archive-subdir demo [--sku RE-VRC-24-1234]

Run it with the same env as the app (SCOUTHUB_ARCHIVE_ROOT etc). The video is
copied into <archive_root>/<archive-subdir>/full/ and processed by the normal
pipeline.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.config import get_settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Event, Recording, RecordingStatus, SourceType  # noqa: E402
from app.services import jobs  # noqa: E402
from app.services.robotevents import RobotEventsClient, RobotEventsError  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--archive-subdir", default="demo")
    ap.add_argument("--sku", default=None, help="RobotEvents SKU to link (needs a token)")
    args = ap.parse_args()

    if not args.video.exists():
        ap.error(f"{args.video} not found")

    init_db()
    cfg = get_settings()
    archive_dir = (cfg.archive_root / args.archive_subdir).resolve()
    (archive_dir / "full").mkdir(parents=True, exist_ok=True)
    dest = archive_dir / "full" / args.video.name
    if not dest.exists():
        print(f"copying {args.video} -> {dest}")
        shutil.copy2(args.video, dest)

    event_id = None
    if args.sku:
        try:
            ev = RobotEventsClient().find_event(args.sku)
        except RobotEventsError as exc:
            print(f"! could not load event: {exc}")
        else:
            with session_scope() as s:
                row = Event(sku=ev.sku, re_event_id=ev.re_event_id, name=ev.name,
                            season=ev.season, program=ev.program, start_date=ev.start_date)
                s.add(row)
                s.flush()
                event_id = row.id
            print(f"linked event: {ev.name} (#{event_id})")

    with session_scope() as s:
        rec = Recording(
            source_url=f"file://{dest}",
            source_type=SourceType.other,
            status=RecordingStatus.completed,
            archive_dir=str(archive_dir),
            file_path=str(dest),
            event_id=event_id,
            progress="seeded",
        )
        s.add(rec)
        s.flush()
        rec_id = rec.id
    print(f"recording #{rec_id} created; processing…")
    detail = jobs.process_recording(rec_id)
    print("done:", detail)


if __name__ == "__main__":
    main()
