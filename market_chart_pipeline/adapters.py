from __future__ import annotations

from typing import Any

from .core import ValidationError


def normalize_portfolio_snapshot(snapshot: dict[str, Any], session_date: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    metadata = snapshot.get("metadata") or {}
    if metadata.get("latest_completed_market_session") != session_date:
        raise ValidationError("Portfolio snapshot session does not match the review session")
    if metadata.get("broker_import_complete") is not True:
        raise ValidationError("Portfolio snapshot broker import is incomplete")
    if metadata.get("price_data_as_of", "")[:10] != session_date:
        raise ValidationError("Portfolio snapshot prices are not from the review session")

    positions = []
    for item in snapshot.get("open_positions") or []:
        if str(item.get("side", "")).upper() != "LONG":
            continue
        positions.append(
            {
                "ticker": item.get("ticker"),
                "entry_price": item.get("average_entry"),
                "entry_date": item.get("entry_date"),
                "breakout_date": item.get("entry_date"),
                "current_price": item.get("current_price"),
                "stop_price": item.get("current_stop"),
                "pivot_price": item.get("pivot_price"),
                "atr": item.get("atr"),
                "highest_close_since_entry": item.get("highest_close_since_entry"),
                "earnings_date": item.get("earnings_date"),
                "source_trade_id": item.get("trade_id"),
            }
        )
    summary = snapshot.get("portfolio_summary") or {}
    portfolio_risk = {
        "account_value": metadata.get("account_value"),
        "gross_exposure_pct": summary.get("gross_exposure_pct"),
        "total_remaining_risk_pct": summary.get("total_remaining_risk_pct"),
        "source_position_count": summary.get("open_position_count"),
        "normalized_long_position_count": len(positions),
    }
    return positions, portfolio_risk


def _clamp(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 2)


def derive_candidates_from_chart(chart_payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    seen = set()
    for record in chart_payload.get("records") or []:
        metrics = record.get("metrics") or {}
        technical = record.get("technical_context") or {}
        fmp = record.get("fmp") or {}
        quarterly = fmp.get("quarterly_growth") or {}
        annual = fmp.get("annual_growth") or {}
        relative = technical.get("relative_strength") or {}
        volume = technical.get("volume") or {}
        base = technical.get("base_analysis") or {}
        sources = record.get("sources") or []
        source_types = {item.get("source_type") for item in sources}
        origin = "open_position" if "PORTFOLIO" in source_types else "watchlist" if "BRANDENS_WATCHLIST" in source_types else "scanner"
        pivot = base.get("pivot_price")
        current = metrics.get("current_price")
        distance = ((float(current) - float(pivot)) / float(pivot) * 100.0) if pivot and current else None
        fundamental = _clamp(
            50
            + float(quarterly.get("eps_growth_pct") or 0) * 0.6
            + float(quarterly.get("revenue_growth_pct") or 0) * 0.3
            + float(annual.get("annual_eps_growth_pct") or 0) * 0.1
        )
        rs_score = _clamp(50 + (25 if relative.get("trend_21d") == "RISING" else -10) + (25 if relative.get("new_high_52w") else 0))
        technical_score = _clamp(
            25
            + (25 if metrics.get("current_price") and metrics.get("sma50") and metrics["current_price"] > metrics["sma50"] else 0)
            + (25 if metrics.get("sma50") and metrics.get("sma200") and metrics["sma50"] > metrics["sma200"] else 0)
            + (25 if base.get("status") == "CANDIDATE_ONLY" else 0)
        )
        accumulation = _clamp(50 + (float(volume.get("up_down_volume_ratio_20") or 1) - 1) * 25)
        earnings = fmp.get("earnings") or {}
        catalyst = 75 if earnings.get("earnings_status") == "VERIFIED" else 25
        candidates.append(
            {
                "ticker": metrics.get("ticker"),
                "origin": origin,
                "pivot_verification_status": "verified" if base.get("pivot_status") == "VERIFIED" and pivot else "unverified",
                "inside_buy_zone": distance is not None and 0 <= distance <= 5,
                "extended": distance is not None and distance > 5,
                "fundamental_quality_score": fundamental,
                "relative_strength_group_score": rs_score,
                "technical_setup_score": technical_score,
                "accumulation_supply_score": accumulation,
                "new_catalyst_score": catalyst,
                "source_labels": [item.get("label") for item in sources],
            }
        )
        seen.add(str(metrics.get("ticker", "")).upper())
    manifest_records = (chart_payload.get("source_manifest") or {}).get("records") or {}
    for ticker in sorted(set(chart_payload.get("requested_tickers") or []) - seen):
        sources = (manifest_records.get(ticker) or {}).get("sources") or []
        source_types = {item.get("source_type") for item in sources}
        origin = "open_position" if "PORTFOLIO" in source_types else "watchlist" if "BRANDENS_WATCHLIST" in source_types else "scanner"
        candidates.append(
            {
                "ticker": ticker,
                "origin": origin,
                "pivot_verification_status": "unverified",
                "inside_buy_zone": False,
                "extended": False,
                "fundamental_quality_score": 0,
                "relative_strength_group_score": 0,
                "technical_setup_score": 0,
                "accumulation_supply_score": 0,
                "new_catalyst_score": 0,
                "source_labels": [item.get("label") for item in sources],
                "chart_error": (chart_payload.get("errors") or {}).get(ticker),
            }
        )
    return candidates
