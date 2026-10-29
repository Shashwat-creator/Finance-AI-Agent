"""Token usage tracking and structured per-agent LLM logs."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import LLM_TELEMETRY_ENABLED, LLM_USAGE_LOG

logger = logging.getLogger("manual_adjustments.llm_telemetry")

_LOCK = threading.Lock()
_RECORDS: list[dict[str, Any]] = []
_MAX_IN_MEMORY_RECORDS = 5000
USAGE_LOG_PATH: Path = LLM_USAGE_LOG


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_token_usage(response: Any) -> dict[str, int | None]:
    """Extract prompt/completion/total tokens from common SDK response shapes."""
    prompt = completion = total = None

    if isinstance(response, dict):
        usage = response.get("usage") or response.get("token_usage")
        metadata = response.get("usage_metadata")
    else:
        usage = getattr(response, "usage", None)
        metadata = getattr(response, "usage_metadata", None)
        if metadata is None:
            response_metadata = getattr(response, "response_metadata", None) or {}
            if isinstance(response_metadata, dict):
                metadata = response_metadata.get("token_usage") or response_metadata.get(
                    "usage"
                )

    if isinstance(metadata, dict):
        prompt = _as_int(
            metadata.get("input_tokens")
            or metadata.get("prompt_tokens")
            or metadata.get("input_token_count")
        )
        completion = _as_int(
            metadata.get("output_tokens")
            or metadata.get("completion_tokens")
            or metadata.get("output_token_count")
        )
        total = _as_int(metadata.get("total_tokens") or metadata.get("total_token_count"))

    if isinstance(usage, dict) and prompt is None and completion is None:
        prompt = _as_int(usage.get("prompt_tokens") or usage.get("input_tokens"))
        completion = _as_int(usage.get("completion_tokens") or usage.get("output_tokens"))
        total = _as_int(usage.get("total_tokens"))
    elif usage is not None and prompt is None and completion is None:
        prompt = _as_int(getattr(usage, "prompt_tokens", None))
        completion = _as_int(getattr(usage, "completion_tokens", None))
        total = _as_int(getattr(usage, "total_tokens", None))

    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)

    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


_SECRET_IN_TEXT = re.compile(
    r"(?i)(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,}|"
    r"(?:api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*\S+)"
)


def _safe_error(error: Any) -> str | None:
    if not error:
        return None
    text = _SECRET_IN_TEXT.sub("[REDACTED]", str(error).replace("\n", " ").strip())
    if len(text) > 240:
        text = text[:237] + "..."
    return text


def record_llm_usage(
    agent_name: str,
    model_name: str,
    response: Any = None,
    duration_ms: int | None = None,
    status: str = "success",
    attempt: int = 1,
    request_id: str = "",
    run_id: str = "",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    error: Any = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one model invocation as a structured log and JSONL event."""
    usage = extract_token_usage(response) if response is not None else {}
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "llm_usage",
        "agent": str(agent_name or "unknown"),
        "model": str(model_name or "unknown"),
        "status": str(status),
        "attempt": int(attempt),
        "duration_ms": _as_int(duration_ms),
        "prompt_tokens": _as_int(prompt_tokens) if prompt_tokens is not None else usage.get("prompt_tokens"),
        "completion_tokens": _as_int(completion_tokens) if completion_tokens is not None else usage.get("completion_tokens"),
        "total_tokens": _as_int(total_tokens) if total_tokens is not None else usage.get("total_tokens"),
        "request_id": str(request_id or ""),
        "run_id": str(run_id or ""),
    }
    safe_error = _safe_error(error)
    if safe_error:
        record["error"] = safe_error
    if metadata:
        allowed = {
            key: value
            for key, value in metadata.items()
            if key
            in {
                "input_chars",
                "redaction_count",
                "injection_detected",
                "truncated",
                "privacy_provider",
                "latency_ms",
            }
        }
        record.update(allowed)

    with _LOCK:
        _RECORDS.append(record)
        if len(_RECORDS) > _MAX_IN_MEMORY_RECORDS:
            del _RECORDS[: len(_RECORDS) - _MAX_IN_MEMORY_RECORDS]
        if LLM_TELEMETRY_ENABLED:
            try:
                USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(USAGE_LOG_PATH, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception as exc:
                logger.warning("Could not write LLM telemetry: %s", type(exc).__name__)

    logger.info("llm_usage %s", json.dumps(record, ensure_ascii=False))
    return record


def get_usage_records() -> list[dict[str, Any]]:
    with _LOCK:
        return [dict(record) for record in _RECORDS]


def reset_usage_records() -> None:
    with _LOCK:
        _RECORDS.clear()


def usage_summary() -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for record in get_usage_records():
        key = f"{record.get('agent')}:{record.get('model')}"
        entry = summary.setdefault(
            key,
            {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )
        entry["calls"] += 1
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            entry[field] += int(record.get(field) or 0)
    return summary
