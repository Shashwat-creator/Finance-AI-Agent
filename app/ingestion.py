"""
Ingestion module for the Manual Adjustments Agent.

Provides file ingestion with full source lineage tracking, SHA-256 integrity
hashing, and structured error handling for Chart of Accounts, manual adjustment
batches, trial balance data, and foreign exchange rates.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional, Sequence

import pandas as pd

from app.config import DATA_DIR
from app.schemas import (
    AdjustmentRequest,
    ChartOfAccount,
    JournalEntryLineInput,
    SourceMetadata,
)

logger = logging.getLogger(__name__)


def compute_file_hash(path: Path) -> str:
    """
    Compute the SHA-256 cryptographic hash of a file for audit lineage.

    Args:
        path: Absolute or relative path to the file.

    Returns:
        Hex-encoded SHA-256 string.

    Raises:
        FileNotFoundError: If the file does not exist.
        IOError: If reading the file fails.
    """
    if not path.is_file():
        raise FileNotFoundError(f"File not found for hash calculation: {path}")

    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def load_chart_of_accounts(path: Path) -> tuple[list[ChartOfAccount], SourceMetadata]:
    """
    Load the Chart of Accounts from CSV and track source lineage.

    Detects if the `is_postable` column is missing (as in standard input files)
    and infers it from account_type: Header -> False, everything else -> True.
    Computes SHA-256 hash of the file.

    Args:
        path: Path to chart_of_accounts.csv.

    Returns:
        Tuple of (list of ChartOfAccount models, SourceMetadata).

    Raises:
        FileNotFoundError: If the path does not exist.
        ValueError: If the CSV is empty, malformed, or missing required columns.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Chart of Accounts file not found: {path}")

    file_hash = compute_file_hash(path)

    try:
        df = pd.read_csv(
            path,
            dtype=str,
            keep_default_na=False,
            na_values=[],
            skipinitialspace=True,
        )
    except Exception as exc:
        raise ValueError(f"Malformed Chart of Accounts CSV at {path}: {exc}") from exc

    if df.empty:
        raise ValueError(f"Chart of Accounts CSV at {path} is empty.")

    # Validate essential columns
    required_cols = {"account_code", "account_name", "account_type", "statement"}
    missing_cols = required_cols - set(df.columns)
    if missing_cols:
        raise ValueError(
            f"Chart of Accounts missing required columns: {sorted(missing_cols)}"
        )

    # Infer is_postable if missing
    has_is_postable = "is_postable" in df.columns

    coa_list: list[ChartOfAccount] = []
    for idx, row in df.iterrows():
        account_code = str(row["account_code"]).strip()
        account_name = str(row["account_name"]).strip()
        account_type = str(row["account_type"]).strip()
        parent_code = str(row.get("parent_code", "")).strip() or None
        statement = str(row.get("statement", "")).strip()
        cf_category = str(row.get("cf_category", "")).strip() or None
        normal_balance = str(row.get("normal_balance", "")).strip()

        if has_is_postable:
            val_str = str(row["is_postable"]).strip().lower()
            is_postable = val_str in {"true", "1", "yes", "t"}
        else:
            # Header accounts cannot accept postings; leaf accounts can
            is_postable = account_type.lower() != "header"

        coa = ChartOfAccount(
            account_code=account_code,
            account_name=account_name,
            account_type=account_type,
            parent_code=parent_code,
            statement=statement,
            cf_category=cf_category,
            normal_balance=normal_balance,
            is_postable=is_postable,
        )
        coa_list.append(coa)

    metadata = SourceMetadata(
        filename=path.name,
        sha256_hash=file_hash,
        row_count=len(coa_list),
        record_count=len(coa_list),
    )

    logger.info(
        "Loaded %d COA accounts from %s (hash: %s...)",
        len(coa_list),
        path.name,
        file_hash[:8],
    )
    return coa_list, metadata


def load_adjustments(path: Path) -> tuple[list[AdjustmentRequest], SourceMetadata]:
    """
    Load manual adjustment requests from a JSON file.

    Handles outer envelope format:
    {"period": "2024-Q4", "functional_currency": "USD", "entries": [...]}
    as well as bare list formats.
    Attaches envelope period and currency to each entry if not already present.
    Computes SHA-256 hash of the input file.

    Args:
        path: Path to the manual adjustments JSON file.

    Returns:
        Tuple of (list of AdjustmentRequest models, SourceMetadata).

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If JSON is malformed or structure is invalid.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Adjustments file not found: {path}")

    file_hash = compute_file_hash(path)

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Malformed JSON in adjustments file {path} (line {exc.lineno}, col {exc.colno}): {exc.msg}"
        ) from exc
    except Exception as exc:
        raise ValueError(f"Error reading adjustments file {path}: {exc}") from exc

    outer_period: Optional[str] = None
    outer_currency: Optional[str] = None
    entries_raw: list[dict[str, Any]] = []

    if isinstance(data, dict):
        outer_period = data.get("period")
        outer_currency = data.get("functional_currency") or data.get("currency")
        raw_entries = data.get("entries")
        if raw_entries is None or not isinstance(raw_entries, list):
            raise ValueError(
                f"Adjustments JSON at {path} must contain an 'entries' list."
            )
        entries_raw = raw_entries
    elif isinstance(data, list):
        entries_raw = data
    else:
        raise ValueError(
            f"Adjustments JSON at {path} must be an object envelope or a list of entries."
        )

    requests: list[AdjustmentRequest] = []
    for idx, entry in enumerate(entries_raw):
        if not isinstance(entry, dict):
            raise ValueError(
                f"Adjustment entry at index {idx} in {path} must be a dictionary."
            )

        # Support both 'id' and 'request_id' keys
        req_id = str(entry.get("id") or entry.get("request_id") or f"ENTRY-{idx + 1}").strip()
        description = str(entry.get("description", "")).strip()
        date_val = str(entry.get("date", "")).strip()
        source_val = str(
            entry.get("source")
            or entry.get("source_file")
            or entry.get("source_reference")
            or "Unknown source"
        ).strip()

        period = str(entry.get("period") or outer_period or "").strip() or None
        currency = (
            str(entry.get("currency") or outer_currency or "").strip().upper() or None
        )

        raw_lines = entry.get("lines", [])
        parsed_lines: list[JournalEntryLineInput] = []

        if isinstance(raw_lines, list):
            for l_idx, line in enumerate(raw_lines):
                if not isinstance(line, dict):
                    raise ValueError(
                        f"Entry {req_id} line at index {l_idx} must be a dictionary."
                    )
                acc = str(line.get("account") or line.get("account_code") or "").strip()
                debit_val = line.get("debit", 0)
                credit_val = line.get("credit", 0)
                memo_val = line.get("memo")
                if memo_val is not None:
                    memo_val = str(memo_val).strip()

                try:
                    debit_dec = Decimal(str(debit_val))
                except (InvalidOperation, TypeError):
                    debit_dec = Decimal("0")

                try:
                    credit_dec = Decimal(str(credit_val))
                except (InvalidOperation, TypeError):
                    credit_dec = Decimal("0")

                parsed_lines.append(
                    JournalEntryLineInput(
                        account=acc,
                        debit=debit_dec,
                        credit=credit_dec,
                        memo=memo_val,
                    )
                )

        request_model = AdjustmentRequest(
            id=req_id,
            description=description,
            date=date_val,
            source=source_val,
            lines=parsed_lines,
            period=period,
            currency=currency,
        )
        requests.append(request_model)

    metadata = SourceMetadata(
        filename=path.name,
        sha256_hash=file_hash,
        record_count=len(requests),
        row_count=len(requests),
    )

    logger.info(
        "Loaded %d adjustments from %s (period=%s, currency=%s)",
        len(requests),
        path.name,
        outer_period,
        outer_currency,
    )
    return requests, metadata


def load_trial_balance(
    path: Path,
    coa: Optional[set[str] | Sequence[ChartOfAccount] | Path] = None,
) -> tuple[pd.DataFrame, SourceMetadata, list[str]]:
    """
    Load the trial balance CSV, detect duplicate rows, and check accounts against COA.

    Args:
        path: Path to trial_balance.csv.
        coa: Optional set of known account codes, list of ChartOfAccount models,
             or Path to chart_of_accounts.csv. If None, checks path.parent or DATA_DIR.

    Returns:
        Tuple of (DataFrame, SourceMetadata, list of warning strings).

    Raises:
        FileNotFoundError: If trial balance file does not exist.
        ValueError: If CSV is malformed.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Trial balance file not found: {path}")

    file_hash = compute_file_hash(path)
    warnings: list[str] = []

    try:
        df = pd.read_csv(
            path,
            dtype={"account_code": str, "currency": str},
            keep_default_na=False,
            na_values=[],
            skipinitialspace=True,
        )
    except Exception as exc:
        raise ValueError(f"Malformed trial balance CSV at {path}: {exc}") from exc

    if df.empty:
        raise ValueError(f"Trial balance CSV at {path} is empty.")

    # Strip string columns
    for col in ["account_code", "account_name", "currency"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    # 1. Detect duplicate rows (same account_code + currency)
    if "account_code" in df.columns and "currency" in df.columns:
        dups = df[df.duplicated(subset=["account_code", "currency"], keep=False)]
        if not dups.empty:
            grouped = dups.groupby(["account_code", "currency"]).size()
            for (acc, curr), count in grouped.items():
                warnings.append(
                    f"Duplicate trial balance row: account_code '{acc}' with currency '{curr}' "
                    f"appears {count} times."
                )

    # 2. Balance check: sum of debits must equal sum of credits
    if {"debit", "credit"}.issubset(df.columns):
        try:
            total_debit = sum(
                (Decimal(str(value or "0")) for value in df["debit"]), Decimal("0")
            )
            total_credit = sum(
                (Decimal(str(value or "0")) for value in df["credit"]), Decimal("0")
            )
        except (InvalidOperation, ValueError):
            warnings.append(
                "Trial balance contains non-numeric debit/credit values; "
                "the balance check was skipped."
            )
        else:
            difference = total_debit - total_credit
            if difference != Decimal("0"):
                warnings.append(
                    f"Trial balance does not balance: total debit {total_debit:,.2f} "
                    f"!= total credit {total_credit:,.2f} "
                    f"(difference {difference:,.2f}). Do not use it as a control total "
                    "until the imbalance is explained."
                )

    # 3. Identify COA codes for missing account detection
    known_coa_codes: Optional[set[str]] = None
    if isinstance(coa, set):
        known_coa_codes = coa
    elif isinstance(coa, (list, tuple)):
        known_coa_codes = {item.account_code for item in coa if isinstance(item, ChartOfAccount)}
    elif isinstance(coa, Path) and coa.is_file():
        try:
            coa_models, _ = load_chart_of_accounts(coa)
            known_coa_codes = {m.account_code for m in coa_models}
        except Exception as err:
            logger.warning("Could not load COA from provided path %s: %s", coa, err)
    elif coa is None:
        # Check standard locations
        fallback_coa_path = path.parent / "chart_of_accounts.csv"
        if not fallback_coa_path.is_file():
            fallback_coa_path = DATA_DIR / "chart_of_accounts.csv"
        if fallback_coa_path.is_file():
            try:
                coa_models, _ = load_chart_of_accounts(fallback_coa_path)
                known_coa_codes = {m.account_code for m in coa_models}
            except Exception as err:
                logger.warning("Could not auto-load COA from %s: %s", fallback_coa_path, err)

    if known_coa_codes is not None and "account_code" in df.columns:
        for idx, row in df.iterrows():
            code = str(row["account_code"]).strip()
            name = str(row.get("account_name", "")).strip()
            if code and code not in known_coa_codes:
                warnings.append(
                    f"Trial balance account '{code}' ('{name}') is not present in Chart of Accounts."
                )

    metadata = SourceMetadata(
        filename=path.name,
        sha256_hash=file_hash,
        row_count=len(df),
        record_count=len(df),
    )

    logger.info(
        "Loaded trial balance from %s: %d rows, %d warnings",
        path.name,
        len(df),
        len(warnings),
    )
    return df, metadata, warnings


def load_fx_rates(path: Path) -> tuple[dict[str, dict[str, Decimal]], SourceMetadata, list[str]]:
    """
    Load foreign exchange rates CSV and detect missing expected rate types.

    CSV format:
        currency,rate_type,rate,period

    Expected rate types for non-functional currencies (EUR, GBP, etc.):
        {"period_average", "period_end"}

    Args:
        path: Path to fx_rates.csv.

    Returns:
        Tuple of (
            nested dict: {currency: {rate_type: Decimal(rate)}},
            SourceMetadata,
            list of warning strings for missing or invalid rates
        ).

    Raises:
        FileNotFoundError: If fx_rates file does not exist.
        ValueError: If CSV is malformed.
    """
    if not path.is_file():
        raise FileNotFoundError(f"FX rates file not found: {path}")

    file_hash = compute_file_hash(path)
    warnings: list[str] = []

    try:
        df = pd.read_csv(
            path,
            dtype=str,
            keep_default_na=False,
            na_values=[],
            skipinitialspace=True,
        )
    except Exception as exc:
        raise ValueError(f"Malformed FX rates CSV at {path}: {exc}") from exc

    required_cols = {"currency", "rate_type", "rate"}
    missing_cols = required_cols - set(df.columns)
    if missing_cols:
        raise ValueError(f"FX rates CSV missing required columns: {sorted(missing_cols)}")

    fx_rates: dict[str, dict[str, Decimal]] = {}
    periods_by_curr: dict[str, str] = {}

    for idx, row in df.iterrows():
        curr = str(row["currency"]).strip().upper()
        rate_type = str(row["rate_type"]).strip().lower()
        rate_str = str(row["rate"]).strip()
        period = str(row.get("period", "")).strip()

        if period:
            periods_by_curr[curr] = period

        if not curr or not rate_type:
            continue

        try:
            rate_dec = Decimal(rate_str)
        except (InvalidOperation, TypeError):
            warnings.append(
                f"Invalid FX rate value '{rate_str}' for currency '{curr}' and rate_type '{rate_type}'."
            )
            continue

        if curr not in fx_rates:
            fx_rates[curr] = {}
        fx_rates[curr][rate_type] = rate_dec

    # Standard expected rate types for multi-currency conversion
    expected_rate_types = {"period_average", "period_end"}

    for curr, rates in fx_rates.items():
        if curr == "USD":
            # Functional currency typically doesn't require all reval rate types
            continue
        missing_types = expected_rate_types - set(rates.keys())
        period_str = periods_by_curr.get(curr, "unspecified period")
        for m_type in sorted(missing_types):
            warnings.append(
                f"Missing FX rate for currency '{curr}': rate_type '{m_type}' "
                f"is not provided for period '{period_str}'."
            )

    metadata = SourceMetadata(
        filename=path.name,
        sha256_hash=file_hash,
        row_count=len(df),
        record_count=len(df),
    )

    logger.info(
        "Loaded FX rates for %d currencies from %s with %d warnings",
        len(fx_rates),
        path.name,
        len(warnings),
    )
    return fx_rates, metadata, warnings
