from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import requests

from .utils import canonical_json, sha256_text


DEFAULT_MODEL = "gpt-5.4-nano"
INPUT_SCHEMA_VERSION = "cross_market_llm_input_v1"
SYNTHESIS_SCHEMA_VERSION = "cross_market_llm_synthesis_v1"
OUTPUT_SCHEMA_VERSION = "cross_market_llm_output_v1"
PROMPT_VERSION = "cross_market_synthesis_prompt_v1"
MAX_INPUT_BYTES = 400_000
MAX_OUTPUT_TOKENS = 900
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

SYSTEM_INSTRUCTIONS = """You synthesize a completed-session cross-market brief from one frozen JSON evidence contract.
Use only the supplied evidence records. Do not use outside knowledge, web search, tools, memory, or unstated facts.
Every paragraph, theme, and uncertainty note must cite the exact evidence IDs that support it.
Do not introduce a number, ticker, index, currency pair, or economic result unless it appears in the cited records.
Do not recommend trades or alter market regime, exposure, candidate ranking, portfolio actions, sell rules, or audit priority.
Write one or two concise professional paragraphs, then optional compact themes and uncertainty notes."""

OUTPUT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["session_date", "summary_paragraphs", "key_themes", "uncertainty_notes"],
    "properties": {
        "session_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "summary_paragraphs": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "citation_ids"],
                "properties": {
                    "text": {"type": "string", "minLength": 1, "maxLength": 1200},
                    "citation_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1, "maxLength": 80},
                    },
                },
            },
        },
        "key_themes": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["theme", "citation_ids"],
                "properties": {
                    "theme": {"type": "string", "minLength": 1, "maxLength": 240},
                    "citation_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 6,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1, "maxLength": 80},
                    },
                },
            },
        },
        "uncertainty_notes": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["note", "citation_ids"],
                "properties": {
                    "note": {"type": "string", "minLength": 1, "maxLength": 320},
                    "citation_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 6,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1, "maxLength": 80},
                    },
                },
            },
        },
    },
}

COMMON_ACRONYMS = {
    "AD", "API", "CPI", "ETF", "FMP", "FX", "GDP", "LLM", "NYSE", "NASDAQ",
    "PCE", "PMI", "PPI", "US", "USA", "USD", "UTC",
}
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\d+(?:,\d{3})*(?:\.\d+)?%?(?![A-Za-z0-9])")
UPPER_TOKEN_RE = re.compile(r"\b[A-Z]{2,6}(?:[./-][A-Z]{1,6})?\b")


class SynthesisValidationError(ValueError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error(error: Exception) -> str:
    return (str(error).replace("\n", " ").strip() or type(error).__name__)[:300]


def _evidence_record(evidence_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"evidence_id": evidence_id, "kind": kind, **payload}


def build_llm_input_contract(
    session_date: str,
    cross_market_context: dict[str, Any],
    market_regime: dict[str, Any],
) -> dict[str, Any]:
    if cross_market_context.get("session_date") != session_date:
        raise SynthesisValidationError("Cross-market context session mismatch")
    evidence: list[dict[str, Any]] = []
    for article in cross_market_context.get("news") or []:
        evidence_id = str(article.get("evidence_id") or "")
        if not evidence_id:
            raise SynthesisValidationError("Normalized news record lacks an evidence ID")
        evidence.append(
            _evidence_record(
                evidence_id,
                "news",
                {
                    "category": article.get("category"),
                    "title": article.get("title"),
                    "publisher": article.get("publisher"),
                    "published_at": article.get("published_at"),
                    "url": article.get("url"),
                    "symbols": article.get("symbols") or [],
                    "themes": article.get("themes") or [],
                },
            )
        )
    for event in cross_market_context.get("economic_calendar") or []:
        evidence_id = str(event.get("evidence_id") or "")
        if not evidence_id:
            raise SynthesisValidationError("Normalized economic record lacks an evidence ID")
        evidence.append(_evidence_record(evidence_id, "economic_calendar", dict(event)))
    treasury = cross_market_context.get("treasury_context") or {}
    if treasury:
        evidence_id = str(treasury.get("evidence_id") or "")
        if not evidence_id:
            raise SynthesisValidationError("Normalized Treasury record lacks an evidence ID")
        evidence.append(
            _evidence_record(
                evidence_id,
                "treasury_rates",
                {
                    **dict(treasury),
                    "maturity_labels": ["1-month", "2-month", "3-month", "6-month", "1-year", "2-year", "5-year", "10-year", "20-year", "30-year"],
                },
            )
        )
    evidence.append(
        _evidence_record(
            "GAUGE_POSTURE",
            "dashboard_market_gauge",
            {
                "posture": market_regime.get("dashboard_market_gauge_posture"),
                "score": market_regime.get("dashboard_market_gauge_score"),
                "generated_at": market_regime.get("dashboard_market_gauge_generated_at"),
                "scope": "Dashboard trend and extension posture; not an O'Neil regime classification.",
            },
        )
    )
    for item in market_regime.get("dashboard_market_gauge_indexes") or []:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            raise SynthesisValidationError("Dashboard Gauge index evidence lacks a symbol")
        evidence.append(
            _evidence_record(
                f"GAUGE_INDEX_{symbol}",
                "dashboard_market_gauge_index",
                {
                    **dict(item),
                    "moving_average_labels": ["21-day EMA", "50-day SMA", "200-day SMA"],
                },
            )
        )
    for index, item in enumerate(market_regime.get("dashboard_market_gauge_components") or [], start=1):
        evidence.append(
            _evidence_record(
                f"GAUGE_COMPONENT_{index:03d}",
                "dashboard_market_gauge_component",
                dict(item),
            )
        )
    for index, gap in enumerate(cross_market_context.get("evidence_gaps") or [], start=1):
        evidence.append(
            _evidence_record(
                f"EVIDENCE_GAP_{index:03d}",
                "evidence_gap",
                {"description": str(gap)},
            )
        )
    evidence.sort(key=lambda item: item["evidence_id"])
    evidence_ids = [item["evidence_id"] for item in evidence]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise SynthesisValidationError("Frozen LLM evidence IDs are not unique")
    contract = {
        "schema_version": INPUT_SCHEMA_VERSION,
        "session_date": session_date,
        "provider_scope": ["FMP", "Dashboard Market Gauge"],
        "lookback_window": cross_market_context.get("lookback_window") or {},
        "evidence": evidence,
        "allowed_evidence_ids": evidence_ids,
        "constraints": {
            "frozen_sources_only": True,
            "autonomous_web_or_tool_access": False,
            "headline_driven_candidate_selection": False,
            "decision_influence": "INTERPRETATION_ONLY",
            "may_override_deterministic_outputs": False,
        },
    }
    if len(canonical_json(contract).encode("utf-8")) > MAX_INPUT_BYTES:
        raise SynthesisValidationError(f"Frozen LLM input exceeds {MAX_INPUT_BYTES} bytes")
    return contract


def build_request_contract(input_contract: dict[str, Any], model: str) -> dict[str, Any]:
    input_sha256 = sha256_text(canonical_json(input_contract))
    api_request = {
        "model": model,
        "store": False,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "reasoning": {"effort": "none"},
        "tools": [],
        "tool_choice": "none",
        "instructions": SYSTEM_INSTRUCTIONS,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": canonical_json(input_contract)}],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "cross_market_synthesis",
                "strict": True,
                "schema": OUTPUT_JSON_SCHEMA,
            }
        },
    }
    return {
        "schema_version": "openai_responses_request_v1",
        "provider": "OpenAI",
        "endpoint": "v1/responses",
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "input_sha256": input_sha256,
        "api_request": api_request,
    }


def _extract_output_text(response: dict[str, Any]) -> str:
    if response.get("status") != "completed":
        raise SynthesisValidationError("OpenAI response did not complete")
    for item in response.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") == "refusal":
                raise SynthesisValidationError("OpenAI response was refused")
            if content.get("type") == "output_text" and content.get("text"):
                return str(content["text"])
    raise SynthesisValidationError("OpenAI response omitted structured output text")


def _normalized_number(token: str) -> str:
    value = token.replace(",", "").rstrip("%")
    try:
        normalized = Decimal(value).normalize()
    except InvalidOperation:
        return value
    return format(normalized, "f")


def _statement_rows(output: dict[str, Any]) -> list[tuple[str, list[str]]]:
    rows = [
        (str(item.get("text") or ""), list(item.get("citation_ids") or []))
        for item in output.get("summary_paragraphs") or []
    ]
    rows.extend(
        (str(item.get("theme") or ""), list(item.get("citation_ids") or []))
        for item in output.get("key_themes") or []
    )
    rows.extend(
        (str(item.get("note") or ""), list(item.get("citation_ids") or []))
        for item in output.get("uncertainty_notes") or []
    )
    return rows


def _validate_items(
    value: Any,
    *,
    field: str,
    text_field: str,
    minimum: int,
    maximum: int,
    text_maximum: int,
    citation_maximum: int,
) -> None:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise SynthesisValidationError(f"Structured synthesis {field} has an invalid item count")
    for item in value:
        if not isinstance(item, dict) or set(item) != {text_field, "citation_ids"}:
            raise SynthesisValidationError(f"Structured synthesis {field} has malformed fields")
        text = item.get(text_field)
        citations = item.get("citation_ids")
        if not isinstance(text, str) or not text.strip() or len(text) > text_maximum:
            raise SynthesisValidationError(f"Structured synthesis {field} has invalid text")
        if (
            not isinstance(citations, list)
            or not 1 <= len(citations) <= citation_maximum
            or len(citations) != len(set(citations))
            or any(not isinstance(value, str) or not value or len(value) > 80 for value in citations)
        ):
            raise SynthesisValidationError(f"Structured synthesis {field} has invalid citations")


def validate_model_output(output: dict[str, Any], input_contract: dict[str, Any]) -> dict[str, Any]:
    if set(output) != {"session_date", "summary_paragraphs", "key_themes", "uncertainty_notes"}:
        raise SynthesisValidationError("Structured synthesis has unexpected or missing fields")
    if not isinstance(output.get("session_date"), str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", output["session_date"]):
        raise SynthesisValidationError("Structured synthesis session is malformed")
    if output.get("session_date") != input_contract.get("session_date"):
        raise SynthesisValidationError("Structured synthesis session mismatch")
    paragraphs = output.get("summary_paragraphs")
    themes = output.get("key_themes")
    notes = output.get("uncertainty_notes")
    _validate_items(paragraphs, field="summary_paragraphs", text_field="text", minimum=1, maximum=2, text_maximum=1200, citation_maximum=8)
    _validate_items(themes, field="key_themes", text_field="theme", minimum=0, maximum=5, text_maximum=240, citation_maximum=6)
    _validate_items(notes, field="uncertainty_notes", text_field="note", minimum=0, maximum=6, text_maximum=320, citation_maximum=6)
    evidence_by_id = {item["evidence_id"]: item for item in input_contract.get("evidence") or []}
    for text, citations in _statement_rows(output):
        if not text.strip() or not citations or len(citations) != len(set(citations)):
            raise SynthesisValidationError("Every synthesis statement requires unique frozen citations")
        unknown = sorted(set(citations) - set(evidence_by_id))
        if unknown:
            raise SynthesisValidationError(f"Structured synthesis uses unknown citations: {unknown}")
        cited_text = canonical_json({citation: evidence_by_id[citation] for citation in citations})
        allowed_numbers = {_normalized_number(value) for value in NUMBER_RE.findall(cited_text)}
        actual_numbers = {_normalized_number(value) for value in NUMBER_RE.findall(text)}
        unsupported_numbers = sorted(actual_numbers - allowed_numbers)
        if unsupported_numbers:
            raise SynthesisValidationError(f"Structured synthesis uses unsupported numbers: {unsupported_numbers}")
        allowed_upper = set(UPPER_TOKEN_RE.findall(cited_text)) | COMMON_ACRONYMS
        unsupported_upper = sorted(set(UPPER_TOKEN_RE.findall(text)) - allowed_upper)
        if unsupported_upper:
            raise SynthesisValidationError(f"Structured synthesis uses unsupported tickers or acronyms: {unsupported_upper}")
    return output


def _default_request(api_key: str, api_request: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        OPENAI_RESPONSES_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=api_request,
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise SynthesisValidationError("OpenAI response payload is not an object")
    return payload


def _fallback(
    *,
    session_date: str,
    model: str,
    request_contract: dict[str, Any] | None,
    generated_at: str,
    reason_code: str,
    reason: str,
) -> dict[str, Any]:
    output = {
        "session_date": session_date,
        "summary_paragraphs": [],
        "key_themes": [],
        "uncertainty_notes": [],
    }
    return {
        "schema_version": SYNTHESIS_SCHEMA_VERSION,
        "status": "INSUFFICIENT_EVIDENCE",
        "provider": "OpenAI",
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "session_date": session_date,
        "generated_at": generated_at,
        "input_sha256": request_contract.get("input_sha256") if request_contract else None,
        "request_sha256": sha256_text(canonical_json(request_contract)) if request_contract else None,
        "request_contract": request_contract,
        "validated_output": output,
        "output_sha256": sha256_text(canonical_json(output)),
        "reason_code": reason_code,
        "reason": reason,
        "usage": None,
        "interpretation": {
            "decision_influence": "INTERPRETATION_ONLY",
            "may_override_deterministic_outputs": False,
            "autonomous_web_or_tool_access": False,
        },
    }


def validate_frozen_synthesis(
    synthesis: dict[str, Any],
    cross_market_context: dict[str, Any],
    market_regime: dict[str, Any],
) -> dict[str, Any]:
    if synthesis.get("schema_version") != SYNTHESIS_SCHEMA_VERSION:
        raise SynthesisValidationError("Frozen synthesis schema is unsupported")
    session_date = str(synthesis.get("session_date") or "")
    if synthesis.get("status") not in {"AVAILABLE", "INSUFFICIENT_EVIDENCE"}:
        raise SynthesisValidationError("Frozen synthesis status is unsupported")
    interpretation = synthesis.get("interpretation") or {}
    if (
        interpretation.get("decision_influence") != "INTERPRETATION_ONLY"
        or interpretation.get("may_override_deterministic_outputs") is not False
        or interpretation.get("autonomous_web_or_tool_access") is not False
    ):
        raise SynthesisValidationError("Frozen synthesis decision boundary is invalid")
    output = synthesis.get("validated_output")
    if not isinstance(output, dict) or synthesis.get("output_sha256") != sha256_text(canonical_json(output)):
        raise SynthesisValidationError("Frozen synthesis output hash mismatch")
    request_contract = synthesis.get("request_contract")
    if not isinstance(request_contract, dict):
        if synthesis.get("status") == "INSUFFICIENT_EVIDENCE" and synthesis.get("reason_code") == "LLM_INPUT_INVALID":
            expected_output = {
                "session_date": session_date,
                "summary_paragraphs": [],
                "key_themes": [],
                "uncertainty_notes": [],
            }
            if output != expected_output:
                raise SynthesisValidationError("Frozen synthesis input-error fallback is malformed")
            return synthesis
        raise SynthesisValidationError("Frozen synthesis request contract is unavailable")
    input_contract = build_llm_input_contract(session_date, cross_market_context, market_regime)
    expected_request = build_request_contract(input_contract, str(synthesis.get("model") or ""))
    if request_contract != expected_request:
        raise SynthesisValidationError("Frozen synthesis request contract mismatch")
    if synthesis.get("input_sha256") != expected_request["input_sha256"]:
        raise SynthesisValidationError("Frozen synthesis input hash mismatch")
    if synthesis.get("request_sha256") != sha256_text(canonical_json(request_contract)):
        raise SynthesisValidationError("Frozen synthesis request hash mismatch")
    if synthesis.get("status") == "AVAILABLE":
        validate_model_output(output, input_contract)
    else:
        expected_output = {
            "session_date": session_date,
            "summary_paragraphs": [],
            "key_themes": [],
            "uncertainty_notes": [],
        }
        if output != expected_output:
            raise SynthesisValidationError("Frozen synthesis fallback is malformed")
    api_request = request_contract.get("api_request") or {}
    if api_request.get("store") is not False or api_request.get("tools") != [] or api_request.get("tool_choice") != "none":
        raise SynthesisValidationError("Frozen synthesis request enabled storage or tools")
    return synthesis


def synthesize_cross_market_context(
    session_date: str,
    cross_market_context: dict[str, Any],
    market_regime: dict[str, Any],
    *,
    api_key: str | None = None,
    model: str | None = None,
    requester: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    generated_at: str | None = None,
    frozen_synthesis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_model = (model or os.getenv("OPENAI_MARKET_REVIEW_MODEL") or DEFAULT_MODEL).strip()
    timestamp = generated_at or _now_iso()
    if frozen_synthesis is not None:
        return validate_frozen_synthesis(frozen_synthesis, cross_market_context, market_regime)
    try:
        input_contract = build_llm_input_contract(session_date, cross_market_context, market_regime)
        request_contract = build_request_contract(input_contract, selected_model)
    except Exception as exc:
        return _fallback(
            session_date=session_date,
            model=selected_model,
            request_contract=None,
            generated_at=timestamp,
            reason_code="LLM_INPUT_INVALID",
            reason=_safe_error(exc),
        )
    secret = api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")
    if not secret:
        return _fallback(
            session_date=session_date,
            model=selected_model,
            request_contract=request_contract,
            generated_at=timestamp,
            reason_code="OPENAI_API_KEY_MISSING",
            reason="LLM synthesis unavailable because OPENAI_API_KEY is not configured.",
        )
    try:
        response = (
            requester(secret, request_contract["api_request"])
            if requester
            else _default_request(secret, request_contract["api_request"])
        )
        output = json.loads(_extract_output_text(response))
        if not isinstance(output, dict):
            raise SynthesisValidationError("Structured synthesis output is not an object")
        validate_model_output(output, input_contract)
        synthesis = {
            "schema_version": SYNTHESIS_SCHEMA_VERSION,
            "status": "AVAILABLE",
            "provider": "OpenAI",
            "model": selected_model,
            "resolved_model": str(response.get("model") or selected_model),
            "prompt_version": PROMPT_VERSION,
            "output_schema_version": OUTPUT_SCHEMA_VERSION,
            "session_date": session_date,
            "generated_at": timestamp,
            "input_sha256": request_contract["input_sha256"],
            "request_sha256": sha256_text(canonical_json(request_contract)),
            "request_contract": request_contract,
            "validated_output": output,
            "output_sha256": sha256_text(canonical_json(output)),
            "reason_code": None,
            "reason": None,
            "usage": response.get("usage"),
            "interpretation": {
                "decision_influence": "INTERPRETATION_ONLY",
                "may_override_deterministic_outputs": False,
                "autonomous_web_or_tool_access": False,
            },
        }
        validate_frozen_synthesis(synthesis, cross_market_context, market_regime)
        return synthesis
    except Exception as exc:
        return _fallback(
            session_date=session_date,
            model=selected_model,
            request_contract=request_contract,
            generated_at=timestamp,
            reason_code="LLM_SYNTHESIS_VALIDATION_FAILED",
            reason=_safe_error(exc),
        )
