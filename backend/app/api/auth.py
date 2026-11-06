"""Sign-in/out, the current-user dependencies, and account management.

Access model:
- No account: read-only — browse/search the match library, watch clips.
- Any signed-in account: everything else (recordings, events, settings, match edits).
- Admin accounts: additionally create/remove accounts and grant admin.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_session
from app.models import User
from app.schemas import LoginIn, MeOut, UserCreate, UserOut, UserPatch
from app.services import auth as auth_svc
from app.services.auth import COOKIE_NAME, AuthError

router = APIRouter(prefix="/api", tags=["auth"])


# --- dependencies -----------------------------------------------------

def current_user(request: Request, session: Session = Depends(get_session)) -> User | None:
    return auth_svc.user_for_token(session, request.cookies.get(COOKIE_NAME))


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(401, "sign in required")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "admin account required")
    return user


# --- helpers ------------------------------------------------------------

def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=auth_svc.session_max_age_s(),
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
        path="/",
    )


def _me(session: Session, user: User | None) -> MeOut:
    return MeOut(
        user=UserOut.model_validate(user) if user else None,
        setup_required=auth_svc.user_count(session) == 0,
    )


# --- sign in / out ----------------------------------------------------

@router.get("/auth/me", response_model=MeOut)
def me(user: User | None = Depends(current_user), session: Session = Depends(get_session)) -> MeOut:
    return _me(session, user)


@router.post("/auth/login", response_model=MeOut)
def login(body: LoginIn, response: Response, session: Session = Depends(get_session)) -> MeOut:
    user = auth_svc.authenticate(session, body.username, body.password)
    if user is None:
        raise HTTPException(401, "incorrect username or password")
    _set_cookie(response, auth_svc.start_session(session, user))
    return _me(session, user)


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, session: Session = Depends(get_session)) -> None:
    auth_svc.end_session(session, request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")


@router.post("/auth/setup", response_model=MeOut, status_code=201)
def setup_first_admin(body: LoginIn, response: Response, session: Session = Depends(get_session)) -> MeOut:
    """One-time: create the first (admin) account while no accounts exist."""
    try:
        user = auth_svc.create_first_admin(session, body.username, body.password)
    except AuthError as exc:
        raise HTTPException(400, str(exc))
    if user is None:
        raise HTTPException(409, "setup already completed; sign in instead")
    _set_cookie(response, auth_svc.start_session(session, user))
    return _me(session, user)


# --- account management -----------------------------------------------

@router.get("/users", response_model=list[UserOut])
def list_users(_: User = Depends(require_user), session: Session = Depends(get_session)) -> list[User]:
    return session.exec(select(User).order_by(User.username)).all()


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(
    body: UserCreate, _: User = Depends(require_admin), session: Session = Depends(get_session)
) -> User:
    try:
        return auth_svc.create_user(session, body.username, body.password, is_admin=body.is_admin)
    except AuthError as exc:
        raise HTTPException(400, str(exc))


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    body: UserPatch,
    request: Request,
    actor: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> User:
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(404, "user not found")
    is_self = target.id == actor.id
    if not is_self and not actor.is_admin:
        raise HTTPException(403, "admin account required")

    if body.is_admin is not None and body.is_admin != target.is_admin:
        if not actor.is_admin:
            raise HTTPException(403, "admin account required")
        if not body.is_admin and auth_svc.admin_count(session) <= 1:
            raise HTTPException(409, "can't remove the last admin")
        target.is_admin = body.is_admin
        session.add(target)
        session.commit()

    if body.password is not None:
        if is_self and not auth_svc.verify_password(body.current_password or "", target.password_hash):
            raise HTTPException(400, "current password is incorrect")
        try:
            auth_svc.set_password(session, target, body.password)
        except AuthError as exc:
            raise HTTPException(400, str(exc))
        # sign out everywhere else (keep the caller's own session if it's theirs)
        auth_svc.revoke_sessions(
            session, target.id, keep_token=request.cookies.get(COOKIE_NAME) if is_self else None
        )

    session.refresh(target)
    return target


@router.delete("/users/{user_id}", status_code=204)
def delete_user(
    user_id: int, actor: User = Depends(require_admin), session: Session = Depends(get_session)
) -> None:
    target = session.get(User, user_id)
    if target is None:
        return
    if target.id == actor.id:
        raise HTTPException(409, "you can't delete your own account")
    if target.is_admin and auth_svc.admin_count(session) <= 1:
        raise HTTPException(409, "can't remove the last admin")
    auth_svc.delete_user(session, target)
