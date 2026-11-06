"""Settings, the /media folder picker, and overlay-profile endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.config import get_settings
from app.db import get_session
from app.schemas import (
    DirEntry,
    DirListing,
    MakeDirIn,
    OverlayProfileOut,
    SettingsIn,
)
from app.services import settings_store
from app.services.overlay import OverlayProfile, list_profiles, load_profile, save_profile
from app.services.vexevents import VexEventsClient, VexEventsError

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/settings")
def read_settings(session: Session = Depends(get_session)) -> dict:
    data = settings_store.public_view(session)
    data["overlay_profiles"] = list_profiles()
    return data


@router.put("/settings")
def update_settings(body: SettingsIn, session: Session = Depends(get_session)) -> dict:
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    settings_store.set_many(session, values)
    data = settings_store.public_view(session)
    data["overlay_profiles"] = list_profiles()
    return data


@router.post("/settings/test-vexevents")
def test_vexevents(session: Session = Depends(get_session)) -> dict:
    cfg = settings_store.get_all(session)
    source = {"env": "environment variable", "settings": "Settings page"}.get(
        cfg["vexevents_token_source"], ""
    )
    try:
        VexEventsClient(token=cfg["vexevents_token"] or None).ping()
        return {"ok": True, "source": cfg["vexevents_token_source"]}
    except VexEventsError as exc:
        detail = str(exc)
        if source:
            detail += f" (key from the {source}, sent to {cfg['vexevents_base_url']})"
        raise HTTPException(400, detail)


# --- /media folder picker -----------------------------------------

def _archive_root() -> Path:
    return get_settings().archive_root.resolve()


def _safe(path: str | None) -> Path:
    root = _archive_root()
    if not path:
        return root
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    if p != root and root not in p.parents:
        raise HTTPException(400, "path escapes the archive root")
    return p


@router.get("/fs/dirs", response_model=DirListing)
def list_dirs(path: str | None = None) -> DirListing:
    root = _archive_root()
    target = _safe(path)
    if not target.is_dir():
        raise HTTPException(404, f"{target} is not a directory")
    entries: list[DirEntry] = []
    try:
        for child in sorted(target.iterdir(), key=lambda c: c.name.lower()):
            if child.is_dir() and not child.name.startswith("."):
                has_children = any(
                    gc.is_dir() for gc in child.iterdir() if not gc.name.startswith(".")
                )
                entries.append(
                    DirEntry(name=child.name, path=str(child), has_children=has_children)
                )
    except PermissionError:
        raise HTTPException(403, "permission denied reading that directory")
    parent = None if target == root else str(target.parent)
    return DirListing(path=str(target), parent=parent, entries=entries)


@router.post("/fs/dirs", response_model=DirListing)
def make_dir(body: MakeDirIn) -> DirListing:
    base = _safe(body.path)
    name = body.name.strip().strip("/")
    if not name or "/" in name or name in {".", ".."}:
        raise HTTPException(400, "invalid folder name")
    new = _safe(str(base / name))
    new.mkdir(parents=True, exist_ok=True)
    return list_dirs(str(base))


# --- overlay profiles -------------------------------------------

@router.get("/overlay-profiles", response_model=list[OverlayProfileOut])
def get_overlay_profiles() -> list[OverlayProfileOut]:
    return [OverlayProfileOut(**load_profile(n).to_dict()) for n in list_profiles()]


@router.put("/overlay-profiles/{name}", response_model=OverlayProfileOut)
def put_overlay_profile(name: str, body: OverlayProfileOut) -> OverlayProfileOut:
    data = body.model_dump()
    data["name"] = name
    profile = OverlayProfile.from_dict(data)
    save_profile(profile)
    return OverlayProfileOut(**profile.to_dict())
