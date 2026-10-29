"""
Reconciliation and evidence checking module for the Manual Adjustments Agent.

Computes exact debit and credit totals using Decimal arithmetic, verifies double-entry
balance, validates that all accounts exist in the Chart of Accounts, verifies normal
balance alignment (recording warnings for legitimate accounting reversals/reclassifications),
and checks the availability of source evidence references.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from app.schemas import (
    ChartOfAccount,
    JournalEntryLine,
    ProposedJournalEntry,
    ReconciliationResult,
)

logger = logging.getLogger("manual_adjustments.reconciliation")


def check_normal_balance_alignment(
    line: JournalEntryLine,
    account: ChartOfAccount,
) -> Optional[str]:
    """
    Check if a journal entry line's direction matches the account's normal balance.

    Reclassification and adjustment entries may legitimately reverse normal balances
    (e.g., crediting an expense to reduce it, or debiting a liability upon payment/reclass).
    Therefore, misalignment produces a descriptive warning rather than a validation failure.

    Args:
        line: The journal entry line with debit and credit amounts.
        account: The ChartOfAccount record for the line's account code.

    Returns:
        A warning message string if misaligned, or None if aligned or normal balance is undefined.
    """
    norm = (account.normal_balance or "").strip().lower()
    if not norm:
        return None

    debit = line.debit if isinstance(line.debit, Decimal) else Decimal(str(line.debit or "0"))
    credit = line.credit if isinstance(line.credit, Decimal) else Decimal(str(line.credit or "0"))

    if debit > Decimal("0") and norm != "debit":
        return (
            f"Line for account '{account.account_code}' ({account.account_name}) has debit of "
            f"{debit}, but account normal balance is '{account.normal_balance}'."
        )

    if credit > Decimal("0") and norm != "credit":
        return (
            f"Line for account '{account.account_code}' ({account.account_name}) has credit of "
            f"{credit}, but account normal balance is '{account.normal_balance}'."
        )

    return None


def reconcile_entry(
    proposed: ProposedJournalEntry,
    coa_lookup: dict[str, ChartOfAccount],
) -> ReconciliationResult:
    """
    Perform reconciliation and evidence checks on a proposed journal entry.

    Steps performed:
    1. Compute total_debit and total_credit from proposed.lines (using Decimal arithmetic).
    2. Compute difference = total_debit - total_credit.
    3. Determine is_balanced = (difference == Decimal('0')).
    4. Perform evidence checks:
       - Verify each line's account exists in the Chart of Accounts (COA).
       - Check normal balance alignment (debits to Debit accounts, credits to Credit accounts),
         logging a warning if reversed.
       - Verify that source_references is not empty.
    5. Return a populated ReconciliationResult object.

    Args:
        proposed: ProposedJournalEntry containing lines, metadata, and references.
        coa_lookup: Mapping from account_code string to ChartOfAccount object.

    Returns:
        ReconciliationResult with totals, balance status, evidence check details, and warnings.
    """
    if isinstance(proposed, dict):
        proposed = ProposedJournalEntry(**proposed)

    total_debit = Decimal("0")
    total_credit = Decimal("0")

    for line in proposed.lines:
        d = line.debit if isinstance(line.debit, Decimal) else Decimal(str(line.debit or "0"))
        c = line.credit if isinstance(line.credit, Decimal) else Decimal(str(line.credit or "0"))
        total_debit += d
        total_credit += c

    difference = total_debit - total_credit
    is_balanced = (difference == Decimal("0"))

    # Update the proposed journal entry totals
    proposed.total_debit = total_debit
    proposed.total_credit = total_credit
    proposed.difference = difference

    evidence_checks: list[dict[str, Any]] = []
    warnings: list[str] = []

    # 1. Balance check
    evidence_checks.append({
        "check": "entry_balanced",
        "passed": is_balanced,
        "details": f"Total debit: {total_debit}, Total credit: {total_credit}, Difference: {difference}",
    })
    if not is_balanced:
        warnings.append(
            f"Entry is not balanced: total debit ({total_debit}) != total credit ({total_credit}), difference: {difference}."
        )

    # 2. Account existence and normal balance alignment checks
    for idx, line in enumerate(proposed.lines, start=1):
        code = str(line.account_code).strip()
        if code in coa_lookup:
            account = coa_lookup[code]
            evidence_checks.append({
                "check": "account_exists",
                "line_number": idx,
                "account_code": code,
                "account_name": account.account_name,
                "passed": True,
                "details": f"Account '{code}' found in COA: {account.account_name}.",
            })

            # Check normal balance alignment
            alignment_warning = check_normal_balance_alignment(line, account)
            if alignment_warning:
                warnings.append(alignment_warning)
                evidence_checks.append({
                    "check": "normal_balance_alignment",
                    "line_number": idx,
                    "account_code": code,
                    "passed": False,
                    "details": alignment_warning,
                })
            else:
                evidence_checks.append({
                    "check": "normal_balance_alignment",
                    "line_number": idx,
                    "account_code": code,
                    "passed": True,
                    "details": f"Aligned with normal balance ({account.normal_balance}).",
                })
        else:
            msg = f"Line {idx}: Account '{code}' not found in Chart of Accounts."
            warnings.append(msg)
            evidence_checks.append({
                "check": "account_exists",
                "line_number": idx,
                "account_code": code,
                "passed": False,
                "details": msg,
            })

    # 3. Source references evidence check
    has_sources = bool(proposed.source_references and len(proposed.source_references) > 0)
    if has_sources:
        evidence_checks.append({
            "check": "source_references",
            "passed": True,
            "details": f"Found {len(proposed.source_references)} source reference(s): {', '.join(proposed.source_references)}.",
        })
    else:
        warn_msg = "Proposed journal entry has no source references."
        warnings.append(warn_msg)
        evidence_checks.append({
            "check": "source_references",
            "passed": False,
            "details": warn_msg,
        })

    return ReconciliationResult(
        total_debit=total_debit,
        total_credit=total_credit,
        difference=difference,
        is_balanced=is_balanced,
        evidence_checks=evidence_checks,
        warnings=warnings,
    )
