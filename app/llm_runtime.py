"""Central LLM invocation boundary: privacy, guardrails, rate limits, retries,
and token telemetry all run here before an agent sees a model response."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.config import (
    LLM_MAX_ATTEMPTS,
    LLM_TEMPERATURE,
    OPENAI_API_KEY,
    is_mock_mode,
)
from app.guardrails import GuardedText, HARDENED_SYSTEM_SUFFIX, prepare_untrusted_text
from app.integrations import RedisRateLimiter
from app.llm_telemetry import record_llm_usage

logger = logging.getLogger("manual_adjustments.llm_runtime")

_rate_limiter: RedisRateLimiter | None = None
_rate_lock = threading.Lock()


class LLMUnavailable(RuntimeError):
    pass


class LLMResponseError(RuntimeError):
    pass


@dataclass
class LLMInvocation:
    value: Any
    content: str
    attempts: int
    agent: str
    model: str
    guarded_fields: dict[str, GuardedText]

    @property
    def redaction_count(self) -> int:
        return sum(field.redaction_count for field in self.guarded_fields.values())

    @property
    def injection_detected(self) -> bool:
        return any(field.injection_detected for field in self.guarded_fields.values())

    @property
    def privacy_provider(self) -> str:
        providers = {field.privacy.provider for field in self.guarded_fields.values()}
        return "+".join(sorted(providers)) if providers else "local"


def _get_rate_limiter() -> RedisRateLimiter:
    global _rate_limiter
    with _rate_lock:
        if _rate_limiter is None:
            _rate_limiter = RedisRateLimiter()
        return _rate_limiter


def reset_rate_limiter_for_tests() -> None:
    global _rate_limiter
    with _rate_lock:
        _rate_limiter = None


def default_chat_invoker(
    model_name: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
) -> Any:
    if is_mock_mode():
        raise LLMUnavailable("LLM calls are disabled in mock/offline mode")
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model=model_name,
        api_key=OPENAI_API_KEY,
        temperature=temperature,
    )
    return llm.invoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
    )


def _response_content(response: Any) -> str:
    if isinstance(response, str):
        return response
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(getattr(item, "text", item)))
        return "".join(parts)
    return str(content)


def invoke_llm(
    *,
    agent_name: str,
    model_name: str,
    system_prompt: str,
    untrusted_fields: Mapping[str, Any],
    parse_fn: Callable[[str], Any],
    safe_fields: Mapping[str, Any] | None = None,
    request_id: str = "",
    run_id: str = "",
    max_attempts: int | None = None,
    temperature: float | None = None,
    invoker: Callable[[str, str, str, float], Any] | None = None,
    rate_limiter: RedisRateLimiter | None = None,
    max_chars: int | None = None,
    language: str = "en",
    use_azure_privacy: bool | None = None,
    azure_client: Any = None,
) -> LLMInvocation:
    """Invoke a model after privacy and injection guardrails are applied."""
    guarded = {
        field_name: prepare_untrusted_text(
            value,
            max_chars=max_chars,
            language=language,
            use_azure=use_azure_privacy,
            azure_client=azure_client,
        )
        for field_name, value in untrusted_fields.items()
    }
    payload = dict(safe_fields or {})
    payload.update({field_name: field.text for field_name, field in guarded.items()})
    user_prompt = json.dumps(payload, ensure_ascii=False, default=str)
    hardened_prompt = f"{system_prompt}\n\n{HARDENED_SYSTEM_SUFFIX}"

    metadata = {
        "input_chars": len(user_prompt),
        "redaction_count": sum(field.redaction_count for field in guarded.values()),
        "injection_detected": any(field.injection_detected for field in guarded.values()),
        "truncated": any(field.truncated for field in guarded.values()),
        "privacy_provider": "+".join(
            sorted({field.privacy.provider for field in guarded.values()})
        ),
    }

    attempts = max(1, int(max_attempts if max_attempts is not None else LLM_MAX_ATTEMPTS))
    call = invoker or default_chat_invoker
    model_temperature = LLM_TEMPERATURE if temperature is None else temperature
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        limiter = rate_limiter if rate_limiter is not None else _get_rate_limiter()
        try:
            allowed = limiter.acquire(bucket=agent_name)
        except Exception as exc:
            logger.error(
                "Rate limiter unavailable for agent '%s': %s",
                agent_name,
                type(exc).__name__,
            )
            allowed = False
        if not allowed:
            record_llm_usage(
                agent_name,
                model_name,
                status="rate_limited",
                attempt=attempt,
                request_id=request_id,
                run_id=run_id,
                metadata=metadata,
            )
            raise LLMUnavailable(f"Rate limit denied for agent '{agent_name}'")

        started = time.perf_counter()
        try:
            response = call(model_name, hardened_prompt, user_prompt, model_temperature)
            content = _response_content(response)
            value = parse_fn(content)
        except LLMUnavailable as exc:
            record_llm_usage(
                agent_name,
                model_name,
                status="unavailable",
                attempt=attempt,
                request_id=request_id,
                run_id=run_id,
                error=exc,
                metadata=metadata,
            )
            raise
        except Exception as exc:
            last_error = exc
            status = "invalid_output" if isinstance(exc, (ValueError, KeyError, TypeError, json.JSONDecodeError)) else "error"
            record_llm_usage(
                agent_name,
                model_name,
                status=status,
                duration_ms=int((time.perf_counter() - started) * 1000),
                attempt=attempt,
                request_id=request_id,
                run_id=run_id,
                error=f"{type(exc).__name__}: {exc}",
                metadata=metadata,
            )
            logger.warning(
                "LLM call for agent '%s' failed on attempt %d/%d: %s",
                agent_name,
                attempt,
                attempts,
                type(exc).__name__,
            )
            continue

        record_llm_usage(
            agent_name,
            model_name,
            response,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="success",
            attempt=attempt,
            request_id=request_id,
            run_id=run_id,
            metadata=metadata,
        )
        return LLMInvocation(
            value=value,
            content=content,
            attempts=attempt,
            agent=agent_name,
            model=model_name,
            guarded_fields=guarded,
        )

    raise LLMResponseError(
        f"Agent '{agent_name}' failed after {attempts} attempt(s): "
        f"{type(last_error).__name__ if last_error else 'unknown error'}"
    ) from last_error
