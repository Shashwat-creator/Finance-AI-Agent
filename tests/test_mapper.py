"""Tests for the deterministic COA mapper fallback."""

from pathlib import Path

from app.agents.mapper_agent import (
    map_unknown_accounts,
    suggest_account_candidates,
)
from app.ingestion import load_chart_of_accounts

INPUTS_DIR = Path(__file__).resolve().parent.parent.parent / "inputs"


def _coa():
    records, _ = load_chart_of_accounts(INPUTS_DIR / "chart_of_accounts.csv")
    return records


def test_suggests_6310_for_orphan_6315():
    candidates = suggest_account_candidates(
        unknown_code="6315",
        hint_text="Reclassify travel and entertainment spend mis-posted to 6315",
        coa_records=_coa(),
        top_k=3,
    )
    assert candidates, "expected at least one candidate"
    codes = [candidate.account_code for candidate in candidates]
    assert "6310" in codes
    top = candidates[0]
    assert top.rank == 1
    assert 0.0 < top.confidence <= 1.0
    assert top.evidence
    # Ranks are 1..n and confidence is non-increasing.
    assert [c.rank for c in candidates] == list(range(1, len(candidates) + 1))
    assert candidates[0].confidence >= candidates[-1].confidence


def test_does_not_suggest_non_postable_headers():
    candidates = suggest_account_candidates(
        unknown_code="1295",
        hint_text="Other assets reclassification",
        coa_records=_coa(),
        top_k=5,
    )
    posted = {candidate.account_code for candidate in candidates}
    assert "1290" not in posted  # 1290 is a Header with no children


def test_map_unknown_accounts_dedupes_codes():
    candidates = map_unknown_accounts(
        accounts=["6315", "6315", ""],
        hint_text="travel expense",
        coa_records=_coa(),
        top_k=2,
    )
    assert candidates
    # A single distinct unknown code should yield at most top_k candidates.
    assert len(candidates) <= 2
