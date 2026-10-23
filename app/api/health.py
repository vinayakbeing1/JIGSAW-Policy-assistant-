"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine
from app.rag import rerank

router = APIRouter(tags=["health"])


@router.get("/health", summary="The process is running")
def health() -> dict:
    return {"status": "ok"}


@router.get("/ready", summary="Database, schema, model configuration and reranker are in place")
def ready() -> JSONResponse:
    settings = get_settings()
    checks: dict[str, dict] = {}
    try:
        with get_engine().connect() as conn:
            checks["database"] = {"ok": True}
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
            checks["migrations"] = {"ok": version is not None, "version": version}
            mismatched = conn.execute(
                text(
                    "SELECT count(*) FROM documents WHERE status = 'ready' AND embedding_model IS DISTINCT FROM :m"
                ),
                {"m": settings.openai_embedding_model},
            ).scalar_one()
            checks["embeddings"] = {
                "ok": mismatched == 0,
                "model": settings.openai_embedding_model,
                "documents_indexed_with_another_model": mismatched,
            }
    except Exception as exc:
        checks["database"] = {"ok": False, "error": type(exc).__name__}
    checks["openai"] = {"ok": settings.openai_configured, "chat_model": settings.openai_chat_model}
    checks["reranker"] = rerank.status()
    ok = all(c["ok"] for c in checks.values())
    return JSONResponse(
        {"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503
    )
