from __future__ import annotations

from typing import Any

from .core import ValidationError
from .utils import canonical_json, sha256_text


def audit_packet(packet: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    expected_hash = packet.get("packet_sha256")
    body = dict(packet)
    body.pop("packet_sha256", None)
    actual_hash = sha256_text(canonical_json(body))
    if expected_hash != actual_hash:
        raise ValidationError("Packet hash does not match canonical packet body")
    evidence.append({"gate": "packet_hash", "status": "pass", "sha256": actual_hash})

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

    for item in packet.get("candidate_results", []):
        components = item.get("score_components") or {}
        weights = item.get("score_weights") or {}
        if components and weights:
            score = round(sum(float(components[key]) * float(weights[key]) for key in weights), 2)
            if score != item.get("internal_canslim_score"):
                raise ValidationError(f"Candidate score arithmetic failed for {item.get('ticker')}")
    evidence.append({"gate": "candidate_score_arithmetic", "status": "pass"})
    return evidence
