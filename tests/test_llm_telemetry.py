"""Token usage extraction and structured telemetry tests."""

import json
from types import SimpleNamespace

from app import llm_telemetry


class _Response:
    def __init__(self) -> None:
        self.content = "ACCRUAL"
        self.usage_metadata = {
            "input_tokens": 120,
            "output_tokens": 7,
            "total_tokens": 127,
        }


def test_extract_token_usage_from_langchain_metadata():
    usage = llm_telemetry.extract_token_usage(_Response())
    assert usage == {
        "prompt_tokens": 120,
        "completion_tokens": 7,
        "total_tokens": 127,
    }


def test_extract_token_usage_from_openai_usage_object():
    response = SimpleNamespace(
        content="ok",
        usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    )
    usage = llm_telemetry.extract_token_usage(response)
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 2
    assert usage["total_tokens"] == 12


def test_record_llm_usage_writes_structured_jsonl_without_prompt(tmp_path, monkeypatch):
    log_path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(llm_telemetry, "USAGE_LOG_PATH", log_path)
    llm_telemetry.reset_usage_records()

    record = llm_telemetry.record_llm_usage(
        "intent_classifier",
        "gpt-5-mini",
        _Response(),
        duration_ms=42,
        status="success",
        attempt=1,
        request_id="JE-001",
        run_id="RUN-1",
        metadata={
            "input_chars": 88,
            "redaction_count": 2,
            "injection_detected": False,
            "privacy_provider": "azure+local",
            "unexpected_field": "must-not-leak",
        },
    )

    assert record["prompt_tokens"] == 120
    assert record["completion_tokens"] == 7
    assert record["total_tokens"] == 127
    assert record["agent"] == "intent_classifier"
    assert record["model"] == "gpt-5-mini"
    assert record["redaction_count"] == 2
    assert "unexpected_field" not in record
    assert "prompt" not in record

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    persisted = json.loads(lines[-1])
    assert persisted["request_id"] == "JE-001"
    assert persisted["run_id"] == "RUN-1"
    assert "jane@example.com" not in lines[-1]


def test_telemetry_redacts_secrets_in_error_text(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_telemetry, "USAGE_LOG_PATH", tmp_path / "usage.jsonl")
    llm_telemetry.reset_usage_records()
    record = llm_telemetry.record_llm_usage(
        "intent_classifier",
        "gpt-5-mini",
        status="error",
        error="provider rejected sk-super-secret-token-123456789",
    )
    assert "sk-super-secret" not in record["error"]
    assert "[REDACTED]" in record["error"]


def test_usage_summary_groups_by_agent_and_model(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_telemetry, "USAGE_LOG_PATH", tmp_path / "usage.jsonl")
    llm_telemetry.reset_usage_records()

    llm_telemetry.record_llm_usage(
        "intent_classifier", "gpt-5-mini", _Response(), status="success"
    )
    llm_telemetry.record_llm_usage(
        "intent_evaluator", "gpt-5.1", _Response(), status="success"
    )

    summary = llm_telemetry.usage_summary()
    assert summary["intent_classifier:gpt-5-mini"]["calls"] == 1
    assert summary["intent_evaluator:gpt-5.1"]["total_tokens"] == 127
