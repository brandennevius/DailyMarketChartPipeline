from copy import deepcopy
import json
from pathlib import Path

from market_chart_pipeline.cross_market import collect_fmp_cross_market_context
from market_chart_pipeline.llm_context import (
    DEFAULT_MODEL,
    synthesize_cross_market_context,
    validate_frozen_synthesis,
)
from market_chart_pipeline.packet import llm_non_influence_record
from market_chart_pipeline.utils import canonical_json, sha256_text


SESSION = "2026-08-17"


def _fetcher(endpoint, **params):
    if endpoint == "news/general-latest":
        return [{
            "title": "Treasury yields rise before Federal Reserve minutes",
            "site": "Example Wire",
            "publishedDate": "2026-08-17T14:30:00Z",
            "url": "https://example.com/rates",
        }]
    if endpoint == "news/stock-latest":
        return [{
            "title": "ACME raises earnings guidance",
            "site": "Stock Desk",
            "publishedDate": "2026-08-17T15:00:00Z",
            "url": "https://example.com/acme",
            "symbol": "ACME",
        }]
    if endpoint in {"news/forex-latest", "news/crypto-latest"}:
        return []
    if endpoint == "economic-calendar":
        return [{
            "date": f"{SESSION} 08:30:00",
            "event": "Retail Sales",
            "country": "US",
            "impact": "High",
            "actual": 0.7,
            "estimate": 0.4,
            "previous": 0.2,
            "unit": "%",
        }]
    if endpoint == "treasury-rates":
        return [{"date": SESSION, "year2": 4.01, "year10": 4.22, "year30": 4.8}]
    raise AssertionError(endpoint)


def _context_and_regime():
    _, context = collect_fmp_cross_market_context(
        SESSION,
        fetcher=_fetcher,
        retrieved_at="2026-08-17T21:00:00Z",
    )
    regime = {
        "classification": "INSUFFICIENT_EVIDENCE",
        "dashboard_market_gauge_posture": "Neutral",
        "dashboard_market_gauge_score": 2,
        "dashboard_market_gauge_generated_at": "2026-08-17T20:10:00-04:00",
        "dashboard_market_gauge_indexes": [{
            "symbol": "SPY",
            "close": 500,
            "ema21": 496,
            "sma50": 490,
            "sma200": 450,
            "distance_above_21d_pct": 0.81,
            "distance_above_50d_pct": 2.04,
            "short_term_trend": "UP",
            "medium_term_trend": "UP",
            "long_term_trend": "UP",
            "extension": "Normal",
            "price_session": SESSION,
        }],
        "dashboard_market_gauge_components": [{
            "label": "Index trend",
            "state": "Positive",
            "detail": "SPY is above its moving averages.",
        }],
    }
    return context, regime


def _model_output():
    return {
        "session_date": SESSION,
        "summary_paragraphs": [{
            "text": "The 10-year Treasury rate was 4.22% while SPY closed at 500, and the frozen Dashboard Gauge posture was Neutral.",
            "citation_ids": ["FMP_TREASURY_001", "GAUGE_INDEX_SPY", "GAUGE_POSTURE"],
        }],
        "key_themes": [{
            "theme": "Retail Sales came in at 0.7 versus the 0.4 estimate.",
            "citation_ids": ["FMP_ECON_001"],
        }],
        "uncertainty_notes": [{
            "note": "Forex and crypto context are insufficient.",
            "citation_ids": ["EVIDENCE_GAP_001", "EVIDENCE_GAP_002"],
        }],
    }


def _response(output):
    return {
        "status": "completed",
        "model": "gpt-5.4-nano-2026-08-01",
        "usage": {"input_tokens": 1000, "output_tokens": 120, "total_tokens": 1120},
        "output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": json.dumps(output)}],
        }],
    }


def test_valid_synthesis_is_strict_frozen_and_hash_verified():
    context, regime = _context_and_regime()
    captured = {}

    def requester(secret, request):
        captured["secret"] = secret
        captured["request"] = request
        return _response(_model_output())

    synthesis = synthesize_cross_market_context(
        SESSION,
        context,
        regime,
        api_key="test-key",
        requester=requester,
        generated_at="2026-08-17T21:05:00Z",
    )

    assert synthesis["status"] == "AVAILABLE"
    assert synthesis["model"] == DEFAULT_MODEL
    assert synthesis["resolved_model"] == "gpt-5.4-nano-2026-08-01"
    assert synthesis["output_sha256"] == sha256_text(canonical_json(_model_output()))
    assert synthesis["request_contract"]["input_sha256"] == synthesis["input_sha256"]
    assert captured["secret"] == "test-key"
    assert captured["request"]["store"] is False
    assert captured["request"]["tools"] == []
    assert captured["request"]["tool_choice"] == "none"
    assert validate_frozen_synthesis(synthesis, context, regime) == synthesis


def test_unknown_citation_is_rejected_to_visible_fallback():
    context, regime = _context_and_regime()
    output = _model_output()
    output["summary_paragraphs"][0]["citation_ids"] = ["WEB_SEARCH_RESULT"]
    synthesis = synthesize_cross_market_context(
        SESSION, context, regime, api_key="test-key", requester=lambda *_: _response(output)
    )
    assert synthesis["status"] == "INSUFFICIENT_EVIDENCE"
    assert synthesis["reason_code"] == "LLM_SYNTHESIS_VALIDATION_FAILED"
    assert "unknown citations" in synthesis["reason"]


def test_unsupported_number_is_rejected_to_visible_fallback():
    context, regime = _context_and_regime()
    output = _model_output()
    output["summary_paragraphs"][0]["text"] = "SPY closed at 999."
    output["summary_paragraphs"][0]["citation_ids"] = ["GAUGE_INDEX_SPY"]
    synthesis = synthesize_cross_market_context(
        SESSION, context, regime, api_key="test-key", requester=lambda *_: _response(output)
    )
    assert synthesis["status"] == "INSUFFICIENT_EVIDENCE"
    assert "unsupported numbers" in synthesis["reason"]


def test_malformed_or_session_mismatched_output_is_rejected():
    context, regime = _context_and_regime()
    malformed = _model_output()
    malformed["session_date"] = "2026-08-18"
    malformed["summary_paragraphs"][0]["unexpected"] = True
    synthesis = synthesize_cross_market_context(
        SESSION, context, regime, api_key="test-key", requester=lambda *_: _response(malformed)
    )
    assert synthesis["status"] == "INSUFFICIENT_EVIDENCE"
    assert synthesis["reason_code"] == "LLM_SYNTHESIS_VALIDATION_FAILED"


def test_missing_key_falls_back_without_request():
    context, regime = _context_and_regime()
    calls = []
    synthesis = synthesize_cross_market_context(
        SESSION, context, regime, api_key="", requester=lambda *_: calls.append(True)
    )
    assert synthesis["status"] == "INSUFFICIENT_EVIDENCE"
    assert synthesis["reason_code"] == "OPENAI_API_KEY_MISSING"
    assert not calls
    assert validate_frozen_synthesis(synthesis, context, regime) == synthesis


def test_replay_uses_frozen_synthesis_without_second_call():
    context, regime = _context_and_regime()
    frozen = synthesize_cross_market_context(
        SESSION, context, regime, api_key="test-key", requester=lambda *_: _response(_model_output())
    )
    calls = []
    replayed = synthesize_cross_market_context(
        SESSION,
        context,
        regime,
        api_key="test-key",
        requester=lambda *_: calls.append(True),
        frozen_synthesis=deepcopy(frozen),
    )
    assert replayed == frozen
    assert not calls


def test_llm_output_is_excluded_from_decision_fingerprint():
    packet = {
        "market_regime": {"classification": "INSUFFICIENT_EVIDENCE"},
        "exposure_guidance": {"exact_exposure": "indeterminate"},
        "market_breadth": {"verified_symbols": 3},
        "portfolio_risk": {"status": "calculated"},
        "sell_rule_results": [{"ticker": "ACME", "action": "HOLD"}],
        "candidate_results": [{"ticker": "SPY", "action": "HOLD"}],
        "shakeout_results": [],
        "cross_market_context": {"llm_synthesis": {"validated_output": {"summary": "one"}}},
    }
    before = llm_non_influence_record(packet)
    packet["cross_market_context"]["llm_synthesis"]["validated_output"]["summary"] = "two"
    assert llm_non_influence_record(packet) == before


def test_daily_review_workflow_plumbs_secret_and_optional_model_variable():
    workflow = Path(".github/workflows/daily-review.yml").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}" in workflow
    assert "OPENAI_MARKET_REVIEW_MODEL: ${{ vars.OPENAI_MARKET_REVIEW_MODEL }}" in workflow
