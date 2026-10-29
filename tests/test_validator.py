"""
Unit tests for deterministic validation in app/validator.py.
"""

from decimal import Decimal
import pytest

from app.schemas import ChartOfAccount, ValidationStatus
from app.validator import get_validation_summary, validate_journal_entry


@pytest.fixture
def mock_coa_lookup() -> dict[str, ChartOfAccount]:
    """Provide a standard COA lookup fixture for testing."""
    return {
        "1000": ChartOfAccount(
            account_code="1000",
            account_name="Total Assets",
            account_type="Header",
            statement="BS",
            normal_balance="Debit",
            is_postable=False,
        ),
        "1110": ChartOfAccount(
            account_code="1110",
            account_name="Cash and Cash Equivalents",
            account_type="Asset",
            statement="BS",
            normal_balance="Debit",
            is_postable=True,
        ),
        "2120": ChartOfAccount(
            account_code="2120",
            account_name="Accrued Expenses",
            account_type="Liability",
            statement="BS",
            normal_balance="Credit",
            is_postable=True,
        ),
        "6100": ChartOfAccount(
            account_code="6100",
            account_name="Salaries and Wages",
            account_type="Expense",
            statement="PL",
            normal_balance="Debit",
            is_postable=True,
        ),
        "6300": ChartOfAccount(
            account_code="6300",
            account_name="Marketing and Advertising",
            account_type="Expense",
            statement="PL",
            normal_balance="Debit",
            is_postable=True,
        ),
        "6310": ChartOfAccount(
            account_code="6310",
            account_name="Travel and Entertainment",
            account_type="Expense",
            statement="PL",
            normal_balance="Debit",
            is_postable=True,
        ),
    }


@pytest.fixture
def valid_entry() -> dict:
    """Provide a valid, balanced journal entry dictionary."""
    return {
        "id": "JE-001",
        "description": "Accrue Q4 bonus pool",
        "date": "2024-12-31",
        "source": "Finance team bonus accrual workbook v3",
        "period": "2024-Q4",
        "currency": "USD",
        "lines": [
            {
                "account": "6100",
                "debit": Decimal("850000.00"),
                "credit": Decimal("0.00"),
                "memo": "Bonus accrual",
            },
            {
                "account": "2120",
                "debit": Decimal("0.00"),
                "credit": Decimal("850000.00"),
                "memo": "Accrual offset",
            },
        ],
    }


def test_validate_journal_entry_all_pass(valid_entry, mock_coa_lookup):
    """Test that a completely valid entry passes all validation rules VAL-001 through VAL-010."""
    results = validate_journal_entry(valid_entry, mock_coa_lookup, expected_period="2024-Q4")
    assert len(results) == 10

    for res in results:
        assert res.status == ValidationStatus.PASS, f"Rule {res.rule_id} unexpectedly failed: {res.message}"

    summary = get_validation_summary(results)
    assert summary["valid"] is True
    assert summary["passed"] == 10
    assert summary["failed"] == 0
    assert summary["errors"] == []
    assert summary["total_debit"] == "850000.00"
    assert summary["total_credit"] == "850000.00"
    assert summary["difference"] == "0"


def test_val_001_missing_required_fields(valid_entry, mock_coa_lookup):
    """VAL-001: Description, date, source must be non-empty; lines must be non-empty list."""
    # Missing description
    entry1 = dict(valid_entry, description="")
    results1 = validate_journal_entry(entry1, mock_coa_lookup)
    r1 = next(r for r in results1 if r.rule_id == "VAL-001")
    assert r1.status == ValidationStatus.FAIL
    assert "description" in r1.details["missing_fields"]

    # Missing date and source
    entry2 = dict(valid_entry, date="   ", source="")
    results2 = validate_journal_entry(entry2, mock_coa_lookup)
    r2 = next(r for r in results2 if r.rule_id == "VAL-001")
    assert r2.status == ValidationStatus.FAIL
    assert "date" in r2.details["missing_fields"]
    assert "source" in r2.details["missing_fields"]

    # Empty lines
    entry3 = dict(valid_entry, lines=[])
    results3 = validate_journal_entry(entry3, mock_coa_lookup)
    r3 = next(r for r in results3 if r.rule_id == "VAL-001")
    assert r3.status == ValidationStatus.FAIL
    assert "lines" in r3.details["missing_fields"]


def test_val_002_period_check(valid_entry, mock_coa_lookup):
    """VAL-002: If period is present, it must match expected_period."""
    # Matching period
    results_match = validate_journal_entry(valid_entry, mock_coa_lookup, expected_period="2024-Q4")
    r_match = next(r for r in results_match if r.rule_id == "VAL-002")
    assert r_match.status == ValidationStatus.PASS

    # Mismatching period
    entry_mismatch = dict(valid_entry, period="2024-Q3")
    results_mismatch = validate_journal_entry(entry_mismatch, mock_coa_lookup, expected_period="2024-Q4")
    r_mismatch = next(r for r in results_mismatch if r.rule_id == "VAL-002")
    assert r_mismatch.status == ValidationStatus.FAIL
    assert "2024-Q3" in r_mismatch.message

    # Missing period passes optional check
    entry_no_period = dict(valid_entry)
    del entry_no_period["period"]
    results_no_period = validate_journal_entry(entry_no_period, mock_coa_lookup, expected_period="2024-Q4")
    r_no_period = next(r for r in results_no_period if r.rule_id == "VAL-002")
    assert r_no_period.status == ValidationStatus.PASS


def test_val_003_currency_check(valid_entry, mock_coa_lookup):
    """VAL-003: Currency must be in {'USD', 'EUR', 'GBP'}."""
    # Supported currencies
    for curr in ["USD", "EUR", "GBP", "usd", "eur"]:
        entry = dict(valid_entry, currency=curr)
        results = validate_journal_entry(entry, mock_coa_lookup)
        r = next(r for r in results if r.rule_id == "VAL-003")
        assert r.status == ValidationStatus.PASS

    # Unsupported currency
    for invalid_curr in ["JPY", "CAD", "", None]:
        entry = dict(valid_entry, currency=invalid_curr)
        results = validate_journal_entry(entry, mock_coa_lookup)
        r = next(r for r in results if r.rule_id == "VAL-003")
        assert r.status == ValidationStatus.FAIL


def test_val_004_amount_validity(valid_entry, mock_coa_lookup):
    """VAL-004: Every debit and credit must be numeric, finite, and >= 0."""
    # Negative debit
    entry_neg = dict(valid_entry, lines=[
        {"account": "6100", "debit": Decimal("-100"), "credit": Decimal("0")},
        {"account": "2120", "debit": Decimal("0"), "credit": Decimal("100")},
    ])
    results_neg = validate_journal_entry(entry_neg, mock_coa_lookup)
    r_neg = next(r for r in results_neg if r.rule_id == "VAL-004")
    assert r_neg.status == ValidationStatus.FAIL

    # Non-numeric string
    entry_str = dict(valid_entry, lines=[
        {"account": "6100", "debit": "not_a_number", "credit": Decimal("0")},
        {"account": "2120", "debit": Decimal("0"), "credit": Decimal("100")},
    ])
    results_str = validate_journal_entry(entry_str, mock_coa_lookup)
    r_str = next(r for r in results_str if r.rule_id == "VAL-004")
    assert r_str.status == ValidationStatus.FAIL

    # Non-finite float NaN / Infinity
    entry_nan = dict(valid_entry, lines=[
        {"account": "6100", "debit": float("nan"), "credit": Decimal("0")},
        {"account": "2120", "debit": Decimal("0"), "credit": Decimal("100")},
    ])
    results_nan = validate_journal_entry(entry_nan, mock_coa_lookup)
    r_nan = next(r for r in results_nan if r.rule_id == "VAL-004")
    assert r_nan.status == ValidationStatus.FAIL


def test_val_005_account_exists(valid_entry, mock_coa_lookup):
    """VAL-005: Every line's account code must exist in coa_lookup."""
    # Unknown account code 9999
    entry_unknown = dict(valid_entry, lines=[
        {"account": "9999", "debit": Decimal("500"), "credit": Decimal("0")},
        {"account": "2120", "debit": Decimal("0"), "credit": Decimal("500")},
    ])
    results = validate_journal_entry(entry_unknown, mock_coa_lookup)
    r = next(r for r in results if r.rule_id == "VAL-005")
    assert r.status == ValidationStatus.FAIL
    assert any("9999" in msg for msg in r.details["missing_accounts"])


def test_val_006_account_postable(valid_entry, mock_coa_lookup):
    """VAL-006: Accounts must not be Header type (is_postable must be True)."""
    # Using Header account 1000
    entry_header = dict(valid_entry, lines=[
        {"account": "1000", "debit": Decimal("500"), "credit": Decimal("0")},
        {"account": "2120", "debit": Decimal("0"), "credit": Decimal("500")},
    ])
    results = validate_journal_entry(entry_header, mock_coa_lookup)
    r = next(r for r in results if r.rule_id == "VAL-006")
    assert r.status == ValidationStatus.FAIL
    assert any("1000" in msg for msg in r.details["non_postable_accounts"])


def test_val_007_single_side(valid_entry, mock_coa_lookup):
    """VAL-007: Each line must have exactly one positive side (debit > 0 XOR credit > 0)."""
    # Both sides positive on line 1
    entry_both_pos = dict(valid_entry, lines=[
        {"account": "6100", "debit": Decimal("100"), "credit": Decimal("50")},
        {"account": "2120", "debit": Decimal("0"), "credit": Decimal("50")},
    ])
    results_both = validate_journal_entry(entry_both_pos, mock_coa_lookup)
    r_both = next(r for r in results_both if r.rule_id == "VAL-007")
    assert r_both.status == ValidationStatus.FAIL

    # Both sides zero on line 1
    entry_both_zero = dict(valid_entry, lines=[
        {"account": "6100", "debit": Decimal("0"), "credit": Decimal("0")},
        {"account": "2120", "debit": Decimal("0"), "credit": Decimal("0")},
    ])
    results_zero = validate_journal_entry(entry_both_zero, mock_coa_lookup)
    r_zero = next(r for r in results_zero if r.rule_id == "VAL-007")
    assert r_zero.status == ValidationStatus.FAIL


def test_val_008_entry_balanced(valid_entry, mock_coa_lookup):
    """VAL-008: Sum of debits must equal sum of credits EXACTLY (Decimal comparison)."""
    # Unbalanced entry: 28500 debit vs 25000 credit (difference 3500)
    entry_unbalanced = dict(valid_entry, lines=[
        {"account": "6300", "debit": Decimal("28500.00"), "credit": Decimal("0.00")},
        {"account": "6310", "debit": Decimal("0.00"), "credit": Decimal("25000.00")},
    ])
    results = validate_journal_entry(entry_unbalanced, mock_coa_lookup)
    r = next(r for r in results if r.rule_id == "VAL-008")
    assert r.status == ValidationStatus.FAIL
    assert r.details["difference"] == "3500.00"
    assert r.details["total_debit"] == "28500.00"
    assert r.details["total_credit"] == "25000.00"

    summary = get_validation_summary(results)
    assert summary["valid"] is False
    assert summary["difference"] == "3500.00"
    assert any("VAL-008" in err for err in summary["errors"])


def test_val_009_minimum_lines(valid_entry, mock_coa_lookup):
    """VAL-009: Entry must have at least 2 lines."""
    # 1 line entry
    entry_1line = dict(valid_entry, lines=[
        {"account": "6100", "debit": Decimal("100"), "credit": Decimal("0")},
    ])
    results = validate_journal_entry(entry_1line, mock_coa_lookup)
    r = next(r for r in results if r.rule_id == "VAL-009")
    assert r.status == ValidationStatus.FAIL
    assert r.details["line_count"] == 1


def test_val_010_duplicate_check(valid_entry, mock_coa_lookup):
    """VAL-010: Entry must not be flagged as a duplicate."""
    # Not duplicate
    results_clean = validate_journal_entry(valid_entry, mock_coa_lookup, is_duplicate=False)
    r_clean = next(r for r in results_clean if r.rule_id == "VAL-010")
    assert r_clean.status == ValidationStatus.PASS

    # Flagged duplicate via parameter
    results_dup = validate_journal_entry(valid_entry, mock_coa_lookup, is_duplicate=True)
    r_dup = next(r for r in results_dup if r.rule_id == "VAL-010")
    assert r_dup.status == ValidationStatus.FAIL

    # Flagged duplicate via dict key
    entry_dup_key = dict(valid_entry, is_duplicate=True)
    results_dup_key = validate_journal_entry(entry_dup_key, mock_coa_lookup)
    r_dup_key = next(r for r in results_dup_key if r.rule_id == "VAL-010")
    assert r_dup_key.status == ValidationStatus.FAIL


def test_get_validation_summary_structure(valid_entry, mock_coa_lookup):
    """Verify get_validation_summary structure and behavior."""
    results = validate_journal_entry(valid_entry, mock_coa_lookup)
    summary = get_validation_summary(results)

    assert "valid" in summary
    assert "total_debit" in summary
    assert "total_credit" in summary
    assert "difference" in summary
    assert "passed" in summary
    assert "failed" in summary
    assert "errors" in summary
    assert isinstance(summary["errors"], list)
    assert isinstance(summary["valid"], bool)
