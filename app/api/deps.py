"""Request dependencies: database session, signed-in user, permission checks."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_sessionmaker
from app.models import User, UserSession
from app.security import CSRF_HEADER, SESSION_COOKIE, Perm, Principal, csrf_valid, hash_token


def get_db() -> Iterator[Session]:
    db = get_sessionmaker()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def current_user(request: Request, db: Session = Depends(get_db)) -> Principal:
    auth = request.headers.get("authorization", "")
    via_cookie = not auth.lower().startswith("bearer ")
    token = request.cookies.get(SESSION_COOKIE) if via_cookie else auth[7:].strip()
    if not token:
        raise error(401, "not_signed_in", "Please sign in.")
    row = db.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(
            UserSession.token_hash == hash_token(token),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > datetime.now(UTC),
            User.is_active.is_(True),
        )
    ).first()
    if row is None:
        raise error(401, "session_expired", "Your session has expired. Please sign in again.")
    session, user = row
    if (
        via_cookie
        and request.method not in ("GET", "HEAD")
        and not csrf_valid(token, request.headers.get(CSRF_HEADER))
    ):
        raise error(403, "csrf_failed", "Missing or invalid CSRF token.")
    return Principal(user.id, user.email, user.full_name, user.role, session.id, via_cookie)


def require(perm: Perm) -> Callable[..., Principal]:
    def dependency(principal: Principal = Depends(current_user)) -> Principal:
        if not principal.can(perm):
            raise error(403, "forbidden", "Your role does not allow this.")
        return principal

    return dependency
