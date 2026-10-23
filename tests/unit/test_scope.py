import uuid
from datetime import date

import pytest

from app.rag.scope import DocInfo, Selection, detect_product, detect_region, find_date, resolve

TODAY = date(2026, 10, 12)


def doc(product, kind, version, region, start, end=None, applies_to=None, title=None):
    return DocInfo(
        uuid.uuid4(), product, f"Jigsaw {product.title()} Insurance", kind, title or f"{product} {kind}",
        version, region, date.fromisoformat(start), date.fromisoformat(end) if end else None, applies_to,
    )  # fmt: skip


@pytest.fixture
def catalogue():
    home_2025 = doc("HOME", "wording", "2025.1", "UK", "2025-01-01", "2026-03-31")
    home_2026 = doc("HOME", "wording", "2026.1", "UK", "2026-04-01")
    home_ie = doc("HOME", "wording", "2026.1", "IE", "2026-04-01")
    he12 = doc("HOME", "endorsement", "HE12", "UK", "2026-07-01", applies_to=home_2026.id)
    guide = doc("HOME", "guide", "2026.1", "UK", "2026-04-01")
    motor = doc("MOTOR", "wording", "2026.1", "UK", "2026-01-01")
    return {
        "home_2025": home_2025,
        "home_2026": home_2026,
        "home_ie": home_ie,
        "he12": he12,
        "guide": guide,
        "motor": motor,
    }


def test_dates_in_questions_are_found():
    assert find_date("A pipe burst on 10 June 2025, what excess?") == date(2025, 6, 10)
    assert find_date("incident on June 3rd, 2026") == date(2026, 6, 3)
    assert find_date("loss date 2026-02-28") == date(2026, 2, 28)
    assert find_date("on 05/04/2026") == date(2026, 4, 5)  # UK day/month order
    assert find_date("what is the excess?") is None


def test_product_and_region_detection():
    codes = {"HOME", "MOTOR", "TRAVEL", "SHOP"}
    assert detect_product("Is a courtesy car provided?", codes) == "MOTOR"
    assert detect_product("What is the limit for home office equipment on my contents?", codes) == "HOME"
    assert detect_product("What is the excess?", codes) is None
    assert detect_region("cover for a house in Ireland") == "IE"
    assert detect_region("a flat in Northern Ireland") == "UK"
    assert detect_region("what is the excess?") is None


def test_asks_which_product_when_unclear(catalogue):
    scope = resolve(list(catalogue.values()), Selection(), "What is the cancellation fee?", TODAY)
    assert scope.status == "clarify"
    assert {o.selection["product_code"] for o in scope.options} == {"HOME", "MOTOR"}


def test_asks_which_region_when_a_product_has_several(catalogue):
    scope = resolve(list(catalogue.values()), Selection(product_code="HOME"), "What is the excess?", TODAY)
    assert scope.status == "clarify"
    assert {o.selection["region"] for o in scope.options} == {"UK", "IE"}


def test_current_version_with_endorsement_and_guide(catalogue):
    scope = resolve(list(catalogue.values()), Selection(product_code="HOME", region="UK"), "excess?", TODAY)
    assert scope.status == "ok"
    assert scope.wording == catalogue["home_2026"]
    assert set(scope.attachments) == {catalogue["he12"], catalogue["guide"]}
    assert any("2026.1" in n for n in scope.notes)  # tells the user which version was assumed


def test_incident_date_picks_the_version_in_force(catalogue):
    scope = resolve(
        list(catalogue.values()), Selection(region="UK"), "Escape of water at my house on 10 June 2025", TODAY
    )
    assert scope.wording == catalogue["home_2025"]
    assert catalogue["he12"] not in scope.attachments


def test_endorsement_not_yet_in_force_is_left_out_with_a_note(catalogue):
    scope = resolve(
        list(catalogue.values()),
        Selection(product_code="HOME", region="UK", incident_date=date(2026, 5, 1)),
        "excess",
        TODAY,
    )
    assert scope.wording == catalogue["home_2026"]
    assert catalogue["he12"] not in scope.attachments
    assert any("effective from 1 July 2026" in n for n in scope.notes)


def test_selected_version_not_in_force_on_the_date_asks_first(catalogue):
    selection = Selection(
        product_code="HOME", document_id=catalogue["home_2026"].id, incident_date=date(2025, 6, 10)
    )
    scope = resolve(list(catalogue.values()), selection, "excess", TODAY)
    assert scope.status == "clarify"
    assert scope.options[0].selection == {"document_id": str(catalogue["home_2025"].id)}
    assert scope.options[-1].selection == {"ignore_date": True}

    selection.ignore_date = True
    assert resolve(list(catalogue.values()), selection, "excess", TODAY).wording == catalogue["home_2026"]


def test_no_version_in_force_on_the_date(catalogue):
    scope = resolve(
        list(catalogue.values()), Selection(product_code="MOTOR"), "accident on 1 March 2025", TODAY
    )
    assert scope.status == "clarify"
    assert "No Jigsaw Motor Insurance wording" in scope.question


def test_no_documents():
    assert resolve([], Selection(), "anything", TODAY).status == "no_documents"
