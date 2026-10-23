"""Administration: users, usage report, audit log and answer traces."""

from __future__ import annotations

import secrets
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.api.deps import error, get_db, require
from app.models import AuditEvent, Message, User, UserSession
from app.security import Perm, Principal, hash_password, password_problems
from app.services import audit

router = APIRouter(prefix="/api/admin", tags=["admin"])

Role = Literal["admin", "underwriter", "claims_handler", "customer_service"]


class NewUser(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320)
    full_name: str = Field(min_length=1, max_length=200)
    role: Role
    password: str | None = Field(default=None, max_length=256)


class UserChange(BaseModel):
    role: Role | None = None
    is_active: bool | None = None


def _user(u: User) -> dict:
    return {
        "id": str(u.id),
        "email": u.email,
        "full_name": u.full_name,
        "role": u.role,
        "is_active": u.is_active,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
    }


@router.get("/users", summary="All users")
def list_users(
    principal: Principal = Depends(require(Perm.MANAGE_USERS)), db: Session = Depends(get_db)
) -> dict:
    return {"users": [_user(u) for u in db.execute(select(User).order_by(User.full_name)).scalars()]}


@router.post(
    "/users", status_code=201, summary="Create a user (a temporary password is returned if none is given)"
)
def create_user(
    body: NewUser, principal: Principal = Depends(require(Perm.MANAGE_USERS)), db: Session = Depends(get_db)
) -> dict:
    if db.execute(select(User).where(func.lower(User.email) == body.email.lower())).first():
        raise error(409, "email_exists", "A user with this email already exists.")
    password = body.password or secrets.token_urlsafe(12)
    if problems := password_problems(password):
        raise error(422, "weak_password", " ".join(problems))
    user = User(
        email=body.email.lower(),
        full_name=body.full_name.strip(),
        role=body.role,
        password_hash=hash_password(password),
    )
    db.add(user)
    db.flush()
    audit.record(
        "user.created", user_id=principal.user_id, target_id=user.id, details={"role": user.role}, db=db
    )
    return {"user": _user(user), "temporary_password": None if body.password else password}


@router.patch("/users/{user_id}", summary="Change a user's role or disable them")
def change_user(
    user_id: uuid.UUID,
    body: UserChange,
    principal: Principal = Depends(require(Perm.MANAGE_USERS)),
    db: Session = Depends(get_db),
) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise error(404, "not_found", "User not found.")
    if user.id == principal.user_id and (body.is_active is False or body.role not in (None, "admin")):
        raise error(409, "self_lockout", "You cannot remove your own admin access.")
    changes = body.model_dump(exclude_none=True)
    for field, value in changes.items():
        setattr(user, field, value)
    if changes:  # a changed role or a disabled account takes effect immediately
        db.execute(update(UserSession).where(UserSession.user_id == user.id).values(revoked_at=func.now()))
    audit.record("user.changed", user_id=principal.user_id, target_id=user.id, details=changes, db=db)
    return {"user": _user(user)}


@router.get("/stats", summary="Usage over the last N days")
def stats(
    days: int = Query(30, ge=1, le=365),
    principal: Principal = Depends(require(Perm.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> dict:
    window = {"days": days}
    answers = (
        db.execute(
            text(
                """
            SELECT count(*) AS questions,
                   count(DISTINCT c.user_id) AS active_users,
                   count(*) FILTER (WHERE m.status = 'answered') AS answered,
                   count(*) FILTER (WHERE m.status = 'abstained') AS abstained,
                   count(*) FILTER (WHERE m.status = 'clarification') AS clarifications,
                   count(*) FILTER (WHERE m.status = 'error') AS errors,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY m.latency_ms) AS p50_latency_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY m.latency_ms) AS p95_latency_ms,
                   coalesce(sum(m.cost_usd), 0) AS cost_usd
            FROM messages m JOIN conversations c ON c.id = m.conversation_id
            WHERE m.role = 'assistant' AND m.created_at > now() - make_interval(days => :days)
            """
            ),
            window,
        )
        .mappings()
        .one()
    )
    weekly = (
        db.execute(
            text(
                """
            SELECT date_trunc('week', m.created_at)::date AS week, count(*) AS questions,
                   count(DISTINCT c.user_id) AS active_users
            FROM messages m JOIN conversations c ON c.id = m.conversation_id
            WHERE m.role = 'assistant' AND m.created_at > now() - make_interval(days => :days)
            GROUP BY 1 ORDER BY 1
            """
            ),
            window,
        )
        .mappings()
        .all()
    )
    feedback = (
        db.execute(
            text(
                "SELECT count(*) FILTER (WHERE rating = 1) AS helpful, count(*) FILTER (WHERE rating = -1) AS not_helpful "
                "FROM feedback WHERE created_at > now() - make_interval(days => :days)"
            ),
            window,
        )
        .mappings()
        .one()
    )
    documents = dict(
        db.execute(
            text("SELECT status, count(*) FROM documents WHERE status <> 'deleted' GROUP BY status")
        ).all()
    )
    return {
        "days": days,
        "answers": {
            k: float(v) if v is not None and k.endswith(("_ms", "_usd")) else v for k, v in answers.items()
        },
        "weekly": [
            {"week": r["week"].isoformat(), "questions": r["questions"], "active_users": r["active_users"]}
            for r in weekly
        ],
        "feedback": dict(feedback),
        "documents": documents,
    }


@router.get("/audit", summary="Audit log, newest first")
def audit_log(
    limit: int = Query(100, ge=1, le=500),
    principal: Principal = Depends(require(Perm.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> dict:
    rows = db.execute(
        select(AuditEvent, User.email)
        .outerjoin(User, User.id == AuditEvent.user_id)
        .order_by(AuditEvent.id.desc())
        .limit(limit)
    ).all()
    return {
        "events": [
            {
                "at": e.created_at.isoformat(),
                "user": email,
                "action": e.action,
                "target_id": e.target_id,
                "details": e.details,
            }
            for e, email in rows
        ]
    }


@router.get(
    "/messages/{message_id}/trace", summary="How an answer was produced (scope, retrieval scores, checks)"
)
def message_trace(
    message_id: uuid.UUID,
    principal: Principal = Depends(require(Perm.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> dict:
    msg = db.get(Message, message_id)
    if msg is None or msg.role != "assistant":
        raise error(404, "not_found", "Answer not found.")
    return {"message_id": str(msg.id), "model": msg.model, "latency_ms": msg.latency_ms, "trace": msg.trace}
