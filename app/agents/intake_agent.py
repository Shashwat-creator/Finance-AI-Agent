"""
Intake Agent implementation.

The model boundary is centralized in app.llm_runtime so that privacy
redaction, prompt-injection guardrails, rate limiting, retries, and token
telemetry cannot be bypassed by an individual agent.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.audit import append_audit_event
from app.config import (
    EVALUATOR_MODEL_NAME,
    INTENT_MODEL_NAME,
    LLM_EVALUATOR_PROMPT,
    LLM_SYSTEM_PROMPT,
)
from app.llm_runtime import (
    LLMResponseError,
    LLMUnavailable,
    invoke_llm,
)
from app.schemas import AdjustmentIntent, WorkflowStatus

logger = logging.getLogger("manual_adjustments.agents.intake")

NODE_NAME = "intake_agent"
_ALLOWED_INTENTS = {intent.value for intent in AdjustmentIntent}


def classify_intent_deterministic(description: str) -> str:
    """
    Classify the adjustment intent from description using deterministic keyword matching.

    Rules:
      - 'tax' or 'deferred tax' -> TAX_ADJUSTMENT
      - 'provision' or 'bad debt' or 'allowance' -> PROVISION
      - 'reval' or 'FX' or 'translation' -> REVALUATION
      - 'deprec' or 'amortiz' -> DEPRECIATION
      - 'intercompany' or 'IC ' -> INTERCOMPANY
      - 'reclass' -> RECLASSIFICATION
      - 'accru' -> ACCRUAL
      - else -> OTHER
    """
    desc = (description or "").lower()

    if "deferred tax" in desc or "tax" in desc:
        return AdjustmentIntent.TAX_ADJUSTMENT.value
    if "provision" in desc or "bad debt" in desc or "allowance" in desc:
        return AdjustmentIntent.PROVISION.value
    if "reval" in desc or "fx" in desc or "translation" in desc:
        return AdjustmentIntent.REVALUATION.value
    if "deprec" in desc or "amortiz" in desc:
        return AdjustmentIntent.DEPRECIATION.value
    if "intercompany" in desc or "ic " in desc:
        return AdjustmentIntent.INTERCOMPANY.value
    if "reclass" in desc:
        return AdjustmentIntent.RECLASSIFICATION.value
    if "accru" in desc:
        return AdjustmentIntent.ACCRUAL.value
    return AdjustmentIntent.OTHER.value


def _extract_json_object(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    if start < 0:
        raise ValueError("Response does not contain a JSON object")
    data, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(data, dict):
        raise ValueError("Response JSON is not an object")
    return data


def _parse_intent_response(content: str) -> str:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
    if text.startswith("{"):
        try:
            data = _extract_json_object(text)
            text = str(data.get("intent") or data.get("category") or "").strip()
        except (ValueError, json.JSONDecodeError):
            pass
    candidate = text.strip().strip('"').strip("'").upper()
    if not candidate:
        raise ValueError("Empty intent response")
    if candidate not in _ALLOWED_INTENTS:
        raise ValueError(f"Intent '{candidate}' is not an allowed category")
    return candidate


def _parse_evaluator_response(content: str) -> dict[str, Any]:
    data = _extract_json_object(content)
    supported = data.get("supported")
    hallucination = data.get("hallucination")
    if not isinstance(supported, bool) or not isinstance(hallucination, bool):
        raise ValueError("Evaluator response must contain boolean supported/hallucination")
    reason = data.get("reason", "")
    if reason is None:
        reason = ""
    if not isinstance(reason, str):
        reason = str(reason)
    return {
        "supported": supported,
        "hallucination": hallucination,
        "reason": reason[:500],
    }


def classify_intent_llm(
    description: str,
    *,
    invoker: Any = None,
    rate_limiter: Any = None,
    request_id: str = "",
    run_id: str = "",
    use_azure_privacy: bool | None = None,
    azure_client: Any = None,
) -> str:
    """Classify intent with the configured intent model, falling back safely."""
    fallback = classify_intent_deterministic(description)
    system_prompt = (
        f"{LLM_SYSTEM_PROMPT} Allowed categories: "
        f"{', '.join(sorted(_ALLOWED_INTENTS))}. Reply with only one category."
    )
    try:
        invocation = invoke_llm(
            agent_name="intent_classifier",
            model_name=INTENT_MODEL_NAME,
            system_prompt=system_prompt,
            untrusted_fields={"description": description},
            parse_fn=_parse_intent_response,
            request_id=request_id,
            run_id=run_id,
            invoker=invoker,
            rate_limiter=rate_limiter,
            use_azure_privacy=use_azure_privacy,
            azure_client=azure_client,
        )
    except LLMUnavailable as exc:
        logger.info("LLM intent classifier unavailable; using deterministic fallback: %s", exc)
        return fallback
    except LLMResponseError as exc:
        logger.warning("LLM intent classifier failed; using deterministic fallback: %s", exc)
        return fallback
    except Exception as exc:
        logger.exception("Unexpected LLM intent failure: %s", type(exc).__name__)
        return fallback
    return str(invocation.value)


def evaluate_intent_candidate(
    description: str,
    candidate: str,
    *,
    invoker: Any = None,
    rate_limiter: Any = None,
    request_id: str = "",
    run_id: str = "",
    use_azure_privacy: bool | None = None,
    azure_client: Any = None,
) -> dict[str, Any]:
    """Run the advisory evaluator/hallucination check without changing status."""
    candidate_value = str(candidate or "").upper()
    deterministic = classify_intent_deterministic(description)
    result: dict[str, Any] = {
        "supported": None,
        "hallucination": None,
        "reason": "Evaluator unavailable",
        "agreement_with_keyword": candidate_value == deterministic,
        "model": EVALUATOR_MODEL_NAME,
        "attempts": 0,
        "evaluator_available": False,
    }

    if candidate_value not in _ALLOWED_INTENTS:
        result.update(
            {
                "supported": False,
                "hallucination": True,
                "reason": "Candidate is not in the allowed intent set",
                "hallucination_source": "deterministic",
            }
        )
        return result

    try:
        invocation = invoke_llm(
            agent_name="intent_evaluator",
            model_name=EVALUATOR_MODEL_NAME,
            system_prompt=LLM_EVALUATOR_PROMPT,
            untrusted_fields={"description": description},
            safe_fields={"candidate": candidate_value},
            parse_fn=_parse_evaluator_response,
            request_id=request_id,
            run_id=run_id,
            invoker=invoker,
            rate_limiter=rate_limiter,
            use_azure_privacy=use_azure_privacy,
            azure_client=azure_client,
        )
    except LLMUnavailable as exc:
        result["reason"] = f"Evaluator unavailable: {exc}"
        return result
    except LLMResponseError as exc:
        result["reason"] = f"Evaluator response invalid after retry: {exc}"
        return result
    except Exception as exc:
        logger.exception("Unexpected evaluator failure: %s", type(exc).__name__)
        result["reason"] = f"Evaluator error: {type(exc).__name__}"
        return result

    parsed = dict(invocation.value)
    if parsed["supported"] and parsed["hallucination"]:
        parsed["supported"] = False
        parsed["reason"] = (
            parsed["reason"] + " (inconsistent evaluator response normalized)"
        ).strip()
    result.update(parsed)
    result.update(
        {
            "evaluator_available": True,
            "attempts": invocation.attempts,
            "redaction_count": invocation.redaction_count,
            "injection_detected": invocation.injection_detected,
            "privacy_provider": invocation.privacy_provider,
            "hallucination_source": "evaluator",
        }
    )
    return result


def run_intake_agent(state: dict[str, Any]) -> dict[str, Any]:
    """
    Execute the Intake Agent.

    Args:
        state: Shared workflow state dictionary.

    Returns:
        Partial state dictionary with intent, warnings, and audit events.
    """
    try:
        events = append_audit_event(state, NODE_NAME, "node_start")

        raw_req = state.get("adjustment_request") or {}
        parsed_req = state.get("parsed_request")

        if parsed_req is not None:
            req_id = getattr(parsed_req, "id", "")
            description = getattr(parsed_req, "description", "")
            date_val = getattr(parsed_req, "date", "")
            period_val = getattr(parsed_req, "period", "")
            currency_val = getattr(parsed_req, "currency", "")
            lines = getattr(parsed_req, "lines", [])
        else:
            req_id = raw_req.get("id", "")
            description = raw_req.get("description", "")
            date_val = raw_req.get("date", "")
            period_val = raw_req.get("period", "")
            currency_val = raw_req.get("currency", "")
            lines = raw_req.get("lines", [])

        warnings = list(state.get("warnings") or [])
        if not req_id:
            warnings.append("Missing required field: id")
        if not description:
            warnings.append("Missing required field: description")
        if not date_val:
            warnings.append("Missing required field: date")
        if not period_val:
            warnings.append("Missing required field: period")
        if not currency_val:
            warnings.append("Missing required field: currency")
        if not lines:
            warnings.append("Missing required field: lines (no entry lines provided)")

        request_id = str(req_id or state.get("request_id") or "")
        run_id = str(state.get("run_id") or "")
        intent = classify_intent_llm(
            description, request_id=request_id, run_id=run_id
        )
        evaluation = evaluate_intent_candidate(
            description, intent, request_id=request_id, run_id=run_id
        )
        if evaluation.get("hallucination") is True or evaluation.get("supported") is False:
            warnings.append(
                "LLM evaluator flagged the intent as potentially unsupported; "
                "deterministic controls remain authoritative."
            )
        logger.info("Classified intent for request '%s' as '%s'", req_id, intent)

        state_for_audit = dict(state)
        state_for_audit["audit_events"] = events
        events = append_audit_event(
            state_for_audit,
            NODE_NAME,
            "node_end",
            details={
                "intent": intent,
                "warnings_count": len(warnings),
                "evaluator": {
                    "supported": evaluation.get("supported"),
                    "hallucination": evaluation.get("hallucination"),
                    "attempts": evaluation.get("attempts"),
                    "model": evaluation.get("model"),
                },
            },
        )

        return {
            "intent": intent,
            "llm_evaluation": evaluation,
            "warnings": warnings,
            "audit_events": events,
        }

    except Exception as exc:
        logger.exception("Unexpected error in intake_agent: %s", exc)
        errors = list(state.get("errors") or [])
        errors.append(f"Unexpected error in {NODE_NAME}: {exc}")
        events = append_audit_event(
            state,
            NODE_NAME,
            "node_failure",
            details={"error": str(exc)},
        )
        return {
            "status": WorkflowStatus.FAILED_SAFE,
            "errors": errors,
            "audit_events": events,
        }
