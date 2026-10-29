"""Privacy guardrails for text sent to an LLM.

Local deterministic redaction always runs first. When Azure AI Language is
configured, PII detection is layered on top so names and other entity types
that regex cannot detect are removed before any model call. Hashing is not used
for values that the model does not need.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from app.config import PRIVACY_USE_AZURE_LANGUAGE
from app.integrations import AzureLanguagePrivacyClient

logger = logging.getLogger("manual_adjustments.privacy")

_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_SECRET = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,}|"
    r"(?:api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*[^\s,;]+)"
)
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_IBAN = re.compile(r"(?i)\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_PHONE = re.compile(
    r"(?<![\w\d])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)|\d{2,4})"
    r"[\s.-]?\d{3,4}[\s.-]?\d{3,4}(?![\w\d])"
)
_LABELED_ACCOUNT = re.compile(
    r"(?i)\b((?:bank|vendor|loan|card|credit\s+card)\s+account(?:\s*(?:number|no\.?|#))?|"
    r"routing(?:\s*number)?|tax\s*id|ein|vat\s*(?:id|number|no\.?))\s*[:#]?\s*"
    r"([A-Z0-9][A-Z0-9 \-]{3,30})"
)

_LOCAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("SECRET", _SECRET),
    ("EMAIL", _EMAIL),
    ("SSN", _SSN),
    ("IBAN", _IBAN),
    ("CARD", _CARD),
    ("PHONE", _PHONE),
    ("ACCOUNT", _LABELED_ACCOUNT),
)

_azure_client: AzureLanguagePrivacyClient | None = None
_azure_lock = threading.Lock()


@dataclass(frozen=True)
class PrivacyResult:
    text: str
    redactions: dict[str, int] = field(default_factory=dict)
    provider: str = "local"
    original_length: int = 0

    @property
    def redaction_count(self) -> int:
        return sum(self.redactions.values())


def _local_redact(text: str) -> tuple[str, dict[str, int]]:
    redacted = text
    counts: dict[str, int] = {}
    for category, pattern in _LOCAL_PATTERNS:
        redacted, count = pattern.subn(f"[REDACTED_{category}]", redacted)
        if count:
            counts[category] = counts.get(category, 0) + count
    return redacted, counts


def _get_azure_client() -> AzureLanguagePrivacyClient:
    global _azure_client
    with _azure_lock:
        if _azure_client is None:
            _azure_client = AzureLanguagePrivacyClient()
        return _azure_client


def reset_azure_client_for_tests() -> None:
    global _azure_client
    with _azure_lock:
        _azure_client = None


def sanitize_for_llm(
    text: str,
    *,
    language: str = "en",
    use_azure: bool | None = None,
    azure_client: Any = None,
) -> PrivacyResult:
    """Redact sensitive values before text is sent to a model."""
    raw = "" if text is None else str(text)
    redacted, counts = _local_redact(raw)
    provider = "local"
    azure_enabled = PRIVACY_USE_AZURE_LANGUAGE if use_azure is None else use_azure

    if azure_enabled:
        client = azure_client if azure_client is not None else _get_azure_client()
        try:
            azure_result = client.redact_text(redacted, language=language)
        except Exception as exc:
            logger.warning("Azure Language privacy filter failed: %s", type(exc).__name__)
            azure_result = None
        if azure_result is not None:
            azure_text, azure_counts = azure_result
            if isinstance(azure_text, str) and azure_text:
                redacted = azure_text
                for category, count in (azure_counts or {}).items():
                    counts[category] = counts.get(category, 0) + int(count)
                provider = "azure+local"

    return PrivacyResult(
        text=redacted,
        redactions=counts,
        provider=provider,
        original_length=len(raw),
    )
