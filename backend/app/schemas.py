"""API request/response models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import MatchRound


# --- settings ---------------------------------------------------------

class SettingsIn(BaseModel):
    robotevents_token: str | None = None
    clip_pad_start_s: float | None = None
    clip_pad_end_s: float | None = None
    sample_fps: float | None = None
    keep_full_recording: bool | None = None
    overlay_profile: str | None = None


# --- filesystem picker ---------------------------------------------

class DirEntry(BaseModel):
    name: str
    path: str
    has_children: bool


class DirListing(BaseModel):
    path: str
    parent: str | None
    entries: list[DirEntry]


class MakeDirIn(BaseModel):
    path: str
    name: str


# --- events ----------------------------------------------------------

class EventOut(BaseModel):
    id: int
    sku: str
    re_event_id: int | None
    name: str
    season: str
    program: str
    start_date: datetime | None

    model_config = ConfigDict(from_attributes=True)


class EventSearchOut(BaseModel):
    sku: str
    re_event_id: int
    name: str
    season: str
    program: str
    start_date: datetime | None
    divisions: list[dict]


class EventImportIn(BaseModel):
    sku: str


# --- recordings ----------------------------------------------------

class RecordingCreate(BaseModel):
    source_url: str
    archive_dir: str
    event_id: int | None = None
    live_from_start: bool = True
    # VOD only: download just this span, e.g. "0-600" or "1:03:00-1:21:30"
    sections: str | None = None


class RecordingOut(BaseModel):
    id: int
    source_url: str
    source_type: str
    status: str
    archive_dir: str
    file_path: str | None
    title: str
    live_from_start: bool
    is_live: bool | None
    sections: str | None
    progress: str
    error: str | None
    duration_s: float | None
    started_at: datetime | None
    ended_at: datetime | None
    event_id: int | None
    created_at: datetime
    match_count: int = 0

    model_config = ConfigDict(from_attributes=True)


# --- matches -------------------------------------------------------

class TeamOut(BaseModel):
    number: str
    name: str = ""
    station: int = 1

    model_config = ConfigDict(from_attributes=True)


class MatchOut(BaseModel):
    id: int
    recording_id: int | None
    event_id: int | None
    name: str
    round: str
    match_num: int | None
    scheduled_at: datetime | None
    video_start_s: float | None
    video_end_s: float | None
    detection_confidence: float | None
    detection_method: str
    status: str
    review_note: str
    red_score: int | None
    blue_score: int | None
    red_teams: list[TeamOut] = []
    blue_teams: list[TeamOut] = []
    has_clip: bool = False
    winner: str | None = None


class MatchCreate(BaseModel):
    recording_id: int
    name: str = "Match"
    round: MatchRound = MatchRound.unknown
    match_num: int | None = None
    video_start_s: float
    video_end_s: float
    red_teams: list[str] = Field(default_factory=list)
    blue_teams: list[str] = Field(default_factory=list)
    red_score: int | None = None
    blue_score: int | None = None


class MatchPatch(BaseModel):
    name: str | None = None
    round: MatchRound | None = None
    match_num: int | None = None
    video_start_s: float | None = None
    video_end_s: float | None = None
    red_teams: list[str] | None = None
    blue_teams: list[str] | None = None
    red_score: int | None = None
    blue_score: int | None = None
    review_note: str | None = None
    reclip: bool = True


# --- jobs --------------------------------------------------------

class JobOut(BaseModel):
    id: int
    kind: str
    recording_id: int | None
    status: str
    detail: str
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


# --- overlay profiles -------------------------------------------

class OverlayProfileOut(BaseModel):
    name: str
    description: str
    timer_region: list[float]
    red_teams_region: list[float]
    blue_teams_region: list[float]
    red_score_region: list[float]
    blue_score_region: list[float]
    timer_invert: bool


# --- accounts --------------------------------------------------

class LoginIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    is_admin: bool
    created_at: datetime
    last_login_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class MeOut(BaseModel):
    user: UserOut | None
    # True while no account exists yet: /login offers "create the first admin".
    setup_required: bool


class UserCreate(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class UserPatch(BaseModel):
    password: str | None = None
    # required when changing your own password
    current_password: str | None = None
    is_admin: bool | None = None
