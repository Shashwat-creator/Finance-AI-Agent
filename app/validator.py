"""
Deterministic accounting validation module for the Manual Adjustments Agent.

Enforces critical accounting controls using pure Python (no LLM calls):
- Required fields, accounting period, supported currencies
- Numeric, finite, non-negative amounts (Decimal arithmetic only)
- Account existence and posting eligibility in Chart of Accounts
- Single-side line integrity (debit > 0 XOR credit > 0)
- Double-entry balance (sum of debits == sum of credits)
- Minimum line requirements
- Duplicate entry detection
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app.config import EXPECTED_PERIOD, SUPPORTED_CURRENCIES
from app.schemas import ChartOfAccount, ValidationResult, ValidationStatus

logger = logging.getLogger("manual_adjustments.validator")


def _parse_decimal_amount(val: Any) -> tuple[bool, Optional[Decimal], str]:
    """
    Safely convert and validate a monetary value as a Decimal.

    Args:
        val: Value to parse and validate.

    Returns:
        tuple of (is_valid, decimal_value, error_message).
    """
    if val is None:
        return True, Decimal("0"), ""

    if isinstance(val, (int, Decimal)):
        d = Decimal(val)
    elif isinstance(val, float):
        import math

        if math.isnan(val) or math.isinf(val):
            return False, None, "Amount is not finite (NaN or Infinity)"
        d = Decimal(str(val))
    elif isinstance(val, str):
        cleaned = val.strip()
        if not cleaned:
            return True, Decimal("0"), ""
        try:
            d = Decimal(cleaned)
        except InvalidOperation:
            return False, None, f"Amount '{val}' is not a valid numeric value"
    else:
        try:
            d = Decimal(str(val))
        except (InvalidOperation, TypeError):
            return False, None, f"Amount '{val}' cannot be converted to Decimal"

    if not d.is_finite():
        return False, None, "Amount is not finite (NaN or Infinity)"

    if d < Decimal("0"):
        return False, d, f"Amount {d} is negative"

    return True, d, ""


def validate_journal_entry(
    entry: dict[str, Any],
    coa_lookup: dict[str, ChartOfAccount],
    expected_period: str = EXPECTED_PERIOD,
    is_duplicate: bool = False,
) -> list[ValidationResult]:
    """
    Perform deterministic validation on a journal entry dictionary against accounting rules.

    The entry dictionary must have the following keys:
        - id: Identifier of the entry
        - description: Non-empty description string
        - date: Non-empty date string
        - source: Non-empty source reference string
        - lines: List of line dicts (account, debit, credit, memo)
        - period: Accounting period string (optional; if present, must match expected_period)
        - currency: Currency code (must be in {'USD', 'EUR', 'GBP'})

    Rules checked:
        - VAL-001 Required Fields: description, date, source must be non-empty; lines must be non-empty list.
        - VAL-002 Period Check: if period is present, it must match expected_period.
        - VAL-003 Currency Check: currency must be in {'USD', 'EUR', 'GBP'}.
        - VAL-004 Amount Validity: every debit and credit must be numeric, finite, and >= 0 (Decimal).
        - VAL-005 Account Exists: every line's account code must exist in coa_lookup.
        - VAL-006 Account Postable: accounts must not be Header type (is_postable must be True).
        - VAL-007 Single Side: each line must have exactly one positive side (debit > 0 XOR credit > 0).
        - VAL-008 Entry Balanced: sum of debits must equal sum of credits EXACTLY (Decimal comparison).
        - VAL-009 Minimum Lines: entry must have at least 2 lines.
        - VAL-010 Duplicate Check: placeholder taking an optional is_duplicate bool parameter.

    Args:
        entry: Dictionary representing the journal entry.
        coa_lookup: Mapping from account_code string to ChartOfAccount object.
        expected_period: Expected accounting period (default from config: '2024-Q4').
        is_duplicate: Optional boolean flag indicating if entry is a duplicate.

    Returns:
        List of ValidationResult objects for all validation rules VAL-001 through VAL-010.
    """
    # Normalize entry if a Pydantic model is passed
    if hasattr(entry, "model_dump"):
        entry_dict: dict[str, Any] = entry.model_dump()
    elif isinstance(entry, dict):
        entry_dict = dict(entry)
    else:
        entry_dict = {}

    results: list[ValidationResult] = []

    # Extract lines list
    raw_lines = entry_dict.get("lines")
    lines: list[dict[str, Any]] = []
    if isinstance(raw_lines, list):
        for line in raw_lines:
            if hasattr(line, "model_dump"):
                lines.append(line.model_dump())
            elif isinstance(line, dict):
                lines.append(dict(line))

    # ── VAL-001 Required Fields ───────────────────────────────────────────────
    missing_fields: list[str] = []
    desc = entry_dict.get("description")
    if not isinstance(desc, str) or not desc.strip():
        missing_fields.append("description")

    dt = entry_dict.get("date")
    if not isinstance(dt, str) or not dt.strip():
        missing_fields.append("date")

    src = entry_dict.get("source")
    if not isinstance(src, str) or not src.strip():
        missing_fields.append("source")

    if not isinstance(raw_lines, list) or len(raw_lines) == 0:
        missing_fields.append("lines")

    if missing_fields:
        results.append(
            ValidationResult(
                rule_id="VAL-001",
                rule_name="Required Fields",
                status=ValidationStatus.FAIL,
                message=f"Missing or empty required field(s): {', '.join(missing_fields)}.",
                details={"missing_fields": missing_fields},
            )
        )
    else:
        results.append(
            ValidationResult(
                rule_id="VAL-001",
                rule_name="Required Fields",
                status=ValidationStatus.PASS,
                message="All required fields (description, date, source, lines) are present and non-empty.",
                details={"fields": ["description", "date", "source", "lines"]},
            )
        )

    # ── VAL-002 Period Check ──────────────────────────────────────────────────
    period_val = entry_dict.get("period")
    if period_val is not None and str(period_val).strip() != "":
        period_str = str(period_val).strip()
        if period_str == expected_period:
            results.append(
                ValidationResult(
                    rule_id="VAL-002",
                    rule_name="Period Check",
                    status=ValidationStatus.PASS,
                    message=f"Period '{period_str}' matches expected period '{expected_period}'.",
                    details={"period": period_str, "expected_period": expected_period},
                )
            )
        else:
            results.append(
                ValidationResult(
                    rule_id="VAL-002",
                    rule_name="Period Check",
                    status=ValidationStatus.FAIL,
                    message=f"Period '{period_str}' does not match expected period '{expected_period}'.",
                    details={"period": period_str, "expected_period": expected_period},
                )
            )
    else:
        # Period not present: optional check passes
        results.append(
            ValidationResult(
                rule_id="VAL-002",
                rule_name="Period Check",
                status=ValidationStatus.PASS,
                message="Period is not specified; period check passed.",
                details={"period": None, "expected_period": expected_period},
            )
        )

    # ── VAL-003 Currency Check ────────────────────────────────────────────────
    curr_val = entry_dict.get("currency")
    curr_str = str(curr_val).strip().upper() if curr_val is not None else ""
    if curr_str in SUPPORTED_CURRENCIES:
        results.append(
            ValidationResult(
                rule_id="VAL-003",
                rule_name="Currency Check",
                status=ValidationStatus.PASS,
                message=f"Currency '{curr_str}' is supported.",
                details={"currency": curr_str, "supported_currencies": sorted(SUPPORTED_CURRENCIES)},
            )
        )
    else:
        results.append(
            ValidationResult(
                rule_id="VAL-003",
                rule_name="Currency Check",
                status=ValidationStatus.FAIL,
                message=f"Currency '{curr_val}' is invalid. Supported currencies: {', '.join(sorted(SUPPORTED_CURRENCIES))}.",
                details={"currency": curr_val, "supported_currencies": sorted(SUPPORTED_CURRENCIES)},
            )
        )

    # ── VAL-004 Amount Validity ───────────────────────────────────────────────
    amount_errors: list[str] = []
    parsed_lines_amounts: list[tuple[Decimal, Decimal]] = []

    if not lines:
        results.append(
            ValidationResult(
                rule_id="VAL-004",
                rule_name="Amount Validity",
                status=ValidationStatus.FAIL,
                message="No lines available to validate amounts.",
                details={"invalid_amounts": ["Lines list is empty"]},
            )
        )
    else:
        for idx, line in enumerate(lines, start=1):
            debit_raw = line.get("debit", Decimal("0"))
            credit_raw = line.get("credit", Decimal("0"))

            valid_d, d_val, err_d = _parse_decimal_amount(debit_raw)
            valid_c, c_val, err_c = _parse_decimal_amount(credit_raw)

            if not valid_d:
                amount_errors.append(f"Line {idx} debit: {err_d}")
            if not valid_c:
                amount_errors.append(f"Line {idx} credit: {err_c}")

            d_parsed = d_val if valid_d and d_val is not None else Decimal("0")
            c_parsed = c_val if valid_c and c_val is not None else Decimal("0")
            parsed_lines_amounts.append((d_parsed, c_parsed))

        if amount_errors:
            results.append(
                ValidationResult(
                    rule_id="VAL-004",
                    rule_name="Amount Validity",
                    status=ValidationStatus.FAIL,
                    message=f"Invalid amounts found in {len(amount_errors)} instance(s): {'; '.join(amount_errors)}.",
                    details={"invalid_amounts": amount_errors},
                )
            )
        else:
            results.append(
                ValidationResult(
                    rule_id="VAL-004",
                    rule_name="Amount Validity",
                    status=ValidationStatus.PASS,
                    message="All debit and credit amounts are numeric, finite, and non-negative.",
                    details={"line_count": len(lines)},
                )
            )

    # ── VAL-005 Account Exists ────────────────────────────────────────────────
    missing_accounts: list[str] = []
    line_account_codes: list[str] = []

    if not lines:
        results.append(
            ValidationResult(
                rule_id="VAL-005",
                rule_name="Account Exists",
                status=ValidationStatus.FAIL,
                message="No lines available to validate account codes.",
                details={"missing_accounts": ["Lines list is empty"]},
            )
        )
    else:
        for idx, line in enumerate(lines, start=1):
            raw_acc = line.get("account") or line.get("account_code")
            acc_str = str(raw_acc).strip() if raw_acc is not None else ""
            line_account_codes.append(acc_str)
            if not acc_str or acc_str not in coa_lookup:
                missing_accounts.append(f"Line {idx}: '{acc_str}'")

        if missing_accounts:
            results.append(
                ValidationResult(
                    rule_id="VAL-005",
                    rule_name="Account Exists",
                    status=ValidationStatus.FAIL,
                    message=f"Account code(s) not found in COA: {', '.join(missing_accounts)}.",
                    details={"missing_accounts": missing_accounts},
                )
            )
        else:
            results.append(
                ValidationResult(
                    rule_id="VAL-005",
                    rule_name="Account Exists",
                    status=ValidationStatus.PASS,
                    message="All account codes exist in the Chart of Accounts.",
                    details={"account_codes": line_account_codes},
                )
            )

    # ── VAL-006 Account Postable ──────────────────────────────────────────────
    non_postable_accounts: list[str] = []

    if not lines:
        results.append(
            ValidationResult(
                rule_id="VAL-006",
                rule_name="Account Postable",
                status=ValidationStatus.FAIL,
                message="No lines available to validate account postability.",
                details={"non_postable_accounts": ["Lines list is empty"]},
            )
        )
    else:
        for idx, line in enumerate(lines, start=1):
            raw_acc = line.get("account") or line.get("account_code")
            acc_str = str(raw_acc).strip() if raw_acc is not None else ""
            if acc_str in coa_lookup:
                coa = coa_lookup[acc_str]
                is_header = str(coa.account_type).strip().lower() == "header"
                if is_header or not coa.is_postable:
                    non_postable_accounts.append(
                        f"Line {idx}: Account '{acc_str}' ({coa.account_name}) is non-postable "
                        f"(account_type='{coa.account_type}', is_postable={coa.is_postable})"
                    )

        if non_postable_accounts:
            results.append(
                ValidationResult(
                    rule_id="VAL-006",
                    rule_name="Account Postable",
                    status=ValidationStatus.FAIL,
                    message=f"Non-postable account(s) used: {'; '.join(non_postable_accounts)}.",
                    details={"non_postable_accounts": non_postable_accounts},
                )
            )
        else:
            results.append(
                ValidationResult(
                    rule_id="VAL-006",
                    rule_name="Account Postable",
                    status=ValidationStatus.PASS,
                    message="All accounts are postable non-Header accounts.",
                    details={},
                )
            )

    # ── VAL-007 Single Side ───────────────────────────────────────────────────
    single_side_violations: list[str] = []

    if not lines:
        results.append(
            ValidationResult(
                rule_id="VAL-007",
                rule_name="Single Side",
                status=ValidationStatus.FAIL,
                message="No lines available to validate single side rule.",
                details={"violations": ["Lines list is empty"]},
            )
        )
    else:
        for idx, (d_val, c_val) in enumerate(parsed_lines_amounts, start=1):
            has_debit = d_val > Decimal("0")
            has_credit = c_val > Decimal("0")

            if has_debit and has_credit:
                single_side_violations.append(
                    f"Line {idx}: Both debit ({d_val}) and credit ({c_val}) are positive."
                )
            elif not has_debit and not has_credit:
                single_side_violations.append(
                    f"Line {idx}: Neither debit nor credit is positive (both are zero)."
                )

        if single_side_violations:
            results.append(
                ValidationResult(
                    rule_id="VAL-007",
                    rule_name="Single Side",
                    status=ValidationStatus.FAIL,
                    message=f"Single-side rule violated: {'; '.join(single_side_violations)}.",
                    details={"violations": single_side_violations},
                )
            )
        else:
            results.append(
                ValidationResult(
                    rule_id="VAL-007",
                    rule_name="Single Side",
                    status=ValidationStatus.PASS,
                    message="Each line has exactly one positive side (debit > 0 XOR credit > 0).",
                    details={},
                )
            )

    # ── VAL-008 Entry Balanced ────────────────────────────────────────────────
    total_debit = Decimal("0")
    total_credit = Decimal("0")

    if not lines:
        results.append(
            ValidationResult(
                rule_id="VAL-008",
                rule_name="Entry Balanced",
                status=ValidationStatus.FAIL,
                message="Cannot verify balance: entry has no lines.",
                details={"total_debit": "0", "total_credit": "0", "difference": "0"},
            )
        )
    else:
        for d_val, c_val in parsed_lines_amounts:
            total_debit += d_val
            total_credit += c_val

        diff = total_debit - total_credit
        if diff == Decimal("0"):
            results.append(
                ValidationResult(
                    rule_id="VAL-008",
                    rule_name="Entry Balanced",
                    status=ValidationStatus.PASS,
                    message=f"Entry is balanced. Total debit: {total_debit}, Total credit: {total_credit}.",
                    details={
                        "total_debit": str(total_debit),
                        "total_credit": str(total_credit),
                        "difference": "0",
                    },
                )
            )
        else:
            results.append(
                ValidationResult(
                    rule_id="VAL-008",
                    rule_name="Entry Balanced",
                    status=ValidationStatus.FAIL,
                    message=f"Entry is unbalanced: total debit ({total_debit}) != total credit ({total_credit}), difference: {diff}.",
                    details={
                        "total_debit": str(total_debit),
                        "total_credit": str(total_credit),
                        "difference": str(diff),
                    },
                )
            )

    # ── VAL-009 Minimum Lines ─────────────────────────────────────────────────
    line_count = len(lines)
    if line_count >= 2:
        results.append(
            ValidationResult(
                rule_id="VAL-009",
                rule_name="Minimum Lines",
                status=ValidationStatus.PASS,
                message=f"Entry has {line_count} lines (minimum required: 2).",
                details={"line_count": line_count},
            )
        )
    else:
        results.append(
            ValidationResult(
                rule_id="VAL-009",
                rule_name="Minimum Lines",
                status=ValidationStatus.FAIL,
                message=f"Entry must have at least 2 lines, found {line_count}.",
                details={"line_count": line_count},
            )
        )

    # ── VAL-010 Duplicate Check ───────────────────────────────────────────────
    is_dup = is_duplicate or bool(entry_dict.get("is_duplicate", False))
    if is_dup:
        results.append(
            ValidationResult(
                rule_id="VAL-010",
                rule_name="Duplicate Check",
                status=ValidationStatus.FAIL,
                message="Duplicate entry detected.",
                details={"is_duplicate": True},
            )
        )
    else:
        results.append(
            ValidationResult(
                rule_id="VAL-010",
                rule_name="Duplicate Check",
                status=ValidationStatus.PASS,
                message="Entry is not a duplicate.",
                details={"is_duplicate": False},
            )
        )

    return results


def _period_bounds(period: str) -> Optional[tuple[str, str]]:
    """Return (first_day, last_day) for a calendar quarter such as 2024-Q4."""
    match = re.match(r"^(\d{4})-Q([1-4])$", (period or "").strip().upper())
    if not match:
        return None
    year = int(match.group(1))
    quarter = int(match.group(2))
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    end_day = 31 if end_month in (3, 12) else 30
    return (
        f"{year:04d}-{start_month:02d}-01",
        f"{year:04d}-{end_month:02d}-{end_day:02d}",
    )


def _as_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def check_entry_semantics(
    entry: dict[str, Any],
    coa_lookup: dict[str, ChartOfAccount],
    fx_rates: Optional[dict[str, dict[str, Decimal]]] = None,
    tb_df: Any = None,
    expected_period: str = EXPECTED_PERIOD,
) -> tuple[list[ValidationResult], list[str]]:
    """
    Accounting-semantics checks that complement the ten structural rules.

    These are deterministic and address the failure modes the assignment calls
    out that pure debit/credit arithmetic misses:

    - SEM-001 single-account (wash) entry -> FAIL
    - SEM-002 intercompany entry with no counterparty/cash leg -> FAIL
    - SEM-003 account with unresolved ``TBD`` cash-flow category -> warning
    - SEM-004 FX revaluation amount does not tie to rate-derived estimate -> warning
    - SEM-005 duplicate identical line -> warning
    - SEM-006 entry date outside the accounting period -> FAIL

    Returns:
        (list[ValidationResult], list[str] of human-readable warnings)
    """
    if hasattr(entry, "model_dump"):
        entry_dict = entry.model_dump()
    elif isinstance(entry, dict):
        entry_dict = dict(entry)
    else:
        entry_dict = {}

    raw_lines = entry_dict.get("lines") or []
    lines: list[dict[str, Any]] = []
    for line in raw_lines:
        if hasattr(line, "model_dump"):
            lines.append(line.model_dump())
        elif isinstance(line, dict):
            lines.append(dict(line))

    results: list[ValidationResult] = []
    warnings: list[str] = []

    def add(rule_id: str, name: str, passed: bool, message: str, details: Any = None) -> None:
        results.append(
            ValidationResult(
                rule_id=rule_id,
                rule_name=name,
                status=ValidationStatus.PASS if passed else ValidationStatus.FAIL,
                message=message,
                details=details or {},
            )
        )

    accounts = [str(line.get("account") or line.get("account_code") or "").strip() for line in lines]
    description = str(entry_dict.get("description") or "")
    memos = " ".join(str(line.get("memo") or "") for line in lines)

    # ── SEM-001 Single-account wash ───────────────────────────────────────────
    distinct_accounts = {a for a in accounts if a}
    if len(lines) >= 2 and len(distinct_accounts) == 1:
        only = next(iter(distinct_accounts))
        add(
            "SEM-001",
            "No-op / Single-Account Wash",
            False,
            (
                f"Entry posts debits and credits to the same account ({only}) only. "
                "Its net effect is zero, which is typical of a circular or already-settled "
                "intercompany entry. Reject and request the counterparty/cash leg."
            ),
            {"accounts": sorted(distinct_accounts), "line_count": len(lines)},
        )
    else:
        add("SEM-001", "No-op / Single-Account Wash", True, "Entry touches more than one account.")

    # ── SEM-002 Intercompany needs a counterparty ─────────────────────────────
    ic_accounts = {
        code
        for code in distinct_accounts
        if code in coa_lookup and "intercompany" in coa_lookup[code].account_name.lower()
    }
    mentions_ic = "intercompany" in description.lower() or "ic " in f"{description.lower()} "
    if mentions_ic or ic_accounts:
        non_ic_accounts = distinct_accounts - ic_accounts
        if not non_ic_accounts:
            add(
                "SEM-002",
                "Intercompany Counterparty",
                False,
                (
                    "Intercompany entry has no non-IC counterparty or cash leg. "
                    "It cannot be eliminated or settled as posted; provide the matching "
                    "counterparty entity/account."
                ),
                {"ic_accounts": sorted(ic_accounts)},
            )
        else:
            add(
                "SEM-002",
                "Intercompany Counterparty",
                True,
                "Intercompany entry includes a counterparty/cash account.",
            )
    else:
        add("SEM-002", "Intercompany Counterparty", True, "Not an intercompany entry.")

    # ── SEM-003 Unresolved cash-flow category ─────────────────────────────────
    tbd_accounts = [
        code
        for code in distinct_accounts
        if code in coa_lookup and str(coa_lookup[code].cf_category or "").strip().upper() == "TBD"
    ]
    if tbd_accounts:
        details = ", ".join(
            f"{code} ({coa_lookup[code].account_name})" for code in sorted(tbd_accounts)
        )
        msg = (
            f"Cash-flow category is unresolved (TBD) for {details}. "
            "The entry can be proposed, but cash-flow reporting must block until a "
            "category is configured."
        )
        warnings.append(msg)
        add("SEM-003", "Cash-Flow Category Resolved", True, msg, {"tbd_accounts": sorted(tbd_accounts)})
    else:
        add("SEM-003", "Cash-Flow Category Resolved", True, "All accounts have a cash-flow category.")

    # ── SEM-004 FX revaluation tie-out ────────────────────────────────────────
    text = f"{description} {memos}".upper()
    is_reval = any(token in text for token in ("REVAL", "FX", "TRANSLATION"))
    if is_reval and fx_rates:
        currencies = [c for c in ("EUR", "GBP") if c in text]
        cash_line = next(
            (
                line
                for line in lines
                if line.get("account") in coa_lookup
                and (coa_lookup[str(line.get("account"))].cf_category or "").lower() == "cash"
            ),
            None,
        )
        if not currencies:
            msg = "FX revaluation entry does not state the currency being revalued."
            warnings.append(msg)
            add("SEM-004", "FX Revaluation Tie-Out", True, msg)
        elif cash_line is None:
            msg = "FX revaluation entry does not touch a cash account; verify the revaluation basis."
            warnings.append(msg)
            add("SEM-004", "FX Revaluation Tie-Out", True, msg)
        else:
            currency = currencies[0]
            reval_amount = abs(_as_decimal(cash_line.get("debit")) - _as_decimal(cash_line.get("credit")))
            period_end = (fx_rates.get(currency) or {}).get("period_end")
            average = (fx_rates.get(currency) or {}).get("period_average")
            opening = (fx_rates.get(currency) or {}).get("opening")
            balance = None
            if tb_df is not None:
                try:
                    code = str(cash_line.get("account"))
                    mask = (tb_df["account_code"].astype(str) == code) & (
                        tb_df["currency"].astype(str).str.upper() == currency
                    )
                    subset = tb_df[mask]
                    balance = Decimal("0")
                    for _, row in subset.iterrows():
                        balance += _as_decimal(row.get("debit")) - _as_decimal(row.get("credit"))
                except Exception:  # pragma: no cover - defensive
                    balance = None
            estimates = []
            if balance is not None and period_end is not None:
                if average is not None:
                    estimates.append(abs(balance * (period_end - average)))
                if opening is not None:
                    estimates.append(abs(balance * (period_end - opening)))
            if estimates:
                matched = any(abs(reval_amount - est) <= Decimal("1.00") for est in estimates)
                est_str = " / ".join(f"{est:,.2f}" for est in estimates)
                if matched:
                    msg = (
                        f"FX revaluation for {currency} ({reval_amount:,.2f}) ties to the "
                        f"rate-derived estimate ({est_str})."
                    )
                    add("SEM-004", "FX Revaluation Tie-Out", True, msg)
                else:
                    msg = (
                        f"FX revaluation for {currency} is {reval_amount:,.2f} but the provided "
                        f"rates imply {est_str} on the {balance:,.2f} {currency} balance. "
                        "Verify the revaluation calculation before posting."
                    )
                    warnings.append(msg)
                    add(
                        "SEM-004",
                        "FX Revaluation Tie-Out",
                        True,
                        msg,
                        {"reval_amount": str(reval_amount), "estimates": [str(e) for e in estimates]},
                    )
            else:
                msg = (
                    f"FX revaluation for {currency} could not be tied out: a required "
                    "period-end/average/opening rate or trial-balance balance is missing."
                )
                warnings.append(msg)
                add("SEM-004", "FX Revaluation Tie-Out", True, msg)
    else:
        add("SEM-004", "FX Revaluation Tie-Out", True, "Not an FX revaluation entry.")

    # ── SEM-005 Duplicate identical line ──────────────────────────────────────
    fingerprints = [
        (
            str(line.get("account") or "").strip(),
            str(_as_decimal(line.get("debit"))),
            str(_as_decimal(line.get("credit"))),
        )
        for line in lines
    ]
    if len(fingerprints) != len(set(fingerprints)):
        msg = "Entry contains duplicate identical lines; confirm this is intentional (e.g. split posting)."
        warnings.append(msg)
        add("SEM-005", "Duplicate Line", True, msg)
    else:
        add("SEM-005", "Duplicate Line", True, "No duplicate identical lines.")

    # ── SEM-006 Date inside accounting period ─────────────────────────────────
    bounds = _period_bounds(expected_period)
    date_value = str(entry_dict.get("date") or "").strip()
    if bounds and date_value:
        start, end = bounds
        inside = start <= date_value <= end
        add(
            "SEM-006",
            "Date Within Period",
            inside,
            (
                f"Entry date {date_value} is within {expected_period} ({start} to {end})."
                if inside
                else f"Entry date {date_value} is outside {expected_period} ({start} to {end})."
            ),
            {"date": date_value, "period_start": start, "period_end": end},
        )
    else:
        add("SEM-006", "Date Within Period", True, "Date/period not available for range check.")

    return results, warnings


def get_validation_summary(results: list[ValidationResult]) -> dict[str, Any]:
    """
    Summarize a list of ValidationResult objects.

    Args:
        results: List of ValidationResult objects from validate_journal_entry.

    Returns:
        Dictionary with keys:
            - valid: bool (True if all checks passed, False otherwise)
            - total_debit: str
            - total_credit: str
            - difference: str
            - passed: int
            - failed: int
            - errors: list[str] (error messages for failed checks)
    """
    passed_count = sum(1 for r in results if r.status == ValidationStatus.PASS)
    failed_count = sum(1 for r in results if r.status == ValidationStatus.FAIL)
    is_valid = failed_count == 0 and passed_count > 0

    total_debit = "0"
    total_credit = "0"
    difference = "0"

    val_008 = next((r for r in results if r.rule_id == "VAL-008"), None)
    if val_008 and val_008.details:
        total_debit = str(val_008.details.get("total_debit", "0"))
        total_credit = str(val_008.details.get("total_credit", "0"))
        difference = str(val_008.details.get("difference", "0"))

    errors: list[str] = []
    for r in results:
        if r.status == ValidationStatus.FAIL:
            if r.message.startswith(r.rule_id):
                errors.append(r.message)
            else:
                errors.append(f"{r.rule_id}: {r.message}")

    return {
        "valid": is_valid,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "difference": difference,
        "passed": passed_count,
        "failed": failed_count,
        "errors": errors,
    }
