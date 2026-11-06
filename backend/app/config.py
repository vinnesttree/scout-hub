"""Deployment configuration (env-driven, not user-editable at runtime).

Runtime-editable knobs (RobotEvents token, clip padding, sample fps, ...) live in the
database via :mod:`app.services.settings_store` and fall back to the values here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCOUTHUB_", env_file=".env", extra="ignore"
    )

    # --- deployment paths -------------------------------------------------
    # Fixed archive root inside the container. docker-compose bind-mounts the
    # user's real storage directory here.
    archive_root: Path = Path("/media")
    # Holds the SQLite database and any scratch files.
    data_dir: Path = Path("/data")

    # --- external services (defaults; overridable in the Settings page) --
    robotevents_token: str = ""
    robotevents_base_url: str = "https://www.robotevents.com/api/v2"

    # --- processing defaults (overridable in the Settings page) ---------
    clip_pad_start_s: float = 8.0
    clip_pad_end_s: float = 6.0
    sample_fps: float = 1.0
    keep_full_recording: bool = True
    overlay_profile: str = "vex_default"

    # --- accounts / auth ------------------------------------------------
    # Optional bootstrap admin, created at startup if no account with this
    # username exists yet. Otherwise the first visitor to /login is offered a
    # one-time "create the first admin" form while the user table is empty.
    admin_username: str = ""
    admin_password: str = ""
    # How long a sign-in lasts.
    session_days: float = 30.0
    # Set true when served over HTTPS so the session cookie is Secure-only.
    cookie_secure: bool = False

    # --- server --------------------------------------------------------
    # Browser origins allowed to call the API cross-origin. With the default
    # "*" credentials (the session cookie) are NOT allowed cross-origin; list
    # explicit origins to allow a separately-hosted frontend to sign in.
    cors_origins: list[str] = ["*"]
    # Directory containing the built frontend (index.html + assets). Optional.
    frontend_dist: Path = Path("/app/frontend")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scouthub.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
