from __future__ import annotations

from typing import Any

from .core import ValidationError


REQUIRED_INDEXES = ("SPY", "QQQ", "IWM")


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _index_evidence(item: dict[str, Any], generated_at: Any) -> dict[str, Any]:
    return {
        "symbol": item.get("symbol"),
        "price_session": item.get("date"),
        "source_generated_at": generated_at,
        "close": _finite_number(item.get("close")),
        "ema21": _finite_number(item.get("ema21")),
        "sma50": _finite_number(item.get("sma50")),
        "sma200": _finite_number(item.get("sma200")),
        "distance_above_21d_pct": _finite_number(item.get("above21Percent")),
        "distance_above_50d_pct": _finite_number(item.get("above50Percent")),
        "short_term_trend": item.get("shortTerm"),
        "medium_term_trend": item.get("mediumTerm"),
        "long_term_trend": item.get("longTerm"),
        "raw_short_term_side": item.get("rawShortTerm"),
        "raw_medium_term_side": item.get("rawMediumTerm"),
        "raw_long_term_side": item.get("rawLongTerm"),
        "extension": item.get("extension"),
    }


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

    records_by_symbol = {str(item.get("symbol") or "").upper(): item for item in indexes}
    missing_indexes = [symbol for symbol in REQUIRED_INDEXES if symbol not in records_by_symbol]
    if missing_indexes:
        raise ValidationError(f"Dashboard Market Gauge lacks required index evidence: {missing_indexes}")
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        raise ValidationError("Dashboard Market Gauge generated timestamp is unavailable")
    index_evidence = [_index_evidence(records_by_symbol[symbol], generated_at) for symbol in REQUIRED_INDEXES]
    required_numeric = ("close", "ema21", "sma50", "sma200", "distance_above_21d_pct", "distance_above_50d_pct")
    required_text = ("short_term_trend", "medium_term_trend", "long_term_trend", "extension")
    for item in index_evidence:
        missing_fields = [field for field in required_numeric if item.get(field) is None]
        missing_fields.extend(field for field in required_text if not item.get(field))
        if missing_fields:
            raise ValidationError(
                f"Dashboard Market Gauge {item['symbol']} evidence is incomplete: {missing_fields}"
            )
    component_evidence = [
        {
            "label": item.get("label"),
            "state": item.get("state"),
            "detail": item.get("detail"),
            "previous_state": item.get("previousState"),
            "pending_state": item.get("pendingState"),
        }
        for item in components
    ]

    evidence = [
        f"Dashboard Market Gauge posture {posture} from {len(indexes)} exact-session index records.",
        *[
            f"{item.get('label')}: {item.get('state')} ({item.get('detail')})"
            for item in components
        ],
        *[
            f"{item['symbol']}: {item.get('short_term_trend')}/{item.get('medium_term_trend')}/{item.get('long_term_trend')} trend; extension {item.get('extension')}; price session {item.get('price_session')}."
            for item in index_evidence
        ],
    ]
    missing = [
        "index distribution-day counts and unique distribution sessions",
        "follow-through-day status",
        "official NYSE/Nasdaq breadth",
        "portfolio breakout success feedback",
    ]
    return {
        "source_timestamps": {
            "dashboard_market_gauge_generated_at": generated_at,
            **{f"dashboard_market_gauge_{item['symbol'].lower()}_session": item["price_session"] for item in index_evidence},
        },
        "manifests": [
            {
                "kind": "dashboard_market_gauge",
                "session_date": session_date,
                "universe": payload.get("universe"),
                "providers": payload.get("providers"),
                "source_generated_at": generated_at,
            }
        ],
        "market_regime": {
            "classification": "INSUFFICIENT_EVIDENCE",
            "confidence": "low",
            "dashboard_market_gauge_posture": posture,
            "dashboard_market_gauge_score": payload.get("overall_score"),
            "dashboard_market_gauge_generated_at": generated_at,
            "dashboard_market_gauge_components": component_evidence,
            "dashboard_market_gauge_indexes": index_evidence,
            "dashboard_market_gauge_providers": payload.get("providers") or [],
            "dashboard_market_gauge_universe": payload.get("universe") or {},
            "dashboard_market_gauge_limitations": payload.get("limitations") or [],
            "trend_extension_posture": {
                "index_count": len(index_evidence),
                "extension_counts": {
                    state: sum(item.get("extension") == state for item in index_evidence)
                    for state in ("Normal", "Caution", "Extended")
                },
                "scope": "SPY, QQQ, and IWM exact-session trend and extension evidence from the frozen dashboard gauge.",
            },
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
