from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core import ValidationError
from .llm_context import SynthesisValidationError, validate_frozen_synthesis
from .packet import llm_non_influence_record
from .rules import (
    CANDIDATE_ACTION_AVOID,
    CANDIDATE_ACTION_BUILDING,
    CANDIDATE_ACTION_BUY,
    CANDIDATE_ACTION_EARLY,
    CANDIDATE_ACTION_INSUFFICIENT,
    CANDIDATE_ACTION_NEAR,
    CANDIDATE_ACTION_NON_EQUITY,
    CANDIDATE_ACTION_OPEN_POSITION,
    CANDIDATE_ACTION_WAIT,
)
from .utils import canonical_json, sha256_file, sha256_text


def audit_packet(packet: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    expected_hash = packet.get("packet_sha256")
    body = dict(packet)
    body.pop("packet_sha256", None)
    actual_hash = sha256_text(canonical_json(body))
    if expected_hash != actual_hash:
        raise ValidationError("Packet hash does not match canonical packet body")
    evidence.append({"gate": "packet_hash", "status": "pass", "sha256": actual_hash})

    if packet.get("audit_profile") == "strict-core":
        required_sources = {
            "portfolio_snapshot",
            "marketsurge_scan",
            "market_gauge_json",
            "chart_packet_artifact",
            "chart_packet_json",
            "chart_packet_pdf",
        }
        if (packet.get("cross_market_context") or {}).get("raw_inputs"):
            required_sources.update({"fmp_cross_market_context", "openai_cross_market_synthesis"})
        sources = {item.get("label"): item for item in packet.get("sources", [])}
        missing = sorted(required_sources - set(sources))
        if missing:
            raise ValidationError(f"Strict source audit is missing required sources: {missing}")
        for label in sorted(required_sources):
            source = sources[label]
            path = Path(str(source.get("path") or ""))
            if source.get("status") != "verified" or not source.get("sha256") or not path.is_file():
                raise ValidationError(f"Strict source audit failed for {label}")
            if sha256_file(path) != source["sha256"]:
                raise ValidationError(f"Strict source hash mismatch for {label}")
        if packet.get("chart_verification", {}).get("status") != "verified":
            raise ValidationError("Strict chart verification gate failed")
        if packet.get("portfolio_risk", {}).get("status") != "calculated":
            raise ValidationError("Strict portfolio evidence gate failed")
        requested = set(packet.get("chart_verification", {}).get("requested_tickers", []))
        candidates = set(packet.get("input_sets", {}).get("candidate_tickers", []))
        if requested != candidates:
            raise ValidationError("Strict candidate universe does not match the chart source manifest")
        evidence.append({"gate": "strict_core_sources", "status": "pass", "required": sorted(required_sources)})

        invalid_sandbox_states = []
        verified_sandboxes = []
        explicit_failures = []
        for item in packet.get("sell_rule_results", []):
            ticker = item.get("ticker")
            snapshot = item.get("position_snapshot", {})
            if snapshot.get("sell_sandbox_status") == "verified" and snapshot.get("sell_sandbox_asset", {}).get("sha256"):
                verified_sandboxes.append(ticker)
            elif snapshot.get("sell_sandbox_status") == "insufficient_evidence" and snapshot.get("sell_sandbox_error"):
                explicit_failures.append(ticker)
            else:
                invalid_sandbox_states.append(ticker)
        if invalid_sandbox_states:
            raise ValidationError(f"Strict sell-sandbox state gate failed: {sorted(invalid_sandbox_states)}")
        evidence.append({
            "gate": "sell_sandbox_charts",
            "status": "pass",
            "verified": sorted(verified_sandboxes),
            "explicit_failures": sorted(explicit_failures),
        })

        cross_market = packet.get("cross_market_context") or {}
        if cross_market.get("raw_inputs"):
            source = sources.get("fmp_cross_market_context")
            if not source:
                raise ValidationError("Frozen cross-market context is missing its source record")
            source_path = Path(str(source.get("path") or ""))
            if source.get("status") != "verified" or not source_path.is_file():
                raise ValidationError("Frozen cross-market context source is unavailable")
            if sha256_file(source_path) != source.get("sha256"):
                raise ValidationError("Frozen cross-market context source hash mismatch")
            raw_hash = sha256_text(canonical_json(cross_market["raw_inputs"]))
            if raw_hash != cross_market.get("raw_input_sha256"):
                raise ValidationError("Frozen cross-market raw-input hash mismatch")
            interpretation = cross_market.get("interpretation") or {}
            if interpretation.get("decision_influence") != "INTERPRETATION_ONLY" or interpretation.get("may_override_deterministic_outputs") is not False:
                raise ValidationError("Cross-market context is not constrained to interpretation-only use")
            evidence.append({
                "gate": "cross_market_frozen_context",
                "status": "pass",
                "source_sha256": source.get("sha256"),
                "raw_input_sha256": raw_hash,
                "context_status": cross_market.get("status"),
            })
            synthesis = cross_market.get("llm_synthesis")
            if not isinstance(synthesis, dict):
                raise ValidationError("Frozen cross-market LLM synthesis is missing")
            synthesis_source = sources.get("openai_cross_market_synthesis")
            synthesis_path = Path(str((synthesis_source or {}).get("path") or ""))
            if not synthesis_source or synthesis_source.get("status") != "verified" or not synthesis_path.is_file():
                raise ValidationError("Frozen cross-market LLM synthesis source is unavailable")
            if sha256_file(synthesis_path) != synthesis_source.get("sha256"):
                raise ValidationError("Frozen cross-market LLM synthesis source hash mismatch")
            try:
                stored_synthesis = json.loads(synthesis_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValidationError(f"Frozen cross-market LLM synthesis source is invalid: {exc}") from exc
            if stored_synthesis != synthesis:
                raise ValidationError("Packet synthesis does not match its frozen source")
            try:
                validate_frozen_synthesis(synthesis, cross_market, packet.get("market_regime") or {})
            except SynthesisValidationError as exc:
                raise ValidationError(f"Frozen cross-market LLM synthesis validation failed: {exc}") from exc
            evidence.append({
                "gate": "cross_market_llm_synthesis",
                "status": "pass",
                "synthesis_status": synthesis.get("status"),
                "input_sha256": synthesis.get("input_sha256"),
                "output_sha256": synthesis.get("output_sha256"),
                "model": synthesis.get("model"),
            })

    allowed_origins = {"scanner", "watchlist", "open_position", None}
    bad_origins = [item for item in packet.get("candidate_results", []) if item.get("origin") not in allowed_origins]
    if bad_origins:
        raise ValidationError(f"Candidate origin audit failed: {bad_origins}")
    evidence.append({"gate": "candidate_origins", "status": "pass"})

    deterministic_actions = {"HOLD", "ADD", "REDUCE", "EXIT", "REPAIR", "INSUFFICIENT_EVIDENCE"}
    candidate_actions = {
        CANDIDATE_ACTION_BUY,
        CANDIDATE_ACTION_EARLY,
        CANDIDATE_ACTION_NEAR,
        CANDIDATE_ACTION_BUILDING,
        CANDIDATE_ACTION_WAIT,
        CANDIDATE_ACTION_AVOID,
        CANDIDATE_ACTION_INSUFFICIENT,
        CANDIDATE_ACTION_OPEN_POSITION,
        CANDIDATE_ACTION_NON_EQUITY,
    }
    action_sections = ["sell_rule_results", "candidate_results", "shakeout_results"]
    bad_actions: list[dict[str, Any]] = []
    for section in action_sections:
        allowed_actions = candidate_actions if section == "candidate_results" else deterministic_actions
        for item in packet.get(section, []):
            if item.get("action") not in allowed_actions:
                bad_actions.append({"section": section, "item": item})
    if bad_actions:
        raise ValidationError(f"Unknown deterministic action(s): {bad_actions}")
    evidence.append({"gate": "action_set", "status": "pass"})

    for section in action_sections:
        for item in packet.get(section, []):
            if not item.get("rationale") or not isinstance(item.get("events"), list) or not item["events"]:
                raise ValidationError(f"Actionable completeness failed in {section} for {item.get('ticker')}")
    evidence.append({"gate": "explicit_rule_events", "status": "pass"})

    for item in packet.get("candidate_results", []):
        expected = {"BUY_NOW": CANDIDATE_ACTION_BUY, "EARLY_ENTRY": CANDIDATE_ACTION_EARLY}.get(
            item.get("classification")
        )
        if expected and item.get("action") != expected:
            raise ValidationError(f"Actionable candidate has an invalid action mapping: {item.get('ticker')}")
        if item.get("action") == CANDIDATE_ACTION_BUY:
            snapshot = item.get("snapshot") or {}
            required = {
                "pivot_verification_status": snapshot.get("pivot_verification_status"),
                "pivot_structure_verification_status": snapshot.get("pivot_structure_verification_status"),
                "exact_pivot_price": snapshot.get("exact_pivot_price"),
                "breakout_volume_confirmation": snapshot.get("breakout_volume_confirmation"),
            }
            if (
                required["pivot_verification_status"] != "verified"
                or required["pivot_structure_verification_status"] != "VERIFIED"
                or required["exact_pivot_price"] is None
                or required["breakout_volume_confirmation"] is not True
            ):
                raise ValidationError(f"BUY NOW candidate lacks a fully verified pivot/breakout: {item.get('ticker')}")
        if item.get("action") == CANDIDATE_ACTION_EARLY:
            snapshot = item.get("snapshot") or {}
            if (
                snapshot.get("early_entry_verification_status") != "verified"
                or snapshot.get("pivot_structure_verification_status") != "VERIFIED"
                or snapshot.get("breakout_volume_confirmation") is not True
            ):
                raise ValidationError(f"EARLY ENTRY candidate lacks verified structural/volume safeguards: {item.get('ticker')}")
    actionable = {
        item.get("ticker")
        for item in packet.get("candidate_results", [])
        if item.get("classification") in {"BUY_NOW", "EARLY_ENTRY"}
    }
    verified = set(packet.get("chart_verification", {}).get("verified_tickers", []))
    if actionable - verified:
        raise ValidationError(f"Actionable candidates lack current verified charts: {sorted(actionable - verified)}")
    evidence.append({"gate": "actionable_completeness", "status": "pass"})

    portfolio_tickers = set(packet.get("input_sets", {}).get("portfolio_tickers", []))
    result_tickers = {str(item.get("ticker", "")).upper() for item in packet.get("sell_rule_results", [])}
    if portfolio_tickers != result_tickers:
        raise ValidationError(
            f"Portfolio/result set relationship failed: missing={sorted(portfolio_tickers - result_tickers)}, "
            f"extra={sorted(result_tickers - portfolio_tickers)}"
        )
    candidate_tickers = set(packet.get("input_sets", {}).get("candidate_tickers", []))
    scored_tickers = {str(item.get("ticker", "")).upper() for item in packet.get("candidate_results", [])}
    if candidate_tickers != scored_tickers:
        raise ValidationError("Candidate/result set relationship failed")
    watchlist_tickers = set(packet.get("input_sets", {}).get("watchlist_tickers", []))
    watchlist_results = {
        str(item.get("ticker", "")).upper()
        for item in packet.get("candidate_results", [])
        if "BRANDENS_WATCHLIST" in set((item.get("snapshot") or {}).get("origin_categories") or [])
    }
    if watchlist_tickers != watchlist_results:
        raise ValidationError("Watchlist/result set relationship failed")
    evidence.append({"gate": "set_relationships", "status": "pass"})
    evidence.append({"gate": "complete_watchlist_results", "status": "pass", "ticker_count": len(watchlist_tickers)})

    universe = packet.get("candidate_universe_audit") or {}
    top_setups = packet.get("top_canslim_setups") or []
    if packet.get("candidate_results") and universe.get("schema_version") != "marketsurge_candidate_universe_audit_v1":
        raise ValidationError("MarketSurge candidate-universe audit is missing")
    ranked_limit = int(universe.get("ranking_limit") or 10)
    if len(top_setups) > ranked_limit or len(top_setups) > 10:
        raise ValidationError("Top CANSLIM setup ranking exceeds its configured limit")
    valid_manifest = set(universe.get("valid_manifest_equity_tickers") or [])
    ranked_tickers = [str(item.get("ticker") or "").upper() for item in top_setups]
    if len(ranked_tickers) != len(set(ranked_tickers)):
        raise ValidationError("Top CANSLIM setup ranking contains duplicate tickers")
    if set(ranked_tickers) - valid_manifest:
        raise ValidationError("Top CANSLIM setup ranking contains a ticker outside the valid MarketSurge equities")
    if set(ranked_tickers) & portfolio_tickers:
        raise ValidationError("Top CANSLIM setup ranking contains a current portfolio position")
    eligible = [
        item
        for item in packet.get("candidate_results", [])
        if (item.get("ranking_eligibility") or {}).get("status") == "ELIGIBLE"
    ]
    eligible.sort(
        key=lambda item: (
            -float(item.get("internal_canslim_score") or 0),
            -float((item.get("score_components") or {}).get("technical_setup") or 0),
            -float((item.get("score_components") or {}).get("relative_strength_group") or 0),
            str(item.get("ticker") or ""),
        )
    )
    expected_tickers = [str(item.get("ticker") or "").upper() for item in eligible[:ranked_limit]]
    if ranked_tickers != expected_tickers:
        raise ValidationError("Top CANSLIM setup order is not reproducible from the frozen candidate results")
    for expected_rank, item in enumerate(top_setups, start=1):
        if item.get("rank") != expected_rank:
            raise ValidationError("Top CANSLIM setup ranks are not contiguous")
        if not item.get("why_ranked") or not item.get("trigger") or not item.get("risk_invalidates"):
            raise ValidationError(f"Top CANSLIM setup lacks decision-useful evidence: {item.get('ticker')}")
        if not (item.get("snapshot") or {}).get("source_evidence"):
            raise ValidationError(f"Top CANSLIM setup lacks MarketSurge provenance: {item.get('ticker')}")
    if universe:
        if universe.get("ranked_top10_tickers") != ranked_tickers:
            raise ValidationError("Candidate-universe audit does not match Top CANSLIM setup ranking")
        if universe.get("news_candidate_admission") is not False or universe.get("llm_ranking_influence") is not False:
            raise ValidationError("News or LLM influence was admitted into candidate ranking")
        if universe.get("malformed_ocr_tokens_admitted") != 0:
            raise ValidationError("Malformed OCR tokens were admitted into candidate ranking")
    evidence.append({
        "gate": "top_canslim_setups",
        "status": "pass",
        "ranked_tickers": ranked_tickers,
        "ranking_limit": ranked_limit,
    })
    evidence.append({"gate": "news_llm_candidate_non_influence", "status": "pass"})

    for item in packet.get("candidate_results", []):
        components = item.get("score_components") or {}
        weights = item.get("score_weights") or {}
        if components and weights:
            score = round(sum(float(components[key]) * float(weights[key]) for key in weights), 2)
            if score != item.get("internal_canslim_score"):
                raise ValidationError(f"Candidate score arithmetic failed for {item.get('ticker')}")
    evidence.append({"gate": "candidate_score_arithmetic", "status": "pass"})
    expected_non_influence = llm_non_influence_record(packet)
    if packet.get("llm_non_influence") != expected_non_influence:
        raise ValidationError("Cross-market LLM non-influence invariant failed")
    evidence.append({
        "gate": "llm_non_influence",
        "status": "pass",
        "decision_outputs_sha256": expected_non_influence["decision_outputs_sha256"],
    })
    return evidence
