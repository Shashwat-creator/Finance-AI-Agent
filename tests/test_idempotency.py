"""Batch idempotency tests using real input files copied to a temp directory."""

import json
import shutil
from pathlib import Path

from app.schemas import WorkflowStatus
from app.workflow import run_batch

INPUTS_DIR = Path(__file__).resolve().parent.parent.parent / "inputs"


def _entry(source: str) -> dict:
    return {
        "id": "JE-DUP",
        "description": "Accrue Q4 bonus pool",
        "date": "2024-12-31",
        "source": source,
        "lines": [
            {"account": "6100", "debit": 1000.0, "credit": 0.0, "memo": "Bonus"},
            {"account": "2120", "debit": 0.0, "credit": 1000.0, "memo": "Accrual"},
        ],
    }


def test_duplicate_request_is_flagged_and_not_double_accepted(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for name in (
        "chart_of_accounts.csv",
        "trial_balance.csv",
        "prior_period_tb.csv",
        "fx_rates.csv",
    ):
        shutil.copy(INPUTS_DIR / name, inputs / name)

    payload = {
        "period": "2024-Q4",
        "functional_currency": "USD",
        "entries": [
            _entry("Original source note"),
            _entry("Different source note"),
        ],
    }
    (inputs / "manual_adjustments.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    result = run_batch(inputs, tmp_path / "outputs", offline=True, write=True)
    reports = result["reports"]

    assert len(reports) == 2
    assert reports[0].status == WorkflowStatus.READY_FOR_REVIEW
    assert reports[1].status == WorkflowStatus.REJECTED_VALIDATION
    assert any(
        item.rule_id == "VAL-010" and item.status.value == "FAIL"
        for item in reports[1].validation_results
    )
    assert result["summary"]["counts"]["ready_for_review"] == 1
    assert result["summary"]["counts"]["rejected_validation"] == 1

    accepted = json.loads(
        (tmp_path / "outputs" / "accepted_adjustments.json").read_text(encoding="utf-8")
    )
    assert len(accepted["entries"]) == 1


def test_cosmos_prior_decision_marks_entry_duplicate(monkeypatch, tmp_path):
    class _PriorStore:
        enabled = True

        def get(self, idempotency_key):
            return {"status": "READY_FOR_REVIEW"}

        def save(self, idempotency_key, document):
            return None

    monkeypatch.setattr(
        "app.integrations.CosmosDecisionStore", lambda: _PriorStore()
    )

    result = run_batch(INPUTS_DIR, tmp_path / "outputs", offline=True, write=False)
    assert result["summary"]["counts"]["ready_for_review"] == 0
    assert result["summary"]["counts"]["rejected_validation"] == 10
    for report in result["reports"]:
        assert any(
            item.rule_id == "VAL-010" and item.status.value == "FAIL"
            for item in report.validation_results
        )
