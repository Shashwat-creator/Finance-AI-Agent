"""
Account mapping agent.

The mapper is intentionally the smallest possible "AI" surface: given an
account code that does not exist in the Chart of Accounts, it proposes ranked
COA candidates with a confidence score and evidence. The deterministic scoring
below is the offline fallback; an LLM can be layered on top but must only ever
return codes that already exist in the COA.

For the supplied data this is what resolves (or escalates) JE-005's orphan
account ``6315``.
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Iterable, Sequence

from app.schemas import AccountCandidate, ChartOfAccount

logger = logging.getLogger("manual_adjustments.agents.mapper")

_STOPWORDS = {
    "the",
    "and",
    "for",
    "from",
    "into",
    "out",
    "of",
    "to",
    "a",
    "an",
    "old",
    "code",
    "reclass",
    "reclassify",
    "entry",
    "adjustment",
}


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if token not in _STOPWORDS and not token.isdigit()
    }


def _common_prefix_len(a: str, b: str) -> int:
    count = 0
    for left, right in zip(a, b):
        if left != right:
            break
        count += 1
    return count


def suggest_account_candidates(
    unknown_code: str,
    hint_text: str,
    coa_records: Sequence[ChartOfAccount],
    top_k: int = 3,
    min_score: float = 0.25,
) -> list[AccountCandidate]:
    """
    Rank postable COA accounts as possible homes for an unknown account code.

    Scoring is a transparent blend of:
      - shared name tokens between the COA account and the JE description/memos
      - fuzzy name similarity
      - shared numeric code prefix
    """
    hint = f"{unknown_code} {hint_text or ''}".strip()
    hint_tokens = _tokens(hint)
    code = str(unknown_code or "").strip()

    scored: list[tuple[float, ChartOfAccount, str]] = []
    for account in coa_records:
        if not getattr(account, "is_postable", True):
            continue
        name_tokens = _tokens(account.account_name)
        overlap = hint_tokens & name_tokens

        token_score = (len(overlap) / len(hint_tokens)) if hint_tokens else 0.0
        name_score = SequenceMatcher(
            None, hint.lower(), account.account_name.lower()
        ).ratio()
        prefix_score = 0.0
        if code and account.account_code:
            common = _common_prefix_len(code, account.account_code)
            prefix_score = common / max(len(code), len(account.account_code))

        score = max(token_score, name_score * 0.9, prefix_score * 0.6)
        if token_score > 0 and prefix_score >= 0.75:
            score = min(1.0, score + 0.1)

        if score < min_score:
            continue

        evidence: list[str] = []
        if overlap:
            evidence.append(f"shared terms: {', '.join(sorted(overlap))}")
        if prefix_score >= 0.75:
            evidence.append(f"account-code prefix matches '{code[:3]}'")
        if not evidence:
            evidence.append("lexical name similarity")
        scored.append((round(min(score, 1.0), 4), account, "; ".join(evidence)))

    scored.sort(key=lambda item: (-item[0], item[1].account_code))

    candidates = [
        AccountCandidate(
            account_code=account.account_code,
            account_name=account.account_name,
            confidence=score,
            evidence=evidence,
            rank=index,
        )
        for index, (score, account, evidence) in enumerate(scored[:top_k], start=1)
    ]

    if candidates:
        logger.info(
            "Mapped unknown account '%s' to %d candidate(s); top=%s (%.2f)",
            unknown_code,
            len(candidates),
            candidates[0].account_code,
            candidates[0].confidence,
        )
    else:
        logger.warning("No COA candidates found for unknown account '%s'", unknown_code)

    return candidates


def map_unknown_accounts(
    accounts: Iterable[str],
    hint_text: str,
    coa_records: Sequence[ChartOfAccount],
    top_k: int = 3,
) -> list[AccountCandidate]:
    """Return one ranked candidate list for each distinct unknown account."""
    seen: set[str] = set()
    results: list[AccountCandidate] = []
    for code in accounts:
        code = str(code or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        results.extend(
            suggest_account_candidates(code, hint_text, coa_records, top_k=top_k)
        )
    return results
