"""Hybrid retrieval as a LangChain retriever.

Two searches run over the chunks of the documents in scope only (access control and version selection are
already applied by app.rag.scope):

* semantic - pgvector cosine similarity between the question embedding and chunk embeddings;
* keyword  - PostgreSQL full-text search (English stemming), any-term match ranked by ts_rank_cd.

Results are merged with reciprocal rank fusion (RRF). Semantic hits below ``min_similarity`` are dropped,
so an unrelated question can come back with no sources at all and the assistant says it cannot answer.
When a ``reranker`` is given (app.rag.rerank), the best ``rerank_candidates`` fused hits are re-scored by
the cross-encoder and the top ``top_k`` by that score are returned; if scoring fails the fused order is kept.
The document scope is small (one version plus its endorsements and guides), so the vector search is an
exact scan and needs no approximate index.
"""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document as LCDocument
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.log import get_logger
from app.models import Chunk, Document, Product
from app.rag.rerank import Reranker

log = get_logger(__name__)

RRF_K = 60

_KEYWORD_SQL = text(
    """
    WITH q AS (
        SELECT to_tsquery('english', string_agg(quote_literal(lexeme), ' | ')) AS query
        FROM unnest(to_tsvector('english', :question))
    )
    SELECT c.id, ts_rank_cd(c.search, q.query, 1) AS rank
    FROM chunks c, q
    WHERE q.query IS NOT NULL AND c.document_id = ANY(:document_ids) AND c.search @@ q.query
    ORDER BY rank DESC, c.id
    LIMIT :limit
    """
)


class PolicyRetriever(BaseRetriever):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: Session
    embedder: Embeddings
    document_ids: list[uuid.UUID]
    top_k: int = 6
    candidates: int = 30
    min_similarity: float = 0.25
    reranker: Reranker | None = None
    rerank_candidates: int = 20

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[LCDocument]:
        if not self.document_ids:
            return []
        vector = self.embedder.embed_query(query)
        distance = Chunk.embedding.cosine_distance(vector)
        semantic = [
            (chunk_id, 1.0 - float(dist))
            for chunk_id, dist in self.session.execute(
                select(Chunk.id, distance)
                .where(Chunk.document_id.in_(self.document_ids))
                .order_by(distance, Chunk.id)
                .limit(self.candidates)
            )
        ]
        semantic = [(cid, sim) for cid, sim in semantic if sim >= self.min_similarity]
        keyword = [
            (row.id, float(row.rank))
            for row in self.session.execute(
                _KEYWORD_SQL,
                {"question": query, "document_ids": self.document_ids, "limit": self.candidates},
            )
        ]

        scores: dict[uuid.UUID, dict[str, Any]] = {}
        for name, hits in (("semantic", semantic), ("keyword", keyword)):
            for rank, (chunk_id, value) in enumerate(hits, start=1):
                entry = scores.setdefault(chunk_id, {"rrf": 0.0})
                entry["rrf"] += 1.0 / (RRF_K + rank)
                entry[f"{name}_rank"] = rank
                entry["similarity" if name == "semantic" else "keyword_score"] = round(value, 4)
        fused = sorted(scores, key=lambda cid: (-scores[cid]["rrf"], str(cid)))
        for rank, chunk_id in enumerate(fused, start=1):
            scores[chunk_id]["fused_rank"] = rank
        pool = fused[: max(self.top_k, self.rerank_candidates) if self.reranker else self.top_k]
        if not pool:
            return []

        rows = self.session.execute(
            select(Chunk, Document, Product)
            .join(Document, Document.id == Chunk.document_id)
            .join(Product, Product.id == Document.product_id)
            .where(Chunk.id.in_(pool))
        ).all()
        by_id = {chunk.id: (chunk, doc, product) for chunk, doc, product in rows}
        best = (self._rerank(query, pool, by_id, scores) if self.reranker else pool)[: self.top_k]
        results = []
        for chunk_id in best:
            chunk, doc, product = by_id[chunk_id]
            results.append(
                LCDocument(
                    page_content=chunk.text,
                    metadata={
                        "chunk_id": str(chunk.id),
                        "document_id": str(doc.id),
                        "product_code": product.code,
                        "title": doc.title,
                        "kind": doc.kind,
                        "version_label": doc.version_label,
                        "region": doc.region,
                        "section": chunk.section,
                        "page_start": chunk.page_start,
                        "page_end": chunk.page_end,
                        **{
                            k: round(v, 5) if isinstance(v, float) else v for k, v in scores[chunk_id].items()
                        },
                    },
                )
            )
        return results

    def _rerank(
        self,
        query: str,
        pool: list[uuid.UUID],
        by_id: dict[uuid.UUID, tuple[Chunk, Document, Product]],
        scores: dict[uuid.UUID, dict[str, Any]],
    ) -> list[uuid.UUID]:
        assert self.reranker is not None
        passages = [f"{by_id[cid][0].section}\n{by_id[cid][0].text}".strip() for cid in pool]
        try:
            values = self.reranker.score(query, passages)
            if len(values) != len(pool):
                raise ValueError(f"expected {len(pool)} scores, got {len(values)}")
        except Exception as exc:
            log.warning("rerank_failed", reranker=self.reranker.name, error=type(exc).__name__)
            return pool
        for chunk_id, value in zip(pool, values, strict=True):
            scores[chunk_id]["rerank_score"] = float(value)
        return sorted(pool, key=lambda cid: (-scores[cid]["rerank_score"], scores[cid]["fused_rank"]))
