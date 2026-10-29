"""
Normalization and data quality helpers for adjustment requests.

Enforces deterministic cleanup:
- Trims whitespace and normalizes casing
- Logs every transformation with field, original, normalized, rule, source_reference
- Detects missing periods, currencies, dates, sources, memos, and ambiguous amounts
- Computes deterministic SHA-256 idempotency keys and detects duplicates

Nothing here calls an LLM: this module is the deterministic front door that makes
the rest of the pipeline reproducible.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app.config import SUPPORTED_CURRENCIES
from app.schemas import (
    AdjustmentRequest,
    DataQualityIssue,
    IssueSeverity,
    TransformationLog,
)

_DATE_FORMAT = "%Y-%m-%d"


# ── Idempotency & duplicate detection ────────────────────────────────────────


def compute_idempotency_key(request_dict: dict[str, Any]) -> str:
    """
    Compute a stable SHA-256 idempotency key based on core adjustment data.

    Source free-text is deliberately excluded so the same economic entry is
    recognised as a duplicate even when the source note changes.
    """
    lines = []
    for line in request_dict.get("lines", []):
        lines.append(
            {
                "account": str(line.get("account", "")).strip().upper(),
                "debit": str(line.get("debit", "0")).strip(),
                "credit": str(line.get("credit", "0")).strip(),
                "memo": str(line.get("memo", "")).strip(),
            }
        )
    lines.sort(key=lambda x: (x["account"], x["debit"], x["credit"], x["memo"]))

    payload = {
        "id": str(request_dict.get("id", "")).strip(),
        "description": str(request_dict.get("description", "")).strip(),
        "date": str(request_dict.get("date", "")).strip(),
        "period": str(request_dict.get("period", "")).strip(),
        "currency": str(request_dict.get("currency", "")).strip().upper(),
        "lines": lines,
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def generate_idempotency_key(request: Any) -> str:
    """Idempotency key for a Pydantic model or plain dict request."""
    if hasattr(request, "model_dump"):
        request_dict = request.model_dump()
    elif isinstance(request, dict):
        request_dict = request
    else:
        raise TypeError(f"Unsupported request type for idempotency key: {type(request)!r}")
    return compute_idempotency_key(request_dict)


def check_duplicate(idempotency_key: str, seen_keys: set[str]) -> bool:
    """Return True if the key has already been processed in this batch."""
    return idempotency_key in seen_keys


# ── Normalization ────────────────────────────────────────────────────────────


def normalize_adjustment(
    request: Any,
    source_reference: str = "",
) -> tuple[AdjustmentRequest, list[TransformationLog], list[DataQualityIssue]]:
    """
    Normalize an AdjustmentRequest model (or dict) and return a cleaned model.

    This is the public, model-friendly wrapper around
    :func:`normalize_adjustment_request`.
    """
    if hasattr(request, "model_dump"):
        raw_request = request.model_dump()
    elif isinstance(request, dict):
        raw_request = request
    else:
        raise TypeError(f"Unsupported request type for normalization: {type(request)!r}")

    normalized, logs, issues = normalize_adjustment_request(raw_request, source_reference)

    model = AdjustmentRequest(
        id=str(normalized.get("id", "")),
        description=str(normalized.get("description", "")),
        date=str(normalized.get("date", "")),
        source=str(normalized.get("source", "")),
        lines=normalized.get("lines", []),
        period=normalized.get("period"),
        currency=normalized.get("currency"),
    )
    return model, logs, issues


def _issue(
    field: str,
    severity: IssueSeverity,
    reason: str,
    source_ref: str,
    original_value: Optional[str] = None,
    suggested_value: Optional[str] = None,
) -> DataQualityIssue:
    return DataQualityIssue(
        field=field,
        severity=severity,
        reason=reason,
        original_value=original_value,
        suggested_value=suggested_value,
        source_reference=source_ref,
    )


def normalize_adjustment_request(
    raw_request: dict[str, Any],
    source_reference: str = "",
) -> tuple[dict[str, Any], list[TransformationLog], list[DataQualityIssue]]:
    """
    Normalize an adjustment request and classify data quality issues.

    Returns:
        tuple of (normalized_dict, transformation_logs, data_quality_issues)
    """
    normalized = copy.deepcopy(raw_request)
    logs: list[TransformationLog] = []
    issues: list[DataQualityIssue] = []

    req_id = str(raw_request.get("id", "")).strip()
    source_ref = source_reference or raw_request.get("source", "") or f"request:{req_id}"

    def _trim(field: str, required: bool = False) -> None:
        original = raw_request.get(field)
        if original is None or str(original).strip() == "":
            if required:
                issues.append(
                    _issue(
                        field=field,
                        severity=IssueSeverity.CLARIFICATION_REQUIRED,
                        reason=f"Missing required field: {field}",
                        source_ref=source_ref,
                        original_value=None if original is None else str(original),
                    )
                )
            return
        value = str(original).strip()
        if value != str(original):
            normalized[field] = value
            logs.append(
                TransformationLog(
                    field=field,
                    original_value=str(original),
                    normalized_value=value,
                    rule="TRIM_WHITESPACE",
                    source_reference=source_ref,
                )
            )
            issues.append(
                _issue(
                    field=field,
                    severity=IssueSeverity.SAFE_CLEANUP,
                    reason=f"Trimmed leading/trailing whitespace from {field}",
                    source_ref=source_ref,
                    original_value=str(original),
                    suggested_value=value,
                )
            )

    # 1. Scalar fields
    _trim("id", required=True)
    _trim("description", required=True)
    _trim("source", required=True)
    _trim("date", required=True)
    _trim("period", required=False)

    # 2. Date format check
    date_value = str(raw_request.get("date", "")).strip()
    if date_value:
        try:
            datetime.strptime(date_value, _DATE_FORMAT)
        except ValueError:
            issues.append(
                _issue(
                    field="date",
                    severity=IssueSeverity.CLARIFICATION_REQUIRED,
                    reason=(
                        f"Date '{date_value}' is not in YYYY-MM-DD format "
                        f"(e.g. 2024-12-31)."
                    ),
                    source_ref=source_ref,
                    original_value=date_value,
                )
            )

    # 3. Period presence check
    if not str(raw_request.get("period") or "").strip():
        issues.append(
            _issue(
                field="period",
                severity=IssueSeverity.CLARIFICATION_REQUIRED,
                reason="Missing accounting period",
                source_ref=source_ref,
            )
        )

    # 4. Currency check and normalization
    orig_currency = raw_request.get("currency")
    if orig_currency is None or str(orig_currency).strip() == "":
        issues.append(
            _issue(
                field="currency",
                severity=IssueSeverity.CLARIFICATION_REQUIRED,
                reason="Missing currency in adjustment request",
                source_ref=source_ref,
            )
        )
    else:
        orig_str = str(orig_currency)
        norm_currency = orig_str.strip().upper()
        if norm_currency != orig_str:
            normalized["currency"] = norm_currency
            case_only = norm_currency != orig_str.strip()
            rule = "NORMALIZE_CASE" if case_only else "TRIM_WHITESPACE"
            logs.append(
                TransformationLog(
                    field="currency",
                    original_value=orig_str,
                    normalized_value=norm_currency,
                    rule=rule,
                    source_reference=source_ref,
                )
            )
            issues.append(
                _issue(
                    field="currency",
                    severity=IssueSeverity.SAFE_CLEANUP,
                    reason="Normalized currency casing and whitespace",
                    source_ref=source_ref,
                    original_value=orig_str,
                    suggested_value=norm_currency,
                )
            )
        if norm_currency not in SUPPORTED_CURRENCIES:
            issues.append(
                _issue(
                    field="currency",
                    severity=IssueSeverity.CLARIFICATION_REQUIRED,
                    reason=(
                        f"Currency '{norm_currency}' is not in supported currencies: "
                        f"{', '.join(sorted(SUPPORTED_CURRENCIES))}"
                    ),
                    source_ref=source_ref,
                    original_value=orig_str,
                )
            )

    # 5. Lines normalization
    raw_lines = raw_request.get("lines") or []
    norm_lines: list[dict[str, Any]] = []

    if not raw_lines:
        issues.append(
            _issue(
                field="lines",
                severity=IssueSeverity.CLARIFICATION_REQUIRED,
                reason="Adjustment request has no journal entry lines",
                source_ref=source_ref,
            )
        )

    for idx, line in enumerate(raw_lines):
        line_ref = f"{source_ref}#line_{idx + 1}"
        norm_line = copy.deepcopy(line)

        # Account
        orig_account = line.get("account")
        if orig_account is None or str(orig_account).strip() == "":
            issues.append(
                _issue(
                    field=f"lines[{idx}].account",
                    severity=IssueSeverity.CLARIFICATION_REQUIRED,
                    reason=f"Line {idx + 1} has missing or empty account code",
                    source_ref=line_ref,
                )
            )
        else:
            norm_account = str(orig_account).strip()
            if norm_account != str(orig_account):
                norm_line["account"] = norm_account
                logs.append(
                    TransformationLog(
                        field=f"lines[{idx}].account",
                        original_value=str(orig_account),
                        normalized_value=norm_account,
                        rule="TRIM_WHITESPACE",
                        source_reference=line_ref,
                    )
                )
                issues.append(
                    _issue(
                        field=f"lines[{idx}].account",
                        severity=IssueSeverity.SAFE_CLEANUP,
                        reason=f"Trimmed whitespace on account code for line {idx + 1}",
                        source_ref=line_ref,
                        original_value=str(orig_account),
                        suggested_value=norm_account,
                    )
                )

        # Memo
        memo_val = line.get("memo")
        if memo_val is None or str(memo_val).strip() == "":
            issues.append(
                _issue(
                    field=f"lines[{idx}].memo",
                    severity=IssueSeverity.WARNING,
                    reason=f"Line {idx + 1} is missing a memo description",
                    source_ref=line_ref,
                )
            )
        else:
            norm_memo = str(memo_val).strip()
            if norm_memo != str(memo_val):
                norm_line["memo"] = norm_memo
                logs.append(
                    TransformationLog(
                        field=f"lines[{idx}].memo",
                        original_value=str(memo_val),
                        normalized_value=norm_memo,
                        rule="TRIM_WHITESPACE",
                        source_reference=line_ref,
                    )
                )
                issues.append(
                    _issue(
                        field=f"lines[{idx}].memo",
                        severity=IssueSeverity.SAFE_CLEANUP,
                        reason=f"Trimmed whitespace on memo for line {idx + 1}",
                        source_ref=line_ref,
                        original_value=str(memo_val),
                        suggested_value=norm_memo,
                    )
                )

        # Debit & credit amounts
        parsed_sides: dict[str, Decimal] = {"debit": Decimal("0"), "credit": Decimal("0")}
        for side in ("debit", "credit"):
            val = line.get(side)
            if val is None or str(val).strip() == "":
                issues.append(
                    _issue(
                        field=f"lines[{idx}].{side}",
                        severity=IssueSeverity.CLARIFICATION_REQUIRED,
                        reason=f"Line {idx + 1} has missing {side} value",
                        source_ref=line_ref,
                    )
                )
                continue
            try:
                dec_val = Decimal(str(val).strip())
            except (InvalidOperation, TypeError, ValueError):
                issues.append(
                    _issue(
                        field=f"lines[{idx}].{side}",
                        severity=IssueSeverity.CLARIFICATION_REQUIRED,
                        reason=(
                            f"Line {idx + 1} {side} amount '{val}' cannot be parsed "
                            f"as a valid Decimal"
                        ),
                        source_ref=line_ref,
                        original_value=str(val),
                    )
                )
                continue

            if not dec_val.is_finite():
                issues.append(
                    _issue(
                        field=f"lines[{idx}].{side}",
                        severity=IssueSeverity.CLARIFICATION_REQUIRED,
                        reason=f"Line {idx + 1} {side} amount '{val}' is not finite",
                        source_ref=line_ref,
                        original_value=str(val),
                    )
                )
            elif dec_val < 0:
                issues.append(
                    _issue(
                        field=f"lines[{idx}].{side}",
                        severity=IssueSeverity.CLARIFICATION_REQUIRED,
                        reason=(
                            f"Negative {side} amount '{val}' is not allowed; "
                            f"record the amount on the opposite side instead."
                        ),
                        source_ref=line_ref,
                        original_value=str(val),
                    )
                )
            else:
                parsed_sides[side] = dec_val
            norm_line[side] = dec_val

        if parsed_sides["debit"] > 0 and parsed_sides["credit"] > 0:
            issues.append(
                _issue(
                    field=f"lines[{idx}]",
                    severity=IssueSeverity.CLARIFICATION_REQUIRED,
                    reason=(
                        f"Line {idx + 1} has both debit and credit positive "
                        f"(debit={parsed_sides['debit']}, credit={parsed_sides['credit']}); "
                        f"each line must be single-sided."
                    ),
                    source_ref=line_ref,
                    original_value=str(line),
                )
            )

        norm_lines.append(norm_line)

    normalized["lines"] = norm_lines
    return normalized, logs, issues
