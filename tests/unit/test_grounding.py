from langchain_core.documents import Document

from app.rag.grounding import numbers, summary_is_safe, verify
from app.rag.schemas import Claim, DraftAnswer

SOURCES = {
    "S1": Document(
        page_content="Type of claim | Compulsory excess\nEscape of water | £350\nSubsidence, heave or landslip | £1,000",
        metadata={"section": "Excesses"},
    ),
    "S2": Document(
        page_content="You may cancel this policy within 14 days of receiving your policy documents.",
        metadata={"section": "General conditions > Cancelling your policy"},
    ),
}


def draft(*claims: Claim) -> DraftAnswer:
    return DraftAnswer(status="answered", summary="", claims=list(claims), missing_information="")


def problems(claim: Claim, question: str = "What is the excess?") -> list[str]:
    return verify(draft(claim), SOURCES, question, chat=None).checks[0].problems


def test_numbers_are_normalised():
    assert numbers("£1,000 and 14 days, 10.50%") == {"1000", "14", "10.5"}


def test_supported_claim_passes():
    claim = Claim(
        text="The escape of water excess is £350.", source_ids=["S1"], quote="Escape of water | £350"
    )
    assert problems(claim) == []


def test_claim_without_citation_fails():
    assert "no citation" in problems(Claim(text="The excess is £350.", source_ids=[], quote=""))


def test_claim_citing_unknown_source_fails():
    assert "cites a source that was not provided" in problems(
        Claim(text="Excess is £350.", source_ids=["S9"], quote="")
    )


def test_quote_must_be_verbatim():
    claim = Claim(
        text="Escape of water excess is £350.", source_ids=["S1"], quote="escape of water excess is £300"
    )
    assert "quote not found in the cited source" in problems(claim)


def test_quote_matching_ignores_case_and_spacing():
    claim = Claim(
        text="Cancel within 14 days.", source_ids=["S2"], quote="you may  CANCEL this policy within 14 days"
    )
    assert problems(claim) == []


def test_invented_amount_fails_even_with_a_citation():
    claim = Claim(text="The subsidence excess is £2,500.", source_ids=["S1"], quote="")
    assert problems(claim) == ["numbers not in the cited sources: 2500"]


def test_numbers_from_the_question_are_allowed():
    claim = Claim(text="A home empty for 20 days is within 14 days?", source_ids=["S2"], quote="")
    assert problems(claim, question="My home was empty for 20 days") == []


def test_numbers_from_the_document_scope_are_allowed():
    claim = Claim(
        text="Under wording version 2026.1 the escape of water excess is £350.", source_ids=["S1"], quote=""
    )
    scope = "- Jigsaw Home Insurance Policy Wording, version 2026.1 (UK), policy wording"
    assert problems(claim, question=f"What is the excess?\n{scope}") == []


def test_coverage_decisions_are_rejected():
    claim = Claim(text="Your claim will be paid, less the £350 excess.", source_ids=["S1"], quote="")
    assert "states a coverage or claim decision" in problems(claim)


def test_judge_can_remove_a_claim(fake_models):
    fake_models.judge_hook = lambda claims: [False for _ in claims]
    claim = Claim(text="The escape of water excess is £350.", source_ids=["S1"], quote="")
    result = verify(draft(claim), SOURCES, "excess?", chat=fake_models)
    assert result.kept == [] and result.judge_used
    assert result.removed[0].problems == ["judge: fake judge"]


def test_summary_may_not_add_numbers():
    kept = [Claim(text="The escape of water excess is £350.", source_ids=["S1"], quote="")]
    assert summary_is_safe("The escape of water excess is £350.", kept, SOURCES, "q")
    assert not summary_is_safe("The excess is £500.", kept, SOURCES, "q")
    assert not summary_is_safe("", kept, SOURCES, "q")
