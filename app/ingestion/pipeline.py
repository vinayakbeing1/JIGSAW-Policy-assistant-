"""Ingest one document: extract pages (OCR where needed) -> chunk -> embed -> store, in one transaction."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, insert

from app.config import get_settings
from app.db import session_scope
from app.ingestion.chunking import build_chunks
from app.ingestion.pdf import extract_pages, file_sha256
from app.log import get_logger
from app.models import Chunk, Document, Page
from app.rag import llm
from app.services import storage

log = get_logger(__name__)
EMBEDDING_BATCH = 64


class IngestionError(Exception):
    """``permanent`` errors are not retried (the file itself is the problem)."""

    def __init__(self, message: str, permanent: bool):
        super().__init__(message)
        self.message, self.permanent = message, permanent


def ingest_document(document_id: UUID) -> dict[str, int]:
    settings = get_settings()
    with session_scope() as s:
        doc = s.get(Document, document_id)
        if doc is None or doc.status == "deleted" or not doc.storage_key:
            raise IngestionError("The document no longer exists.", permanent=True)
        doc.status, doc.error_message = "processing", None
        title, key, sha256 = doc.title, doc.storage_key, doc.sha256

    path = storage.path_for(key)
    if not path.exists() or file_sha256(path) != sha256:
        raise IngestionError("The stored file is missing or does not match its checksum.", permanent=True)
    try:
        pages = extract_pages(path, settings.ocr_language, settings.ocr_dpi)
    except Exception as exc:
        raise IngestionError(f"Text extraction failed ({type(exc).__name__}).", permanent=True) from exc
    chunks = build_chunks(pages, settings.chunk_size, settings.chunk_overlap, title)
    if not chunks:
        raise IngestionError("No text could be extracted from the document.", permanent=True)

    try:
        embedder = llm.embeddings()
        texts = [f"{title}\n{c.section}\n{c.text}" for c in chunks]
        vectors = [
            v
            for i in range(0, len(texts), EMBEDDING_BATCH)
            for v in embedder.embed_documents(texts[i : i + EMBEDDING_BATCH])
        ]
        model_name = llm.embedding_model_name()
    except Exception as exc:
        error = llm.model_error(exc)
        raise IngestionError(error.message, permanent=not error.retryable) from exc

    with session_scope() as s:
        doc = s.get(Document, document_id, with_for_update=True)
        if doc is None or doc.status == "deleted":
            raise IngestionError("The document was deleted during ingestion.", permanent=True)
        s.execute(delete(Chunk).where(Chunk.document_id == document_id))
        s.execute(delete(Page).where(Page.document_id == document_id))
        s.execute(
            insert(Page),
            [
                {"document_id": document_id, "page_number": p.number, "text": p.text, "ocr": p.ocr}
                for p in pages
            ],
        )
        s.execute(
            insert(Chunk),
            [
                {
                    "document_id": document_id,
                    "ordinal": i,
                    "section": c.section,
                    "page_start": c.page_start,
                    "page_end": c.page_end,
                    "text": c.text,
                    "embedding": v,
                }
                for i, (c, v) in enumerate(zip(chunks, vectors, strict=True))
            ],
        )
        doc.status, doc.ready_at, doc.page_count = "ready", datetime.now(UTC), len(pages)
        doc.ocr_pages, doc.embedding_model = sum(p.ocr for p in pages), model_name
    stats = {"pages": len(pages), "chunks": len(chunks), "ocr_pages": sum(p.ocr for p in pages)}
    log.info("document_ingested", document_id=str(document_id), **stats)
    return stats
