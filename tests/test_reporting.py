"""
Unit tests for app/reporting.py.
"""

import json
from decimal import Decimal
from pathlib import Path

from app.reporting import generate_json_report, generate_markdown_report
from app.schemas import (
    AccountCandidate,
    AuditEvent,
    FinalAdjustmentReport,
    JournalEntryLine,
    ProposedJournalEntry,
    ReconciliationResult,
    SourceMetadata,
    TransformationLog,
    ValidationResult,
    ValidationStatus,
    WorkflowStatus,
)


def _build_sample_report() -> FinalAdjustmentReport:
    return FinalAdjustmentReport(
        request_id="JE-001",
        run_id="RUN-2024-TEST-001",
        status=WorkflowStatus.READY_FOR_REVIEW,
        input_sources=[
            SourceMetadata(
                filename="manual_adjustments.json",
                sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                row_count=10,
                record_count=10,
                ingestion_timestamp="2024-12-31T23:59:59Z",
            )
        ],
        input_hash="e3b0c44298fc1c14",
        normalization_changes=[
            TransformationLog(
                field="currency",
                original_value="usd",
                normalized_value="USD",
                rule="NORMALIZE_CASE",
                source_reference="JE-001",
            )
        ],
        account_candidates=[
            AccountCandidate(
                account_code="6100",
                account_name="Salaries and Wages",
                confidence=0.95,
                evidence="Bonus accrual maps to personnel expense",
                rank=1,
            )
        ],
        proposed_entry=ProposedJournalEntry(
            request_id="JE-001",
            description="Accrue Q4 bonus pool",
            period="2024-Q4",
            currency="USD",
            lines=[
                JournalEntryLine(
                    account_code="6100",
                    account_name="Salaries and Wages",
                    debit=Decimal("850000.00"),
                    credit=Decimal("0.00"),
                    memo="Bonus accrual",
                ),
                JournalEntryLine(
                    account_code="2120",
                    account_name="Accrued Expenses",
                    debit=Decimal("0.00"),
                    credit=Decimal("850000.00"),
                    memo="Accrual offset",
                ),
            ],
            total_debit=Decimal("850000.00"),
            total_credit=Decimal("850000.00"),
            difference=Decimal("0.00"),
            rationale="Accruing unpaid employee bonuses for period end.",
            source_references=["bonus_schedule_q4.xlsx"],
        ),
        validation_results=[
            ValidationResult(
                rule_id="JE-001",
                rule_name="Balance check",
                status=ValidationStatus.PASS,
                message="Debits equal credits exactly.",
            )
        ],
        reconciliation=ReconciliationResult(
            total_debit=Decimal("850000.00"),
            total_credit=Decimal("850000.00"),
            difference=Decimal("0.00"),
            is_balanced=True,
            evidence_checks=["Source totals match entry totals."],
        ),
        warnings=["Test warning notice"],
        errors=[],
        audit_events=[
            AuditEvent(
                node="intake",
                event_type="node_start",
                details={"step": "registered"},
            )
        ],
        human_review_required=True,
    )


class TestReporting:
    def test_generate_json_report(self, tmp_path: Path):
        report = _build_sample_report()
        report_path = generate_json_report(report, tmp_path)

        assert report_path.is_file()
        assert report_path.name == "RUN-2024-TEST-001_JE-001.json"

        content = report_path.read_text(encoding="utf-8")
        data = json.loads(content)

        assert data["request_id"] == "JE-001"
        assert data["run_id"] == "RUN-2024-TEST-001"
        assert data["status"] == "READY_FOR_REVIEW"
        # Check Decimals serialized to str
        assert data["proposed_entry"]["total_debit"] == "850000.0" or data["proposed_entry"]["total_debit"] == "850000.00"
        assert data["reconciliation"]["is_balanced"] is True

    def test_generate_markdown_report_structure(self):
        report = _build_sample_report()
        md = generate_markdown_report(report)

        # 1. Header & Status
        assert "# Manual Adjustment Audit Report: JE-001" in md
        assert "**Run ID**: `RUN-2024-TEST-001`" in md
        assert "**Workflow Status**: `READY_FOR_REVIEW`" in md

        # 2. Disclaimer
        assert "REQUIRES HUMAN REVIEW — NOT POSTED" in md

        # 3. Tables
        assert "## 1. Input Sources & Lineage" in md
        assert "manual_adjustments.json" in md

        assert "## 2. Normalization & Transformation Log" in md
        assert "NORMALIZE_CASE" in md

        assert "## 3. Account Mapping Candidates" in md
        assert "6100" in md
        assert "Salaries and Wages" in md
        assert "95.0%" in md

        assert "## 4. Proposed Journal Entry" in md
        assert "850,000.00" in md

        assert "## 5. Validation Results" in md
        assert "JE-001" in md
        assert "PASS" in md

        assert "## 6. Reconciliation Summary" in md
        assert "BALANCED" in md

        assert "## 7. Operational Warnings & Errors" in md
        assert "Test warning notice" in md
