"""In-process background job worker (single-threaded, FIFO)."""

from __future__ import annotations

import logging
import queue
import threading

from sqlmodel import select

from app.db import session_scope
from app.models import Job, JobStatus
from app.services import jobs as jobs_mod
from app.services.recorder import manager as recorder_manager

log = logging.getLogger("scouthub.worker")


class JobWorker:
    def __init__(self) -> None:
        self._q: queue.Queue[int] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scouthub-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._q.put(-1)

    def submit(self, job_id: int) -> None:
        self._q.put(job_id)

    def enqueue_and_submit(self, recording_id: int, kind: str = jobs_mod.KIND_PROCESS) -> int:
        job_id = jobs_mod.enqueue(recording_id, kind)
        self.submit(job_id)
        return job_id

    def on_capture_complete(self, recording_id: int, needs_processing: bool) -> None:
        """Matches were already split off while capturing; only run the full
        after-the-fact pipeline if live splitting didn't work."""
        if needs_processing:
            self.enqueue_and_submit(recording_id)

    def recover(self) -> None:
        """Re-queue jobs left behind by a restart."""
        with session_scope() as session:
            rows = session.exec(
                select(Job).where(Job.status.in_([JobStatus.queued, JobStatus.running]))
            ).all()
            ids = []
            for job in rows:
                job.status = JobStatus.queued
                session.add(job)
                ids.append(job.id)
        for jid in ids:
            self.submit(jid)
        if ids:
            log.info("re-queued %d job(s) after restart", len(ids))

    def _loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._q.get()
            if job_id == -1:
                break
            try:
                jobs_mod.run_job(job_id)
            except Exception:  # pragma: no cover
                log.exception("worker crashed on job %s", job_id)
            finally:
                self._q.task_done()


worker = JobWorker()


def wire_and_start(recovered_recording_ids: list[int] | None = None) -> None:
    recorder_manager.set_completion_callback(worker.on_capture_complete)
    worker.start()
    worker.recover()
    # captures cut off by a restart: their live split state is gone, so run the
    # full pipeline over the joined recording
    for rid in recovered_recording_ids or []:
        worker.enqueue_and_submit(rid)
