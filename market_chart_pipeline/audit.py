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

        missing_sandboxes = [
            item.get("ticker")
            for item in packet.get("sell_rule_results", [])
            if item.get("position_snapshot", {}).get("sell_sandbox_status") != "verified"
            or not item.get("position_snapshot", {}).get("sell_sandbox_asset", {}).get("sha256")
        ]
        if missing_sandboxes:
            raise ValidationError(f"Strict sell-sandbox chart gate failed: {sorted(missing_sandboxes)}")
        evidence.append({"gate": "sell_sandbox_charts", "status": "pass"})

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
    evidence.append({"gate": "set_relationships", "status": "pass"})

    for item in packet.get("candidate_results", []):
        components = item.get("score_components") or {}
        weights = item.get("score_weights") or {}
        if components and weights:
            score = round(sum(float(components[key]) * float(weights[key]) for key in weights), 2)
            if score != item.get("internal_canslim_score"):
                raise ValidationError(f"Candidate score arithmetic failed for {item.get('ticker')}")
    evidence.append({"gate": "candidate_score_arithmetic", "status": "pass"})
    return evidence
