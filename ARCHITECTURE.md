# Architecture — Agentic Financial Statement Generation

## 1. Problem and design stance

Inputs are a messy trial balance, a Chart of Accounts that is itself inconsistent, a prior-period trial balance, FX rates with gaps, and a set of manual journal entries. The outputs are a balance sheet, P&L, cash-flow statement and statement of changes in equity that tie to each other and to the TB, plus an audit trail explaining every number.

The core stance: **accounting is deterministic; language is probabilistic.** Controls, arithmetic, mapping acceptance and tie-outs are code. LLMs are used only where judgement over text is genuinely required — classifying intent, proposing account candidates, drafting narrative — and every LLM output is schema-validated and re-checked against deterministic facts before it can affect a statement. An LLM never computes a total that is used downstream.

This is deliberately **five agents, not twelve**. Each agent owns one accounting artifact and one failure boundary; splitting further would multiply hand-offs and make the audit trail harder to follow, not easier.

## 2. Topology

```
                    ┌──────────────────────────────┐
 inputs/            │ 0. Orchestrator (deterministic)│
 COA, TB, prior TB, │  run id, hashes, retries,      │
 FX, adjustments    │  state, artifact manifest      │
                    └──────────────┬───────────────┘
                                   │
                    ┌──────────────▼───────────────┐
                    │ 1. Intake & Controls Agent    │
                    │ normalize → validate → route  │
                    │ (this repo: Manual Adjustments)│
                    └──────┬───────────────┬───────┘
             accepted JEs  │               │ rejected / needs clarification
                           │               └──────────────► review queue
                    ┌──────▼───────────────┐
                    │ 2. TB & Mapping Agent │
                    │ COA map, FX translate │
                    │ adjusted TB, control  │
                    └──────┬───────────────┘
                           │ adjusted TB + mapping decisions
                    ┌──────▼───────────────┐
                    │ 3. Statement Assembly │
                    │ BS + P&L (Decimal)    │
                    └──────┬───────────────┘
                           │ BS/P&L + movement ledger
                    ┌──────▼───────────────┐
                    │ 4. Cash Flow & Equity │
                    │ indirect CF, SOCIE    │
                    └──────┬───────────────┘
                           │ four statements + evidence
                    ┌──────▼───────────────┐
                    │ 5. Review & Disclosure│
                    │ tie-out, narrative,   │
                    │ exception pack, HITL  │
                    └───────────────────────┘
```

The orchestrator passes typed artifacts, never free text. Agents are stateless functions over an explicit state object, so any run can be replayed from its inputs and hashes.

## 3. Agent responsibilities

| Agent | Owns | Deterministic work | LLM work (optional, bounded) | Refuses to proceed when |
| :--- | :--- | :--- | :--- | :--- |
| **0. Orchestrator** | Run lifecycle, state, retries, manifest | Hashing, idempotency, ordering, retry budget, artifact writes | None | Any upstream artifact fails schema validation |
| **1. Intake & Controls** | Manual adjustments, COA validation | Normalization, duplicate detection, VAL-001..010, SEM-001..006, reconciliation | Intent classification; ranked COA candidates for unknown codes | Entry is unbalanced, non-postable, duplicate, or references an unknown account |
| **2. TB & Mapping** | TB preparation, COA hierarchy, FX | Account roll-ups, normal-balance signs, FX translation, TB balance check, rename detection | Explain ambiguous mappings / TBD categorisation; never choose the number | TB does not balance without an identified cause; FX rate missing |
| **3. Statement Assembly** | BS and P&L | Aggregation by `statement` and `account_type`, subtotal logic, Decimal arithmetic | Draft section labels / variance commentary | Any account has no statement assignment or sign convention |
| **4. Cash Flow & Equity** | Indirect cash flow, SOCIE | Working-capital deltas, non-cash add-backs, equity roll-forward, cash tie-out | Draft cash-flow narrative | Cash-flow categories are `TBD`; CF does not tie to BS cash movement |
| **5. Review & Disclosure** | Tie-out, exception pack, disclosures | Cross-statement checks, materiality, evidence assembly, sign-off gate | Draft disclosure prose and reviewer notes | Any hard tie-out fails |

## 4. Deterministic / LLM boundary

| Concern | Deterministic (code) | LLM may assist | Hard rule |
| :--- | :--- | :--- | :--- |
| Money | `Decimal` only, no floats | — | LLM output containing amounts is ignored; amounts are re-read from source |
| Balance | `sum(debit) == sum(credit)` | — | A non-zero difference can never be approved |
| Account existence | Exact COA lookup | Rank candidate codes | LLM may only return codes that already exist in the COA |
| Postability | `is_postable` / `Header` flag | — | Header accounts are never postable |
| Mapping acceptance | Human approval after candidate proposal | Explain candidate | Model suggestion ≠ accepted mapping |
| Period / dates | Range check against quarter bounds | — | Out-of-period entries are rejected |
| Tie-outs | Exact cross-statement checks | — | No tolerance beyond configured rounding |
| Intent | Keyword fallback | Classify description into a fixed enum | Output must validate against the enum or fallback is used |
| Narrative | Template from computed facts | Draft wording | Every number in prose must resolve to a computed fact |

**Why this boundary.** Auditors need a reproducible reason for every number. Probabilistic output can be useful for *where to look* and *how to explain*, but it cannot be the basis of a debit or credit. The LLM is therefore an adviser with a schema, not an actor with write access.

## 5. Data contracts

All inter-agent payloads are typed models (Pydantic v2), versioned and validated at the boundary:

- `AdjustmentRequest` → `NormalizedAdjustment` → `ProposedJournalEntry`
- `ValidationResult` (`rule_id`, `status`, `message`, `details`)
- `ReconciliationResult` (`total_debit`, `total_credit`, `difference`, `evidence_checks`)
- `FinalAdjustmentReport` (status, intent, reason, lineage, audit events)
- `SourceMetadata` (filename, SHA-256, row counts)
- Downstream: `AdjustedTrialBalance`, `StatementSet` (BS, P&L, CF, SOCIE), `TieOutMatrix`

Money is `Decimal` end to end. Enums are closed (`WorkflowStatus`, `ValidationStatus`, `AdjustmentIntent`). Unknown fields fail validation rather than being silently dropped.

## 6. Validation and reconciliation loop

1. **Normalize** — trim, uppercase currency, parse dates, coerce amounts. Every change is logged as a `NormalizationChange`.
2. **Structural rules** — required fields, period, currency, amounts, account existence/postability, single-sided lines, exact balance, minimum lines, duplicates.
3. **Semantic rules** — single-account wash, intercompany without counterparty, `TBD` cash-flow category, FX revaluation tie-out, duplicate lines, date-in-period.
4. **Reconcile** — recompute totals independently and compare; check normal-balance direction; attach evidence checks.
5. **Decide** — hard failures reject; ambiguity clarifies; otherwise ready for review. Every status carries a reason built from the rule results.
6. **Retry policy** — only *transient* failures (I/O, LLM timeout) are retried, bounded (default 1 retry), with backoff. Validation failures are **never** retried; retrying a bad journal only wastes time and hides the defect.
7. **Escalate** — anything unresolved goes to a human queue with the exact failed rules, candidate mappings and source evidence.

The loop terminates because each stage either produces a typed artifact or a terminal status (`REJECTED_VALIDATION`, `NEEDS_CLARIFICATION`, `FAILED_SAFE`). There is no "best effort" fallback that invents a number.

## 7. Failure modes and behaviour

| Failure | Detection | Behaviour |
| :--- | :--- | :--- |
| TB does not balance | Sum debits/credits at ingest | Warn; block use as a control total; require an identified cause before statements |
| Missing FX rate | Rate-type completeness check | Mark affected translation impossible; escalate; never default to 1.0 |
| Unknown account in JE | COA lookup | `NEEDS_CLARIFICATION` + ranked candidates; no auto-map |
| Account in COA but Header | `is_postable` | Reject posting |
| Unbalanced JE | Exact Decimal comparison | Reject with the exact difference |
| Duplicate JE | Content idempotency key | Reject duplicate; first occurrence proceeds |
| Circular / wash entry | Single distinct account | Reject |
| `TBD` cash-flow category | COA metadata | Statement can be drafted; cash-flow statement blocks |
| LLM unavailable / bad output | Schema + enum validation, timeout | Deterministic fallback; run continues |
| LLM hallucinated account | Check returned code against COA | Discard output; deterministic candidates only |
| Unexpected exception | Top-level guard | `FAILED_SAFE`, entry quarantined, stack logged |
| Partial batch failure | Per-entry isolation | Other entries continue; summary reports counts |

**Fail closed, not open.** A missing rate, ambiguous mapping or failed tie-out stops the dependent artifact. The system prefers "cannot produce a reliable statement" over a plausible-looking wrong one.

## 8. Traceability

Every run records:

- SHA-256 of every input file, plus row counts.
- A content-derived idempotency key per journal entry.
- The normalization log (field, before, after, rule).
- Every rule result, passed or failed, with structured details.
- Reconciliation evidence checks (balance, account existence, normal balance, source references).
- An append-only audit trail per entry (node start/end, intent, mapping decision, validation, reconciliation, failure).
- The decision reason, derived from the above, not written independently.
- Artifacts keyed by `RUN_ID` and entry ID, so a reviewer can go from statement line → TB account → adjustment → source document.

Payload sanitization removes secrets before anything is logged. Logs are structured and correlated by run ID.

## 9. Human-in-the-loop

- No entry is posted automatically. `READY_FOR_REVIEW` is a queue state.
- The reviewer sees the proposed entry, the rule matrix, warnings, candidate mappings and source hashes on one page.
- Approvals, rejections and mapping choices are recorded with reviewer identity and timestamp (persisted in the production design; this slice models the status and evidence).
- Materiality thresholds and conflict-of-interest rules can route entries to a second reviewer.

## 10. Scope of this repository

Built and tested here: **Agent 1 (Manual Adjustments)** — ingestion, normalization, idempotency, structural + semantic validation, mapping candidates, reconciliation, status policy, audit trail, batch artifacts, CLI, LLM privacy/guardrails/telemetry, and 95 tests.

Designed but not built here: TB preparation and FX translation (Agent 2), statement assembly (Agent 3), cash flow and equity (Agent 4), review/disclosure (Agent 5), and the persisted approval workflow. The contracts and status semantics in this slice are the seam those agents plug into.

## 11. Build order and why

1. Controls first (this slice): without trustworthy adjustments, every downstream statement is built on sand.
2. TB + FX next: establishes the control totals and translation basis.
3. BS/P&L: the easiest artifacts to tie out and the base for the other two statements.
4. CF + SOCIE: depend on BS/P&L movements and cash definition.
5. Review/disclosure last: only meaningful once all four statements tie.

Each stage ships with its own deterministic tests before the next begins. LLM assistance is added only after the deterministic path is green.
