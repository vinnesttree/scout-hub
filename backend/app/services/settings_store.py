"""Runtime-editable settings, layered over :class:`app.config.Settings`.

Values are persisted as strings in the ``app_setting`` table. Anything not set
there falls back to the env-driven default from ``app.config``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from app.config import get_settings
from app.models import AppSetting


@dataclass(frozen=True)
class Knob:
    key: str
    cast: type
    label: str
    secret: bool = False


# Keys exposed on the Settings page. `cast` is used both ways (store/read).
KNOBS: dict[str, Knob] = {
    "vexevents_token": Knob("vexevents_token", str, "VEX Events API token", secret=True),
    "clip_pad_start_s": Knob("clip_pad_start_s", float, "Clip padding before match (s)"),
    "clip_pad_end_s": Knob("clip_pad_end_s", float, "Clip padding after match (s)"),
    "sample_fps": Knob("sample_fps", float, "Detection sample rate (fps)"),
    "live_sample_fps": Knob("live_sample_fps", float, "Live splitting sample rate (fps)"),
    "keep_full_recording": Knob("keep_full_recording", bool, "Keep full recording after clipping"),
    "overlay_profile": Knob("overlay_profile", str, "Broadcast overlay profile"),
}


# Renamed settings: old DB key -> new key.
_LEGACY_KEYS = {"robotevents_token": "vexevents_token"}


def _parse(raw: str, cast: type) -> Any:
    if cast is bool:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if cast is float:
        return float(raw)
    if cast is int:
        return int(raw)
    return raw


def _dump(value: Any, cast: type) -> str:
    if cast is bool:
        return "true" if value else "false"
    return str(value)


def get_all(session: Session) -> dict[str, Any]:
    cfg = get_settings()
    overrides = {row.key: row.value for row in session.exec(select(AppSetting)).all()}
    out: dict[str, Any] = {}
    # A token saved before the move to VEX Events (same token format, same API)
    # keeps working until a new one is entered.
    for old, new in _LEGACY_KEYS.items():
        if overrides.get(old) and not overrides.get(new):
            overrides[new] = overrides[old]
    for key, knob in KNOBS.items():
        if key in overrides and overrides[key] != "":
            out[key] = _parse(overrides[key], knob.cast)
        else:
            out[key] = getattr(cfg, key)
    # An API key from the environment always wins over the Settings page.
    env_token = env_vexevents_token()
    if env_token:
        out["vexevents_token"] = env_token
        out["vexevents_token_source"] = "env"
    else:
        out["vexevents_token_source"] = "settings" if out["vexevents_token"] else "none"
    # Non-editable but useful for the UI.
    out["archive_root"] = str(cfg.archive_root)
    out["vexevents_base_url"] = cfg.vexevents_base_url
    return out


def env_vexevents_token() -> str:
    """The VEX Events key from SCOUTHUB_VEXEVENTS_TOKEN / VEX_EVENTS_API_KEY (or the
    legacy SCOUTHUB_ROBOTEVENTS_TOKEN), or "" if none is set."""
    cfg = get_settings()
    return (cfg.vexevents_token or cfg.robotevents_token or "").strip()


def get(session: Session, key: str) -> Any:
    return get_all(session)[key]


def set_many(session: Session, values: dict[str, Any]) -> dict[str, Any]:
    for key, value in values.items():
        knob = KNOBS.get(key)
        if knob is None:
            continue
        row = session.get(AppSetting, key)
        raw = _dump(value, knob.cast)
        if row is None:
            session.add(AppSetting(key=key, value=raw))
        else:
            row.value = raw
    session.commit()
    return get_all(session)


def public_view(session: Session) -> dict[str, Any]:
    """Same as :func:`get_all` but secret values are replaced with a boolean-ish marker."""
    data = get_all(session)
    for key, knob in KNOBS.items():
        if knob.secret:
            data[f"{key}_set"] = bool(data.get(key))
            data[key] = "••••••••" if data.get(key) else ""
    return data
