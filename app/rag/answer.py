"""Answering a question: scope -> follow-up rewrite -> hybrid retrieval and reranking -> structured draft ->
verification.

Used by the chat API and by the evaluation harness, so both measure exactly the same pipeline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from langchain_core.documents import Document as LCDocument
from sqlalchemy.orm import Session

from app.config import get_settings
from app.log import get_logger
from app.rag import llm, rerank
from app.rag.grounding import summary_is_safe, verify
from app.rag.prompts import PROMPT_VERSION, answer_prompt, escape, rewrite_prompt
from app.rag.retriever import PolicyRetriever
from app.rag.schemas import DraftAnswer, StandaloneQuestion
from app.rag.scope import Scope, Selection, load_documents, resolve
from app.security import Principal

log = get_logger(__name__)

DISCLAIMER = (
    "For internal reference only. This summarises policy wording; it is not a coverage or claim decision. "
    "Check the customer's schedule and endorsements before relying on it."
)

ABSTAIN_MESSAGES = {
    "no_documents": "None of the documents available to your role cover this product.",
    "no_sources": "I couldn't find anything in the selected documents that answers this.",
    "not_found": "The selected documents don't answer this question.",
    "unverified": "I couldn't verify an answer against the policy wording, so I'm not giving one.",
}


@dataclass
class Turn:
    question: str
    answer: str


@dataclass
class AnswerResult:
    status: str  # answered | abstained | clarification | error
    answer: dict[str, Any]
    trace: dict[str, Any] = field(default_factory=dict)
    retrieved: list[LCDocument] = field(default_factory=list)
    usage: llm.Usage = field(default_factory=llm.Usage)
    latency_ms: int = 0
    model: str | None = None

    @property
    def text(self) -> str:
        """Plain-text version of the answer, stored as the message content."""
        a = self.answer
        if self.status == "clarification":
            return a["clarification"]["question"]
        if self.status != "answered":
            return a.get("message", "")
        lines = [a["summary"], ""] + [f"- {c['text']} [{', '.join(c['source_ids'])}]" for c in a["claims"]]
        return "\n".join(lines).strip()


def answer_question(
    db: Session,
    principal: Principal,
    question: str,
    selection: Selection,
    history: list[Turn] | None = None,
    today: date | None = None,
) -> AnswerResult:
    started = time.perf_counter()
    settings = get_settings()
    scope = resolve(load_documents(db, principal), selection, question, today or date.today())
    with llm.track_usage() as usage:
        try:
            result = _run(db, scope, question, history or [])
            result.model = settings.openai_chat_model
        except Exception as exc:
            error = llm.model_error(exc)
            log.warning("answer_failed", code=error.code, error=type(exc).__name__)
            result = AnswerResult(
                "error",
                {
                    "status": "error",
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                },
            )
    result.usage = usage
    result.latency_ms = int((time.perf_counter() - started) * 1000)
    result.trace = {"prompt_version": PROMPT_VERSION, "scope": scope.as_dict(), **result.trace}
    return result


def _run(db: Session, scope: Scope, question: str, history: list[Turn]) -> AnswerResult:
    if scope.status == "clarify":
        clarification = {
            "question": scope.question,
            "options": [{"label": o.label, "selection": o.selection} for o in scope.options],
        }
        return AnswerResult("clarification", {"status": "clarification", "clarification": clarification})
    if scope.status == "no_documents":
        return _abstain(scope, "no_documents")

    settings = get_settings()
    chat = llm.chat_model()
    standalone = question
    if history:
        rewrite = rewrite_prompt | chat.with_structured_output(
            StandaloneQuestion, method="json_schema", strict=True
        )
        turns = "\n".join(f"User: {escape(t.question)}\nAssistant: {escape(t.answer)}" for t in history[-3:])
        standalone = rewrite.invoke({"history": turns, "question": escape(question)}).question or question

    retriever = PolicyRetriever(
        session=db,
        embedder=llm.embeddings(),
        document_ids=[d.id for d in scope.documents],
        top_k=settings.retrieval_top_k,
        candidates=settings.retrieval_candidates,
        min_similarity=settings.retrieval_min_similarity,
        reranker=rerank.reranker(),
        rerank_candidates=settings.reranker_candidates,
    )
    retrieved = retriever.invoke(standalone)
    trace: dict[str, Any] = {
        "standalone_question": standalone if standalone != question else None,
        "reranker": retriever.reranker.name if retriever.reranker else None,
        "retrieved": [{"id": f"S{i}", **d.metadata} for i, d in enumerate(retrieved, 1)],
    }
    if not retrieved:
        return _abstain(scope, "no_sources", trace=trace)

    sources = {f"S{i}": d for i, d in enumerate(retrieved, 1)}
    documents = _render_scope(scope)
    draft_chain = answer_prompt | chat.with_structured_output(DraftAnswer, method="json_schema", strict=True)
    draft: DraftAnswer = draft_chain.invoke(
        {"documents": documents, "sources": _render_sources(sources), "question": escape(standalone)}
    )
    trace["draft_status"] = draft.status
    if draft.status == "not_found" or not draft.claims:
        return _abstain(
            scope, "not_found", missing=draft.missing_information, trace=trace, retrieved=retrieved
        )

    # Numbers may also come from the question or the scope (e.g. "version 2026.1", the incident date).
    allowed_text = f"{question}\n{standalone}\n{documents}"
    verification = verify(draft, sources, allowed_text, chat if settings.grounding_judge else None)
    trace["removed_claims"] = [{"text": c.claim.text, "problems": c.problems} for c in verification.removed]
    kept = verification.kept
    if not kept:
        return _abstain(scope, "unverified", trace=trace, retrieved=retrieved)

    cited = list(dict.fromkeys(s for c in kept for s in c.source_ids))
    summary = draft.summary if summary_is_safe(draft.summary, kept, sources, allowed_text) else kept[0].text
    answer = {
        "status": "answered",
        "summary": summary,
        "claims": [c.model_dump() for c in kept],
        "sources": {sid: _citation(sources[sid]) for sid in cited},
        "documents": [d.as_dict() for d in scope.documents],
        "notes": scope.notes,
        "missing_information": draft.missing_information,
        "verification": {
            "claims_checked": len(verification.checks),
            "claims_removed": len(verification.removed),
            "judge": verification.judge_used,
        },
        "disclaimer": DISCLAIMER,
    }
    return AnswerResult("answered", answer, trace=trace, retrieved=retrieved)


def _abstain(
    scope: Scope,
    reason: str,
    missing: str = "",
    trace: dict[str, Any] | None = None,
    retrieved: list[LCDocument] | None = None,
) -> AnswerResult:
    answer = {
        "status": "abstained",
        "reason": reason,
        "message": ABSTAIN_MESSAGES[reason],
        "missing_information": missing,
        "documents": [d.as_dict() for d in scope.documents],
        "notes": scope.notes,
        "disclaimer": DISCLAIMER,
    }
    return AnswerResult("abstained", answer, trace=trace or {}, retrieved=retrieved or [])


def _render_scope(scope: Scope) -> str:
    kinds = {"wording": "policy wording", "endorsement": "endorsement", "guide": "internal guide"}
    lines = [
        f"- {d.title}, version {d.version_label} ({d.region}), {kinds[d.kind]}, effective from "
        f"{d.effective_from:%d %B %Y}"
        for d in scope.documents
    ]
    if scope.as_of:
        lines.append(f"Incident date given by the user: {scope.as_of:%d %B %Y}")
    return escape("\n".join(lines))


def _render_sources(sources: dict[str, LCDocument]) -> str:
    blocks = []
    for sid, doc in sources.items():
        m = doc.metadata
        pages = (
            str(m["page_start"]) if m["page_start"] == m["page_end"] else f"{m['page_start']}-{m['page_end']}"
        )
        attrs = (
            f'id="{sid}" document="{escape(m["title"])} {m["version_label"]} ({m["region"]})" '
            f'type="{m["kind"]}" section="{escape(m["section"])}" pages="{pages}"'
        )
        blocks.append(f"<source {attrs}>\n{escape(doc.page_content)}\n</source>")
    return "\n".join(blocks)


def _citation(doc: LCDocument) -> dict[str, Any]:
    m = doc.metadata
    keys = (
        "chunk_id",
        "document_id",
        "title",
        "kind",
        "version_label",
        "region",
        "section",
        "page_start",
        "page_end",
    )
    return {**{k: m[k] for k in keys}, "excerpt": doc.page_content}
