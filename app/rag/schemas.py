"""Structured outputs requested from the model (OpenAI strict JSON schema: every field required)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Claim(BaseModel):
    text: str = Field(description="One specific statement, in plain English, taken only from the sources.")
    source_ids: list[str] = Field(description="IDs of the sources that support this statement, e.g. ['S2'].")
    quote: str = Field(
        description="The exact words from ONE cited source that support the statement (max 300 characters), "
        "or an empty string."
    )


class DraftAnswer(BaseModel):
    status: Literal["answered", "not_found"] = Field(
        description="'not_found' if the sources do not answer the question."
    )
    summary: str = Field(description="A one or two sentence direct answer, consistent with the claims.")
    claims: list[Claim]
    missing_information: str = Field(
        description="What the question asks that the sources do not say, or an empty string."
    )


class StandaloneQuestion(BaseModel):
    question: str = Field(description="The latest message rewritten as a self-contained question.")


class Verdict(BaseModel):
    claim_index: int
    supported: bool = Field(description="True only if every detail of the claim is stated in its sources.")
    reason: str


class JudgeResult(BaseModel):
    verdicts: list[Verdict]
