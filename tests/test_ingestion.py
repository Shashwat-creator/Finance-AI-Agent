"""
Unit tests for app/ingestion.py.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.ingestion import (
    compute_file_hash,
    load_adjustments,
    load_chart_of_accounts,
    load_fx_rates,
    load_trial_balance,
)
from app.schemas import ChartOfAccount, SourceMetadata

INPUTS_DIR = Path(__file__).resolve().parent.parent.parent / "inputs"


class TestFileHashing:
    def test_compute_file_hash_valid(self, tmp_path: Path):
        test_file = tmp_path / "sample.txt"
        test_file.write_text("hello world", encoding="utf-8")
        h = compute_file_hash(test_file)
        assert len(h) == 64
        assert isinstance(h, str)
        # sha256 of "hello world"
        assert h == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"

    def test_compute_file_hash_missing(self, tmp_path: Path):
        missing = tmp_path / "does_not_exist.txt"
        with pytest.raises(FileNotFoundError):
            compute_file_hash(missing)


class TestLoadChartOfAccounts:
    def test_load_real_coa(self):
        coa_file = INPUTS_DIR / "chart_of_accounts.csv"
        assert coa_file.is_file(), f"Expected real COA file at {coa_file}"

        accounts, meta = load_chart_of_accounts(coa_file)
        assert len(accounts) == 72  # data rows, excluding the CSV header
        assert isinstance(meta, SourceMetadata)
        assert meta.filename == "chart_of_accounts.csv"
        assert len(meta.sha256_hash) == 64

        # Verify Header inference: is_postable is False for Header, True for others
        header_accounts = [a for a in accounts if a.account_type.lower() == "header"]
        leaf_accounts = [a for a in accounts if a.account_type.lower() != "header"]

        assert len(header_accounts) > 0
        for h in header_accounts:
            assert h.is_postable is False, f"Header account {h.account_code} should not be postable"

        for leaf in leaf_accounts:
            assert leaf.is_postable is True, f"Leaf account {leaf.account_code} should be postable"

        # Check specific known accounts
        acc_1110 = next(a for a in accounts if a.account_code == "1110")
        assert acc_1110.account_name == "Cash and Cash Equivalents"
        assert acc_1110.account_type == "Asset"
        assert acc_1110.statement == "BS"
        assert acc_1110.is_postable is True

    def test_coa_with_explicit_is_postable(self, tmp_path: Path):
        csv_content = (
            "account_code,account_name,account_type,parent_code,statement,cf_category,normal_balance,is_postable\n"
            "1000,Assets,Header,,BS,,Debit,false\n"
            "1110,Cash,Asset,1000,BS,Cash,Debit,true\n"
            "1199,Frozen Account,Asset,1000,BS,,Debit,false\n"
        )
        test_file = tmp_path / "custom_coa.csv"
        test_file.write_text(csv_content, encoding="utf-8")

        accounts, meta = load_chart_of_accounts(test_file)
        assert len(accounts) == 3
        acc_dict = {a.account_code: a for a in accounts}
        assert acc_dict["1000"].is_postable is False
        assert acc_dict["1110"].is_postable is True
        assert acc_dict["1199"].is_postable is False

    def test_coa_malformed(self, tmp_path: Path):
        bad_file = tmp_path / "bad_coa.csv"
        bad_file.write_text("random,unrelated,columns\n1,2,3\n", encoding="utf-8")
        with pytest.raises(ValueError, match="missing required columns"):
            load_chart_of_accounts(bad_file)


class TestLoadAdjustments:
    def test_load_real_adjustments(self):
        adj_file = INPUTS_DIR / "manual_adjustments.json"
        assert adj_file.is_file(), f"Expected real adjustments at {adj_file}"

        requests, meta = load_adjustments(adj_file)
        assert len(requests) == 10
        assert meta.filename == "manual_adjustments.json"

        # Verify outer envelope period & currency inherited
        for req in requests:
            assert req.period == "2024-Q4"
            assert req.currency == "USD"
            assert len(req.lines) >= 2

        # Verify JE-001 specifics
        je1 = requests[0]
        assert je1.id == "JE-001"
        assert je1.description == "Accrue Q4 bonus pool"
        assert je1.lines[0].account == "6100"
        assert je1.lines[0].debit == Decimal("850000.0")
        assert je1.lines[0].credit == Decimal("0.0")
        assert je1.lines[1].account == "2120"
        assert je1.lines[1].credit == Decimal("850000.0")

    def test_load_bare_list_format(self, tmp_path: Path):
        data = [
            {
                "request_id": "REQ-101",
                "description": "Office supplies reclass",
                "date": "2024-12-10",
                "source": "AP entry",
                "period": "2024-Q4",
                "currency": "USD",
                "lines": [
                    {"account": "6220", "debit": "150.00", "credit": "0"},
                    {"account": "1110", "debit": "0", "credit": "150.00"},
                ],
            }
        ]
        test_file = tmp_path / "bare_adjustments.json"
        test_file.write_text(json.dumps(data), encoding="utf-8")

        requests, meta = load_adjustments(test_file)
        assert len(requests) == 1
        assert requests[0].id == "REQ-101"
        assert requests[0].lines[0].debit == Decimal("150.00")

    def test_load_malformed_json(self, tmp_path: Path):
        bad_json = tmp_path / "broken.json"
        bad_json.write_text("{ unquoted_key: invalid }", encoding="utf-8")
        with pytest.raises(ValueError, match="Malformed JSON"):
            load_adjustments(bad_json)


class TestLoadTrialBalance:
    def test_load_real_tb_detects_duplicates_and_unmapped(self):
        tb_file = INPUTS_DIR / "trial_balance.csv"
        assert tb_file.is_file(), f"Expected real TB at {tb_file}"

        df, meta, warnings = load_trial_balance(tb_file)
        assert len(df) == 62  # data rows, excluding the CSV header
        assert meta.filename == "trial_balance.csv"

        # Check duplicate row warning (6310 USD appears twice)
        dup_warn = [w for w in warnings if "6310" in w and "Duplicate" in w]
        assert len(dup_warn) > 0, "Expected duplicate warning for account 6310"

        # Check unmapped COA account (9999 Suspense)
        coa_warn = [w for w in warnings if "9999" in w]
        assert len(coa_warn) > 0, "Expected warning for account 9999 not in COA"


class TestLoadFxRates:
    def test_load_real_fx_rates_detects_missing_gbp_period_end(self):
        fx_file = INPUTS_DIR / "fx_rates.csv"
        assert fx_file.is_file(), f"Expected real FX rates at {fx_file}"

        fx_rates, meta, warnings = load_fx_rates(fx_file)
        assert "EUR" in fx_rates
        assert "GBP" in fx_rates
        assert "USD" in fx_rates

        # Verify rate types loaded as Decimal
        assert isinstance(fx_rates["EUR"]["period_end"], Decimal)
        assert fx_rates["EUR"]["period_end"] == Decimal("1.095")
        assert fx_rates["EUR"]["period_average"] == Decimal("1.082")

        # Verify missing GBP period_end is detected in warnings
        gbp_missing = [w for w in warnings if "GBP" in w and "period_end" in w]
        assert len(gbp_missing) > 0, "Expected warning for missing GBP period_end rate"
