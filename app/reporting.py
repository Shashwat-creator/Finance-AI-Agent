"""
Reporting module for the Manual Adjustments Agent.

Generates structured JSON reports with strict Decimal-to-string serialization
and rich, human-readable Markdown reports detailing lineage, normalizations,
account mappings, proposed journal entries, validations, and reconciliation.
Always surfaces prominent disclaimers requiring human sign-off prior to posting.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.schemas import FinalAdjustmentReport, WorkflowStatus

logger = logging.getLogger(__name__)


def _sanitize_filename(name: str) -> str:
    """Sanitize strings for safe usage in file system paths."""
    return re.sub(r'[\\/*?:"<>| ]', "_", name)


def _custom_json_serializer(obj: Any) -> Any:
    """Serialize Decimals, enums, and dates to JSON-safe strings."""
    if isinstance(obj, Decimal):
        return str(obj)
    if hasattr(obj, "value"):  # Enum support
        return obj.value
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def generate_json_report(report: FinalAdjustmentReport, output_dir: Path) -> Path:
    """
    Generate and save a machine-readable JSON audit report.

    Saves as {run_id}_{request_id}.json in output_dir.
    Serializes all Decimals as strings to prevent floating-point inaccuracy.

    Args:
        report: FinalAdjustmentReport model instance.
        output_dir: Destination directory for the report.

    Returns:
        Path to the saved JSON report file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_run_id = _sanitize_filename(report.run_id or "run")
    safe_req_id = _sanitize_filename(report.request_id or "adjustment")
    filename = f"{safe_run_id}_{safe_req_id}.json"
    file_path = output_dir / filename

    # Use Pydantic's model_dump(mode="json")
    report_dict = report.model_dump(mode="json")

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(report_dict, f, indent=2, ensure_ascii=False, default=_custom_json_serializer)

    logger.info("Generated JSON audit report: %s", file_path)
    return file_path


def generate_markdown_report(report: FinalAdjustmentReport) -> str:
    """
    Generate a human-readable Markdown audit report for finance reviewers.

    Includes:
      - Header with request_id, run_id, and workflow status
      - Prominent disclaimer that human review is required before posting
      - Input sources lineage table
      - Normalization changes and transformations
      - Account mapping candidates
      - Proposed journal entry lines table with debit/credit totals
      - Validation results table
      - Reconciliation summary
      - Warnings and errors lists

    Args:
        report: FinalAdjustmentReport model instance.

    Returns:
        Formatted Markdown report string.
    """
    status_str = (
        report.status.value
        if isinstance(report.status, WorkflowStatus)
        else str(report.status)
    )

    lines: list[str] = []

    # 1. Header
    lines.append(f"# Manual Adjustment Audit Report: {report.request_id}")
    lines.append("")
    lines.append(f"**Run ID**: `{report.run_id}`  ")
    lines.append(f"**Workflow Status**: `{status_str}`  ")
    if report.intent:
        lines.append(f"**Classified Intent**: `{report.intent}`  ")
    lines.append(f"**Human Review Required**: `{report.human_review_required}`  ")
    if report.input_hash:
        lines.append(f"**Input Hash**: `{report.input_hash}`  ")
    lines.append("")

    # 2. Decision (plain English)
    if report.decision_reason:
        lines.append("## 0. Decision")
        lines.append("")
        for reason_line in report.decision_reason.splitlines():
            if reason_line.strip().startswith("-"):
                lines.append(reason_line)
            else:
                lines.append(reason_line)
        lines.append("")


    # 2. Disclaimer
    lines.append("> [!IMPORTANT]")
    lines.append("> **DISCLAIMER: REQUIRES HUMAN REVIEW — NOT POSTED**")
    lines.append(f"> {report.disclaimer}")
    lines.append("")

    # 3. Input Sources Table
    lines.append("## 1. Input Sources & Lineage")
    lines.append("")
    if report.input_sources:
        lines.append("| Filename | SHA-256 Hash (First 16) | Rows | Records | Ingestion Timestamp |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for src in report.input_sources:
            hash_abbr = f"`{src.sha256_hash[:16]}...`" if src.sha256_hash else "N/A"
            rows = src.row_count if src.row_count is not None else "-"
            records = src.record_count if src.record_count is not None else "-"
            lines.append(f"| {src.filename} | {hash_abbr} | {rows} | {records} | {src.ingestion_timestamp} |")
    else:
        lines.append("*No input source metadata recorded.*")
    lines.append("")

    # 4. Normalization Changes Table
    lines.append("## 2. Normalization & Transformation Log")
    lines.append("")
    if report.normalization_changes:
        lines.append("| Field | Original Value | Normalized Value | Rule | Source Ref |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for log in report.normalization_changes:
            lines.append(
                f"| `{log.field}` | `{log.original_value}` | `{log.normalized_value}` | `{log.rule}` | {log.source_reference or '-'} |"
            )
    else:
        lines.append("*No data transformations were required.*")
    lines.append("")

    # 4b. Data Quality Issues (if present)
    if report.data_quality_issues:
        lines.append("### Data Quality Flags")
        lines.append("")
        lines.append("| Field | Severity | Reason | Original | Suggested |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for issue in report.data_quality_issues:
            orig = f"`{issue.original_value}`" if issue.original_value is not None else "-"
            sugg = f"`{issue.suggested_value}`" if issue.suggested_value is not None else "-"
            lines.append(
                f"| `{issue.field}` | `{issue.severity.value}` | {issue.reason} | {orig} | {sugg} |"
            )
        lines.append("")

    # 5. Account Candidates Table
    lines.append("## 3. Account Mapping Candidates")
    lines.append("")
    if report.account_candidates:
        lines.append("| Rank | Account Code | Account Name | Confidence | Evidence |")
        lines.append("| :---: | :--- | :--- | :---: | :--- |")
        for cand in report.account_candidates:
            conf_pct = f"{cand.confidence * 100:.1f}%"
            lines.append(
                f"| {cand.rank} | `{cand.account_code}` | {cand.account_name} | {conf_pct} | {cand.evidence} |"
            )
    else:
        lines.append("*No account mapping candidates generated.*")
    lines.append("")

    # 6. Proposed Journal Entry Lines Table
    lines.append("## 4. Proposed Journal Entry")
    lines.append("")
    entry = report.proposed_entry
    if entry:
        lines.append(f"**Description**: {entry.description}  ")
        lines.append(f"**Period**: `{entry.period}` | **Currency**: `{entry.currency}`  ")
        if entry.rationale:
            lines.append(f"**Rationale**: {entry.rationale}  ")
        lines.append("")

        lines.append(f"| Account Code | Account Name | Debit ({entry.currency}) | Credit ({entry.currency}) | Memo |")
        lines.append("| :--- | :--- | :---: | :---: | :--- |")
        for line in entry.lines:
            acc_name = line.account_name or "N/A"
            debit_str = f"{line.debit:,.2f}" if line.debit > 0 else "-"
            credit_str = f"{line.credit:,.2f}" if line.credit > 0 else "-"
            memo_str = line.memo or ""
            lines.append(
                f"| `{line.account_code}` | {acc_name} | {debit_str} | {credit_str} | {memo_str} |"
            )

        # Totals row
        total_debit_str = f"{entry.total_debit:,.2f}"
        total_credit_str = f"{entry.total_credit:,.2f}"
        diff_str = f"{entry.difference:,.2f}"
        lines.append(f"| **TOTALS** | | **{total_debit_str}** | **{total_credit_str}** | Difference: **{diff_str}** |")
        lines.append("")

        if entry.source_references:
            lines.append(f"**Source References**: {', '.join(entry.source_references)}")
            lines.append("")
    else:
        lines.append("*No journal entry proposed for this request.*")
        lines.append("")

    # 7. Validation Results Table
    lines.append("## 5. Validation Results")
    lines.append("")
    if report.validation_results:
        lines.append("| Rule ID | Rule Name | Status | Message |")
        lines.append("| :--- | :--- | :---: | :--- |")
        for val in report.validation_results:
            status_val = val.status.value if hasattr(val.status, "value") else str(val.status)
            lines.append(f"| `{val.rule_id}` | {val.rule_name} | `{status_val}` | {val.message} |")
    else:
        lines.append("*No deterministic validation rules were executed.*")
    lines.append("")

    # 8. Reconciliation Summary
    lines.append("## 6. Reconciliation Summary")
    lines.append("")
    recon = report.reconciliation
    if recon:
        balanced_badge = "[BALANCED]" if recon.is_balanced else "[UNBALANCED]"
        lines.append(f"- **Balance Status**: {balanced_badge}")
        lines.append(f"- **Total Debits**: `{recon.total_debit:,.2f}`")
        lines.append(f"- **Total Credits**: `{recon.total_credit:,.2f}`")
        lines.append(f"- **Difference**: `{recon.difference:,.2f}`")
        if recon.evidence_checks:
            lines.append("- **Evidence Checks**:")
            for check in recon.evidence_checks:
                lines.append(f"  - {check}")
        if recon.warnings:
            lines.append("- **Reconciliation Warnings**:")
            for warn in recon.warnings:
                lines.append(f"  - WARNING: {warn}")
    else:
        lines.append("*Reconciliation was not performed.*")
    lines.append("")

    # 9. Warnings and Errors
    lines.append("## 7. Operational Warnings & Errors")
    lines.append("")
    if report.warnings:
        lines.append("### Warnings")
        for w in report.warnings:
            lines.append(f"- WARNING: {w}")
        lines.append("")
    else:
        lines.append("### Warnings: None")
        lines.append("")

    if report.errors:
        lines.append("### Errors")
        for e in report.errors:
            lines.append(f"- ERROR: {e}")
        lines.append("")
    else:
        lines.append("### Errors: None")
        lines.append("")

    # 10. Retry History (if any)
    if report.retry_history:
        lines.append("### Retry History")
        for attempt in report.retry_history:
            lines.append(f"- {attempt}")
        lines.append("")

    return "\n".join(lines)
