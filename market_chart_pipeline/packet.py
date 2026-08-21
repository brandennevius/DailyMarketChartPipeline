from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .rules import evaluate_position, evaluate_shakeout, score_candidate
from .utils import canonical_json, sha256_file, sha256_text


LLM_NON_INFLUENCE_FIELDS = (
    "market_regime",
    "exposure_guidance",
    "market_breadth",
    "portfolio_risk",
    "sell_rule_results",
    "candidate_results",
    "shakeout_results",
)


def llm_non_influence_record(packet: dict[str, Any]) -> dict[str, Any]:
    decisions = {field: packet.get(field) for field in LLM_NON_INFLUENCE_FIELDS}
    return {
        "schema_version": "llm_non_influence_v1",
        "covered_fields": list(LLM_NON_INFLUENCE_FIELDS),
        "decision_outputs_sha256": sha256_text(canonical_json(decisions)),
        "statement": "Cross-market LLM synthesis is excluded from every deterministic decision input and output.",
    }


def _source_record(path: Path | None, label: str) -> dict[str, Any]:
    if path is None:
        return {"label": label, "path": None, "sha256": None, "status": "not_provided"}
    if not path.exists():
        return {"label": label, "path": str(path), "sha256": None, "status": "missing"}
    return {
        "label": label,
        "path": str(path),
        "sha256": sha256_file(path),
        "status": "verified",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def freeze_packet(packet: dict[str, Any]) -> dict[str, Any]:
    frozen = dict(packet)
    frozen.pop("packet_sha256", None)
    frozen["packet_sha256"] = sha256_text(canonical_json(frozen))
    return frozen


def _verify_chart_packet(chart_json: Path | None, chart_pdf: Path | None, session_date: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "insufficient_evidence",
        "json_path": str(chart_json) if chart_json else None,
        "pdf_path": str(chart_pdf) if chart_pdf else None,
        "verified_tickers": [],
        "errors": [],
    }
    if not chart_json or not chart_pdf or not chart_json.exists() or not chart_pdf.exists():
        result["errors"].append("Current-session chart JSON and PDF are both required")
        return result
    try:
        payload = json.loads(chart_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["errors"].append(f"Chart JSON could not be parsed: {exc}")
        return result

    requested = {str(ticker).upper() for ticker in payload.get("requested_tickers", [])}
    manifest_records = set(((payload.get("source_manifest") or {}).get("records") or {}).keys())
    records = payload.get("records") if isinstance(payload.get("records"), list) else []
    verified = {
        str(record.get("metrics", {}).get("ticker", "")).upper()
        for record in records
        if record.get("latest_bar_date") == session_date
        and record.get("daily_chart")
        and record.get("weekly_chart")
    }
    if payload.get("session_date") != session_date:
        result["errors"].append("Chart packet session date does not match review session")
    if payload.get("chart_data_source") != "FMP":
        result["errors"].append("Chart packet historical price provider is not FMP")
    if (payload.get("chart_data_policy") or {}).get("live_quote_substitution") is not False:
        result["errors"].append("Chart packet does not explicitly prohibit live quote substitution")
    if int(payload.get("verified_count", -1)) + int(payload.get("error_count", -1)) != len(requested):
        result["errors"].append("Chart packet requested/verified/error counts do not reconcile")
    expected_pdf_hash = payload.get("artifacts", {}).get("pdf_sha256")
    actual_pdf_hash = sha256_file(chart_pdf)
    if expected_pdf_hash != actual_pdf_hash:
        result["errors"].append("Chart packet PDF hash does not match chart JSON")
    if verified - requested:
        result["errors"].append("Chart records contain tickers outside the requested set")
    if manifest_records != requested:
        result["errors"].append("Chart source-manifest ticker set does not match requested tickers")
    result.update(
        {
            "status": "verified" if not result["errors"] else "insufficient_evidence",
            "requested_tickers": sorted(requested),
            "verified_tickers": sorted(verified),
            "pdf_sha256": actual_pdf_hash,
            "price_history_provider": payload.get("chart_data_source"),
            "price_history_endpoint": payload.get("chart_data_endpoint"),
            "live_quote_substitution": (payload.get("chart_data_policy") or {}).get("live_quote_substitution"),
        }
    )
    return result


def build_review_packet(
    *,
    requested_date: str,
    session_date: str,
    policy: dict[str, Any],
    market_data: dict[str, Any] | None = None,
    portfolio: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    shakeouts: list[dict[str, Any]] | None = None,
    chart_packet_dir: Path | None = None,
    source_manifest: dict[str, Any] | None = None,
    portfolio_risk_details: dict[str, Any] | None = None,
    market_breadth: dict[str, Any] | None = None,
    audit_profile: str = "standard",
) -> dict[str, Any]:
    market_data = market_data or {}
    portfolio = portfolio or []
    candidates = candidates or []
    shakeouts = shakeouts or []
    chart_json = chart_packet_dir / f"Market_Chart_Data_{session_date}.json" if chart_packet_dir else None
    chart_pdf = chart_packet_dir / f"Market_Chart_Packet_{session_date}.pdf" if chart_packet_dir else None
    position_results = [evaluate_position(position, policy, session_date) for position in portfolio]
    candidate_results = sorted(
        [score_candidate(candidate, policy) for candidate in candidates],
        key=lambda item: (-item["internal_canslim_score"], item["ticker"]),
    )
    shakeout_results = [evaluate_shakeout(record, policy) for record in shakeouts]
    source_records = list((source_manifest or {}).get("sources") or [])
    source_records.extend(
        [
            _source_record(chart_json, "chart_packet_json"),
            _source_record(chart_pdf, "chart_packet_pdf"),
        ]
    )
    packet = {
        "schema_version": "daily_review_packet_v1",
        "audit_profile": audit_profile,
        "requested_date": requested_date,
        "session_date": session_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy_version": policy["policy_version"],
        "calculation_version": policy["calculation_version"],
        "source_timestamps": market_data.get("source_timestamps", {}),
        "sources": source_records,
        "manifests": market_data.get("manifests", []),
        "market_regime": market_data.get(
            "market_regime",
            {
                "classification": "INSUFFICIENT_EVIDENCE",
                "confidence": "low",
                "evidence": ["No deterministic market-regime input was supplied."],
            },
        ),
        "exposure_guidance": market_data.get(
            "exposure_guidance",
            {
                "status": "INSUFFICIENT_EVIDENCE",
                "exact_exposure": "indeterminate",
                "evidence": ["No deterministic market-permission input was supplied."],
            },
        ),
        "market_breadth": market_breadth or {"status": "insufficient_evidence", "verified_symbols": 0},
        "cross_market_context": market_data.get(
            "cross_market_context",
            {
                "schema_version": "fmp_cross_market_context_v1",
                "status": "INSUFFICIENT_EVIDENCE",
                "evidence_gaps": ["No frozen cross-market source was supplied."],
                "interpretation": {
                    "decision_influence": "INTERPRETATION_ONLY",
                    "may_override_deterministic_outputs": False,
                },
            },
        ),
        "portfolio_risk": {
            "position_count": len(portfolio),
            "max_position_risk_pct": policy["portfolio"]["max_position_risk_pct"],
            "max_total_open_risk_pct": policy["portfolio"]["max_total_open_risk_pct"],
            "status": "calculated" if portfolio else "insufficient_evidence",
            **(portfolio_risk_details or {}),
        },
        "sell_rule_results": position_results,
        "candidate_results": candidate_results,
        "shakeout_results": shakeout_results,
        "chart_verification": _verify_chart_packet(chart_json, chart_pdf, session_date),
        "input_sets": {
            "portfolio_tickers": sorted(str(item.get("ticker", "")).upper() for item in portfolio if item.get("ticker")),
            "candidate_tickers": sorted(str(item.get("ticker", "")).upper() for item in candidates if item.get("ticker")),
            "watchlist_tickers": sorted(
                str(item.get("ticker", "")).upper()
                for item in candidates
                if item.get("ticker") and item.get("origin") == "watchlist"
            ),
        },
        "validation_evidence": [],
    }
    packet["llm_non_influence"] = llm_non_influence_record(packet)
    return freeze_packet(packet)
