"""Ingestion worker backed by a PostgreSQL job table.

Jobs are claimed with ``SELECT ... FOR UPDATE SKIP LOCKED``, so several workers can run side by side.
Temporary failures (for example the embeddings API timing out) are retried with exponential back-off up to
JOB_MAX_ATTEMPTS; problems with the file itself fail straight away with a readable message. A job left
'running' by a worker that died is picked up again after 15 minutes.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.config import get_settings
from app.db import session_scope
from app.ingestion.pipeline import IngestionError, ingest_document
from app.log import get_logger
from app.services import audit

log = get_logger(__name__)

_CLAIM = text(
    """
    UPDATE ingestion_jobs SET status = 'running', attempts = attempts + 1, locked_at = now()
    WHERE id = (
        SELECT id FROM ingestion_jobs
        WHERE (status = 'queued' AND run_after <= now())
           OR (status = 'running' AND locked_at < now() - interval '15 minutes')
        ORDER BY run_after
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, document_id, attempts
    """
)


def run_next_job() -> str | None:
    """Process one job if one is due. Returns its outcome, or None when the queue is empty."""
    with session_scope() as s:
        job = s.execute(_CLAIM).mappings().first()
    if job is None:
        return None
    try:
        ingest_document(job["document_id"])
    except Exception as exc:
        permanent = isinstance(exc, IngestionError) and exc.permanent
        message = (
            exc.message if isinstance(exc, IngestionError) else f"Unexpected error ({type(exc).__name__})."
        )
        if not isinstance(exc, IngestionError):
            log.exception("ingestion_error", job_id=str(job["id"]))
        final = permanent or job["attempts"] >= get_settings().job_max_attempts
        retry_at = datetime.now(UTC) + timedelta(seconds=30 * 2 ** (job["attempts"] - 1))
        with session_scope() as s:
            s.execute(
                text(
                    "UPDATE ingestion_jobs SET status = :status, error_message = :msg, run_after = :retry_at, "
                    "finished_at = CASE WHEN :final THEN now() END WHERE id = :id"
                ),
                {
                    "status": "failed" if final else "queued",
                    "msg": message,
                    "retry_at": retry_at,
                    "final": final,
                    "id": job["id"],
                },
            )
            s.execute(
                text(
                    "UPDATE documents SET status = :status, error_message = :msg WHERE id = :id AND status <> 'deleted'"
                ),
                {"status": "failed" if final else "queued", "msg": message, "id": job["document_id"]},
            )
        log.warning("ingestion_failed", job_id=str(job["id"]), final=final, error=message)
        if final:
            audit.record(
                "document.ingestion_failed", target_id=job["document_id"], details={"error": message}
            )
        return "failed" if final else "retrying"
    with session_scope() as s:
        s.execute(
            text(
                "UPDATE ingestion_jobs SET status = 'succeeded', error_message = NULL, finished_at = now() WHERE id = :id"
            ),
            {"id": job["id"]},
        )
    return "succeeded"


def drain() -> dict[str, int]:
    """Run jobs until none are due (CLI and tests)."""
    counts: dict[str, int] = {}
    while (outcome := run_next_job()) is not None:
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def run_worker(stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    log.info("worker_started")
    while not stop.is_set():
        try:
            if run_next_job() is None:
                stop.wait(get_settings().worker_poll_seconds)
        except Exception:
            log.exception("worker_loop_error")
            stop.wait(5)
