# Manual Adjustments - Batch Decision Summary

**Run ID**: `RUN-20261028T193605Z-92fe41-113971`  
**Period**: `2024-Q4` | **Functional currency**: `USD`  
**Mode**: `offline / deterministic`  
**Generated**: 2026-10-28T19:36:05.650551+00:00  

> [!IMPORTANT]
> Nothing in this run has been posted. Every entry requires human review and approval.

## Decision Counts

| Outcome | Count |
| :--- | ---: |
| Total | 10 |
| Ready For Review | 7 |
| Needs Clarification | 1 |
| Rejected Validation | 2 |
| Failed Safe | 0 |

## Per-Entry Decisions

| Entry | Intent | Status | Debit | Credit | Difference | Reason |
| :--- | :--- | :--- | ---: | ---: | ---: | :--- |
| `JE-001` | ACCRUAL | `READY_FOR_REVIEW` | 850,000.00 | 850,000.00 | 0.00 | Approved for human review. Intent: ACCRUAL. Debits equal credits at 850,000.00 USD. No posting occurs until a finance user signs off. |
| `JE-002` | RECLASSIFICATION | `REJECTED_VALIDATION` | 28,500.00 | 25,000.00 | 3,500.00 | Rejected by deterministic controls; this entry must not be posted.  - The entry does not balance: debits are 28,500.00 but credits are 25,000.00, a difference of 3,500.00. Rejected at VAL-008. |
| `JE-003` | REVALUATION | `READY_FOR_REVIEW` | 11,200.00 | 11,200.00 | 0.00 | Approved for human review with 1 warning(s) that need reviewer attention. Intent: REVALUATION. Debits equal credits at 11,200.00 USD. No posting occurs until a finance user signs off.  - FX revaluation for EUR is 11,2... |
| `JE-004` | PROVISION | `READY_FOR_REVIEW` | 45,000.00 | 45,000.00 | 0.00 | Approved for human review. Intent: PROVISION. Debits equal credits at 45,000.00 USD. No posting occurs until a finance user signs off. |
| `JE-005` | RECLASSIFICATION | `NEEDS_CLARIFICATION` | 18,500.00 | 18,500.00 | 0.00 | Paused for human clarification; do not post yet.  - VAL-005: Account code(s) not found in COA: Line 1: '6315'.  - Unknown account(s) 6315 have no COA node. Nearest match: 6310 Travel and Entertainment (55% confidence)... |
| `JE-006` | DEPRECIATION | `READY_FOR_REVIEW` | 215,000.00 | 215,000.00 | 0.00 | Approved for human review. Intent: DEPRECIATION. Debits equal credits at 215,000.00 USD. No posting occurs until a finance user signs off. |
| `JE-007` | TAX_ADJUSTMENT | `READY_FOR_REVIEW` | 38,000.00 | 38,000.00 | 0.00 | Approved for human review. Intent: TAX_ADJUSTMENT. Debits equal credits at 38,000.00 USD. No posting occurs until a finance user signs off. |
| `JE-008` | INTERCOMPANY | `REJECTED_VALIDATION` | 320,000.00 | 320,000.00 | 0.00 | Rejected by deterministic controls; this entry must not be posted.  - SEM-001: Entry posts debits and credits to the same account (2170) only. Its net effect is zero, which is typical of a circular or already-settled ... |
| `JE-009` | ACCRUAL | `READY_FOR_REVIEW` | 75,000.00 | 75,000.00 | 0.00 | Approved for human review. Intent: ACCRUAL. Debits equal credits at 75,000.00 USD. No posting occurs until a finance user signs off. |
| `JE-010` | RECLASSIFICATION | `READY_FOR_REVIEW` | 200,000.00 | 200,000.00 | 0.00 | Approved for human review. Intent: RECLASSIFICATION. Debits equal credits at 200,000.00 USD. No posting occurs until a finance user signs off. |

## Input Lineage

| File | SHA-256 (first 16) | Rows |
| :--- | :--- | ---: |
| chart_of_accounts.csv | `1139711bd276b2d4...` | 72 |
| manual_adjustments.json | `26fcad0c0c7bda53...` | 10 |
| trial_balance.csv | `6a5d38f4a7792e5a...` | 62 |
| fx_rates.csv | `268c9b9b5a14ea55...` | 7 |
| prior_period_tb.csv | `aa1439622935eadb...` | 33 |

## Input Data-Quality Warnings

- WARNING: Duplicate trial balance row: account_code '6310' with currency 'USD' appears 2 times.
- WARNING: Trial balance does not balance: total debit 71,072,200.00 != total credit 71,077,000.00 (difference -4,800.00). Do not use it as a control total until the imbalance is explained.
- WARNING: Trial balance account '9999' ('Suspense - Unmapped') is not present in Chart of Accounts.
- WARNING: Missing FX rate for currency 'GBP': rate_type 'period_end' is not provided for period '2024-Q4'.
- WARNING: Trial balance does not balance: total debit 35,177,800.00 != total credit 32,345,000.00 (difference 2,832,800.00). Do not use it as a control total until the imbalance is explained.
- WARNING: Trial balance account '6905' ('Sundry Operating Expenses') is not present in Chart of Accounts.
- WARNING: Prior-period account '6905' ('Sundry Operating Expenses') is absent from the current trial balance; confirm any account renumbering before roll-forward.
