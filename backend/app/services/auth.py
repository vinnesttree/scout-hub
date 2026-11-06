"""Accounts, password hashing and cookie sessions.

Passwords are hashed with PBKDF2-HMAC-SHA256 (stdlib, no extra dependency).
Session tokens are random 256-bit values handed to the browser in an HttpOnly
cookie; the database only ever stores their SHA-256, so a leaked DB can't be
replayed as live sessions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, func, select

from app.config import get_settings
from app.models import AuthSession, User, utcnow

log = logging.getLogger("scouthub.auth")

COOKIE_NAME = "scouthub_session"
PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LEN = 8
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")

# Serialises "is the user table empty? -> create the first admin" so two
# simultaneous first-run requests can't both succeed.
_setup_lock = threading.Lock()


class AuthError(ValueError):
    """Invalid input for an account operation (bad username, short password...)."""


# --- passwords ----------------------------------------------------------

def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str, *, iterations: int | None = None) -> str:
    iters = iterations or PBKDF2_ITERATIONS
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return f"pbkdf2_sha256${iters}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iters_s, salt_b64, hash_b64 = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters_s))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


# Verified against when the username doesn't exist, so a login attempt takes
# about the same time whether or not the account is real.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


# --- validation ---------------------------------------------------------

def normalize_username(username: str) -> str:
    name = (username or "").strip().lower()
    if not _USERNAME_RE.match(name):
        raise AuthError(
            "username must be 3-32 characters: letters, digits, '.', '_' or '-'"
        )
    return name


def check_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD_LEN:
        raise AuthError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    return password


# --- users --------------------------------------------------------------

def user_count(session: Session) -> int:
    return int(session.exec(select(func.count()).select_from(User)).one())


def admin_count(session: Session) -> int:
    return int(
        session.exec(select(func.count()).select_from(User).where(User.is_admin == True)).one()  # noqa: E712
    )


def get_by_username(session: Session, username: str) -> User | None:
    return session.exec(
        select(User).where(User.username == (username or "").strip().lower())
    ).first()


def create_user(session: Session, username: str, password: str, *, is_admin: bool = False) -> User:
    name = normalize_username(username)
    check_password(password)
    if get_by_username(session, name):
        raise AuthError(f"username '{name}' is already taken")
    user = User(username=name, password_hash=hash_password(password), is_admin=is_admin)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def create_first_admin(session: Session, username: str, password: str) -> User | None:
    """Create an admin only if there are no accounts at all. Returns None otherwise."""
    with _setup_lock:
        if user_count(session) > 0:
            return None
        return create_user(session, username, password, is_admin=True)


def set_password(session: Session, user: User, password: str) -> None:
    check_password(password)
    user.password_hash = hash_password(password)
    session.add(user)
    session.commit()


def authenticate(session: Session, username: str, password: str) -> User | None:
    user = get_by_username(session, username)
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def delete_user(session: Session, user: User) -> None:
    revoke_sessions(session, user.id)
    session.delete(user)
    session.commit()


def ensure_bootstrap_admin(session: Session) -> None:
    """Create the env-configured admin (SCOUTHUB_ADMIN_USERNAME/PASSWORD) if missing."""
    cfg = get_settings()
    if not cfg.admin_username or not cfg.admin_password:
        return
    try:
        if get_by_username(session, cfg.admin_username):
            return
        create_user(session, cfg.admin_username, cfg.admin_password, is_admin=True)
        log.info("created bootstrap admin account '%s'", cfg.admin_username.strip().lower())
    except AuthError as exc:
        log.error("SCOUTHUB_ADMIN_USERNAME/PASSWORD not usable: %s", exc)


# --- sessions -----------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware(dt: datetime) -> datetime:
    # SQLite hands datetimes back naive; they were stored as UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def session_max_age_s() -> int:
    return int(get_settings().session_days * 86400)


def start_session(session: Session, user: User) -> str:
    """Create a session row and return the raw token for the cookie."""
    token = secrets.token_urlsafe(32)
    now = utcnow()
    session.add(
        AuthSession(
            token_hash=_token_hash(token),
            user_id=user.id,
            created_at=now,
            expires_at=now + timedelta(seconds=session_max_age_s()),
        )
    )
    user.last_login_at = now
    session.add(user)
    # opportunistic cleanup of expired rows
    for old in session.exec(select(AuthSession).where(AuthSession.expires_at < now)).all():
        session.delete(old)
    session.commit()
    return token


def user_for_token(session: Session, token: str | None) -> User | None:
    if not token:
        return None
    row = session.exec(
        select(AuthSession).where(AuthSession.token_hash == _token_hash(token))
    ).first()
    if row is None:
        return None
    if _aware(row.expires_at) <= utcnow():
        session.delete(row)
        session.commit()
        return None
    return session.get(User, row.user_id)


def end_session(session: Session, token: str | None) -> None:
    if not token:
        return
    row = session.exec(
        select(AuthSession).where(AuthSession.token_hash == _token_hash(token))
    ).first()
    if row is not None:
        session.delete(row)
        session.commit()


def revoke_sessions(session: Session, user_id: int, *, keep_token: str | None = None) -> None:
    keep = _token_hash(keep_token) if keep_token else None
    for row in session.exec(select(AuthSession).where(AuthSession.user_id == user_id)).all():
        if row.token_hash != keep:
            session.delete(row)
    session.commit()
