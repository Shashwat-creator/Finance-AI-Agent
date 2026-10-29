# Manual Adjustments Agent

A deterministic-first agent that takes a batch of manual journal entries, validates them against the Chart of Accounts and the trial balance, and produces auditable **ACCEPT / QUARANTINE / REJECT** decision records with plain-English reasons. It never posts anything.

This repository is the **Manual Adjustments slice** of a larger five-agent financial-close system described in [`ARCHITECTURE.md`](ARCHITECTURE.md). The slice was chosen because it consumes both messy inputs (the COA as the validation oracle, the JSON batch as the workload) and exposes the hard problems the assignment is about: silent data defects, double-entry arithmetic, account mapping, and the line between deterministic controls and LLM judgement.

---

## What it does

For each entry in `manual_adjustments.json` the agent:

1. Normalizes the payload (trim, uppercase currency, parse dates, coerce amounts to `Decimal`).
2. Detects duplicates with a content-derived idempotency key.
3. Classifies intent (LLM when configured, deterministic keywords otherwise).
4. Resolves unknown account codes against the COA and proposes ranked candidates — it never invents an account.
5. Runs ten structural double-entry rules plus six accounting-semantics checks.
6. Reconciles exact totals and normal-balance direction.
7. Assigns a status and writes a plain-English reason, then emits JSON + Markdown evidence.

It deliberately **does not** generate statements, post journals, mutate source files, or let an LLM do arithmetic. Those are downstream systems in the wider design.

## Quickstart

Requires Python 3.11+.

```powershell
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env      # optional; offline mode works without a key
python main.py --offline
```

```bash
# macOS / Linux
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py --offline
```

By default the CLI reads `../inputs` and writes to `./outputs`. Override either:

```bash
python main.py --inputs ../inputs --outputs outputs --period 2024-Q4 --offline
python -m app.cli --offline --json      # machine-readable summary on stdout
```

`--offline` (or `OFFLINE=1`) guarantees no network call; the fake key in `.env.example` also forces mock mode.

## Outputs

Each run writes to `outputs/`:

| File | Purpose |
| :--- | :--- |
| `<RUN_ID>_<JE>.json` | Full audit report: lineage hashes, normalization log, every rule result, reconciliation evidence, audit trail, decision reason. |
| `<RUN_ID>_<JE>.md` | Human-readable review pack for a finance user. |
| `batch_summary.json` / `.md` | Counts, per-entry decisions, input hashes and data-quality warnings. |
| `decision_log.json` | One row per decision with failed rule IDs, idempotency key and mapper candidates — the audit spine. |
| `accepted_adjustments.json` | Only `READY_FOR_REVIEW` entries, with an explicit "nothing posted" note. Intended as the hand-off payload to the posting system after human approval. |

The run is reproducible: the run ID and every entry's idempotency key are derived from the input file hashes and normalized content.

## How a decision is made

```
adjustment ──► normalize ──► duplicate check ──► intent ──► COA mapper
                                                              │
                                                              ▼
                                             structural rules (VAL-001..010)
                                                              │
                                                              ▼
                                          semantic rules (SEM-001..006)
                                                              │
                                                              ▼
                                       reconcile totals + normal balance
                                                              │
                                                              ▼
                               status + plain-English reason ──► artifacts
```

**Deterministic boundary.** All money and all decisions are computed in Python with `Decimal`. The LLM is optional and confined to intent classification and (in the wider system) narrative drafting. If the LLM is unavailable, the deterministic classifier takes over and the run still completes.

### Status semantics

| Status | Meaning | Posting? |
| :--- | :--- | :--- |
| `READY_FOR_REVIEW` | No blocking failure; may carry advisory warnings. | No — queue for human approval. |
| `NEEDS_CLARIFICATION` | Ambiguous or incomplete; a human must supply/mapping before it can proceed. | No. |
| `REJECTED_VALIDATION` | A hard accounting-integrity rule failed. | Never. |
| `FAILED_SAFE` | Unexpected processing error; the entry is quarantined, not guessed. | Never. |

Hard-fail rules are `VAL-004`, `VAL-006`, `VAL-007`, `VAL-008`, `VAL-009`, `VAL-010`, `SEM-001`, `SEM-002`, and `SEM-006`. Other failures (e.g. unknown account, wrong period, missing source) pause for clarification rather than silently dropping the entry.

### Rule catalogue

| Rule | Check | Failure behaviour |
| :--- | :--- | :--- |
| `VAL-001` | Required fields: description, date, source, lines | Clarification |
| `VAL-002` | Period matches the run period | Clarification |
| `VAL-003` | Currency ∈ {USD, EUR, GBP} | Clarification |
| `VAL-004` | Amounts numeric, finite, non-negative | Reject |
| `VAL-005` | Every account exists in the COA | Clarification |
| `VAL-006` | Accounts are postable (not Header) | Reject |
| `VAL-007` | Each line is debit XOR credit | Reject |
| `VAL-008` | Total debits == total credits exactly | Reject |
| `VAL-009` | At least two lines | Reject |
| `VAL-010` | No duplicate entry in the batch | Reject |
| `SEM-001` | Single-account wash / circular entry | Reject |
| `SEM-002` | Intercompany entry has a counterparty/cash leg | Reject |
| `SEM-003` | COA cash-flow category is not `TBD` | Warning |
| `SEM-004` | FX revaluation ties to rate-implied estimate | Warning |
| `SEM-005` | No duplicate identical lines | Warning |
| `SEM-006` | Entry date falls inside the accounting period | Reject |

## How the supplied data defects are handled

Nothing in `inputs/` is "cleaned" in place. Defects are surfaced as warnings on the run and, where they affect an entry, as a status + reason.

| Defect | Where it surfaces | Handling |
| :--- | :--- | :--- |
| TB debits ≠ credits by **4,800.00** (`71,072,200` vs `71,077,000`) | `batch_summary` input warnings | Reported as an unusable control total; no silent plug. |
| `GBP period_end` rate missing | Input warnings; `SEM-004` | FX tie-out marked "could not be tied out"; human must supply the rate. |
| `9999 Suspense – Unmapped` not in COA | Input warnings; `VAL-005` if used | Never auto-mapped; escalated with evidence. |
| `6310` appears twice in the TB | Input warnings | Flagged as duplicate TB rows, not merged silently. |
| `1150` / `2170` cash-flow category `TBD` | `SEM-003` when used | Blocks cash-flow reporting downstream, not the journal itself. |
| `1290 Other Assets` Header with no children | COA ingestion | `is_postable=False`; `VAL-006` rejects any posting to it. |
| `JE-002` unbalanced by **3,500.00** | `VAL-008` | `REJECTED_VALIDATION` with the exact difference in the reason. |
| `JE-005` references orphan `6315` | `VAL-005` + mapper | `NEEDS_CLARIFICATION`; nearest COA candidate `6310` at 55% confidence. |
| `JE-008` single-account wash on `2170` | `SEM-001` | `REJECTED_VALIDATION`; the net effect is zero. |
| `JE-003` revaluation **11,200** vs rate-implied **10,730.20 / 19,809.60** | `SEM-004` | `READY_FOR_REVIEW` **with a warning** — balanced, but the reviewer must verify the basis. |
| Prior TB `6905` vs current COA `6900`; prior TB imbalanced by ~2.83M | Input warnings | Reported for roll-forward lineage; renumbering must be confirmed. |
| COA 72 data rows, TB 62 data rows | Ingestion metadata | Counts are treated as data, not hard-coded. |

Run on the supplied inputs produces **10 entries: 7 ready, 1 needs clarification, 2 rejected, 0 failed-safe**.

## Architecture

```
app/
  config.py            env + paths + mock/offline switch
  schemas.py           Pydantic contracts (Decimal money, strict enums)
  ingestion.py         CSV/JSON loaders, SHA-256 lineage, defect warnings
  normalizer.py        data-quality transforms, idempotency keys
  validator.py         VAL-001..010 + SEM-001..006
  reconciliation.py    exact totals, normal balance, evidence checks
  reporting.py         JSON + Markdown audit reports
  audit.py             sanitized, append-only audit trail
  workflow.py          orchestration, status policy, batch artifacts
  cli.py               argparse entry point
  agents/
    intake_agent.py    optional LLM intent + deterministic fallback
    mapper_agent.py    ranked COA candidates for unknown codes
```

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the wider five-agent design, the deterministic/LLM boundary, failure modes and the validation loop.

## Tests

```bash
python -m pytest -q
```

95 tests cover ingestion, normalization, all validation rules, reconciliation, reporting, audit-trail sanitization, the mapper, privacy redaction and prompt-injection guardrails, evaluator retries/hallucination handling, token telemetry, Redis/Cosmos/Azure Language integration boundaries, idempotency, and an end-to-end batch run against the supplied files (including all three seeded journal defects and the artifact contract). The suite runs offline and is deterministic.

### Optional LLM safety configuration

- Every LLM call passes through privacy redaction, prompt-injection neutralization, bounded retries, Redis rate limiting (when configured), and structured token telemetry before the agent sees the response.
- `INTENT_MODEL_NAME` and `EVALUATOR_MODEL_NAME` select separate models; defaults are `gpt-5-mini` and `gpt-5.1`.
- Local deterministic redaction always runs. When `AZURE_LANGUAGE_ENDPOINT` and `AZURE_LANGUAGE_KEY` are set, Azure AI Language PII detection is layered on top. No hashing is used where values can be removed.
- `logs/llm_usage.jsonl` records agent, model, status, attempt, latency, token counts, request/run IDs, and redaction metadata, never prompt content.
- `redis`, `azure-cosmos`, and Azure Language credentials are optional and not installed by this repository. Missing configuration is reported in startup warnings and falls back safely.

## Design decisions and trade-offs

- **Deterministic core, narrow AI surface.** Arithmetic and control decisions are Python/`Decimal`; the LLM only classifies intent. This makes every decision reproducible and defensible to an auditor.
- **Fail closed, never guess.** Unknown accounts, duplicates and integrity failures stop the entry. The mapper may *suggest* `6310` for `6315`, but only a human can accept the mapping.
- **Warnings are first-class.** Balanced-but-suspicious entries (FX basis, `TBD` cash-flow categories) proceed to review with explicit warnings rather than being silently accepted or over-blocked. Normal-balance direction is kept as reconciliation evidence because it legitimately flips on reclasses and DTA/DTL movements.
- **Evidence over prose.** Each report carries source hashes, the normalization log, all rule results with details, reconciliation checks and an audit trail. The plain-English reason is derived from those facts, not written independently of them.
- **No posting, ever.** The hand-off file is explicitly labelled "accepted for human review only"; posting is a separate, gated system.

## Limitations / next steps

- The mapper is lexical; a production version would use embeddings + COA metadata, with the same "must return an existing code" constraint.
- The prior-period TB is used for lineage warnings only. A full close would reconcile roll-forward continuity account by account.
- Decimal amounts are exact; FX translation and statement-level rounding policy belong to downstream agents.
- Human approval is represented as a status, not yet a persisted reviewer workflow (assignee, SLA, approval signature).
