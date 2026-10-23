"""Document lifecycle: upload (validated, queued for ingestion) and deletion."""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.config import ROLES, get_settings
from app.ingestion.pdf import InvalidPdf, save_upload, validate_pdf
from app.models import Chunk, Document, IngestionJob, Page, Product
from app.services import audit, storage


class DocumentError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


@dataclass
class UploadDetails:
    product_code: str
    kind: str
    title: str
    version_label: str
    region: str
    effective_from: date
    effective_to: date | None = None
    applies_to_id: uuid.UUID | None = None
    allowed_roles: list[str] | None = None
    reference: str | None = None


def create_document(
    db: Session, fileobj: BinaryIO, filename: str, details: UploadDetails, uploaded_by: uuid.UUID | None
) -> Document:
    """Validate and store the PDF, create the document and queue its ingestion job (caller commits)."""
    settings = get_settings()
    product = db.execute(
        select(Product).where(Product.code == details.product_code.upper())
    ).scalar_one_or_none()
    if product is None:
        raise DocumentError("unknown_product", f"Unknown product code '{details.product_code}'.")
    if details.kind not in ("wording", "endorsement", "guide"):
        raise DocumentError("invalid_kind", "Document type must be wording, endorsement or guide.")
    if details.effective_to and details.effective_to < details.effective_from:
        raise DocumentError("invalid_dates", "The end date must be on or after the start date.")
    roles = sorted(set(details.allowed_roles or ROLES) | {"admin"})
    if not set(roles) <= set(ROLES):
        raise DocumentError("invalid_roles", f"Roles must be chosen from: {', '.join(ROLES)}.")
    if details.kind == "endorsement":
        target = db.get(Document, details.applies_to_id) if details.applies_to_id else None
        if target is None or target.kind != "wording" or target.status == "deleted":
            raise DocumentError("invalid_applies_to", "An endorsement must be attached to a policy wording.")

    doc_id = uuid.uuid4()
    tmp = storage.root() / f"{doc_id}.upload"
    try:
        size, sha256 = save_upload(fileobj, tmp, settings.max_upload_mb * 1024 * 1024)
        pages = validate_pdf(tmp, settings.max_pdf_pages)
        existing = db.execute(
            select(Document).where(Document.sha256 == sha256, Document.status != "deleted")
        ).scalar_one_or_none()
        if existing is not None:
            raise DocumentError(
                "duplicate", f"This file has already been uploaded as '{existing.title}'.", 409
            )
        if details.kind == "wording":
            clash = db.execute(
                select(Document).where(
                    Document.product_id == product.id,
                    Document.kind == "wording",
                    Document.version_label == details.version_label.strip(),
                    Document.region == details.region.strip().upper(),
                    Document.status != "deleted",
                )
            ).scalar_one_or_none()
            if clash is not None:
                raise DocumentError(
                    "version_exists",
                    f"{product.name} version {clash.version_label} ({clash.region}) already exists. "
                    "Delete it first or use a new version label.",
                    409,
                )
        key = storage.key_for(doc_id)
        shutil.move(str(tmp), str(storage.path_for(key)))
    except InvalidPdf as exc:
        raise DocumentError(exc.code, exc.message, exc.status_code) from exc
    finally:
        tmp.unlink(missing_ok=True)

    doc = Document(
        id=doc_id,
        product_id=product.id,
        kind=details.kind,
        title=details.title.strip(),
        reference=details.reference,
        version_label=details.version_label.strip(),
        region=details.region.strip().upper(),
        effective_from=details.effective_from,
        effective_to=details.effective_to,
        applies_to_id=details.applies_to_id,
        allowed_roles=roles,
        original_filename=Path(filename or "document.pdf").name[:300],
        storage_key=key,
        size_bytes=size,
        sha256=sha256,
        page_count=pages,
        uploaded_by=uploaded_by,
    )
    db.add(doc)
    db.add(IngestionJob(document_id=doc_id))
    db.flush()
    audit.record(
        "document.uploaded", user_id=uploaded_by, target_id=doc_id, details={"title": doc.title}, db=db
    )
    return doc


def delete_document(db: Session, document: Document, deleted_by: uuid.UUID | None) -> None:
    """Remove the file, pages and chunks; keep the row (status 'deleted') so old citations can say so."""
    db.execute(delete(Chunk).where(Chunk.document_id == document.id))
    db.execute(delete(Page).where(Page.document_id == document.id))
    db.execute(
        update(IngestionJob)
        .where(IngestionJob.document_id == document.id, IngestionJob.status.in_(("queued", "running")))
        .values(status="failed", error_message="Document deleted.")
    )
    storage.delete(document.storage_key)
    document.status, document.storage_key = "deleted", None
    audit.record(
        "document.deleted",
        user_id=deleted_by,
        target_id=document.id,
        details={"title": document.title},
        db=db,
    )
