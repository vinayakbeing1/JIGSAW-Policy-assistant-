"""Verification of model answers against the retrieved sources.

A claim is kept only if all of these hold:

1. it cites at least one source, and every cited source was actually given to the model;
2. its quote, if any, appears word for word in one of the cited sources;
3. every number in it (amounts, limits, days, percentages) appears in a cited source, or in the user's
   question or the description of the documents in scope (version labels, effective dates);
4. it contains no coverage decision ("your claim will be paid") and no instruction-like text;
5. the LLM judge, when enabled, agrees that the cited sources support it.

Claims that fail are removed; if nothing is left the assistant says it could not find a supported answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from langchain_core.documents import Document as LCDocument
from langchain_core.language_models import BaseChatModel

from app.rag.prompts import escape, judge_prompt
from app.rag.schemas import Claim, DraftAnswer, JudgeResult

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
DECISION_LANGUAGE = re.compile(
    r"\b(your|the|this) claim (is|has been|will be|would be) (approved|accepted|paid|declined|rejected)\b"
    r"|\byou (are|will be) (definitely |certainly )?(covered|paid|reimbursed)\b"
    r"|\b(definitely|certainly|guaranteed to be) (covered|paid|payable)\b",
    re.I,
)
_INSTRUCTION = re.compile(
    r"\b(ignore|disregard) (all |any )?(previous |prior )?(instructions|rules)\b|\bsystem prompt\b|\byou are now\b",
    re.I,
)


def normalise(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[–—]", "-", text)
    return re.sub(r"\s+", " ", text).strip(" .;:\"'")


def numbers(text: str) -> set[str]:
    """Numbers in canonical form: '£1,000' -> '1000', '10.50' -> '10.5'."""
    out = set()
    for n in _NUMBER.findall(text):
        n = n.replace(",", "")
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        out.add(n)
    return out


@dataclass
class ClaimCheck:
    index: int
    claim: Claim
    problems: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems


@dataclass
class Verification:
    checks: list[ClaimCheck]
    judge_used: bool

    @property
    def kept(self) -> list[Claim]:
        return [c.claim for c in self.checks if c.passed]

    @property
    def removed(self) -> list[ClaimCheck]:
        return [c for c in self.checks if not c.passed]


def source_text(doc: LCDocument) -> str:
    return f"{doc.metadata.get('section', '')}\n{doc.page_content}"


def check_claim(index: int, claim: Claim, sources: dict[str, LCDocument], allowed_text: str) -> ClaimCheck:
    check = ClaimCheck(index, claim)
    cited = [sources[s] for s in claim.source_ids if s in sources]
    if not claim.source_ids:
        check.problems.append("no citation")
    elif len(cited) != len(claim.source_ids):
        check.problems.append("cites a source that was not provided")
    cited_text = "\n".join(source_text(d) for d in cited)
    if claim.quote.strip() and not any(normalise(claim.quote) in normalise(source_text(d)) for d in cited):
        check.problems.append("quote not found in the cited source")
    unsupported = numbers(claim.text) - numbers(cited_text) - numbers(allowed_text)
    if unsupported:
        check.problems.append(f"numbers not in the cited sources: {', '.join(sorted(unsupported))}")
    if DECISION_LANGUAGE.search(claim.text):
        check.problems.append("states a coverage or claim decision")
    if _INSTRUCTION.search(claim.text):
        check.problems.append("contains instruction-like text")
    return check


def judge(chat: BaseChatModel, checks: list[ClaimCheck], sources: dict[str, LCDocument]) -> None:
    """Ask the model whether each remaining claim is supported by its cited sources."""
    pending = [c for c in checks if c.passed]
    if not pending:
        return
    blocks = []
    for c in pending:
        cited = "\n".join(
            f'<source id="{s}">\n{escape(source_text(sources[s]))}\n</source>' for s in c.claim.source_ids
        )
        blocks.append(f'<claim index="{c.index}">\n{escape(c.claim.text)}\n{cited}\n</claim>')
    chain = judge_prompt | chat.with_structured_output(JudgeResult, method="json_schema", strict=True)
    result: JudgeResult = chain.invoke({"claims": "\n\n".join(blocks)})
    verdicts = {v.claim_index: v for v in result.verdicts}
    for c in pending:
        verdict = verdicts.get(c.index)
        if verdict is None or not verdict.supported:
            c.problems.append(f"judge: {verdict.reason if verdict else 'no verdict'}")


def verify(
    draft: DraftAnswer,
    sources: dict[str, LCDocument],
    allowed_text: str,
    chat: BaseChatModel | None,
) -> Verification:
    checks = [check_claim(i, claim, sources, allowed_text) for i, claim in enumerate(draft.claims)]
    if chat is not None:
        judge(chat, checks, sources)
    return Verification(checks, judge_used=chat is not None)


def summary_is_safe(
    summary: str, kept: list[Claim], sources: dict[str, LCDocument], allowed_text: str
) -> bool:
    """The summary may only repeat figures from the kept claims' sources, and makes no decisions."""
    cited = "\n".join(source_text(sources[s]) for c in kept for s in c.source_ids if s in sources)
    return (
        bool(summary.strip())
        and not (numbers(summary) - numbers(cited) - numbers(allowed_text))
        and not DECISION_LANGUAGE.search(summary)
        and not _INSTRUCTION.search(summary)
    )
