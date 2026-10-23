"""Uploaded PDFs on the local volume. Names are generated (<document id>.pdf), never taken from the user."""

from __future__ import annotations

import contextlib
from pathlib import Path
from uuid import UUID

from app.config import get_settings


def root() -> Path:
    path = Path(get_settings().storage_dir).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def key_for(document_id: UUID) -> str:
    return f"{document_id}.pdf"


def path_for(key: str) -> Path:
    base = root()
    path = (base / key).resolve()
    if path.parent != base:
        raise ValueError("invalid storage key")
    return path


def delete(key: str | None) -> None:
    if key:
        with contextlib.suppress(ValueError):
            path_for(key).unlink(missing_ok=True)
