"""Privacy guardrail tests: local redaction, Azure Language PII, and LLM boundary."""

from app.agents.intake_agent import classify_intent_llm
from app.privacy import reset_azure_client_for_tests, sanitize_for_llm


class _AllowLimiter:
    def acquire(self, bucket: str = "llm") -> bool:
        return True


class _FakeAzure:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    def redact_text(self, text: str, language: str = "en"):
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("azure unavailable")
        return text.replace("Jane Doe", "[REDACTED_PERSON]"), {"PERSON": 1}


def test_local_redaction_removes_sensitive_values():
    raw = (
        "Contact Jane.Doe@example.com or +1 415 555 0199. SSN 123-45-6789, "
        "IBAN GB82WEST12345698765432, card 4111 1111 1111 1111, "
        "api_key=sk-super-secret-token-123456789."
    )
    result = sanitize_for_llm(raw, use_azure=False)

    assert result.provider == "local"
    assert "Jane.Doe@example.com" not in result.text
    assert "123-45-6789" not in result.text
    assert "GB82WEST12345698765432" not in result.text
    assert "4111 1111 1111 1111" not in result.text
    assert "sk-super-secret-token-123456789" not in result.text
    assert "[REDACTED_EMAIL]" in result.text
    assert "[REDACTED_SECRET]" in result.text
    assert result.redaction_count >= 5


def test_azure_language_is_layered_after_local_redaction():
    azure = _FakeAzure()
    result = sanitize_for_llm(
        "Jane Doe emailed jane@example.com about the accrual.",
        use_azure=True,
        azure_client=azure,
    )

    assert azure.calls
    assert "jane@example.com" not in azure.calls[0]
    assert "Jane Doe" in azure.calls[0]
    assert "Jane Doe" not in result.text
    assert "jane@example.com" not in result.text
    assert result.provider == "azure+local"
    assert result.redactions.get("PERSON") == 1


def test_azure_failure_falls_back_to_local_redaction():
    result = sanitize_for_llm(
        "Email hidden@example.com and keep this text.",
        use_azure=True,
        azure_client=_FakeAzure(fail=True),
    )

    assert result.provider == "local"
    assert "hidden@example.com" not in result.text
    assert "[REDACTED_EMAIL]" in result.text


def test_privacy_runs_before_data_reaches_llm():
    captured = {}

    def invoker(model, system_prompt, user_prompt, temperature):
        captured["prompt"] = user_prompt
        return "ACCRUAL"

    intent = classify_intent_llm(
        "Accrue bonus for Jane Doe (jane.doe@example.com, +1 415 555 0199).",
        invoker=invoker,
        rate_limiter=_AllowLimiter(),
        use_azure_privacy=True,
        azure_client=_FakeAzure(),
    )

    assert intent == "ACCRUAL"
    prompt = captured["prompt"]
    assert "jane.doe@example.com" not in prompt
    assert "Jane Doe" not in prompt
    assert "+1 415 555 0199" not in prompt
    assert "[REDACTED_EMAIL]" in prompt
    assert "[REDACTED_PERSON]" in prompt


def test_reset_azure_client_does_not_break_local_fallback():
    reset_azure_client_for_tests()
    result = sanitize_for_llm("safe text", use_azure=False)
    assert result.text == "safe text"
