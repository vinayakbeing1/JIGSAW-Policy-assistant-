import io
import json
from datetime import date

import pytest
from sqlalchemy import func, select

from app.config import PROJECT_ROOT
from app.ingestion.worker import drain
from app.models import Chunk, Document, IngestionJob, Page
from app.rag.retriever import PolicyRetriever
from app.services.documents import DocumentError, UploadDetails, create_document
from tests.fakes import HashEmbeddings

pytestmark = pytest.mark.integration
MANIFEST = json.loads((PROJECT_ROOT / "sample_data" / "manifest.json").read_text())


def test_every_sample_document_is_ready(db):
    references = [item["reference"] for item in MANIFEST]
    docs = db.execute(select(Document).where(Document.reference.in_(references))).scalars().all()
    assert len(docs) == len(MANIFEST)
    assert {d.status for d in docs} == {"ready"}
    for d in docs:
        chunks = db.execute(select(func.count()).where(Chunk.document_id == d.id)).scalar_one()
        pages = db.execute(select(func.count()).where(Page.document_id == d.id)).scalar_one()
        assert chunks > 0 and pages == d.page_count


def test_scanned_endorsement_was_indexed_through_ocr(db):
    doc = db.execute(select(Document).where(Document.reference == "JIG-MOTOR-END-M07")).scalar_one()
    assert doc.ocr_pages == 1
    texts = db.execute(select(Chunk.text).where(Chunk.document_id == doc.id)).scalars().all()
    assert any("£1,000" in t for t in texts)


def test_endorsement_is_linked_to_its_wording(db):
    he12 = db.execute(select(Document).where(Document.reference == "JIG-HOME-END-HE12")).scalar_one()
    wording = db.get(Document, he12.applies_to_id)
    assert (wording.version_label, wording.region) == ("2026.1", "UK")


def test_hybrid_retrieval_finds_the_excess_table(db):
    wording = db.execute(select(Document).where(Document.reference == "JIG-HOME-PW-2026.1-UK")).scalar_one()
    retriever = PolicyRetriever(session=db, embedder=HashEmbeddings(), document_ids=[wording.id], top_k=3)
    results = retriever.invoke("What is the escape of water excess?")
    assert results[0].metadata["section"] == "Excesses" or any("£350" in r.page_content for r in results)
    assert all(r.metadata["document_id"] == str(wording.id) for r in results)
    assert "keyword_rank" in results[0].metadata or "semantic_rank" in results[0].metadata


class ReverseReranker:
    """Prefers the candidates the hybrid search ranked lowest, so reordering is visible."""

    name = "reverse"

    def score(self, query, passages):
        return [float(i) for i in range(len(passages))]


def test_reranker_reorders_the_fused_candidates(db):
    wording = db.execute(select(Document).where(Document.reference == "JIG-HOME-PW-2026.1-UK")).scalar_one()
    retriever = PolicyRetriever(
        session=db,
        embedder=HashEmbeddings(),
        document_ids=[wording.id],
        top_k=3,
        reranker=ReverseReranker(),
        rerank_candidates=8,
    )
    results = retriever.invoke("What is the escape of water excess?")
    ranks = [r.metadata["fused_rank"] for r in results]
    assert len(results) == 3 and ranks == sorted(ranks, reverse=True)
    assert ranks[0] > 3  # came from beyond the hybrid search's own top 3
    assert all("rerank_score" in r.metadata for r in results)


def test_failing_reranker_keeps_the_fused_order(db):
    class Broken:
        name = "broken"

        def score(self, query, passages):
            raise RuntimeError("onnxruntime error")

    wording = db.execute(select(Document).where(Document.reference == "JIG-HOME-PW-2026.1-UK")).scalar_one()
    retriever = PolicyRetriever(
        session=db, embedder=HashEmbeddings(), document_ids=[wording.id], top_k=3, reranker=Broken()
    )
    results = retriever.invoke("What is the escape of water excess?")
    assert [r.metadata["fused_rank"] for r in results] == [1, 2, 3]
    assert not any("rerank_score" in r.metadata for r in results)


def test_duplicate_upload_is_rejected(db):
    item = MANIFEST[0]
    pdf = (PROJECT_ROOT / "sample_data" / "pdfs" / item["filename"]).read_bytes()
    details = UploadDetails("HOME", "wording", "Copy", "9.9", "UK", effective_from=date(2030, 1, 1))
    with pytest.raises(DocumentError) as exc:
        create_document(db, io.BytesIO(pdf), "copy.pdf", details, None)
    assert exc.value.code == "duplicate"


def test_failed_embedding_is_retried_then_reported(db, fake_models):
    from app.rag import llm

    class BrokenEmbeddings(HashEmbeddings):
        def embed_documents(self, texts):
            raise TimeoutError("embeddings API timed out")

    pdf = (PROJECT_ROOT / "sample_data" / "pdfs" / "travel-2026-uk.pdf").read_bytes() + b"\n% changed\n"
    doc = create_document(
        db,
        io.BytesIO(pdf),
        "travel.pdf",
        UploadDetails("TRAVEL", "wording", "Travel draft", "2099.1", "UK", date(2099, 1, 1)),
        None,
    )
    db.commit()
    llm.use_models(fake_models, BrokenEmbeddings())
    assert drain() == {"retrying": 1}  # unexpected errors are retried later
    db.execute(
        IngestionJob.__table__.update()
        .where(IngestionJob.document_id == doc.id)
        .values(run_after=func.now(), attempts=3)
    )
    db.commit()
    assert drain() == {"failed": 1}
    db.refresh(doc)
    assert doc.status == "failed" and doc.error_message
    db.delete(doc)
    db.commit()
