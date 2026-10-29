"""
Shared LangGraph state definition.

This TypedDict is the single mutable state object that flows through
every node in the LangGraph workflow. Nodes read from and write to
specific keys — LangGraph merges updates automatically.
"""

from __future__ import annotations

from typing import Any, Optional
from typing_extensions import TypedDict

from app.schemas import (
    AccountCandidate,
    AdjustmentRequest,
    AuditEvent,
    DataQualityIssue,
    FinalAdjustmentReport,
    ProposedJournalEntry,
    ReconciliationResult,
    SourceMetadata,
    TransformationLog,
    ValidationResult,
    WorkflowStatus,
    ChartOfAccount,
)


class WorkflowState(TypedDict, total=False):
    """
    Shared state for the LangGraph adjustment workflow.

    Every key is optional (total=False) so nodes can write
    only the fields they own. LangGraph merges partial updates.
    """

    # ── Identity ──────────────────────────────────────────────────────────
    request_id: str
    run_id: str
    idempotency_key: str
    input_hash: str

    # ── Raw input ─────────────────────────────────────────────────────────
    adjustment_request: Optional[dict[str, Any]]  # raw JSON dict
    parsed_request: Optional[AdjustmentRequest]
    source_metadata: list[SourceMetadata]

    # ── COA ───────────────────────────────────────────────────────────────
    coa_records: list[ChartOfAccount]
    coa_lookup: dict[str, ChartOfAccount]  # code -> record

    # ── Normalization ─────────────────────────────────────────────────────
    raw_records: list[dict[str, Any]]
    normalized_records: list[dict[str, Any]]
    normalization_log: list[TransformationLog]

    # ── Data quality ──────────────────────────────────────────────────────
    data_quality_issues: list[DataQualityIssue]
    intent: Optional[str]

    # ── Account mapping ───────────────────────────────────────────────────
    account_candidates: list[AccountCandidate]

    # ── Proposed entry ────────────────────────────────────────────────────
    proposed_entry: Optional[ProposedJournalEntry]

    # ── Validation & reconciliation ───────────────────────────────────────
    validation_results: list[ValidationResult]
    reconciliation_results: Optional[ReconciliationResult]

    # ── Error handling ────────────────────────────────────────────────────
    errors: list[str]
    warnings: list[str]
    retry_count: int
    retry_history: list[dict[str, Any]]

    # ── Status & audit ────────────────────────────────────────────────────
    status: WorkflowStatus
    audit_events: list[AuditEvent]

    # ── Final output ──────────────────────────────────────────────────────
    final_report: Optional[FinalAdjustmentReport]

    # ── Duplicate tracking (across the full batch) ────────────────────────
    seen_idempotency_keys: set[str]
