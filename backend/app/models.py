"""Database models (SQLModel).

Note: this module intentionally does *not* use ``from __future__ import annotations``.
SQLModel/SQLAlchemy need real types (or plain quoted forward refs) on ``Relationship``
attributes; PEP 563 stringised annotations break mapper configuration.
"""

import enum
from datetime import datetime, timezone
from typing import List, Optional

from sqlmodel import Field, Relationship, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SourceType(str, enum.Enum):
    youtube = "youtube"
    twitch = "twitch"
    hls = "hls"
    other = "other"


class RecordingStatus(str, enum.Enum):
    pending = "pending"
    recording = "recording"
    # capture has ended; the live splitter is finishing the last matches and the
    # segments are being joined into the full recording
    finalizing = "finalizing"
    completed = "completed"
    failed = "failed"
    interrupted = "interrupted"


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class Alliance(str, enum.Enum):
    red = "red"
    blue = "blue"


class MatchRound(str, enum.Enum):
    practice = "practice"
    qual = "qual"
    r16 = "r16"
    qf = "qf"
    sf = "sf"
    final = "final"
    unknown = "unknown"


class DetectionMethod(str, enum.Enum):
    api = "api"
    ocr = "ocr"
    manual = "manual"


class MatchStatus(str, enum.Enum):
    detected = "detected"
    clipped = "clipped"
    failed = "failed"


# --------------------------------------------------------------------------


class AppSetting(SQLModel, table=True):
    __tablename__ = "app_setting"

    key: str = Field(primary_key=True)
    value: str = ""
    updated_at: datetime = Field(default_factory=utcnow)


class Event(SQLModel, table=True):
    __tablename__ = "event"

    id: Optional[int] = Field(default=None, primary_key=True)
    sku: str = Field(index=True)
    re_event_id: Optional[int] = Field(default=None, index=True)
    name: str = ""
    season: str = ""
    program: str = ""
    start_date: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)

    recordings: List["Recording"] = Relationship(back_populates="event")
    matches: List["Match"] = Relationship(back_populates="event")


class Recording(SQLModel, table=True):
    __tablename__ = "recording"

    id: Optional[int] = Field(default=None, primary_key=True)
    source_url: str
    source_type: SourceType = SourceType.other
    resolved_url: str = ""
    status: RecordingStatus = RecordingStatus.pending
    archive_dir: str = ""            # absolute dir under /media chosen by the user
    file_path: Optional[str] = None  # full recording file once known
    live_from_start: bool = True
    is_live: Optional[bool] = None   # None until probed; False = archived VOD
    sections: Optional[str] = None   # VOD only: "HH:MM:SS-HH:MM:SS" (yt-dlp download-sections)
    title: str = ""
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_s: Optional[float] = None
    progress: str = ""              # last human-readable progress line
    error: Optional[str] = None
    pid: Optional[int] = None
    # Wall-clock time of t=0 in the recording, when known (live capture start, or
    # the broadcast start reported by the platform). Used to tie detected matches
    # to VEX Events' actual match start times.
    video_started_at: Optional[datetime] = None
    # Live match splitting (runs while the stream is still being captured):
    # "" (not used) | "running" | "done" | "failed"
    split_status: str = ""
    split_detail: str = ""
    analyzed_s: Optional[float] = None   # how far into the recording the splitter has got
    created_at: datetime = Field(default_factory=utcnow)

    event_id: Optional[int] = Field(default=None, foreign_key="event.id", index=True)
    event: Optional[Event] = Relationship(back_populates="recordings")
    matches: List["Match"] = Relationship(back_populates="recording")


class Team(SQLModel, table=True):
    __tablename__ = "team"

    id: Optional[int] = Field(default=None, primary_key=True)
    number: str = Field(index=True, unique=True)  # e.g. "1234A"
    name: str = ""
    org: str = ""
    re_team_id: Optional[int] = Field(default=None, index=True)

    links: List["MatchTeam"] = Relationship(back_populates="team")


class Match(SQLModel, table=True):
    __tablename__ = "match"

    id: Optional[int] = Field(default=None, primary_key=True)
    recording_id: Optional[int] = Field(default=None, foreign_key="recording.id", index=True)
    event_id: Optional[int] = Field(default=None, foreign_key="event.id", index=True)
    re_match_id: Optional[int] = Field(default=None, index=True)

    name: str = ""                        # "Qualification 12"
    round: MatchRound = MatchRound.unknown
    match_num: Optional[int] = None
    scheduled_at: Optional[datetime] = None

    video_start_s: Optional[float] = None
    video_end_s: Optional[float] = None
    detection_confidence: Optional[float] = None

    clip_path: Optional[str] = None
    thumb_path: Optional[str] = None

    red_score: Optional[int] = None
    blue_score: Optional[int] = None

    detection_method: DetectionMethod = DetectionMethod.manual
    status: MatchStatus = MatchStatus.detected
    review_note: str = ""
    # True while teams/score are rough on-screen OCR estimates waiting to be
    # replaced by VEX Events data (only set when the recording has a linked event).
    provisional: bool = False
    created_at: datetime = Field(default_factory=utcnow)

    recording: Optional[Recording] = Relationship(back_populates="matches")
    event: Optional[Event] = Relationship(back_populates="matches")
    teams: List["MatchTeam"] = Relationship(
        back_populates="match",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class MatchTeam(SQLModel, table=True):
    __tablename__ = "match_team"

    id: Optional[int] = Field(default=None, primary_key=True)
    match_id: int = Field(foreign_key="match.id", index=True)
    team_id: int = Field(foreign_key="team.id", index=True)
    alliance: Alliance = Alliance.red
    station: int = 1

    match: Optional[Match] = Relationship(back_populates="teams")
    team: Optional[Team] = Relationship(back_populates="links")


class Job(SQLModel, table=True):
    __tablename__ = "job"

    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str = "process_recording"
    recording_id: Optional[int] = Field(default=None, index=True)
    status: JobStatus = JobStatus.queued
    detail: str = ""
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


# --- accounts ----------------------------------------------------------


class User(SQLModel, table=True):
    """An account. Anyone signed in can manage recordings, events, settings and
    match edits; ``is_admin`` additionally allows managing other accounts.
    Visitors without an account get read-only access to the match library."""

    __tablename__ = "user"

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)  # stored lower-cased
    password_hash: str
    is_admin: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    last_login_at: Optional[datetime] = None


class AuthSession(SQLModel, table=True):
    """A signed-in browser session. Only a SHA-256 of the cookie token is stored."""

    __tablename__ = "auth_session"

    id: Optional[int] = Field(default=None, primary_key=True)
    token_hash: str = Field(index=True, unique=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
