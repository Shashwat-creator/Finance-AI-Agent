"""
Unit tests for app/audit.py.
"""

import json
from decimal import Decimal

from app.audit import AuditTrail, sanitize_payload


class TestSanitizePayload:
    def test_redacts_api_keys_and_tokens(self):
        payload = {
            "node": "account_mapper",
            "api_key": "secret-xyz-12345",
            "auth_token": "bearer-token-abc",
            "headers": {"Authorization": "Bearer sk-testkey1234567890abcdef"},
            "safe_field": "valid_value",
            "amount": Decimal("100.50"),
        }
        clean = sanitize_payload(payload)

        assert clean["api_key"] == "[REDACTED]"
        assert clean["auth_token"] == "[REDACTED]"
        assert clean["headers"]["Authorization"] == "[REDACTED]"
        assert clean["safe_field"] == "valid_value"
        assert clean["amount"] == "100.50"

    def test_redacts_sk_tokens_in_string(self):
        text = "Calling OpenAI with sk-1234567890abcdefghijklmnopqrstuvwxyz model"
        sanitized = sanitize_payload(text)
        assert "sk-1234567890" not in sanitized
        assert "[REDACTED_KEY]" in sanitized


class TestAuditTrail:
    def test_record_and_get_events(self):
        trail = AuditTrail(request_id="JE-001", run_id="RUN-2024-001")
        assert len(trail) == 0

        ev1 = trail.record("intake_agent", "node_start", {"action": "parse"})
        assert ev1.node == "intake_agent"
        assert ev1.event_type == "node_start"
        assert ev1.request_id == "JE-001"
        assert ev1.run_id == "RUN-2024-001"
        assert ev1.details["action"] == "parse"

        trail.record("validator", "validation", {"rule": "JE-001", "status": "PASS"})
        assert len(trail) == 2

        events = trail.get_events()
        assert len(events) == 2
        assert events[0].node == "intake_agent"
        assert events[1].node == "validator"

    def test_auto_sanitizes_secrets_on_record(self):
        trail = AuditTrail(request_id="JE-002", run_id="RUN-2024-002")
        trail.record(
            "llm_agent",
            "transformation",
            {"prompt": "draft entry", "openai_api_key": "sk-secret1234567890abc"},
        )
        events = trail.get_events()
        assert events[0].details["openai_api_key"] == "[REDACTED]"

    def test_to_json_serialization(self):
        trail = AuditTrail(request_id="JE-003", run_id="RUN-2024-003")
        trail.record(
            "reconciler",
            "validation",
            {"total_debit": Decimal("5000.25"), "is_balanced": True},
        )
        json_str = trail.to_json()
        assert isinstance(json_str, str)
        parsed = json.loads(json_str)
        assert len(parsed) == 1
        assert parsed[0]["details"]["total_debit"] == "5000.25"
        assert parsed[0]["details"]["is_balanced"] is True

    def test_from_events_restoration(self):
        trail1 = AuditTrail(request_id="JE-004", run_id="RUN-2024-004")
        trail1.record("node1", "node_start")
        trail1.record("node2", "node_end")

        events = trail1.get_events()
        trail2 = AuditTrail.from_events(events, request_id="JE-004", run_id="RUN-2024-004")
        assert len(trail2) == 2
        assert trail2.get_events()[0].node == "node1"
