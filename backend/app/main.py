"""Scout Hub FastAPI application."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from shutil import which as _which

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import get_settings
from app.db import init_db
from app.api import auth, events, jobs, matches, recordings, settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("scouthub")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from app.db import session_scope
    from app.services import auth as auth_svc

    with session_scope() as s:
        auth_svc.ensure_bootstrap_admin(s)
        if auth_svc.user_count(s) == 0:
            log.warning("no accounts yet: open /login to create the first admin")

    from app.services.recorder import recover_orphans
    from app.worker import wire_and_start

    recover_orphans()
    wire_and_start()

    from app.services.match_detect import tesseract_ready

    ok, msg = tesseract_ready()
    if not ok:
        log.warning("OCR degraded: %s (install tesseract + the 'eng' model)", msg)
    for tool in ("ffmpeg", "ffprobe", "yt-dlp"):
        if _which(tool) is None:
            log.warning("required tool not on PATH: %s", tool)

    log.info("Scout Hub %s ready (archive root: %s)", __version__, get_settings().archive_root)
    yield


app = FastAPI(title="Scout Hub", version=__version__, lifespan=lifespan)

_cfg = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cfg.cors_origins,
    # Never combine a wildcard origin with credentials: that would let any site
    # make signed-in requests with a visitor's session cookie.
    allow_credentials="*" not in _cfg.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(settings.router)
app.include_router(events.router)
app.include_router(recordings.router)
app.include_router(matches.router)
app.include_router(jobs.router)


@app.get("/api/health")
def health() -> dict:
    from app.services.match_detect import tesseract_ready

    ocr_ok, ocr_msg = tesseract_ready()
    return {
        "status": "ok",
        "version": __version__,
        "tools": {
            "ffmpeg": _which("ffmpeg") is not None,
            "ffprobe": _which("ffprobe") is not None,
            "yt_dlp": _which("yt-dlp") is not None,
            "ocr": ocr_ok,
        },
        "ocr_detail": ocr_msg,
    }


# --- serve the built frontend (if present) -----------------------

_dist = _cfg.frontend_dist
if _dist.is_dir() and (_dist / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        candidate = _dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_dist / "index.html")
