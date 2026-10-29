"""
Audit event recording and lineage tracking for the Manual Adjustments Agent.

Provides:
- Structured audit event creation and state-audit helper functions
- ``sanitize_payload`` to redact secrets before anything is logged or serialized
- ``AuditTrail`` for an ordered, JSON-serializable event history

Never logs API keys, secrets, or sensitive credentials.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional, Sequence

from app.schemas import AuditEvent

logger = logging.getLogger("manual_adjustments.audit")

# ── Secret redaction ─────────────────────────────────────────────────────────

_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "auth_token",
    "access_token",
    "secret",
    "password",
    "openai_api_key",
)

_SK_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]+")


def _is_sensitive_key(key: str) -> bool:
    lowered = key.strip().lower()
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def sanitize_payload(payload: Any) -> Any:
    """
    Recursively redact secrets and make a payload JSON-serializable.

    - Sensitive dictionary keys are replaced with ``[REDACTED]``
    - ``sk-...`` tokens embedded in strings become ``[REDACTED_KEY]``
    - ``Decimal`` values become strings (never floats)
    """
    if isinstance(payload, str):
        return _SK_PATTERN.sub("[REDACTED_KEY]", payload)
    if isinstance(payload, dict):
        return {
            key: ("[REDACTED]" if _is_sensitive_key(str(key)) else sanitize_payload(value))
            for key, value in payload.items()
        }
    if isinstance(payload, (list, tuple, set)):
        return [sanitize_payload(item) for item in payload]
    if isinstance(payload, Decimal):
        return str(payload)
    if isinstance(payload, (int, float, bool)) or payload is None:
        return payload
    return str(payload)


# ── Audit event helpers (used by LangGraph-style nodes) ─────────────────────


def create_audit_event(
    node: str,
    event_type: str,
    request_id: str = "",
    run_id: str = "",
    details: Optional[dict[str, Any]] = None,
) -> AuditEvent:
    """Create a structured AuditEvent with secrets redacted."""
    now_iso = datetime.now(timezone.utc).isoformat()
    return AuditEvent(
        timestamp=now_iso,
        request_id=request_id or "",
        run_id=run_id or "",
        node=node,
        event_type=event_type,
        details=sanitize_payload(details or {}),
    )


def append_audit_event(
    state: dict,
    node: str,
    event_type: str,
    details: Optional[dict[str, Any]] = None,
) -> list[AuditEvent]:
    """Create a new audit event and return the updated event list from state."""
    request_id = state.get("request_id") or ""
    run_id = state.get("run_id") or ""
    event = create_audit_event(
        node=node,
        event_type=event_type,
        request_id=request_id,
        run_id=run_id,
        details=details,
    )
    existing_events = list(state.get("audit_events") or [])
    logger.debug("Audit event [%s:%s] for request=%s", node, event_type, request_id)
    return existing_events + [event]


# ── AuditTrail ───────────────────────────────────────────────────────────────


class AuditTrail:
    """An ordered, redacting, JSON-serializable audit trail for one request."""

    def __init__(self, request_id: str = "", run_id: str = "") -> None:
        self.request_id = request_id or ""
        self.run_id = run_id or ""
        self._events: list[AuditEvent] = []

    def record(
        self,
        node: str,
        event_type: str,
        details: Optional[dict[str, Any]] = None,
    ) -> AuditEvent:
        """Record one event and return it. Secrets are redacted automatically."""
        event = create_audit_event(
            node=node,
            event_type=event_type,
            request_id=self.request_id,
            run_id=self.run_id,
            details=details,
        )
        self._events.append(event)
        return event

    def get_events(self) -> list[AuditEvent]:
        """Return a copy of the recorded events in order."""
        return list(self._events)

    def to_json(self) -> str:
        """Serialize the trail to a JSON array string (Decimals as strings)."""
        return json.dumps(
            [event.model_dump(mode="json") for event in self._events],
            ensure_ascii=False,
            indent=2,
        )

    @classmethod
    def from_events(
        cls,
        events: Sequence[AuditEvent | dict[str, Any]],
        request_id: str = "",
        run_id: str = "",
    ) -> "AuditTrail":
        """Restore a trail from already-created events."""
        trail = cls(request_id=request_id, run_id=run_id)
        for event in events:
            if isinstance(event, AuditEvent):
                trail._events.append(event)
            elif isinstance(event, dict):
                trail._events.append(AuditEvent(**event))
            else:
                raise TypeError(f"Unsupported audit event type: {type(event)!r}")
        return trail

    def __len__(self) -> int:
        return len(self._events)
