"""
Deterministic workflow orchestrator for the Manual Adjustments Agent.

This is the state machine that turns a batch of raw, messy journal entries into
auditable decision records. It is intentionally *not* a swarm of LLM agents:

    ingest -> normalize -> classify intent -> map unknown accounts
           -> deterministic validation -> semantic checks -> reconcile
           -> decide status -> write JSON/Markdown reports

Only intent classification and account-candidate suggestion have an optional
LLM path; every number, status and rejection reason is produced by code.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from app.agents.intake_agent import classify_intent_llm, evaluate_intent_candidate
from app.agents.mapper_agent import map_unknown_accounts
from app.audit import AuditTrail
from app.config import (
    EXPECTED_PERIOD,
    FUNCTIONAL_CURRENCY,
    LOG_DIR,
    is_mock_mode,
    validate_config,
)
from app.ingestion import (
    load_adjustments,
    load_chart_of_accounts,
    load_fx_rates,
    load_trial_balance,
)
from app.normalizer import (
    check_duplicate,
    generate_idempotency_key,
    normalize_adjustment,
)
from app.reconciliation import reconcile_entry
from app.reporting import generate_json_report, generate_markdown_report
from app.schemas import (
    AdjustmentRequest,
    ChartOfAccount,
    FinalAdjustmentReport,
    IssueSeverity,
    JournalEntryLine,
    ProposedJournalEntry,
    SourceMetadata,
    ValidationResult,
    ValidationStatus,
    WorkflowStatus,
)
from app.validator import (
    check_entry_semantics,
    get_validation_summary,
    validate_journal_entry,
)

logger = logging.getLogger("manual_adjustments.workflow")

# Rules whose failure means the entry cannot be posted as written.
HARD_FAIL_RULES = {
    "VAL-004",  # invalid amounts
    "VAL-006",  # header / non-postable account
    "VAL-007",  # both sides / neither side
    "VAL-008",  # debits != credits
    "VAL-009",  # < 2 lines
    "VAL-010",  # duplicate batch entry
    "SEM-001",  # single-account wash
    "SEM-002",  # intercompany without counterparty
    "SEM-006",  # date outside period
}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _safe_name(value: str) -> str:
    return re.sub(r'[\\/*?:"<>| ]', "_", str(value or "unnamed"))


def _money(value: Decimal | int | float | str) -> str:
    try:
        return f"{Decimal(str(value)):,.2f}"
    except Exception:
        return str(value)


def _new_run_id(seed: str = "") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:6]
    if seed:
        suffix = f"{suffix}-{seed[:6]}"
    return f"RUN-{stamp}-{suffix}"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ── Input loading ────────────────────────────────────────────────────────────


def load_inputs(inputs_dir: Path) -> dict[str, Any]:
    """Load every input file with lineage metadata and data-quality warnings."""
    inputs_dir = Path(inputs_dir)
    coa_records, coa_meta = load_chart_of_accounts(inputs_dir / "chart_of_accounts.csv")
    requests_raw, adj_meta = load_adjustments(inputs_dir / "manual_adjustments.json")
    tb_df, tb_meta, tb_warnings = load_trial_balance(
        inputs_dir / "trial_balance.csv", coa=coa_records
    )
    fx_rates, fx_meta, fx_warnings = load_fx_rates(inputs_dir / "fx_rates.csv")

    source_metadata = [coa_meta, adj_meta, tb_meta, fx_meta]
    input_warnings = tb_warnings + fx_warnings

    # Prior-period trial balance is used for roll-forward lineage only. We load it
    # (rather than ignore it) so renames and prior imbalances are surfaced.
    prior_path = inputs_dir / "prior_period_tb.csv"
    if prior_path.is_file():
        prior_df, prior_meta, prior_warnings = load_trial_balance(
            prior_path, coa=coa_records
        )
        source_metadata.append(prior_meta)
        input_warnings.extend(prior_warnings)

        current_codes = set(tb_df["account_code"].astype(str).str.strip())
        prior_codes = set(prior_df["account_code"].astype(str).str.strip())
        prior_names = dict(
            zip(
                prior_df["account_code"].astype(str).str.strip(),
                prior_df["account_name"].astype(str).str.strip(),
            )
        )
        for code in sorted(prior_codes - current_codes):
            input_warnings.append(
                f"Prior-period account '{code}' ('{prior_names.get(code, '')}') is "
                "absent from the current trial balance; confirm any account "
                "renumbering before roll-forward."
            )

    return {
        "coa_records": coa_records,
        "coa_lookup": {record.account_code: record for record in coa_records},
        "requests": requests_raw,
        "tb_df": tb_df,
        "fx_rates": fx_rates,
        "source_metadata": source_metadata,
        "input_warnings": _dedupe(input_warnings),
    }


# ── Per-request processing ───────────────────────────────────────────────────


def _build_proposed_entry(
    request: AdjustmentRequest,
    coa_lookup: dict[str, ChartOfAccount],
    period: str,
    intent: str,
) -> ProposedJournalEntry:
    lines: list[JournalEntryLine] = []
    for line in request.lines:
        account = coa_lookup.get(line.account)
        lines.append(
            JournalEntryLine(
                account_code=line.account,
                account_name=account.account_name if account else None,
                debit=line.debit,
                credit=line.credit,
                memo=line.memo,
            )
        )

    return ProposedJournalEntry(
        request_id=request.id,
        description=request.description,
        period=request.period or period,
        currency=request.currency or FUNCTIONAL_CURRENCY,
        date=request.date,
        lines=lines,
        rationale=(
            f"Intent classified as {intent}. Proposed entry reconstructed from the "
            "submitted adjustment lines; nothing has been posted."
        ),
        source_references=[request.source] if request.source else [],
    )


def _decide_status(
    validation_results: list[ValidationResult],
    quality_issues: list[Any],
    is_duplicate: bool,
) -> WorkflowStatus:
    failed = [r for r in validation_results if r.status == ValidationStatus.FAIL]
    failed_ids = {r.rule_id for r in failed}
    has_clarification = any(
        issue.severity == IssueSeverity.CLARIFICATION_REQUIRED for issue in quality_issues
    )

    if is_duplicate or (failed_ids & HARD_FAIL_RULES):
        return WorkflowStatus.REJECTED_VALIDATION
    if failed_ids or has_clarification:
        return WorkflowStatus.NEEDS_CLARIFICATION
    return WorkflowStatus.READY_FOR_REVIEW


def _build_decision_reason(
    status: WorkflowStatus,
    intent: str,
    validation_results: list[ValidationResult],
    quality_issues: list[Any],
    candidates: list[Any],
    unknown_accounts: list[str],
    proposed: ProposedJournalEntry,
    is_duplicate: bool,
    warnings: list[str],
) -> str:
    failed = [r for r in validation_results if r.status == ValidationStatus.FAIL]
    clarifications = [
        issue
        for issue in quality_issues
        if issue.severity == IssueSeverity.CLARIFICATION_REQUIRED
    ]

    if status == WorkflowStatus.READY_FOR_REVIEW:
        head = (
            f"Approved for human review. Intent: {intent}. "
            f"Debits equal credits at {_money(proposed.total_debit)} "
            f"{proposed.currency}. No posting occurs until a finance user signs off."
        )
        if warnings:
            head = (
                f"Approved for human review with {len(warnings)} warning(s) that need "
                f"reviewer attention. Intent: {intent}. Debits equal credits at "
                f"{_money(proposed.total_debit)} {proposed.currency}. No posting occurs "
                "until a finance user signs off."
            )
            return "\n\n- ".join([head, *warnings])
        return head

    if status == WorkflowStatus.NEEDS_CLARIFICATION:
        parts = ["Paused for human clarification; do not post yet."]
        if is_duplicate:
            parts.append("This entry duplicates another entry already seen in the batch.")
        for result in failed:
            if result.rule_id == "VAL-008":
                parts.append(
                    "The entry does not balance: debits are "
                    f"{_money(proposed.total_debit)} but credits are "
                    f"{_money(proposed.total_credit)}, a difference of "
                    f"{_money(abs(proposed.difference))}. Correct the missing or "
                    "mis-stated line."
                )
            else:
                parts.append(f"{result.rule_id}: {result.message}")
        for issue in clarifications[:6]:
            if (
                any(result.rule_id == "VAL-005" for result in failed)
                and "not found in chart of accounts" in issue.reason.lower()
            ):
                continue
            parts.append(f"{issue.field}: {issue.reason}")
        if candidates:
            top = candidates[0]
            codes = ", ".join(sorted(set(unknown_accounts))) or top.account_code
            parts.append(
                f"Unknown account(s) {codes} have no COA node. Nearest match: "
                f"{top.account_code} {top.account_name} "
                f"({top.confidence * 100:.0f}% confidence). Confirm the mapping or create an account."
            )
        for warning in warnings:
            if warning not in parts:
                parts.append(warning)
        return "\n\n- ".join([parts[0], *parts[1:]])

    # REJECTED_VALIDATION
    parts = ["Rejected by deterministic controls; this entry must not be posted."]
    if is_duplicate:
        parts.append("Duplicate of another entry already seen in the batch.")
    for result in failed:
        if result.rule_id == "VAL-008":
            parts.append(
                "The entry does not balance: debits are "
                f"{_money(proposed.total_debit)} but credits are "
                f"{_money(proposed.total_credit)}, a difference of "
                f"{_money(abs(proposed.difference))}. "
                f"Rejected at {result.rule_id}."
            )
        else:
            parts.append(f"{result.rule_id}: {result.message}")
    if not failed:
        parts.append("One or more accounting-integrity checks failed.")
    return "\n\n- ".join([parts[0], *parts[1:]])


def process_request(
    request: AdjustmentRequest,
    coa_records: list[ChartOfAccount],
    coa_lookup: dict[str, ChartOfAccount],
    tb_df: Any,
    fx_rates: dict[str, dict[str, Decimal]],
    run_id: str,
    period: str,
    source_metadata: list[SourceMetadata],
    seen_keys: Optional[set[str]] = None,
) -> FinalAdjustmentReport:
    """Process a single adjustment request and return its audit report."""
    seen_keys = seen_keys if seen_keys is not None else set()
    request_id = request.id or "UNKNOWN"
    trail = AuditTrail(request_id=request_id, run_id=run_id)

    try:
        from app.integrations import CosmosDecisionStore
        durable_store = CosmosDecisionStore()
        trail.record("intake", "node_start", {"description": request.description})

        # 1. Normalize + data quality
        normalized, norm_logs, quality_issues = normalize_adjustment(request)
        trail.record(
            "normalizer",
            "transformation",
            {
                "transformations": len(norm_logs),
                "quality_issues": len(quality_issues),
            },
        )

        # 2. Idempotency / duplicate detection
        idempotency_key = generate_idempotency_key(normalized)
        is_duplicate = check_duplicate(idempotency_key, seen_keys)
        prior_decision = durable_store.get(idempotency_key) if durable_store.enabled else None
        is_duplicate = is_duplicate or prior_decision is not None
        seen_keys.add(idempotency_key)

        # 3. Intent
        intent = classify_intent_llm(
            normalized.description, request_id=request_id, run_id=run_id
        )
        llm_evaluation = evaluate_intent_candidate(
            normalized.description, intent, request_id=request_id, run_id=run_id
        )
        trail.record(
            "intake",
            "intent_classified",
            {
                "intent": intent,
                "evaluator_supported": llm_evaluation.get("supported"),
                "evaluator_hallucination": llm_evaluation.get("hallucination"),
                "evaluator_attempts": llm_evaluation.get("attempts"),
                "evaluator_model": llm_evaluation.get("model"),
                "privacy_redactions": llm_evaluation.get("redaction_count", 0),
                "prompt_injection_detected": llm_evaluation.get(
                    "injection_detected", False
                ),
            },
        )

        # 4. Mapping candidates for unknown accounts
        unknown_accounts = [
            line.account for line in normalized.lines if line.account not in coa_lookup
        ]
        hint_text = " ".join(
            [normalized.description]
            + [str(line.memo or "") for line in normalized.lines]
        )
        candidates = map_unknown_accounts(
            accounts=unknown_accounts,
            hint_text=hint_text,
            coa_records=coa_records,
            top_k=3,
        )
        trail.record(
            "mapper",
            "mapping_decision",
            {
                "unknown_accounts": sorted(set(unknown_accounts)),
                "candidates": [
                    {
                        "account_code": candidate.account_code,
                        "confidence": candidate.confidence,
                        "rank": candidate.rank,
                    }
                    for candidate in candidates
                ],
            },
        )

        # 5. Build proposed entry and run deterministic validation
        proposed = _build_proposed_entry(normalized, coa_lookup, period, intent)
        entry_dict = normalized.model_dump()
        entry_dict["is_duplicate"] = is_duplicate

        validation_results = validate_journal_entry(
            entry_dict, coa_lookup, expected_period=period, is_duplicate=is_duplicate
        )
        trail.record(
            "validator",
            "validation",
            {
                "failed": [
                    result.rule_id
                    for result in validation_results
                    if result.status == ValidationStatus.FAIL
                ]
            },
        )

        # 6. Semantic checks (circular IC, TBD categories, FX tie-out, dates)
        semantic_results, semantic_warnings = check_entry_semantics(
            entry_dict,
            coa_lookup,
            fx_rates=fx_rates,
            tb_df=tb_df,
            expected_period=period,
        )
        all_results = validation_results + semantic_results
        trail.record(
            "validator",
            "semantic_checks",
            {
                "failed": [
                    result.rule_id
                    for result in semantic_results
                    if result.status == ValidationStatus.FAIL
                ]
            },
        )

        # 7. Reconcile exact totals
        reconciliation = reconcile_entry(proposed, coa_lookup)
        trail.record(
            "reconciler",
            "reconciliation",
            {
                "total_debit": proposed.total_debit,
                "total_credit": proposed.total_credit,
                "difference": proposed.difference,
                "is_balanced": reconciliation.is_balanced,
            },
        )

        # 8. Decide status + plain-English reason
        failed_results = [
            result for result in all_results if result.status == ValidationStatus.FAIL
        ]
        warnings = _dedupe(reconciliation.warnings + semantic_warnings)
        if llm_evaluation.get("hallucination") is True or llm_evaluation.get("supported") is False:
            warnings.append("LLM evaluator flagged the intent as potentially unsupported; deterministic controls remain authoritative.")
        warnings = [
            warning
            for warning in warnings
            if "not balanced" not in warning.lower()
            and "not found in chart of accounts" not in warning.lower()
            # Normal-balance direction flips legitimately on reclasses,
            # revaluations and DTA/DTL movements, so it is audit evidence
            # rather than a review-blocking warning.
            and "normal balance" not in warning.lower()
        ]
        errors = [
            f"{result.rule_id}: {result.message}" for result in failed_results
        ]

        status = _decide_status(all_results, quality_issues, is_duplicate)
        decision_reason = _build_decision_reason(
            status,
            intent,
            all_results,
            quality_issues,
            candidates,
            unknown_accounts,
            proposed,
            is_duplicate,
            warnings,
        )
        trail.record(
            "workflow",
            "node_end",
            {"status": status.value, "intent": intent},
        )

        final_report = FinalAdjustmentReport(
            request_id=request_id,
            run_id=run_id,
            status=status,
            intent=intent,
            decision_reason=decision_reason,
            input_sources=source_metadata,
            input_hash=idempotency_key,
            normalization_changes=norm_logs,
            data_quality_issues=quality_issues,
            account_candidates=candidates,
            proposed_entry=proposed,
            validation_results=all_results,
            reconciliation=reconciliation,
            warnings=warnings,
            errors=errors,
            audit_events=trail.get_events(),
            human_review_required=True,
        )
        if durable_store.enabled:
            durable_store.save(idempotency_key, {
                "request_id": request_id,
                "run_id": run_id,
                "status": final_report.status.value,
                "decision_reason": final_report.decision_reason,
                "report": final_report.model_dump(mode="json"),
            })
        return final_report

    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Unexpected failure processing %s: %s", request_id, exc)
        trail.record("workflow", "node_failure", {"error": str(exc)})
        return FinalAdjustmentReport(
            request_id=request_id,
            run_id=run_id,
            status=WorkflowStatus.FAILED_SAFE,
            input_sources=source_metadata,
            errors=[f"Unexpected error: {exc}"],
            warnings=["Entry was not processed; see errors."],
            audit_events=trail.get_events(),
            human_review_required=True,
        )


# ── Batch run ────────────────────────────────────────────────────────────────


def _write_markdown_summary(
    summary: dict[str, Any],
    reports: list[FinalAdjustmentReport],
    path: Path,
) -> None:
    lines: list[str] = []
    lines.append("# Manual Adjustments - Batch Decision Summary")
    lines.append("")
    lines.append(f"**Run ID**: `{summary['run_id']}`  ")
    lines.append(f"**Period**: `{summary['period']}` | **Functional currency**: `{summary['functional_currency']}`  ")
    lines.append(f"**Mode**: `{'offline / deterministic' if summary['offline'] else 'LLM-assisted'}`  ")
    lines.append(f"**Generated**: {summary['generated_at']}  ")
    lines.append("")
    lines.append("> [!IMPORTANT]")
    lines.append("> Nothing in this run has been posted. Every entry requires human review and approval.")
    lines.append("")

    lines.append("## Decision Counts")
    lines.append("")
    lines.append("| Outcome | Count |")
    lines.append("| :--- | ---: |")
    for key, value in summary["counts"].items():
        lines.append(f"| {key.replace('_', ' ').title()} | {value} |")
    lines.append("")

    lines.append("## Per-Entry Decisions")
    lines.append("")
    lines.append("| Entry | Intent | Status | Debit | Credit | Difference | Reason |")
    lines.append("| :--- | :--- | :--- | ---: | ---: | ---: | :--- |")
    for report in reports:
        entry = report.proposed_entry
        debit = _money(entry.total_debit) if entry else "-"
        credit = _money(entry.total_credit) if entry else "-"
        diff = _money(entry.difference) if entry else "-"
        reason = (report.decision_reason or "").replace("\n", " ").replace("|", "/")
        if len(reason) > 220:
            reason = reason[:217] + "..."
        lines.append(
            f"| `{report.request_id}` | {report.intent or '-'} | `{report.status.value}` | "
            f"{debit} | {credit} | {diff} | {reason} |"
        )
    lines.append("")

    lines.append("## Input Lineage")
    lines.append("")
    lines.append("| File | SHA-256 (first 16) | Rows |")
    lines.append("| :--- | :--- | ---: |")
    for meta in summary["inputs"]:
        lines.append(
            f"| {meta['filename']} | `{meta['sha256_hash'][:16]}...` | "
            f"{meta.get('row_count', '-')} |"
        )
    lines.append("")

    if summary["input_warnings"]:
        lines.append("## Input Data-Quality Warnings")
        lines.append("")
        for warning in summary["input_warnings"]:
            lines.append(f"- WARNING: {warning}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def run_batch(
    inputs_dir: Path,
    output_dir: Path,
    period: str = EXPECTED_PERIOD,
    offline: Optional[bool] = None,
    write: bool = True,
) -> dict[str, Any]:
    """
    Run the full manual-adjustments workflow over a directory of inputs.

    Returns a dict with the run id, per-entry reports, summary, and output path.
    """
    if offline is not None:
        os.environ["OFFLINE"] = "1" if offline else "0"

    output_dir = Path(output_dir)
    if write:
        output_dir.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    for warning in validate_config():
        logger.warning("Configuration: %s", warning)

    data = load_inputs(Path(inputs_dir))
    seed = "+".join(meta.sha256_hash for meta in data["source_metadata"])
    run_id = _new_run_id(seed)

    logger.info(
        "Starting run %s over %d adjustments (offline=%s)",
        run_id,
        len(data["requests"]),
        is_mock_mode(),
    )

    seen_keys: set[str] = set()
    reports: list[FinalAdjustmentReport] = []
    for request in data["requests"]:
        report = process_request(
            request=request,
            coa_records=data["coa_records"],
            coa_lookup=data["coa_lookup"],
            tb_df=data["tb_df"],
            fx_rates=data["fx_rates"],
            run_id=run_id,
            period=period,
            source_metadata=data["source_metadata"],
            seen_keys=seen_keys,
        )
        reports.append(report)

        if write:
            json_path = generate_json_report(report, output_dir)
            md_path = output_dir / f"{_safe_name(run_id)}_{_safe_name(report.request_id)}.md"
            md_path.write_text(generate_markdown_report(report), encoding="utf-8")
            logger.info(
                "Wrote %s -> %s (%s)",
                report.request_id,
                report.status.value,
                json_path.name,
            )

    counts = {
        "total": len(reports),
        "ready_for_review": sum(1 for r in reports if r.status == WorkflowStatus.READY_FOR_REVIEW),
        "needs_clarification": sum(1 for r in reports if r.status == WorkflowStatus.NEEDS_CLARIFICATION),
        "rejected_validation": sum(1 for r in reports if r.status == WorkflowStatus.REJECTED_VALIDATION),
        "failed_safe": sum(1 for r in reports if r.status == WorkflowStatus.FAILED_SAFE),
    }

    accepted = [r for r in reports if r.status == WorkflowStatus.READY_FOR_REVIEW]
    total_debit = sum((r.proposed_entry.total_debit for r in accepted if r.proposed_entry), Decimal("0"))
    total_credit = sum((r.proposed_entry.total_credit for r in accepted if r.proposed_entry), Decimal("0"))

    summary: dict[str, Any] = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": period,
        "functional_currency": FUNCTIONAL_CURRENCY,
        "offline": is_mock_mode(),
        "counts": counts,
        "accepted_entry_ids": [r.request_id for r in accepted],
        "rejected": [
            {
                "request_id": r.request_id,
                "status": r.status.value,
                "reason": r.decision_reason,
            }
            for r in reports
            if r.status != WorkflowStatus.READY_FOR_REVIEW
        ],
        "totals": {
            "proposed_debit": str(total_debit),
            "proposed_credit": str(total_credit),
            "difference": str(total_debit - total_credit),
        },
        "inputs": [meta.model_dump() for meta in data["source_metadata"]],
        "input_warnings": data["input_warnings"],
    }

    if write:
        summary_json = output_dir / "batch_summary.json"
        summary_json.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        decision_log = {
            "run_id": run_id,
            "generated_at": summary["generated_at"],
            "decisions": [
                {
                    "request_id": r.request_id,
                    "status": r.status.value,
                    "intent": r.intent,
                    "idempotency_key": r.input_hash,
                    "decision_reason": r.decision_reason,
                    "failed_rules": [
                        result.rule_id
                        for result in r.validation_results
                        if result.status == ValidationStatus.FAIL
                    ],
                    "account_candidates": [
                        {
                            "account_code": candidate.account_code,
                            "account_name": candidate.account_name,
                            "confidence": candidate.confidence,
                            "rank": candidate.rank,
                            "evidence": candidate.evidence,
                        }
                        for candidate in r.account_candidates
                    ],
                }
                for r in reports
            ],
        }
        (output_dir / "decision_log.json").write_text(
            json.dumps(decision_log, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        accepted_payload = {
            "run_id": run_id,
            "period": period,
            "functional_currency": FUNCTIONAL_CURRENCY,
            "note": "Accepted for human review only. Nothing has been posted.",
            "entries": [
                {
                    "id": r.request_id,
                    "intent": r.intent,
                    "description": r.proposed_entry.description,
                    "date": r.proposed_entry.date,
                    "currency": r.proposed_entry.currency,
                    "lines": [
                        {
                            "account": line.account_code,
                            "account_name": line.account_name,
                            "debit": str(line.debit),
                            "credit": str(line.credit),
                            "memo": line.memo,
                        }
                        for line in r.proposed_entry.lines
                    ],
                    "total_debit": str(r.proposed_entry.total_debit),
                    "total_credit": str(r.proposed_entry.total_credit),
                    "source_references": r.proposed_entry.source_references,
                    "run_id": run_id,
                }
                for r in accepted
            ],
        }
        (output_dir / "accepted_adjustments.json").write_text(
            json.dumps(accepted_payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        _write_markdown_summary(summary, reports, output_dir / "batch_summary.md")

        logger.info(
            "Run %s complete: %d total, %d ready, %d need clarification, %d rejected",
            run_id,
            counts["total"],
            counts["ready_for_review"],
            counts["needs_clarification"],
            counts["rejected_validation"],
        )

    return {
        "run_id": run_id,
        "reports": reports,
        "summary": summary,
        "output_dir": str(output_dir),
    }
