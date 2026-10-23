"""Which documents a question is about.

Answers must come from exactly one policy wording version, plus the endorsements and internal guides that go
with it. This is decided here, in code, from the user's selection and the question text:

1. product   - selected, or recognised in the question ("my car", "travel"); otherwise ask.
2. region    - selected, recognised ("Ireland", "€"), or the only one available; otherwise ask.
3. version   - the version in force on the incident date if one is given (selected or written in the question,
               e.g. "a pipe burst on 10 June 2025"); otherwise the version in force today.
4. extras    - endorsements attached to that version and in force on the same date, and guides for the
               product and region that the user's role may read.

When something is ambiguous the result is a clarification with options; each option is a change to the
selection that the UI sends back with the same question.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document
from app.security import Principal


class Selection(BaseModel):
    product_code: str | None = None
    region: str | None = None
    document_id: uuid.UUID | None = None  # a specific wording version
    incident_date: date | None = None
    ignore_date: bool = False  # answer from the selected version even if it was not in force on the date


@dataclass(frozen=True)
class DocInfo:
    id: uuid.UUID
    product_code: str
    product_name: str
    kind: str
    title: str
    version_label: str
    region: str
    effective_from: date
    effective_to: date | None
    applies_to_id: uuid.UUID | None

    def in_force(self, on: date) -> bool:
        return self.effective_from <= on and (self.effective_to is None or on <= self.effective_to)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "product_code": self.product_code,
            "kind": self.kind,
            "title": self.title,
            "version_label": self.version_label,
            "region": self.region,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
        }


@dataclass
class Option:
    label: str
    selection: dict[str, Any]


@dataclass
class Scope:
    status: str  # ok | clarify | no_documents
    wording: DocInfo | None = None
    attachments: list[DocInfo] = field(default_factory=list)
    as_of: date | None = None
    notes: list[str] = field(default_factory=list)
    question: str | None = None
    options: list[Option] = field(default_factory=list)

    @property
    def documents(self) -> list[DocInfo]:
        return ([self.wording] if self.wording else []) + self.attachments

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "documents": [d.as_dict() for d in self.documents],
            "notes": self.notes,
        }


def load_documents(db: Session, principal: Principal) -> list[DocInfo]:
    """Ready documents the user's role may read."""
    docs = db.execute(select(Document).where(Document.status == "ready")).scalars().all()
    return [
        DocInfo(
            d.id,
            d.product.code,
            d.product.name,
            d.kind,
            d.title,
            d.version_label,
            d.region,
            d.effective_from,
            d.effective_to,
            d.applies_to_id,
        )
        for d in docs
        if principal.can_read(d.allowed_roles)
    ]


PRODUCT_KEYWORDS = {
    "HOME": ("home insurance", "house", "buildings", "contents", "household", "home emergency", "subsidence"),
    "MOTOR": (
        "car",
        "motor",
        "vehicle",
        "driver",
        "driving",
        "windscreen",
        "telematics",
        "no claim discount",
    ),
    "TRAVEL": ("travel", "trip", "holiday", "baggage", "luggage", "flight"),
    "SHOP": ("shop", "salon", "small business", "business interruption", "stock", "premises"),
}
_NORTHERN_IRELAND = re.compile(r"\bnorthern ireland\b", re.I)
_IRELAND = re.compile(r"\b(republic of ireland|ireland|irish|roi)\b|€", re.I)
_UK = re.compile(r"\b(uk|united kingdom|england|scotland|wales|northern ireland|britain)\b|£", re.I)

_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
_DATE_PATTERNS = [
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)? ({_MONTHS}) (\d{{4}})\b", re.I), "{0} {1} {2}", "%d %B %Y"),
    (
        re.compile(rf"\b({_MONTHS}) (\d{{1,2}})(?:st|nd|rd|th)?,? (\d{{4}})\b", re.I),
        "{1} {0} {2}",
        "%d %B %Y",
    ),
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "{0}-{1}-{2}", "%Y-%m-%d"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "{0}/{1}/{2}", "%d/%m/%Y"),  # UK day/month/year
]


def find_date(text: str) -> date | None:
    for pattern, fmt, parse in _DATE_PATTERNS:
        if m := pattern.search(text):
            try:
                return datetime.strptime(fmt.format(*m.groups()), parse).date()
            except ValueError:
                continue
    return None


def detect_product(question: str, codes: set[str]) -> str | None:
    q = f" {question.lower()} "
    found = {
        code
        for code, words in PRODUCT_KEYWORDS.items()
        if code in codes and any(re.search(rf"\b{re.escape(w)}s?\b", q) for w in words)
    }
    return found.pop() if len(found) == 1 else None


def detect_region(question: str) -> str | None:
    uk = bool(_UK.search(question))
    ie = bool(_IRELAND.search(_NORTHERN_IRELAND.sub("", question)))
    if uk != ie:
        return "UK" if uk else "IE"
    return None


def _fmt(d: date) -> str:
    return f"{d.day} {d:%B %Y}"


def resolve(docs: list[DocInfo], selection: Selection, question: str, today: date) -> Scope:
    if not docs:
        return Scope("no_documents")
    products = {d.product_code: d.product_name for d in docs}

    code = selection.product_code if selection.product_code in products else None
    code = (
        code
        or detect_product(question, set(products))
        or (next(iter(products)) if len(products) == 1 else None)
    )
    if code is None:
        return Scope(
            "clarify",
            question="Which product is your question about?",
            options=[Option(name, {"product_code": c}) for c, name in sorted(products.items())],
        )

    as_of = selection.incident_date or find_date(question)
    wordings = [d for d in docs if d.product_code == code and d.kind == "wording"]
    if not wordings:  # e.g. only internal guides are available for this product
        guides = [d for d in docs if d.product_code == code and d.kind == "guide"]
        return Scope("ok", attachments=guides, as_of=as_of) if guides else Scope("no_documents")

    notes: list[str] = []
    chosen = next((w for w in wordings if w.id == selection.document_id), None)
    if chosen is not None:
        if as_of and not chosen.in_force(as_of) and not selection.ignore_date:
            in_force = [w for w in wordings if w.region == chosen.region and w.in_force(as_of)]
            return Scope(
                "clarify",
                question=(
                    f"Version {chosen.version_label} was not in force on {_fmt(as_of)} (it applies from "
                    f"{_fmt(chosen.effective_from)}). Which version should I use?"
                ),
                options=[Option(f"Version {w.version_label} (in force on that date)", {"document_id": str(w.id)})
                         for w in in_force]
                + [Option(f"Use version {chosen.version_label} anyway", {"ignore_date": True})],
            )  # fmt: skip
    else:
        regions = sorted({w.region for w in wordings})
        if len(regions) == 1:
            region = regions[0]
        elif selection.region in regions:
            region = selection.region
        else:
            region = detect_region(question)
        if region not in regions:
            return Scope(
                "clarify",
                question=f"Which region's {products[code]} wording do you need?",
                options=[Option({"UK": "United Kingdom", "IE": "Republic of Ireland"}.get(r, r), {"region": r})
                         for r in regions],
            )  # fmt: skip
        versions = sorted((w for w in wordings if w.region == region), key=lambda w: w.effective_from)
        on = as_of or today
        in_force = [w for w in versions if w.in_force(on)]
        if as_of and not in_force:
            return Scope(
                "clarify",
                question=f"No {products[code]} wording ({region}) was in force on {_fmt(as_of)}. Which version should I use?",
                options=[Option(f"Version {w.version_label} (from {_fmt(w.effective_from)})",
                                {"document_id": str(w.id), "ignore_date": True}) for w in versions],
            )  # fmt: skip
        chosen = in_force[-1] if in_force else versions[-1]
        if not as_of and len(versions) > 1:
            notes.append(
                f"Answered from version {chosen.version_label}, in force from {_fmt(chosen.effective_from)}. "
                "If the incident happened before then, give the incident date or choose an earlier version."
            )

    on = as_of or today
    attachments = []
    for d in docs:
        if d.kind == "endorsement" and d.applies_to_id == chosen.id:
            if d.in_force(on):
                attachments.append(d)
            else:
                notes.append(f"{d.title} was not applied: it is effective from {_fmt(d.effective_from)}.")
        elif d.kind == "guide" and d.product_code == code and d.region == chosen.region:
            attachments.append(d)
    return Scope("ok", wording=chosen, attachments=attachments, as_of=as_of, notes=notes)
