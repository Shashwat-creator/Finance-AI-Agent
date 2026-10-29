"""
Pydantic v2 data models for the Manual Adjustments Agent.

All monetary values use Decimal to prevent floating-point errors.
These schemas enforce structural correctness at the boundary of
every agent and every I/O operation.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# ── Enums ────────────────────────────────────────────────────────────────────


class WorkflowStatus(str, enum.Enum):
    """Terminal statuses for the adjustment workflow."""
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    REJECTED_VALIDATION = "REJECTED_VALIDATION"
    FAILED_SAFE = "FAILED_SAFE"
    IN_PROGRESS = "IN_PROGRESS"


class ValidationStatus(str, enum.Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"


class IssueSeverity(str, enum.Enum):
    SAFE_CLEANUP = "SAFE_CLEANUP"
    WARNING = "WARNING"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"


class AdjustmentIntent(str, enum.Enum):
    ACCRUAL = "ACCRUAL"
    RECLASSIFICATION = "RECLASSIFICATION"
    REVALUATION = "REVALUATION"
    PROVISION = "PROVISION"
    DEPRECIATION = "DEPRECIATION"
    TAX_ADJUSTMENT = "TAX_ADJUSTMENT"
    INTERCOMPANY = "INTERCOMPANY"
    OTHER = "OTHER"


# ── Chart of Accounts ────────────────────────────────────────────────────────


class ChartOfAccount(BaseModel):
    """A single account from the Chart of Accounts."""
    account_code: str
    account_name: str
    account_type: str  # Asset, Liability, Equity, Revenue, Expense, Header
    parent_code: Optional[str] = None
    statement: str  # BS or PL
    cf_category: Optional[str] = None
    normal_balance: str  # Debit or Credit
    is_postable: bool = True  # Inferred: False for Header accounts

    @field_validator("account_code", mode="before")
    @classmethod
    def coerce_code(cls, v: Any) -> str:
        return str(v).strip()


# ── Adjustment Request (raw input) ───────────────────────────────────────────


class JournalEntryLineInput(BaseModel):
    """A single line from the raw adjustment JSON."""
    account: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    memo: Optional[str] = None

    @field_validator("debit", "credit", mode="before")
    @classmethod
    def coerce_decimal(cls, v: Any) -> Decimal:
        if v is None:
            return Decimal("0")
        return Decimal(str(v))


class AdjustmentRequest(BaseModel):
    """A single adjustment request as read from the input JSON."""
    id: str
    description: str
    date: str
    source: str
    lines: list[JournalEntryLineInput]
    period: Optional[str] = None
    currency: Optional[str] = None


# ── Proposed Journal Entry (output) ──────────────────────────────────────────


class JournalEntryLine(BaseModel):
    """A validated journal entry line for the proposed entry."""
    account_code: str
    account_name: Optional[str] = None
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    memo: Optional[str] = None

    @field_validator("debit", "credit", mode="before")
    @classmethod
    def coerce_decimal(cls, v: Any) -> Decimal:
        if v is None:
            return Decimal("0")
        return Decimal(str(v))


class ProposedJournalEntry(BaseModel):
    """A complete proposed journal entry ready for review."""
    request_id: str
    description: str
    period: str
    currency: str
    date: str = ""
    lines: list[JournalEntryLine]
    total_debit: Decimal = Decimal("0")
    total_credit: Decimal = Decimal("0")
    difference: Decimal = Decimal("0")
    rationale: str = ""
    source_references: list[str] = Field(default_factory=list)


# ── Validation ───────────────────────────────────────────────────────────────


class ValidationResult(BaseModel):
    """Result of a single validation rule check."""
    rule_id: str
    rule_name: str
    status: ValidationStatus = ValidationStatus.NOT_RUN
    message: str = ""
    details: Optional[dict[str, Any]] = None


# ── Transformation / Normalization ───────────────────────────────────────────


class TransformationLog(BaseModel):
    """Audit record for a single data normalization step."""
    field: str
    original_value: str
    normalized_value: str
    rule: str
    source_reference: str = ""


class DataQualityIssue(BaseModel):
    """An issue found by the Data Quality Agent."""
    field: str
    severity: IssueSeverity
    reason: str
    original_value: Optional[str] = None
    suggested_value: Optional[str] = None
    source_reference: str = ""


# ── Account Mapping ──────────────────────────────────────────────────────────


class AccountCandidate(BaseModel):
    """A candidate GL account mapping with evidence and confidence."""
    account_code: str
    account_name: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = ""
    rank: int = 1


# ── Audit ────────────────────────────────────────────────────────────────────


class AuditEvent(BaseModel):
    """Structured audit event for traceability."""
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    request_id: str = ""
    run_id: str = ""
    node: str = ""
    event_type: str = ""  # node_start, node_end, transformation, validation, etc.
    details: dict[str, Any] = Field(default_factory=dict)


# ── Source Metadata ──────────────────────────────────────────────────────────


class SourceMetadata(BaseModel):
    """Lineage information for an ingested file."""
    filename: str
    sha256_hash: str
    row_count: Optional[int] = None
    ingestion_timestamp: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat() + "Z"
    )
    record_count: Optional[int] = None


# ── Reconciliation ───────────────────────────────────────────────────────────


class ReconciliationResult(BaseModel):
    """Result of reconciliation checks."""
    total_debit: Decimal = Decimal("0")
    total_credit: Decimal = Decimal("0")
    difference: Decimal = Decimal("0")
    is_balanced: bool = False
    evidence_checks: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("evidence_checks", mode="before")
    @classmethod
    def coerce_evidence_checks(cls, v: Any) -> Any:
        """Accept either structured check dicts or plain note strings."""
        if v is None:
            return []
        if isinstance(v, (str, dict)):
            v = [v]
        coerced = []
        for item in v:
            if isinstance(item, dict):
                coerced.append(item)
            else:
                coerced.append({"check": "note", "passed": True, "details": str(item)})
        return coerced


# ── Final Report ─────────────────────────────────────────────────────────────


class FinalAdjustmentReport(BaseModel):
    """The complete audit report for one adjustment request."""
    request_id: str
    run_id: str
    status: WorkflowStatus
    intent: str = ""
    decision_reason: str = ""
    input_sources: list[SourceMetadata] = Field(default_factory=list)
    input_hash: str = ""
    normalization_changes: list[TransformationLog] = Field(default_factory=list)
    data_quality_issues: list[DataQualityIssue] = Field(default_factory=list)
    account_candidates: list[AccountCandidate] = Field(default_factory=list)
    proposed_entry: Optional[ProposedJournalEntry] = None
    validation_results: list[ValidationResult] = Field(default_factory=list)
    reconciliation: Optional[ReconciliationResult] = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    retry_history: list[dict[str, Any]] = Field(default_factory=list)
    audit_events: list[AuditEvent] = Field(default_factory=list)
    human_review_required: bool = True
    disclaimer: str = (
        "This entry REQUIRES HUMAN REVIEW and has NOT been posted. "
        "Verify all account codes, amounts, and accounting policy before approval."
    )
