from __future__ import annotations

from typing import Any
from pathlib import Path

from .core import ValidationError
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

    allowed_origins = {"scanner", "watchlist", "open_position", None}
    bad_origins = [item for item in packet.get("candidate_results", []) if item.get("origin") not in allowed_origins]
    if bad_origins:
        raise ValidationError(f"Candidate origin audit failed: {bad_origins}")
    evidence.append({"gate": "candidate_origins", "status": "pass"})

    allowed_actions = {"HOLD", "ADD", "REDUCE", "EXIT", "REPAIR", "INSUFFICIENT_EVIDENCE"}
    action_sections = ["sell_rule_results", "candidate_results", "shakeout_results"]
    bad_actions: list[dict[str, Any]] = []
    for section in action_sections:
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
        if item.get("classification") in {"BUY_NOW", "EARLY_ENTRY"} and item.get("action") != "ADD":
            raise ValidationError(f"Actionable candidate is not mapped to ADD: {item.get('ticker')}")
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
        if item.get("origin") == "watchlist"
    }
    if watchlist_tickers != watchlist_results:
        raise ValidationError("Watchlist/result set relationship failed")
    evidence.append({"gate": "set_relationships", "status": "pass"})
    evidence.append({"gate": "complete_watchlist_results", "status": "pass", "ticker_count": len(watchlist_tickers)})

    for item in packet.get("candidate_results", []):
        components = item.get("score_components") or {}
        weights = item.get("score_weights") or {}
        if components and weights:
            score = round(sum(float(components[key]) * float(weights[key]) for key in weights), 2)
            if score != item.get("internal_canslim_score"):
                raise ValidationError(f"Candidate score arithmetic failed for {item.get('ticker')}")
    evidence.append({"gate": "candidate_score_arithmetic", "status": "pass"})
    return evidence
