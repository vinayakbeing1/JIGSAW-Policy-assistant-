"""Policy documents: catalogue, upload, status, page text and the original PDF."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.deps import current_user, error, get_db, require
from app.models import Document, IngestionJob, Page, Product
from app.security import Perm, Principal
from app.services import storage
from app.services.documents import DocumentError, UploadDetails, create_document, delete_document

router = APIRouter(prefix="/api", tags=["documents"])


def _document(d: Document, job: IngestionJob | None = None) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "product_code": d.product.code,
        "product_name": d.product.name,
        "kind": d.kind,
        "title": d.title,
        "reference": d.reference,
        "version_label": d.version_label,
        "region": d.region,
        "effective_from": d.effective_from.isoformat(),
        "effective_to": d.effective_to.isoformat() if d.effective_to else None,
        "applies_to_id": str(d.applies_to_id) if d.applies_to_id else None,
        "allowed_roles": d.allowed_roles,
        "status": d.status,
        "error_message": d.error_message,
        "page_count": d.page_count,
        "ocr_pages": d.ocr_pages,
        "created_at": d.created_at.isoformat(),
        "job": {"status": job.status, "attempts": job.attempts, "error": job.error_message} if job else None,
    }


def _readable(db: Session, principal: Principal, document_id: uuid.UUID) -> Document:
    doc = db.get(Document, document_id)
    if doc is None or doc.status == "deleted" or not principal.can_read(doc.allowed_roles):
        raise error(404, "not_found", "Document not found.")
    return doc


@router.get("/catalog", summary="Products, regions and versions available for questions")
def catalog(principal: Principal = Depends(require(Perm.ASK)), db: Session = Depends(get_db)) -> dict:
    docs = db.execute(
        select(Document).where(Document.status == "ready").order_by(Document.effective_from)
    ).scalars()
    products: dict[str, dict[str, Any]] = {}
    for d in docs:
        if not principal.can_read(d.allowed_roles):
            continue
        p = products.setdefault(
            d.product.code, {"code": d.product.code, "name": d.product.name, "wordings": []}
        )
        if d.kind == "wording":
            p["wordings"].append(
                {
                    "id": str(d.id),
                    "version_label": d.version_label,
                    "region": d.region,
                    "effective_from": d.effective_from.isoformat(),
                    "effective_to": d.effective_to.isoformat() if d.effective_to else None,
                }
            )
    return {"products": sorted(products.values(), key=lambda p: p["name"])}


@router.get("/documents", summary="Documents you can read (admins also see failed and queued ones)")
def list_documents(principal: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    docs = db.execute(
        select(Document).where(Document.status != "deleted").order_by(Document.created_at.desc())
    ).scalars()
    visible = [
        d
        for d in docs
        if principal.can_read(d.allowed_roles) and (d.status == "ready" or principal.role == "admin")
    ]
    jobs = {
        j.document_id: j
        for j in db.execute(
            select(IngestionJob)
            .where(IngestionJob.document_id.in_([d.id for d in visible]))
            .order_by(IngestionJob.created_at)
        ).scalars()
    }
    return {"documents": [_document(d, jobs.get(d.id)) for d in visible]}


@router.post("/documents", status_code=202, summary="Upload a PDF; it is indexed in the background")
def upload_document(
    file: UploadFile = File(...),
    product_code: str = Form(..., max_length=32),
    kind: str = Form("wording"),
    title: str = Form(..., min_length=3, max_length=300),
    version_label: str = Form(..., min_length=1, max_length=32),
    region: str = Form("UK", min_length=2, max_length=8),
    effective_from: date = Form(...),
    effective_to: date | None = Form(None),
    applies_to_id: uuid.UUID | None = Form(None),
    allowed_roles: str | None = Form(None, description="Comma-separated roles; default: all roles"),
    reference: str | None = Form(None, max_length=100),
    principal: Principal = Depends(require(Perm.MANAGE_DOCUMENTS)),
    db: Session = Depends(get_db),
) -> dict:
    if file.content_type not in (None, "", "application/pdf", "application/octet-stream"):
        raise error(415, "not_a_pdf", "Only PDF files can be uploaded.")
    details = UploadDetails(
        product_code=product_code,
        kind=kind,
        title=title,
        version_label=version_label,
        region=region,
        effective_from=effective_from,
        effective_to=effective_to,
        applies_to_id=applies_to_id,
        allowed_roles=[r.strip() for r in allowed_roles.split(",") if r.strip()] if allowed_roles else None,
        reference=reference,
    )
    try:
        doc = create_document(db, file.file, file.filename or "document.pdf", details, principal.user_id)
    except DocumentError as exc:
        raise error(exc.status_code, exc.code, exc.message) from exc
    db.commit()
    return {"document": _document(doc)}


@router.get("/documents/{document_id}", summary="One document and its ingestion status")
def get_document(
    document_id: uuid.UUID, principal: Principal = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    doc = _readable(db, principal, document_id)
    job = db.execute(
        select(IngestionJob)
        .where(IngestionJob.document_id == doc.id)
        .order_by(IngestionJob.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return {"document": _document(doc, job)}


@router.delete("/documents/{document_id}", summary="Delete a document and remove it from search")
def remove_document(
    document_id: uuid.UUID,
    principal: Principal = Depends(require(Perm.MANAGE_DOCUMENTS)),
    db: Session = Depends(get_db),
) -> dict:
    delete_document(db, _readable(db, principal, document_id), principal.user_id)
    return {"deleted": True}


@router.post("/documents/{document_id}/retry", summary="Queue a failed document for ingestion again")
def retry_document(
    document_id: uuid.UUID,
    principal: Principal = Depends(require(Perm.MANAGE_DOCUMENTS)),
    db: Session = Depends(get_db),
) -> dict:
    doc = _readable(db, principal, document_id)
    if doc.status != "failed":
        raise error(409, "not_failed", "Only failed documents can be retried.")
    db.execute(
        update(IngestionJob)
        .where(IngestionJob.document_id == doc.id)
        .values(status="queued", attempts=0, run_after=func.now(), error_message=None, finished_at=None)
    )
    doc.status, doc.error_message = "queued", None
    return {"document": _document(doc)}


@router.get("/documents/{document_id}/pages/{page_number}", summary="Extracted text of one page")
def get_page(
    document_id: uuid.UUID,
    page_number: int,
    principal: Principal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    doc = _readable(db, principal, document_id)
    page = db.get(Page, (doc.id, page_number))
    if page is None:
        raise error(404, "page_not_found", "Page not found.")
    return {"document_id": str(doc.id), "title": doc.title, "page_number": page_number, "page_count": doc.page_count,
            "text": page.text, "ocr": page.ocr}  # fmt: skip


@router.get("/documents/{document_id}/file", summary="The original PDF")
def get_file(
    document_id: uuid.UUID, principal: Principal = Depends(current_user), db: Session = Depends(get_db)
) -> FileResponse:
    doc = _readable(db, principal, document_id)
    path = storage.path_for(doc.storage_key) if doc.storage_key else None
    if path is None or not path.exists():
        raise error(404, "file_missing", "The file is not available.")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=doc.original_filename,
        content_disposition_type="inline",
        headers={"Content-Security-Policy": "frame-ancestors 'self'", "X-Frame-Options": "SAMEORIGIN"},
    )


@router.get("/products", summary="Product codes for uploads")
def list_products(principal: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(select(Product).order_by(Product.name)).scalars()
    return {
        "products": [{"code": p.code, "name": p.name, "line_of_business": p.line_of_business} for p in rows]
    }
