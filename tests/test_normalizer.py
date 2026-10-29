"""
Unit tests for app/normalizer.py.
"""

from decimal import Decimal

from app.normalizer import (
    check_duplicate,
    generate_idempotency_key,
    normalize_adjustment,
)
from app.schemas import (
    AdjustmentRequest,
    IssueSeverity,
    JournalEntryLineInput,
)


class TestNormalizeAdjustment:
    def test_trim_whitespace_and_uppercase_currency(self):
        req = AdjustmentRequest(
            id="  JE-999  ",
            description="   Accrue annual software license   ",
            date="  2024-12-31  ",
            source="  Vendor Invoice #4412  ",
            period="  2024-Q4  ",
            currency="usd",
            lines=[
                JournalEntryLineInput(account=" 6200 ", debit=Decimal("5000.00"), credit=Decimal("0"), memo=" Soft lic "),
                JournalEntryLineInput(account=" 2120 ", debit=Decimal("0"), credit=Decimal("5000.00"), memo=" Accr "),
            ],
        )

        cleaned, logs, issues = normalize_adjustment(req)

        # Trimming checks
        assert cleaned.id == "JE-999"
        assert cleaned.description == "Accrue annual software license"
        assert cleaned.date == "2024-12-31"
        assert cleaned.source == "Vendor Invoice #4412"
        assert cleaned.period == "2024-Q4"
        assert cleaned.currency == "USD"
        assert cleaned.lines[0].account == "6200"
        assert cleaned.lines[0].memo == "Soft lic"

        # Logs check
        rules = [l.rule for l in logs]
        assert "TRIM_WHITESPACE" in rules
        assert "NORMALIZE_CASE" in rules

        # Issues check
        cleanups = [i for i in issues if i.severity == IssueSeverity.SAFE_CLEANUP]
        assert len(cleanups) >= 5

    def test_preserves_description_case(self):
        req = AdjustmentRequest(
            id="JE-001",
            description="Reclass AP to Accrued Liabilities for Project Alpha",
            date="2024-12-31",
            source="Manual note",
            period="2024-Q4",
            currency="USD",
            lines=[
                JournalEntryLineInput(account="2110", debit=Decimal("100"), credit=Decimal("0")),
                JournalEntryLineInput(account="2120", debit=Decimal("0"), credit=Decimal("100")),
            ],
        )
        cleaned, _, _ = normalize_adjustment(req)
        assert cleaned.description == "Reclass AP to Accrued Liabilities for Project Alpha"

    def test_flag_invalid_date_format(self):
        req = AdjustmentRequest(
            id="JE-002",
            description="Test date validation",
            date="31/12/2024",  # Invalid format (should be YYYY-MM-DD)
            source="Note",
            period="2024-Q4",
            currency="USD",
            lines=[
                JournalEntryLineInput(account="6100", debit=Decimal("100"), credit=Decimal("0")),
                JournalEntryLineInput(account="2120", debit=Decimal("0"), credit=Decimal("100")),
            ],
        )
        _, _, issues = normalize_adjustment(req)
        date_issues = [i for i in issues if i.field == "date" and i.severity == IssueSeverity.CLARIFICATION_REQUIRED]
        assert len(date_issues) == 1
        assert "YYYY-MM-DD" in date_issues[0].reason

    def test_flag_unsupported_currency(self):
        req = AdjustmentRequest(
            id="JE-003",
            description="Test currency",
            date="2024-12-31",
            source="Note",
            period="2024-Q4",
            currency="JPY",  # JPY is not in SUPPORTED_CURRENCIES (USD, EUR, GBP)
            lines=[
                JournalEntryLineInput(account="6100", debit=Decimal("100"), credit=Decimal("0")),
                JournalEntryLineInput(account="2120", debit=Decimal("0"), credit=Decimal("100")),
            ],
        )
        _, _, issues = normalize_adjustment(req)
        curr_issues = [i for i in issues if i.field == "currency" and i.severity == IssueSeverity.CLARIFICATION_REQUIRED]
        assert len(curr_issues) == 1
        assert "not in supported" in curr_issues[0].reason

    def test_flag_negative_and_both_positive_amounts(self):
        req = AdjustmentRequest(
            id="JE-004",
            description="Test invalid line amounts",
            date="2024-12-31",
            source="Note",
            period="2024-Q4",
            currency="USD",
            lines=[
                JournalEntryLineInput(account="6100", debit=Decimal("-100"), credit=Decimal("0")),
                JournalEntryLineInput(account="2120", debit=Decimal("50"), credit=Decimal("50")),
            ],
        )
        _, _, issues = normalize_adjustment(req)
        reasons = [i.reason for i in issues if i.severity == IssueSeverity.CLARIFICATION_REQUIRED]
        assert any("Negative debit amount" in r for r in reasons)
        assert any("both debit and credit positive" in r for r in reasons)

    def test_flag_missing_required_fields(self):
        req = AdjustmentRequest(
            id="",
            description="",
            date="",
            source="",
            period="",
            currency="",
            lines=[],
        )
        _, _, issues = normalize_adjustment(req)
        flagged_fields = {i.field for i in issues if i.severity == IssueSeverity.CLARIFICATION_REQUIRED}
        assert "id" in flagged_fields
        assert "description" in flagged_fields
        assert "date" in flagged_fields
        assert "source" in flagged_fields
        assert "period" in flagged_fields
        assert "currency" in flagged_fields
        assert "lines" in flagged_fields


class TestIdempotencyAndDuplicates:
    def test_idempotency_key_stability(self):
        req1 = AdjustmentRequest(
            id="JE-001",
            description="Accrue Q4 bonus pool",
            date="2024-12-31",
            source="Finance note",
            period="2024-Q4",
            currency="USD",
            lines=[
                JournalEntryLineInput(account="6100", debit=Decimal("850000.00"), credit=Decimal("0")),
                JournalEntryLineInput(account="2120", debit=Decimal("0"), credit=Decimal("850000.00")),
            ],
        )
        req2 = AdjustmentRequest(
            id="JE-001",
            description="Accrue Q4 bonus pool",
            date="2024-12-31",
            source="Different source note",  # Source doesn't alter idempotency key
            period="2024-Q4",
            currency="USD",
            lines=[
                JournalEntryLineInput(account="6100", debit=Decimal("850000.00"), credit=Decimal("0")),
                JournalEntryLineInput(account="2120", debit=Decimal("0"), credit=Decimal("850000.00")),
            ],
        )
        key1 = generate_idempotency_key(req1)
        key2 = generate_idempotency_key(req2)
        assert key1 == key2
        assert len(key1) == 64

    def test_idempotency_key_changes_on_amount_or_desc(self):
        req1 = AdjustmentRequest(
            id="JE-001",
            description="Accrue bonus",
            date="2024-12-31",
            source="Note",
            period="2024-Q4",
            currency="USD",
            lines=[
                JournalEntryLineInput(account="6100", debit=Decimal("100"), credit=Decimal("0")),
                JournalEntryLineInput(account="2120", debit=Decimal("0"), credit=Decimal("100")),
            ],
        )
        req2 = AdjustmentRequest(
            id="JE-001",
            description="Accrue bonus",
            date="2024-12-31",
            source="Note",
            period="2024-Q4",
            currency="USD",
            lines=[
                JournalEntryLineInput(account="6100", debit=Decimal("200"), credit=Decimal("0")),
                JournalEntryLineInput(account="2120", debit=Decimal("0"), credit=Decimal("200")),
            ],
        )
        assert generate_idempotency_key(req1) != generate_idempotency_key(req2)

    def test_check_duplicate(self):
        seen = {"key_alpha", "key_beta"}
        assert check_duplicate("key_alpha", seen) is True
        assert check_duplicate("key_gamma", seen) is False
