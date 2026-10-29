"""End-to-end tests for the batch workflow on the supplied inputs."""

import json
from pathlib import Path

import pytest

from app.schemas import WorkflowStatus
from app.workflow import run_batch

INPUTS_DIR = Path(__file__).resolve().parent.parent.parent / "inputs"


@pytest.fixture(scope="module")
def batch(tmp_path_factory):
    output_dir = tmp_path_factory.mktemp("outputs")
    result = run_batch(INPUTS_DIR, output_dir, offline=True, write=True)
    return result, output_dir


@pytest.fixture(scope="module")
def reports(batch):
    return {report.request_id: report for report in batch[0]["reports"]}


def test_processes_every_adjustment(reports):
    assert len(reports) == 10
    assert set(reports) == {f"JE-{i:03d}" for i in range(1, 11)}


def test_expected_statuses(reports):
    assert reports["JE-001"].status == WorkflowStatus.READY_FOR_REVIEW
    assert reports["JE-002"].status == WorkflowStatus.REJECTED_VALIDATION
    assert reports["JE-003"].status == WorkflowStatus.READY_FOR_REVIEW
    assert reports["JE-005"].status == WorkflowStatus.NEEDS_CLARIFICATION
    assert reports["JE-008"].status == WorkflowStatus.REJECTED_VALIDATION


def test_unbalanced_entry_is_rejected_with_plain_english_reason(reports):
    report = reports["JE-002"]
    assert report.proposed_entry.total_debit == 28500
    assert report.proposed_entry.total_credit == 25000
    assert report.proposed_entry.difference == 3500
    assert "does not balance" in report.decision_reason
    assert "3,500.00" in report.decision_reason
    assert any(result.rule_id == "VAL-008" and result.status.value == "FAIL"
               for result in report.validation_results)
    assert report.human_review_required is True


def test_unknown_account_is_escalated_with_candidate(reports):
    report = reports["JE-005"]
    assert any(result.rule_id == "VAL-005" for result in report.validation_results)
    assert report.account_candidates
    top = report.account_candidates[0]
    assert top.account_code == "6310"
    assert "Nearest match: 6310" in report.decision_reason
    assert "6315" in report.decision_reason


def test_circular_intercompany_entry_is_rejected(reports):
    report = reports["JE-008"]
    failed = {result.rule_id for result in report.validation_results
              if result.status.value == "FAIL"}
    assert "SEM-001" in failed


def test_fx_revaluation_warning_surfaces_without_blocking(reports):
    report = reports["JE-003"]
    assert report.status == WorkflowStatus.READY_FOR_REVIEW
    assert any("FX" in warning or "rate" in warning.lower() for warning in report.warnings)
    assert "warning" in report.decision_reason.lower()


def test_input_defects_are_reported_at_batch_level(batch):
    summary = batch[0]["summary"]
    joined = " ".join(summary["input_warnings"]).lower()
    assert "9999" in joined
    assert "gbp" in joined
    assert "6310" in joined
    assert summary["counts"] == {
        "total": 10,
        "ready_for_review": 7,
        "needs_clarification": 1,
        "rejected_validation": 2,
        "failed_safe": 0,
    }


def test_artifacts_are_written_and_fail_closed(batch):
    result, output_dir = batch
    summary = json.loads((output_dir / "batch_summary.json").read_text(encoding="utf-8"))
    decisions = json.loads((output_dir / "decision_log.json").read_text(encoding="utf-8"))
    accepted = json.loads(
        (output_dir / "accepted_adjustments.json").read_text(encoding="utf-8")
    )

    assert summary["run_id"] == result["run_id"]
    assert len(decisions["decisions"]) == 10
    accepted_ids = {entry["id"] for entry in accepted["entries"]}
    assert "JE-001" in accepted_ids
    assert not ({"JE-002", "JE-005", "JE-008"} & accepted_ids)
    assert "Nothing has been posted" in accepted["note"]

    for report in result["reports"]:
        assert (output_dir / f"{result['run_id']}_{report.request_id}.json").is_file()
        assert (output_dir / f"{result['run_id']}_{report.request_id}.md").is_file()

    assert (output_dir / "batch_summary.md").is_file()
    assert (output_dir / "batch_summary.json").is_file()
    assert (output_dir / "decision_log.json").is_file()
    assert (output_dir / "accepted_adjustments.json").is_file()


def test_every_reported_amount_is_balanced_or_explicitly_rejected(reports):
    for report in reports.values():
        entry = report.proposed_entry
        if report.status == WorkflowStatus.READY_FOR_REVIEW:
            assert entry.difference == 0, report.request_id
        if entry and entry.difference != 0:
            assert report.status != WorkflowStatus.READY_FOR_REVIEW
