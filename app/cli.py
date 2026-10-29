"""
Command-line entry point for the Manual Adjustments Agent.

Examples
--------
    python -m app.cli --offline
    python -m app.cli --inputs ../inputs --outputs outputs
    python main.py --offline
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from app.config import DATA_DIR, EXPECTED_PERIOD, OUTPUT_DIR
from app.workflow import run_batch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manual-adjustments-agent",
        description=(
            "Validate a batch of manual journal entries against a Chart of Accounts, "
            "classify each entry, and emit auditable decision reports. Nothing is posted."
        ),
    )
    parser.add_argument(
        "--inputs",
        default=str(DATA_DIR),
        help=f"Directory containing the input files (default: {DATA_DIR})",
    )
    parser.add_argument(
        "--outputs",
        default=str(OUTPUT_DIR),
        help=f"Directory for generated reports (default: {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--period",
        default=EXPECTED_PERIOD,
        help=f"Accounting period to validate (default: {EXPECTED_PERIOD})",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Force deterministic mode (never call an LLM).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the batch summary as JSON instead of a table.",
    )
    return parser


def _print_summary(result: dict) -> None:
    summary = result["summary"]
    counts = summary["counts"]
    print("")
    print(f"Run ID : {summary['run_id']}")
    print(f"Mode   : {'offline / deterministic' if summary['offline'] else 'LLM-assisted'}")
    print(f"Period : {summary['period']}   Functional currency: {summary['functional_currency']}")
    print(f"Output : {result['output_dir']}")
    print("")
    print(f"{'Entry':<10}{'Intent':<18}{'Status':<24}{'Debit':>16}{'Credit':>16}{'Diff':>14}")
    print("-" * 98)
    for report in result["reports"]:
        entry = report.proposed_entry
        debit = f"{entry.total_debit:,.2f}" if entry else "-"
        credit = f"{entry.total_credit:,.2f}" if entry else "-"
        diff = f"{entry.difference:,.2f}" if entry else "-"
        print(
            f"{report.request_id:<10}{report.intent or '-':<18}"
            f"{report.status.value:<24}{debit:>16}{credit:>16}{diff:>14}"
        )
    print("-" * 98)
    print(
        f"Total: {counts['total']} | ready: {counts['ready_for_review']} | "
        f"clarification: {counts['needs_clarification']} | "
        f"rejected: {counts['rejected_validation']} | failed-safe: {counts['failed_safe']}"
    )
    if summary["input_warnings"]:
        print("")
        print("Input data-quality warnings:")
        for warning in summary["input_warnings"]:
            print(f"  - {warning}")
    print("")
    print("Nothing has been posted. Open the per-entry Markdown reports for details.")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.offline:
        os.environ["OFFLINE"] = "1"

    try:
        result = run_batch(
            inputs_dir=Path(args.inputs),
            output_dir=Path(args.outputs),
            period=args.period,
            offline=True if args.offline else None,
        )
    except FileNotFoundError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Validation error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
    else:
        _print_summary(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
