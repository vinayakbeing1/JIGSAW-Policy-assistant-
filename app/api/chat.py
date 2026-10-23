"""Conversations, questions, citations and feedback."""

from __future__ import annotations

import itertools
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import error, get_db, require
from app.models import Conversation, Document, Feedback, Message, Page
from app.rag.answer import Turn, answer_question
from app.rag.scope import Selection
from app.security import Perm, Principal

router = APIRouter(prefix="/api", tags=["chat"])


class NewConversation(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    selection: Selection = Field(default_factory=Selection)


class FeedbackIn(BaseModel):
    rating: Literal[1, -1]
    comment: str | None = Field(default=None, max_length=1000)


def _conversation(db: Session, principal: Principal, conversation_id: uuid.UUID) -> Conversation:
    conv = db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != principal.user_id:
        raise error(404, "not_found", "Conversation not found.")
    return conv


def _message(db: Session, principal: Principal, message_id: uuid.UUID) -> Message:
    msg = db.get(Message, message_id)
    if msg is None or _conversation(db, principal, msg.conversation_id) is None:
        raise error(404, "not_found", "Message not found.")
    return msg


def _out(m: Message, rating: int | None = None) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "role": m.role,
        "content": m.content,
        "status": m.status,
        "selection": m.selection,
        "answer": m.answer,
        "latency_ms": m.latency_ms,
        "created_at": m.created_at.isoformat(),
        "rating": rating,
    }


@router.get("/conversations", summary="Your conversations, newest first")
def list_conversations(
    principal: Principal = Depends(require(Perm.ASK)), db: Session = Depends(get_db)
) -> dict:
    rows = db.execute(
        select(Conversation)
        .where(Conversation.user_id == principal.user_id)
        .order_by(Conversation.updated_at.desc())
        .limit(100)
    ).scalars()
    return {
        "conversations": [
            {"id": str(c.id), "title": c.title, "updated_at": c.updated_at.isoformat()} for c in rows
        ]
    }


@router.post("/conversations", status_code=201, summary="Start a conversation")
def create_conversation(
    body: NewConversation, principal: Principal = Depends(require(Perm.ASK)), db: Session = Depends(get_db)
) -> dict:
    conv = Conversation(user_id=principal.user_id, title=(body.title or "New conversation").strip())
    db.add(conv)
    db.flush()
    return {"id": str(conv.id), "title": conv.title}


@router.get("/conversations/{conversation_id}", summary="A conversation with its messages")
def get_conversation(
    conversation_id: uuid.UUID,
    principal: Principal = Depends(require(Perm.ASK)),
    db: Session = Depends(get_db),
) -> dict:
    conv = _conversation(db, principal, conversation_id)
    messages = (
        db.execute(select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at))
        .scalars()
        .all()
    )
    ratings = dict(
        db.execute(
            select(Feedback.message_id, Feedback.rating).where(
                Feedback.user_id == principal.user_id, Feedback.message_id.in_([m.id for m in messages])
            )
        ).all()
    )
    return {
        "id": str(conv.id),
        "title": conv.title,
        "messages": [_out(m, ratings.get(m.id)) for m in messages],
    }


@router.delete("/conversations/{conversation_id}", summary="Delete a conversation")
def delete_conversation(
    conversation_id: uuid.UUID,
    principal: Principal = Depends(require(Perm.ASK)),
    db: Session = Depends(get_db),
) -> dict:
    db.delete(_conversation(db, principal, conversation_id))
    return {"deleted": True}


@router.post("/conversations/{conversation_id}/messages", summary="Ask a question")
def ask(
    conversation_id: uuid.UUID,
    body: Question,
    principal: Principal = Depends(require(Perm.ASK)),
    db: Session = Depends(get_db),
) -> dict:
    conv = _conversation(db, principal, conversation_id)
    question = body.question.strip()
    previous = (
        db.execute(select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at))
        .scalars()
        .all()
    )
    history = [
        Turn(q.content, a.answer.get("summary", ""))
        for q, a in itertools.pairwise(previous)
        if q.role == "user" and a.role == "assistant" and a.status == "answered" and a.answer
    ]
    selection = body.selection.model_dump(mode="json")
    db.add(Message(conversation_id=conv.id, role="user", content=question, selection=selection))
    db.commit()  # keep the question even if answering fails

    result = answer_question(db, principal, question, body.selection, history)
    reply = Message(
        conversation_id=conv.id,
        role="assistant",
        content=result.text,
        status=result.status,
        selection=selection,
        answer=result.answer,
        trace=result.trace,
        model=result.model,
        latency_ms=result.latency_ms,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cost_usd=round(result.usage.cost_usd, 6),
    )
    db.add(reply)
    if conv.title == "New conversation":
        conv.title = question[:80]
    conv.updated_at = datetime.now(UTC)
    db.flush()
    return {"message": _out(reply)}


@router.get("/messages/{message_id}/sources/{source_id}", summary="The page behind a citation")
def citation(
    message_id: uuid.UUID,
    source_id: str,
    principal: Principal = Depends(require(Perm.ASK)),
    db: Session = Depends(get_db),
) -> dict:
    msg = _message(db, principal, message_id)
    source = ((msg.answer or {}).get("sources") or {}).get(source_id)
    if source is None:
        raise error(404, "not_found", "Citation not found.")
    doc = db.get(Document, uuid.UUID(source["document_id"]))
    if doc is None or doc.status != "ready":
        raise error(410, "source_removed", "The cited document has since been removed or replaced.")
    if not principal.can_read(doc.allowed_roles):
        raise error(403, "forbidden", "You no longer have access to this document.")
    quote = next(
        (c["quote"] for c in msg.answer.get("claims", []) if source_id in c["source_ids"] and c["quote"]),
        None,
    )
    pages = (
        db.execute(
            select(Page)
            .where(
                Page.document_id == doc.id, Page.page_number.between(source["page_start"], source["page_end"])
            )
            .order_by(Page.page_number)
        )
        .scalars()
        .all()
    )
    needle = quote or source["excerpt"][:120]
    page = next((p for p in pages if _find(p.text, needle)), pages[0] if pages else None)
    if page is None:
        raise error(404, "page_not_found", "The cited page is not available.")
    return {
        "source_id": source_id,
        "document": {
            "id": str(doc.id),
            "title": doc.title,
            "version_label": doc.version_label,
            "region": doc.region,
        },
        "section": source["section"],
        "page_number": page.page_number,
        "page_count": doc.page_count,
        "page_text": page.text,
        "highlight": _find(page.text, needle),
        "ocr": page.ocr,
    }


def _find(haystack: str, needle: str) -> dict[str, int] | None:
    """Character span of ``needle`` in ``haystack``, ignoring case and whitespace differences."""
    words = [re.escape(w) for w in needle.split()[:25]]
    if len(words) < 3:
        return None
    m = re.search(r"\s+".join(words), haystack, re.I)
    return {"start": m.start(), "end": m.end()} if m else None


@router.put("/messages/{message_id}/feedback", summary="Rate an answer")
def put_feedback(
    message_id: uuid.UUID,
    body: FeedbackIn,
    principal: Principal = Depends(require(Perm.ASK)),
    db: Session = Depends(get_db),
) -> dict:
    msg = _message(db, principal, message_id)
    if msg.role != "assistant":
        raise error(422, "not_an_answer", "Only answers can be rated.")
    fb = db.execute(
        select(Feedback).where(Feedback.message_id == msg.id, Feedback.user_id == principal.user_id)
    ).scalar_one_or_none()
    if fb is None:
        fb = Feedback(message_id=msg.id, user_id=principal.user_id, rating=body.rating)
        db.add(fb)
    fb.rating, fb.comment, fb.created_at = body.rating, body.comment, func.now()
    return {"rating": body.rating}
