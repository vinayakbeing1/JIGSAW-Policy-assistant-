"""Passwords, session tokens, CSRF and role permissions.

* Passwords are hashed with Argon2id (argon2-cffi defaults follow RFC 9106).
* Sessions are random opaque tokens; only their SHA-256 hash is stored, so they can be revoked instantly.
* Browser requests that change state must send ``X-CSRF-Token`` = HMAC(session token); API clients use a
  bearer token instead, which browsers never send automatically.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from enum import StrEnum

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

SESSION_COOKIE = "jigsaw_session"
CSRF_COOKIE = "jigsaw_csrf"
CSRF_HEADER = "x-csrf-token"
MIN_PASSWORD_LENGTH = 12

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-ish time: an unknown user still costs one Argon2 verification."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def password_problems(password: str) -> list[str]:
    problems = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > 256:
        problems.append("Password must be at most 256 characters.")
    return problems


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token(session_token: str) -> str:
    return hmac.new(session_token.encode(), b"csrf", hashlib.sha256).hexdigest()


def csrf_valid(session_token: str, presented: str | None) -> bool:
    return bool(presented) and hmac.compare_digest(csrf_token(session_token), presented or "")


class Perm(StrEnum):
    ASK = "ask"
    MANAGE_DOCUMENTS = "manage_documents"
    MANAGE_USERS = "manage_users"
    VIEW_REPORTS = "view_reports"


ROLE_PERMISSIONS: dict[str, frozenset[Perm]] = {
    "admin": frozenset(Perm),
    "underwriter": frozenset({Perm.ASK}),
    "claims_handler": frozenset({Perm.ASK}),
    "customer_service": frozenset({Perm.ASK}),
}


@dataclass(frozen=True)
class Principal:
    """The signed-in user, built from the session on every request."""

    user_id: uuid.UUID
    email: str
    full_name: str
    role: str
    session_id: uuid.UUID
    via_cookie: bool

    @property
    def permissions(self) -> frozenset[Perm]:
        return ROLE_PERMISSIONS.get(self.role, frozenset())

    def can(self, perm: Perm) -> bool:
        return perm in self.permissions

    def can_read(self, allowed_roles: list[str]) -> bool:
        """Document access: admins see everything, other roles only documents shared with their role."""
        return self.role == "admin" or self.role in allowed_roles
