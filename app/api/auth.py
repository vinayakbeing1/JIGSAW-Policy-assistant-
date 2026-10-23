"""Sign-in (browser cookie or API token), sign-out and the current user."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.deps import current_user, error, get_db
from app.config import get_settings
from app.models import User, UserSession
from app.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    Principal,
    csrf_token,
    hash_password,
    hash_token,
    needs_rehash,
    new_token,
    verify_password,
)
from app.services import audit

router = APIRouter(prefix="/api/auth", tags=["auth"])

MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


def _me(p: Principal, csrf: str | None = None) -> dict:
    return {
        "id": str(p.user_id),
        "email": p.email,
        "full_name": p.full_name,
        "role": p.role,
        "permissions": sorted(p.permissions),
        "csrf_token": csrf,
    }


def _sign_in(db: Session, creds: Credentials) -> tuple[User, str, UserSession]:
    user = db.execute(
        select(User).where(func.lower(User.email) == creds.email.strip().lower())
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    locked = user is not None and user.locked_until is not None and user.locked_until > now
    valid = verify_password(creds.password, user.password_hash if user else None)
    if user is None or locked or not valid or not user.is_active:
        if user is not None and not locked:
            user.failed_logins += 1
            if user.failed_logins >= MAX_FAILED_LOGINS:
                user.locked_until, user.failed_logins = now + LOCKOUT, 0
        audit.record("auth.failed", user_id=user.id if user else None, details={"locked": locked})
        db.commit()
        raise error(401, "invalid_credentials", "Incorrect email or password.")
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(creds.password)
    user.failed_logins, user.locked_until, user.last_login_at = 0, None, now
    token = new_token()
    session = UserSession(
        user_id=user.id,
        token_hash=hash_token(token),
        expires_at=now + timedelta(hours=get_settings().session_ttl_hours),
    )
    db.add(session)
    db.flush()
    audit.record("auth.signed_in", user_id=user.id, db=db)
    return user, token, session


@router.post("/login", summary="Sign in from the browser (sets an httpOnly session cookie)")
def login(creds: Credentials, response: Response, db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    user, token, session = _sign_in(db, creds)
    max_age = settings.session_ttl_hours * 3600
    csrf = csrf_token(token)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="strict",
    )
    response.set_cookie(CSRF_COOKIE, csrf, max_age=max_age, secure=settings.secure_cookies, samesite="strict")
    return _me(Principal(user.id, user.email, user.full_name, user.role, session.id, True), csrf)


@router.post("/token", summary="Get an API token for scripts (send it as 'Authorization: Bearer <token>')")
def api_token(creds: Credentials, db: Session = Depends(get_db)) -> dict:
    _, token, _ = _sign_in(db, creds)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": get_settings().session_ttl_hours * 3600,
    }


@router.post("/logout", summary="Sign out")
def logout(
    response: Response, principal: Principal = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    db.execute(
        update(UserSession).where(UserSession.id == principal.session_id).values(revoked_at=func.now())
    )
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    return {"ok": True}


@router.get("/me", summary="The signed-in user")
def me(request: Request, principal: Principal = Depends(current_user)) -> dict:
    return _me(principal, request.cookies.get(CSRF_COOKIE) if principal.via_cookie else None)
