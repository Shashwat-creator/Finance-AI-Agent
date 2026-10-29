"""
Unit tests for reconciliation and evidence checks in app/reconciliation.py.
"""

from decimal import Decimal
import pytest

from app.reconciliation import check_normal_balance_alignment, reconcile_entry
from app.schemas import ChartOfAccount, JournalEntryLine, ProposedJournalEntry


@pytest.fixture
def mock_coa_lookup() -> dict[str, ChartOfAccount]:
    """Provide standard Chart of Accounts fixture."""
    return {
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
def proposed_bonus_entry() -> ProposedJournalEntry:
    """Provide a standard proposed bonus accrual entry."""
    return ProposedJournalEntry(
        request_id="MA-001",
        description="Accrue Q4 employee bonus",
        period="2024-Q4",
        currency="USD",
        lines=[
            JournalEntryLine(
                account_code="6100",
                account_name="Salaries and Wages",
                debit=Decimal("850000.00"),
                credit=Decimal("0.00"),
                memo="Bonus expense",
            ),
            JournalEntryLine(
                account_code="2120",
                account_name="Accrued Expenses",
                debit=Decimal("0.00"),
                credit=Decimal("850000.00"),
                memo="Accrued bonus liability",
            ),
        ],
        rationale="Accrue bonus expense based on Q4 schedule",
        source_references=["bonus_schedule_oct.csv#row14"],
    )


def test_check_normal_balance_alignment_matching(mock_coa_lookup):
    """Test normal balance matches: debit to Debit account, credit to Credit account."""
    line_debit = JournalEntryLine(account_code="6100", debit=Decimal("100"), credit=Decimal("0"))
    res_debit = check_normal_balance_alignment(line_debit, mock_coa_lookup["6100"])
    assert res_debit is None

    line_credit = JournalEntryLine(account_code="2120", debit=Decimal("0"), credit=Decimal("100"))
    res_credit = check_normal_balance_alignment(line_credit, mock_coa_lookup["2120"])
    assert res_credit is None


def test_check_normal_balance_alignment_misaligned(mock_coa_lookup):
    """Test normal balance misalignment produces informative warnings for reclassifications."""
    # Crediting an expense account (6100 has normal balance Debit)
    line_credit_exp = JournalEntryLine(account_code="6100", debit=Decimal("0"), credit=Decimal("500"))
    warning1 = check_normal_balance_alignment(line_credit_exp, mock_coa_lookup["6100"])
    assert warning1 is not None
    assert "credit of 500" in warning1
    assert "Debit" in warning1

    # Debiting a liability account (2120 has normal balance Credit)
    line_debit_liab = JournalEntryLine(account_code="2120", debit=Decimal("300"), credit=Decimal("0"))
    warning2 = check_normal_balance_alignment(line_debit_liab, mock_coa_lookup["2120"])
    assert warning2 is not None
    assert "debit of 300" in warning2
    assert "Credit" in warning2


def test_reconcile_entry_balanced(proposed_bonus_entry, mock_coa_lookup):
    """Test reconcile_entry on a valid, balanced entry with aligned normal balances."""
    result = reconcile_entry(proposed_bonus_entry, mock_coa_lookup)

    assert result.is_balanced is True
    assert result.total_debit == Decimal("850000.00")
    assert result.total_credit == Decimal("850000.00")
    assert result.difference == Decimal("0")
    assert result.warnings == []

    # Check evidence checks
    check_names = [chk["check"] for chk in result.evidence_checks]
    assert "entry_balanced" in check_names
    assert "account_exists" in check_names
    assert "normal_balance_alignment" in check_names
    assert "source_references" in check_names

    # Check that proposed object's totals were updated
    assert proposed_bonus_entry.total_debit == Decimal("850000.00")
    assert proposed_bonus_entry.total_credit == Decimal("850000.00")
    assert proposed_bonus_entry.difference == Decimal("0")


def test_reconcile_entry_unbalanced(mock_coa_lookup):
    """Test reconcile_entry on an unbalanced entry (28500 vs 25000)."""
    proposed = ProposedJournalEntry(
        request_id="MA-002",
        description="Reclass marketing to T&E",
        period="2024-Q4",
        currency="USD",
        lines=[
            JournalEntryLine(
                account_code="6300",
                debit=Decimal("28500.00"),
                credit=Decimal("0.00"),
            ),
            JournalEntryLine(
                account_code="6310",
                debit=Decimal("0.00"),
                credit=Decimal("25000.00"),
            ),
        ],
        source_references=["marketing_reclass.csv#row8"],
    )

    result = reconcile_entry(proposed, mock_coa_lookup)

    assert result.is_balanced is False
    assert result.total_debit == Decimal("28500.00")
    assert result.total_credit == Decimal("25000.00")
    assert result.difference == Decimal("3500.00")
    assert any("not balanced" in w for w in result.warnings)

    # 6310 is Expense (normal balance Debit), credited 25000.00 -> generates reclass warning
    assert any("6310" in w and "credit of 25000.00" in w for w in result.warnings)


def test_reconcile_entry_missing_source_references(mock_coa_lookup):
    """Test reconcile_entry flags empty source references in warnings and evidence checks."""
    proposed = ProposedJournalEntry(
        request_id="MA-003",
        description="Entry with no source",
        period="2024-Q4",
        currency="USD",
        lines=[
            JournalEntryLine(account_code="6100", debit=Decimal("100"), credit=Decimal("0")),
            JournalEntryLine(account_code="2120", debit=Decimal("0"), credit=Decimal("100")),
        ],
        source_references=[],
    )

    result = reconcile_entry(proposed, mock_coa_lookup)
    assert any("source references" in w.lower() for w in result.warnings)

    src_chk = next(chk for chk in result.evidence_checks if chk["check"] == "source_references")
    assert src_chk["passed"] is False


def test_reconcile_entry_unknown_account(mock_coa_lookup):
    """Test reconcile_entry handles unknown account codes in lines."""
    proposed = ProposedJournalEntry(
        request_id="MA-004",
        description="Entry with unknown account",
        period="2024-Q4",
        currency="USD",
        lines=[
            JournalEntryLine(account_code="9999", debit=Decimal("50"), credit=Decimal("0")),
            JournalEntryLine(account_code="2120", debit=Decimal("0"), credit=Decimal("50")),
        ],
        source_references=["schedule.csv"],
    )

    result = reconcile_entry(proposed, mock_coa_lookup)
    assert any("9999" in w for w in result.warnings)

    acc_chks = [chk for chk in result.evidence_checks if chk.get("account_code") == "9999"]
    assert len(acc_chks) > 0
    assert acc_chks[0]["passed"] is False


def test_reconcile_entry_dict_input(mock_coa_lookup):
    """Test reconcile_entry when input is a dictionary instead of ProposedJournalEntry model."""
    proposed_dict = {
        "request_id": "MA-005",
        "description": "Dict input test",
        "period": "2024-Q4",
        "currency": "USD",
        "lines": [
            {"account_code": "6100", "debit": Decimal("200"), "credit": Decimal("0")},
            {"account_code": "2120", "debit": Decimal("0"), "credit": Decimal("200")},
        ],
        "source_references": ["sample.csv"],
    }
    result = reconcile_entry(proposed_dict, mock_coa_lookup)
    assert result.is_balanced is True
    assert result.total_debit == Decimal("200")
    assert result.total_credit == Decimal("200")

