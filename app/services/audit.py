"""Audit trail of security-relevant actions (sign-ins, uploads, deletions, user changes)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.db import session_scope
from app.log import get_logger
from app.models import AuditEvent

log = get_logger(__name__)


def record(
    action: str,
    *,
    user_id: UUID | None = None,
    target_id: UUID | str | None = None,
    details: dict[str, Any] | None = None,
    db: Session | None = None,
) -> None:
    """Write an audit event. Without ``db`` it is committed on its own, so failed requests still leave a trace."""
    event = AuditEvent(
        user_id=user_id,
        action=action,
        target_id=str(target_id) if target_id else None,
        details=details or {},
    )
    try:
        if db is not None:
            db.add(event)
        else:
            with session_scope() as s:
                s.add(event)
    except Exception:
        log.exception("audit_write_failed", action=action)
    log.info("audit", action=action, target_id=str(target_id) if target_id else None)
