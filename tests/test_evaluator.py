"""Intent classification, evaluator schema handling, and hallucination tests."""

import pytest

from app import llm_telemetry
from app.agents.intake_agent import classify_intent_llm, evaluate_intent_candidate
from app.llm_runtime import LLMUnavailable, invoke_llm


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage_metadata = {
            "input_tokens": 40,
            "output_tokens": 5,
            "total_tokens": 45,
        }


class _AllowLimiter:
    def acquire(self, bucket: str = "llm") -> bool:
        return True


class _DenyLimiter:
    def acquire(self, bucket: str = "llm") -> bool:
        return False


def _invoker(responses):
    calls = []

    def call(model, system_prompt, user_prompt, temperature):
        calls.append(
            {
                "model": model,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
            }
        )
        value = responses.pop(0) if isinstance(responses, list) else responses
        if isinstance(value, Exception):
            raise value
        return _Response(value)

    return call, calls


@pytest.fixture(autouse=True)
def _isolate_telemetry(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_telemetry, "USAGE_LOG_PATH", tmp_path / "usage.jsonl")
    llm_telemetry.reset_usage_records()


def test_intent_classification_uses_configured_model_and_records_usage():
    call, calls = _invoker(["ACCRUAL"])
    intent = classify_intent_llm(
        "Accrue the Q4 bonus pool.",
        invoker=call,
        rate_limiter=_AllowLimiter(),
        request_id="JE-001",
        run_id="RUN-1",
    )

    assert intent == "ACCRUAL"
    assert calls[0]["model"] == "gpt-5-mini"
    assert "untrusted data" in calls[0]["system_prompt"].lower()
    records = llm_telemetry.get_usage_records()
    assert records[-1]["agent"] == "intent_classifier"
    assert records[-1]["total_tokens"] == 45


def test_invalid_intent_output_retries_once_then_uses_fallback():
    call, _ = _invoker(["NOT_A_CATEGORY", "not-a-category-either"])
    intent = classify_intent_llm(
        "Accrue the Q4 bonus pool.",
        invoker=call,
        rate_limiter=_AllowLimiter(),
    )
    assert intent == "ACCRUAL"
    records = llm_telemetry.get_usage_records()
    assert records[-1]["status"] == "invalid_output"
    assert records[-1]["attempt"] == 2


def test_invalid_intent_output_then_valid_retry_succeeds():
    call, _ = _invoker(["NOT_A_CATEGORY", "RECLASSIFICATION"])
    intent = classify_intent_llm(
        "Reclass marketing to advertising.",
        invoker=call,
        rate_limiter=_AllowLimiter(),
    )
    assert intent == "RECLASSIFICATION"
    assert llm_telemetry.get_usage_records()[-1]["attempt"] == 2


def test_evaluator_accepts_json_and_reports_agreement():
    call, calls = _invoker(
        ['{"supported": true, "hallucination": false, "reason": "description says accrue"}']
    )
    result = evaluate_intent_candidate(
        "Accrue the Q4 bonus pool.",
        "ACCRUAL",
        invoker=call,
        rate_limiter=_AllowLimiter(),
    )

    assert result["supported"] is True
    assert result["hallucination"] is False
    assert result["agreement_with_keyword"] is True
    assert result["evaluator_available"] is True
    assert result["attempts"] == 1
    assert result["model"] == "gpt-5.1"
    assert calls[0]["model"] == "gpt-5.1"


def test_evaluator_parses_fenced_json():
    call, _ = _invoker(
        ['```json\n{"supported": true, "hallucination": false, "reason": "ok"}\n```']
    )
    result = evaluate_intent_candidate(
        "Accrue the Q4 bonus pool.",
        "ACCRUAL",
        invoker=call,
        rate_limiter=_AllowLimiter(),
    )
    assert result["supported"] is True
    assert result["hallucination"] is False


def test_evaluator_invalid_schema_retries_once():
    call, _ = _invoker(
        [
            '{"supported": "yes", "hallucination": false}',
            '{"supported": true, "hallucination": false, "reason": "retry worked"}',
        ]
    )
    result = evaluate_intent_candidate(
        "Accrue the Q4 bonus pool.",
        "ACCRUAL",
        invoker=call,
        rate_limiter=_AllowLimiter(),
    )
    assert result["supported"] is True
    assert result["attempts"] == 2


def test_evaluator_invalid_twice_safe_unavailable():
    call, _ = _invoker(["not json", "still not json"])
    result = evaluate_intent_candidate(
        "Accrue the Q4 bonus pool.",
        "ACCRUAL",
        invoker=call,
        rate_limiter=_AllowLimiter(),
    )
    assert result["supported"] is None
    assert result["hallucination"] is None
    assert result["evaluator_available"] is False
    assert "invalid after retry" in result["reason"].lower()


def test_evaluator_hallucination_flag_is_preserved():
    call, _ = _invoker(
        ['{"supported": false, "hallucination": true, "reason": "candidate unsupported"}']
    )
    result = evaluate_intent_candidate(
        "Accrue the Q4 bonus pool.",
        "ACCRUAL",
        invoker=call,
        rate_limiter=_AllowLimiter(),
    )
    assert result["supported"] is False
    assert result["hallucination"] is True


def test_inconsistent_evaluator_output_is_normalized():
    call, _ = _invoker(
        ['{"supported": true, "hallucination": true, "reason": "inconsistent"}']
    )
    result = evaluate_intent_candidate(
        "Accrue the Q4 bonus pool.",
        "ACCRUAL",
        invoker=call,
        rate_limiter=_AllowLimiter(),
    )
    assert result["supported"] is False
    assert result["hallucination"] is True
    assert "normalized" in result["reason"]


def test_invalid_candidate_is_flagged_without_calling_model():
    call, calls = _invoker([])
    result = evaluate_intent_candidate(
        "Accrue the Q4 bonus pool.",
        "BOGUS",
        invoker=call,
        rate_limiter=_AllowLimiter(),
    )
    assert result["supported"] is False
    assert result["hallucination"] is True
    assert result["hallucination_source"] == "deterministic"
    assert calls == []


def test_rate_limit_denial_blocks_llm_and_falls_back():
    call, calls = _invoker(["ACCRUAL"])
    intent = classify_intent_llm(
        "Accrue the Q4 bonus pool.",
        invoker=call,
        rate_limiter=_DenyLimiter(),
    )
    assert intent == "ACCRUAL"
    assert calls == []
    assert llm_telemetry.get_usage_records()[-1]["status"] == "rate_limited"


def test_runtime_raises_unavailable_when_rate_limited():
    call, _ = _invoker(["ACCRUAL"])
    with pytest.raises(LLMUnavailable):
        invoke_llm(
            agent_name="intent_classifier",
            model_name="gpt-5-mini",
            system_prompt="classify",
            untrusted_fields={"description": "accrue"},
            parse_fn=lambda content: content,
            invoker=call,
            rate_limiter=_DenyLimiter(),
        )
