"""Accounts + access control: anonymous visitors are read-only."""

import pytest
from fastapi.testclient import TestClient
from datetime import datetime, timedelta, timezone

from sqlmodel import delete, select

from app.db import init_db, session_scope
from app.main import app
from app.models import AuthSession, Recording, RecordingStatus, User
from app.services import auth as auth_svc
from conftest import TEST_PASSWORD, sign_in


@pytest.fixture()
def anon():
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin():
    init_db()
    with TestClient(app) as c:
        sign_in(c, "authadmin", is_admin=True)
        yield c


@pytest.fixture()
def member():
    init_db()
    with TestClient(app) as c:
        sign_in(c, "authmember", is_admin=False)
        yield c


def _recording_id() -> int:
    with session_scope() as s:
        rec = Recording(source_url="https://youtu.be/x", archive_dir="/tmp",
                        status=RecordingStatus.completed)
        s.add(rec)
        s.flush()
        return rec.id


# --- password + session primitives ----------------------------------

def test_password_hash_roundtrip():
    h = auth_svc.hash_password("hunter2hunter2")
    assert h.startswith("pbkdf2_sha256$") and "hunter2" not in h
    assert auth_svc.verify_password("hunter2hunter2", h)
    assert not auth_svc.verify_password("wrong-password", h)
    assert not auth_svc.verify_password("x", "garbage")


def test_session_token_not_stored_in_plaintext(admin):
    token = admin.cookies.get(auth_svc.COOKIE_NAME)
    assert token
    with session_scope() as s:
        hashes = [r.token_hash for r in s.exec(select(AuthSession)).all()]
    assert token not in hashes


# --- anonymous: read-only ----------------------------------------------

def test_anonymous_can_view_and_search(anon, admin):
    rec_id = _recording_id()
    r = admin.post("/api/matches", json={
        "recording_id": rec_id, "name": "Qual 7", "round": "qual", "match_num": 7,
        "video_start_s": 1, "video_end_s": 2,
        "red_teams": ["7777A"], "blue_teams": ["8888B"],
    })
    assert r.status_code == 201, r.text
    mid = r.json()["id"]

    assert anon.get("/api/auth/me").json()["user"] is None
    rows = anon.get("/api/matches", params={"team": "7777A"})
    assert rows.status_code == 200 and [m["id"] for m in rows.json()] == [mid]
    assert anon.get(f"/api/matches/{mid}").status_code == 200
    assert anon.get("/api/events").status_code == 200
    assert anon.get("/api/health").status_code == 200


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/recordings", {"source_url": "https://youtu.be/x", "archive_dir": "x"}),
    ("get", "/api/recordings", None),
    ("post", "/api/recordings/1/stop", None),
    ("post", "/api/recordings/1/process", None),
    ("delete", "/api/recordings/1", None),
    ("get", "/api/jobs", None),
    ("get", "/api/settings", None),
    ("put", "/api/settings", {"sample_fps": 9}),
    ("post", "/api/settings/test-robotevents", None),
    ("get", "/api/fs/dirs", None),
    ("post", "/api/fs/dirs", {"path": "", "name": "x"}),
    ("get", "/api/overlay-profiles", None),
    ("get", "/api/events/search?sku=RE-VRC-1", None),
    ("post", "/api/events/import", {"sku": "RE-VRC-1"}),
    ("delete", "/api/events/1", None),
    ("post", "/api/matches", {"recording_id": 1, "video_start_s": 0, "video_end_s": 1}),
    ("patch", "/api/matches/1", {"red_score": 1}),
    ("delete", "/api/matches/1", None),
    ("post", "/api/matches/1/reclip", None),
    ("get", "/api/users", None),
    ("post", "/api/users", {"username": "sneaky", "password": "12345678"}),
    ("patch", "/api/users/1", {"is_admin": True}),
    ("delete", "/api/users/1", None),
])
def test_anonymous_cannot_change_anything(anon, method, path, body):
    kwargs = {"json": body} if body is not None else {}
    r = anon.request(method.upper(), path, **kwargs)
    assert r.status_code == 401, (method, path, r.status_code, r.text)


def test_anonymous_cannot_change_a_real_match(anon, admin):
    rec_id = _recording_id()
    mid = admin.post("/api/matches", json={
        "recording_id": rec_id, "name": "Qual 9", "video_start_s": 1, "video_end_s": 2,
        "red_score": 10,
    }).json()["id"]
    assert anon.patch(f"/api/matches/{mid}", json={"red_score": 99}).status_code == 401
    assert anon.delete(f"/api/matches/{mid}").status_code == 401
    assert anon.get(f"/api/matches/{mid}").json()["red_score"] == 10


# --- sign in / out ---------------------------------------------------

def test_login_logout_cycle(anon):
    with session_scope() as s:
        if auth_svc.get_by_username(s, "cycleuser") is None:
            auth_svc.create_user(s, "cycleuser", TEST_PASSWORD)

    bad = anon.post("/api/auth/login", json={"username": "cycleuser", "password": "nope-nope"})
    assert bad.status_code == 401
    unknown = anon.post("/api/auth/login", json={"username": "ghost", "password": "nope-nope"})
    assert unknown.status_code == 401 and unknown.json()["detail"] == bad.json()["detail"]

    # username is case-insensitive
    r = anon.post("/api/auth/login", json={"username": "CycleUser", "password": TEST_PASSWORD})
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "cycleuser"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert anon.get("/api/jobs").status_code == 200

    assert anon.post("/api/auth/logout").status_code == 204
    assert anon.get("/api/auth/me").json()["user"] is None
    assert anon.get("/api/jobs").status_code == 401


def test_stolen_token_dies_on_logout(admin):
    token = admin.cookies.get(auth_svc.COOKIE_NAME)
    admin.post("/api/auth/logout")
    with TestClient(app) as other:
        other.cookies.set(auth_svc.COOKIE_NAME, token)
        assert other.get("/api/jobs").status_code == 401


def test_expired_session_rejected(member):
    with session_scope() as s:
        for sess in s.exec(select(AuthSession)).all():
            sess.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            s.add(sess)
    assert member.get("/api/jobs").status_code == 401


# --- roles -----------------------------------------------------------

def test_member_can_record_and_edit_but_not_manage_accounts(member):
    assert member.get("/api/recordings").status_code == 200
    assert member.get("/api/settings").status_code == 200
    assert member.get("/api/users").status_code == 200
    r = member.post("/api/users", json={"username": "newbie", "password": "12345678"})
    assert r.status_code == 403
    me = member.get("/api/auth/me").json()["user"]
    assert member.patch(f"/api/users/{me['id']}", json={"is_admin": True}).status_code == 403


def test_admin_manages_accounts(admin):
    r = admin.post("/api/users", json={"username": "Scout_1", "password": "short"})
    assert r.status_code == 400
    r = admin.post("/api/users", json={"username": "Scout_1", "password": "longenough1"})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    assert r.json()["username"] == "scout_1" and r.json()["is_admin"] is False
    assert "password" not in str(r.json()).lower()
    dup = admin.post("/api/users", json={"username": "scout_1", "password": "longenough1"})
    assert dup.status_code == 400

    # the new account can sign in; an admin password reset signs it out
    with TestClient(app) as scout:
        assert scout.post("/api/auth/login",
                          json={"username": "scout_1", "password": "longenough1"}).status_code == 200
        assert admin.patch(f"/api/users/{uid}", json={"password": "resetpass99"}).status_code == 200
        assert scout.get("/api/jobs").status_code == 401

    assert admin.delete(f"/api/users/{uid}").status_code == 204
    assert all(u["id"] != uid for u in admin.get("/api/users").json())


def test_change_own_password_requires_current(member):
    me = member.get("/api/auth/me").json()["user"]
    r = member.patch(f"/api/users/{me['id']}", json={"password": "brand-new-pass"})
    assert r.status_code == 400
    r = member.patch(f"/api/users/{me['id']}",
                     json={"password": "brand-new-pass", "current_password": TEST_PASSWORD})
    assert r.status_code == 200
    # still signed in on this browser
    assert member.get("/api/jobs").status_code == 200
    # restore for other tests
    with session_scope() as s:
        auth_svc.set_password(s, auth_svc.get_by_username(s, "authmember"), TEST_PASSWORD)


def test_member_cannot_touch_other_accounts(member, admin):
    admin_id = admin.get("/api/auth/me").json()["user"]["id"]
    assert member.patch(f"/api/users/{admin_id}", json={"password": "takeover123"}).status_code == 403
    assert member.delete(f"/api/users/{admin_id}").status_code == 403


def test_last_admin_is_protected(admin):
    me = admin.get("/api/auth/me").json()["user"]
    assert admin.delete(f"/api/users/{me['id']}").status_code == 409
    with session_scope() as s:
        for u in s.exec(select(User).where(User.is_admin == True, User.id != me["id"])).all():  # noqa: E712
            u.is_admin = False
            s.add(u)
    assert admin.patch(f"/api/users/{me['id']}", json={"is_admin": False}).status_code == 409


# --- first-run setup -------------------------------------------------

def test_first_run_setup_only_when_no_accounts(anon):
    # snapshot + wipe accounts so we can exercise first run
    with session_scope() as s:
        saved = [(u.username, u.password_hash, u.is_admin) for u in s.exec(select(User)).all()]
        s.exec(delete(AuthSession))
        s.exec(delete(User))
    try:
        assert anon.get("/api/auth/me").json()["setup_required"] is True
        r = anon.post("/api/auth/setup", json={"username": "x", "password": "longenough1"})
        assert r.status_code == 400  # username too short
        r = anon.post("/api/auth/setup", json={"username": "owner", "password": "longenough1"})
        assert r.status_code == 201, r.text
        assert r.json()["user"]["is_admin"] is True
        assert anon.get("/api/recordings").status_code == 200  # signed in by setup

        with TestClient(app) as late:
            again = late.post("/api/auth/setup", json={"username": "intruder", "password": "longenough1"})
            assert again.status_code == 409
            assert late.get("/api/auth/me").json()["setup_required"] is False
    finally:
        with session_scope() as s:
            s.exec(delete(AuthSession))
            s.exec(delete(User))
            for name, pw_hash, is_admin in saved:
                s.add(User(username=name, password_hash=pw_hash, is_admin=is_admin))


def test_bootstrap_admin_from_env(monkeypatch):
    from app.config import get_settings

    cfg = get_settings()
    monkeypatch.setattr(cfg, "admin_username", "EnvAdmin")
    monkeypatch.setattr(cfg, "admin_password", "from-the-env-123")
    init_db()
    with session_scope() as s:
        auth_svc.ensure_bootstrap_admin(s)
        auth_svc.ensure_bootstrap_admin(s)  # idempotent
        u = auth_svc.get_by_username(s, "envadmin")
        assert u is not None and u.is_admin
        assert auth_svc.authenticate(s, "envadmin", "from-the-env-123") is not None
