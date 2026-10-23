"""The evaluation harness runs end to end (with the offline fakes, so the numbers are not a quality measure)."""

import pytest

from eval import run_eval

pytestmark = pytest.mark.integration


def test_golden_set_is_well_formed():
    items = run_eval.load_items()
    assert len({i["id"] for i in items}) == len(items) >= 60
    for item in items:
        assert item["expect"] in ("answered", "abstained", "clarification", "any")
        assert item["role"] in ("admin", "underwriter", "claims_handler", "customer_service")
        if item["expect"] == "answered":
            assert item["facts"] and item["evidence"], item["id"]


def test_harness_scores_items(database, capsys):
    summary = run_eval.main(["--ids", "home-19,home-27,clarify-01,shop-07", "--workers", "1", "--no-report"])
    assert summary["items"] == 4
    assert summary["clarification_correct"] == {"rate": 1.0, "n": 2}
    assert summary["correct_version"]["rate"] == 1.0  # home-19 must use the 2025.1 wording
    assert summary["errors"] == 0
    assert "| Items passed (all checks) |" in capsys.readouterr().out
