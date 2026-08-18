from __future__ import annotations

from typing import Any

from .core import ValidationError


def normalize_dashboard_market_gauge(payload: dict[str, Any], session_date: str) -> dict[str, Any]:
    """Translate the frozen dashboard gauge into conservative review evidence.

    The dashboard gauge is trend/extension evidence. It does not contain the
    distribution-day and follow-through-day history required to classify an
    O'Neil market regime or prescribe an exact exposure percentage.
    """
    if payload.get("schema_version") != "dashboard_market_gauge_v1":
        raise ValidationError("Dashboard Market Gauge schema is unsupported")
    if payload.get("session_date") != session_date:
        raise ValidationError("Dashboard Market Gauge session does not match the review session")
    posture = payload.get("overall_state")
    if posture not in {"Grow", "Neutral", "Protect"}:
        raise ValidationError("Dashboard Market Gauge posture is invalid")
    components = payload.get("components")
    indexes = payload.get("index_regimes")
    if not isinstance(components, list) or not components or not isinstance(indexes, list) or not indexes:
        raise ValidationError("Dashboard Market Gauge lacks component or index evidence")
    if any(item.get("date") != session_date for item in indexes):
        raise ValidationError("Dashboard Market Gauge contains non-session index evidence")

    evidence = [
        f"Dashboard Market Gauge posture {posture} from {len(indexes)} exact-session index records.",
        *[
            f"{item.get('label')}: {item.get('state')} ({item.get('detail')})"
            for item in components
        ],
    ]
    missing = [
        "index distribution-day counts and unique distribution sessions",
        "follow-through-day status",
        "official NYSE/Nasdaq breadth",
        "portfolio breakout success feedback",
    ]
    return {
        "source_timestamps": {"dashboard_market_gauge": payload.get("generated_at")},
        "manifests": [
            {
                "kind": "dashboard_market_gauge",
                "session_date": session_date,
                "universe": payload.get("universe"),
                "providers": payload.get("providers"),
            }
        ],
        "market_regime": {
            "classification": "INSUFFICIENT_EVIDENCE",
            "confidence": "low",
            "dashboard_market_gauge_posture": posture,
            "dashboard_market_gauge_score": payload.get("overall_score"),
            "evidence": evidence,
            "missing_evidence": missing,
            "interpretation": "The frozen Dashboard Market Gauge supplies exact-session trend and extension context only; it does not establish an O'Neil regime.",
        },
        "exposure_guidance": {
            "status": "INSUFFICIENT_EVIDENCE",
            "exact_exposure": "indeterminate",
            "dashboard_market_gauge_posture": posture,
            "confidence": "low",
            "evidence": evidence,
            "excluded_inputs": missing,
            "statement": "Exposure guidance excludes portfolio feedback. Exact exposure is indeterminate.",
        },
    }
