import io

import pytest
from sqlalchemy import select

from app.config import PROJECT_ROOT
from app.models import Document
from app.rag import rerank
from app.rag.schemas import Claim, DraftAnswer
from tests.conftest import PASSWORD, USERS, sign_in
from tests.fakes import OverlapReranker

pytestmark = pytest.mark.integration


def ask(client, question, **selection):
    conv = client.post("/api/conversations", json={}).json()["id"]
    r = client.post(
        f"/api/conversations/{conv}/messages", json={"question": question, "selection": selection}
    )
    assert r.status_code == 200, r.text
    return conv, r.json()["message"]


def answer_text(message):
    a = message["answer"]
    return " ".join([a.get("summary", "")] + [c["text"] for c in a.get("claims", [])])


# --- Authentication --------------------------------------------------------------------------------


def test_sign_in_and_out(client):
    assert client.get("/api/auth/me").status_code == 401
    me = sign_in(client, "claims_handler")
    assert me["role"] == "claims_handler" and me["permissions"] == ["ask"]
    assert client.get("/api/auth/me").json()["email"] == USERS["claims_handler"]
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_wrong_password_and_lockout(client):
    for _ in range(5):
        r = client.post("/api/auth/login", json={"email": USERS["underwriter"], "password": "wrong-password"})
        assert r.status_code == 401
    r = client.post("/api/auth/login", json={"email": USERS["underwriter"], "password": PASSWORD})
    assert r.status_code == 401  # locked for 15 minutes even with the right password


def test_state_changes_need_the_csrf_header(client):
    sign_in(client, "customer_service")
    del client.headers["X-CSRF-Token"]
    assert client.post("/api/conversations", json={}).json()["detail"]["code"] == "csrf_failed"


def test_api_token_for_scripts(client):
    token = client.post("/api/auth/token", json={"email": USERS["admin"], "password": PASSWORD}).json()[
        "access_token"
    ]
    client.cookies.clear()
    r = client.post("/api/conversations", json={}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201  # bearer tokens need no CSRF header


# --- Asking ----------------------------------------------------------------------------------------


def test_answer_with_citation_and_page(as_role):
    client = as_role("claims_handler")
    _, msg = ask(client, "What is the escape of water excess?", product_code="HOME", region="UK")
    assert msg["status"] == "answered"
    a = msg["answer"]
    assert "£350" in answer_text(msg) or "£450" in answer_text(msg)
    assert {d["version_label"] for d in a["documents"]} >= {"2026.1", "HE12"}  # current wording + endorsement
    sid = a["claims"][0]["source_ids"][0]
    cite = client.get(f"/api/messages/{msg['id']}/sources/{sid}").json()
    assert cite["page_number"] >= 1 and cite["highlight"] is not None
    span = cite["page_text"][cite["highlight"]["start"] : cite["highlight"]["end"]]
    assert span.lower() in a["claims"][0]["quote"].lower() or a["claims"][0]["quote"].lower() in span.lower()


def test_answers_are_reranked(as_role):
    reranker = OverlapReranker()
    rerank.use_reranker(reranker)
    client = as_role("admin")
    _, msg = ask(client, "What is the escape of water excess?", product_code="HOME", region="UK")
    assert msg["status"] == "answered"
    assert "£350" in answer_text(msg) or "£450" in answer_text(msg)
    assert len(reranker.calls) == 1 and reranker.calls[0][1] > 6  # more candidates than sources kept
    trace = client.get(f"/api/admin/messages/{msg['id']}/trace").json()["trace"]
    assert trace["reranker"] == reranker.name
    scores = [s["rerank_score"] for s in trace["retrieved"]]
    assert len(scores) == 6 and scores == sorted(scores, reverse=True)


def test_incident_date_uses_the_older_wording(as_role):
    client = as_role("claims_handler")
    _, msg = ask(
        client, "A pipe burst at a house on 10 June 2025. What escape of water excess applied?", region="UK"
    )
    assert msg["status"] == "answered"
    assert [d["version_label"] for d in msg["answer"]["documents"]] == ["2025.1"]
    assert "£300" in answer_text(msg)


def test_ireland_wording_answers_in_euro(as_role):
    client = as_role("customer_service")
    _, msg = ask(client, "What is the escape of water excess for a home in Ireland?", product_code="HOME")
    assert msg["status"] == "answered"
    assert msg["answer"]["documents"][0]["region"] == "IE"
    assert "€400" in answer_text(msg)


def test_unclear_product_asks_for_clarification(as_role):
    client = as_role("customer_service")
    _, msg = ask(client, "How much is the cancellation fee?")
    assert msg["status"] == "clarification"
    assert {o["selection"]["product_code"] for o in msg["answer"]["clarification"]["options"]} == {
        "HOME",
        "MOTOR",
        "SHOP",
        "TRAVEL",
    }


def test_unrelated_question_is_not_answered(as_role):
    client = as_role("customer_service")
    _, msg = ask(client, "Does the policy pay veterinary fees for a parrot?", product_code="TRAVEL")
    assert msg["status"] == "abstained"
    assert "claims" not in msg["answer"]


def test_follow_up_question_is_rewritten(as_role, fake_models):
    client = as_role("claims_handler")
    conv, _ = ask(client, "What is the escape of water excess?", product_code="HOME", region="UK")
    r = client.post(
        f"/api/conversations/{conv}/messages",
        json={"question": "And for subsidence?", "selection": {"product_code": "HOME", "region": "UK"}},
    )
    msg = r.json()["message"]
    assert "StandaloneQuestion" in fake_models.calls
    assert msg["status"] == "answered" and "£1,000" in answer_text(msg)


def test_unsupported_figures_are_removed(as_role, fake_models):
    def hallucinate(question, sources):
        sid = next(iter(sources))
        return DraftAnswer(
            status="answered",
            summary="The excess is £9,999.",
            claims=[Claim(text="The escape of water excess is £9,999.", source_ids=[sid], quote="")],
            missing_information="",
        )

    fake_models.draft_hook = hallucinate
    client = as_role("claims_handler")
    _, msg = ask(client, "What is the escape of water excess?", product_code="HOME", region="UK")
    assert msg["status"] == "abstained" and msg["answer"]["reason"] == "unverified"
    assert "9,999" not in msg["content"]


def test_model_outage_is_reported_and_question_kept(as_role, fake_models):
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    fake_models.fail_with = openai.APITimeoutError(request=request)
    client = as_role("claims_handler")
    conv, msg = ask(client, "What is the escape of water excess?", product_code="HOME", region="UK")
    assert (
        msg["status"] == "error"
        and msg["answer"]["code"] == "model_unavailable"
        and msg["answer"]["retryable"]
    )
    messages = client.get(f"/api/conversations/{conv}").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_feedback(as_role):
    client = as_role("underwriter")
    _, msg = ask(client, "Is a courtesy car provided if my car is a total loss?", product_code="MOTOR")
    assert client.put(f"/api/messages/{msg['id']}/feedback", json={"rating": 1}).status_code == 200
    assert client.put(
        f"/api/messages/{msg['id']}/feedback", json={"rating": -1, "comment": "too long"}
    ).json() == {"rating": -1}


def test_conversations_are_private(client):
    sign_in(client, "claims_handler")
    conv, _ = ask(client, "What is the windscreen replacement excess?", product_code="MOTOR")
    sign_in(client, "underwriter")
    assert client.get(f"/api/conversations/{conv}").status_code == 404


# --- Access control --------------------------------------------------------------------------------


def test_restricted_guides_follow_roles(client):
    sign_in(client, "underwriter")
    titles = {d["title"] for d in client.get("/api/documents").json()["documents"]}
    assert "Home Underwriting Guide" in titles and "Motor Claims Handling Manual" not in titles

    sign_in(client, "customer_service")
    titles = {d["title"] for d in client.get("/api/documents").json()["documents"]}
    assert not titles & {"Home Underwriting Guide", "Motor Claims Handling Manual"}
    _, msg = ask(
        client, "What is the authority limit for a senior underwriter?", product_code="HOME", region="UK"
    )
    assert "Home Underwriting Guide" not in {d["title"] for d in msg["answer"].get("documents", [])}


def test_underwriter_can_use_the_underwriting_guide(as_role):
    client = as_role("underwriter")
    _, msg = ask(
        client, "Should a thatched roof be referred, and what load applies?", product_code="HOME", region="UK"
    )
    assert msg["status"] == "answered"
    assert "35%" in answer_text(msg)


def test_admin_endpoints_need_the_admin_role(client):
    sign_in(client, "claims_handler")
    for path in ("/api/admin/users", "/api/admin/stats", "/api/admin/audit"):
        assert client.get(path).status_code == 403
    sign_in(client, "admin")
    users = client.get("/api/admin/users").json()["users"]
    staff = [u for u in users if u["email"].endswith("@jigsaw-insurance.com")]
    assert len(staff) >= 53 and {u["role"] for u in staff} == {
        "admin",
        "underwriter",
        "claims_handler",
        "customer_service",
    }
    stats = client.get("/api/admin/stats").json()
    assert stats["documents"]["ready"] >= 10
    assert any(e["action"] == "auth.signed_in" for e in client.get("/api/admin/audit").json()["events"])


def test_admin_manages_users(client):
    sign_in(client, "admin")
    r = client.post(
        "/api/admin/users",
        json={
            "email": "sam.patel@jigsaw-insurance.com",
            "full_name": "Sam Patel",
            "role": "customer_service",
        },
    )
    assert r.status_code == 201
    user, temporary = r.json()["user"], r.json()["temporary_password"]
    assert client.patch(f"/api/admin/users/{user['id']}", json={"role": "claims_handler"}).json()["user"][
        "role"
    ] == ("claims_handler")
    client.cookies.clear()
    r = client.post(
        "/api/auth/login", json={"email": "sam.patel@jigsaw-insurance.com", "password": temporary}
    )
    assert r.json()["role"] == "claims_handler"


# --- Documents -------------------------------------------------------------------------------------


def test_catalog_lists_versions_for_the_selectors(as_role):
    client = as_role("claims_handler")
    home = next(p for p in client.get("/api/catalog").json()["products"] if p["code"] == "HOME")
    assert {(w["version_label"], w["region"]) for w in home["wordings"]} == {
        ("2025.1", "UK"),
        ("2026.1", "UK"),
        ("2026.1", "IE"),
    }


def test_upload_rejects_non_pdf(as_role):
    client = as_role("admin")
    r = client.post(
        "/api/documents",
        files={"file": ("notes.pdf", b"hello", "application/pdf")},
        data={"product_code": "HOME", "title": "Notes", "version_label": "1", "effective_from": "2026-01-01"},
    )
    assert r.status_code == 415 and r.json()["detail"]["code"] == "not_a_pdf"


def test_upload_needs_admin(as_role):
    client = as_role("underwriter")
    r = client.post("/api/documents", files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")}, data={})
    assert r.status_code in (403, 422)


def test_deleted_document_citations_say_so(as_role, db):
    from app.ingestion.worker import drain

    client = as_role("admin")
    pdf = (PROJECT_ROOT / "sample_data" / "pdfs" / "shop-office-2026-uk.pdf").read_bytes() + b"\n% v2\n"
    r = client.post(
        "/api/documents",
        files={"file": ("shop-2027.pdf", io.BytesIO(pdf), "application/pdf")},
        data={
            "product_code": "SHOP",
            "title": "Jigsaw Shop and Office Insurance Policy Wording",
            "version_label": "2027.1",
            "region": "UK",
            "effective_from": "2027-01-01",
        },
    )
    assert r.status_code == 202, r.text
    doc_id = r.json()["document"]["id"]
    assert drain() == {"succeeded": 1}
    assert client.get(f"/api/documents/{doc_id}").json()["document"]["status"] == "ready"

    _, msg = ask(client, "What is the glass limit for any one claim on 2 March 2027?", product_code="SHOP")
    assert msg["status"] == "answered" and msg["answer"]["documents"][0]["id"] == doc_id
    sid = next(iter(msg["answer"]["sources"]))
    assert client.delete(f"/api/documents/{doc_id}").status_code == 200
    r = client.get(f"/api/messages/{msg['id']}/sources/{sid}")
    assert r.status_code == 410 and r.json()["detail"]["code"] == "source_removed"
    assert db.execute(select(Document.status).where(Document.id == doc_id)).scalar_one() == "deleted"


def test_health_and_readiness(client):
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 503  # documents were indexed with the test embedding model, not OpenAI's
    checks = ready.json()["checks"]
    assert checks["database"]["ok"] and checks["migrations"]["ok"] and checks["openai"]["ok"]
    assert checks["embeddings"]["documents_indexed_with_another_model"] >= 10
    assert checks["reranker"] == {"ok": True, "enabled": False}  # tests opt in with a fake
