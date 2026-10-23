"""Offline stand-ins for the OpenAI models, used by the whole test suite.

* ``HashEmbeddings`` - hashed bag-of-words vectors: deterministic and lexically meaningful.
* ``OverlapReranker`` - scores a passage by the share of the question's words it contains.
* ``FakeChatModel`` - answers structured-output calls by reading the real rendered prompt: the draft is
  extractive (the best matching sentences from the <source> blocks, quoted verbatim), the follow-up rewrite
  joins the previous question to the new one, and the judge accepts every claim unless told otherwise.

Tests can override any behaviour by setting ``draft_hook``, ``judge_hook`` or ``fail_with`` on the fake.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable
from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_core.prompt_values import PromptValue
from langchain_core.runnables import RunnableLambda

from app.rag.schemas import Claim, DraftAnswer, JudgeResult, StandaloneQuestion, Verdict

STOPWORDS = set(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "is",
        "are",
        "be",
        "by",
        "with",
        "what",
        "which",
        "who",
        "how",
        "does",
        "do",
        "any",
        "my",
        "your",
        "our",
        "this",
        "that",
        "it",
        "its",
        "if",
        "under",
        "from",
        "at",
        "as",
        "can",
        "will",
        "we",
        "you",
        "there",
        "their",
        "when",
        "was",
        "were",
        "has",
        "have",
        "per",
        "much",
        "many",
    ]
)


def terms(text: str) -> set[str]:
    words = re.findall(r"[a-z][a-z']+", text.lower())
    return {re.sub(r"(es|s)$", "", w) for w in words if w not in STOPWORDS and len(w) > 2}


class HashEmbeddings(Embeddings):
    model = "test-hash-embedding"

    def __init__(self, dimensions: int = 1536):
        self.dimensions = dimensions

    def _vector(self, text: str) -> list[float]:
        v = [0.0] * self.dimensions
        for t in terms(text):
            n = int.from_bytes(hashlib.blake2b(t.encode(), digest_size=8).digest(), "little")
            v[n % self.dimensions] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def _between(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\n?(.*?)\n?</{tag}>", text, re.S)
    return m.group(1).strip() if m else ""


def parse_sources(prompt: str) -> dict[str, str]:
    return {
        m.group(1): m.group(2).strip()
        for m in re.finditer(r'<source id="(\w+)"[^>]*>\n(.*?)\n</source>', prompt, re.S)
    }


def extractive_draft(question: str, sources: dict[str, str]) -> DraftAnswer:
    """Quote the sentences that best match the question (rarer words count for more)."""
    wanted = terms(question)
    sentences = [
        (rank, sid, s.strip())
        for rank, (sid, text) in enumerate(sources.items())
        for s in re.split(r"\n+|(?<=\.)\s+(?=[A-Z])", text)
        if len(s.strip()) >= 8
    ]
    df = {t: sum(t in terms(s) for _, _, s in sentences) or 1 for t in wanted}
    needed = 2 if len(wanted) > 2 else 1
    scored = []
    for rank, sid, sentence in sentences:
        overlap = wanted & terms(sentence)
        if len(overlap) >= needed:
            scored.append((sum(1 / df[t] for t in overlap) - 0.001 * rank, sid, sentence))
    scored.sort(key=lambda s: -s[0])
    best = scored[:2]
    if not best:
        return DraftAnswer(
            status="not_found", summary="", claims=[], missing_information="Not covered by the sources."
        )
    claims = [Claim(text=s, source_ids=[sid], quote=s if len(s) <= 300 else "") for _, sid, s in best]
    return DraftAnswer(status="answered", summary=claims[0].text, claims=claims, missing_information="")


class FakeChatModel:
    def __init__(self) -> None:
        self.draft_hook: Callable[[str, dict[str, str]], DraftAnswer] | None = None
        self.judge_hook: Callable[[list[str]], list[bool]] | None = None
        self.fail_with: Exception | None = None
        self.calls: list[str] = []

    def with_structured_output(self, schema: type, **_: Any) -> RunnableLambda:
        return RunnableLambda(lambda prompt: self._respond(schema, prompt))

    def _respond(self, schema: type, prompt: PromptValue) -> Any:
        self.calls.append(schema.__name__)
        if self.fail_with is not None:
            raise self.fail_with
        text = prompt.to_string()
        if schema is StandaloneQuestion:
            # Like a real rewrite: keep the new subject, carry over what was being asked about.
            previous = " ".join(re.findall(r"User: (.*)", _between(text, "conversation"))[-1:])
            carried = [w for w in ("excess", "limit", "fee", "cover", "period") if w in previous.lower()]
            latest = _between(text, "latest_message")
            return StandaloneQuestion(question=f"{latest} {' '.join(carried)}".strip())
        if schema is DraftAnswer:
            question, sources = _between(text, "question"), parse_sources(text)
            return (self.draft_hook or extractive_draft)(question, sources)
        if schema is JudgeResult:
            claims = re.findall(r'<claim index="(\d+)">\n(.*?)\n', text)
            verdicts = self.judge_hook([c for _, c in claims]) if self.judge_hook else [True] * len(claims)
            return JudgeResult(
                verdicts=[
                    Verdict(claim_index=int(i), supported=ok, reason="fake judge")
                    for (i, _), ok in zip(claims, verdicts, strict=True)
                ]
            )
        raise AssertionError(f"unexpected schema {schema}")


class OverlapReranker:
    """Offline stand-in for the cross-encoder: fraction of question terms found in the passage."""

    name = "test-overlap-reranker"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def score(self, query: str, passages: list[str]) -> list[float]:
        self.calls.append((query, len(passages)))
        wanted = terms(query) or {""}
        return [len(wanted & terms(p)) / len(wanted) for p in passages]
