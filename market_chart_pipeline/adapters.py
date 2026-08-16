from __future__ import annotations

from pathlib import Path
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
                "shares": item.get("shares"),
                "market_value": item.get("market_value"),
                "position_weight_pct": item.get("position_weight_pct"),
                "unrealized_pnl": item.get("unrealized_pnl"),
                "open_r_multiple": item.get("open_r_multiple"),
                "planned_risk_dollars": item.get("planned_risk_dollars"),
                "remaining_risk_to_stop_dollars": item.get("remaining_risk_to_stop_dollars"),
                "take_profit": item.get("take_profit"),
                "setup": item.get("setup"),
                "grade": item.get("grade"),
                "setup_criteria_score": item.get("setup_criteria_score"),
                "setup_criteria_max": item.get("setup_criteria_max"),
                "data_warnings": item.get("data_warnings") or [],
            }
        )
    summary = snapshot.get("portfolio_summary") or {}
    portfolio_risk = {
        "account_value": metadata.get("account_value"),
        "gross_exposure_pct": summary.get("gross_exposure_pct"),
        "total_remaining_risk_pct": summary.get("total_remaining_risk_pct"),
        "source_position_count": summary.get("open_position_count"),
        "normalized_long_position_count": len(positions),
        "gross_exposure_dollars": summary.get("gross_exposure_dollars"),
        "total_open_pnl": summary.get("total_open_pnl"),
        "total_initial_risk": summary.get("total_initial_risk"),
        "total_remaining_risk_to_stops": summary.get("total_remaining_risk_to_stops"),
        "positions_missing_stops": summary.get("positions_missing_stops"),
        "price_as_of": metadata.get("price_data_as_of") or metadata.get("price_as_of"),
        "portfolio_as_of": metadata.get("portfolio_data_as_of"),
        "price_source": metadata.get("price_source"),
    }
    return positions, portfolio_risk


def enrich_positions_from_charts(
    positions: list[dict[str, Any]], chart_payload: dict[str, Any], chart_dir: Path
) -> list[dict[str, Any]]:
    """Add current-session technical evidence without promoting visual pivots."""
    records = {
        str(record.get("metrics", {}).get("ticker", "")).upper(): record
        for record in chart_payload.get("records") or []
    }
    enriched = []
    for original in positions:
        position = dict(original)
        ticker = str(position.get("ticker", "")).upper()
        record = records.get(ticker)
        if not record:
            enriched.append(position)
            continue
        metrics = record.get("metrics") or {}
        technical = record.get("technical_context") or {}
        fmp = record.get("fmp") or {}
        profile = fmp.get("profile") or {}
        earnings = fmp.get("earnings") or {}
        volume = technical.get("volume") or {}
        relative = technical.get("relative_strength") or {}
        entry_date = str(position.get("entry_date") or "")
        position_history = [
            row
            for row in record.get("price_history") or []
            if row.get("date") and row["date"] >= entry_date
        ] if entry_date else []
        closes = [float(row["close"]) for row in position_history if row.get("close") is not None]
        if closes:
            position["highest_close_since_entry"] = max(closes)
            position["trading_days_since_breakout"] = max(0, len(closes) - 1)
        pivot = position.get("pivot_price")
        if pivot and position_history:
            threshold = float(pivot) * 1.20
            first_rapid_index = next(
                (index for index, row in enumerate(position_history) if float(row.get("close") or 0) >= threshold),
                None,
            )
            if first_rapid_index is not None:
                position["trading_days_to_rapid_advance"] = first_rapid_index
        position.update(
            {
                "atr": position.get("atr") or metrics.get("atr14"),
                "earnings_date": position.get("earnings_date") or earnings.get("earnings_date"),
                "company_name": profile.get("company_name"),
                "sector": profile.get("sector"),
                "sma21": metrics.get("sma21"),
                "sma50": metrics.get("sma50"),
                "sma200": metrics.get("sma200"),
                "pct_from_sma50": metrics.get("pct_from_sma50"),
                "pct_from_52w_high": metrics.get("pct_from_52w_high"),
                "relative_strength_trend": relative.get("trend_21d"),
                "accumulation_distribution": volume.get("accumulation_distribution_estimate"),
                "chart_gate": metrics.get("quantitative_gate"),
                "chart_gate_reasons": metrics.get("gate_reasons") or [],
            }
        )
        chart_path = chart_dir / "charts" / f"{ticker}_daily.png"
        if chart_path.exists():
            from .utils import sha256_file

            position["daily_chart_asset"] = {
                "file": f"charts/{ticker}_daily.png",
                "sha256": sha256_file(chart_path),
            }
        enriched.append(position)
    return enriched


def derive_market_breadth(chart_payload: dict[str, Any]) -> dict[str, Any]:
    records = chart_payload.get("records") or []
    if not records:
        return {"status": "insufficient_evidence", "verified_symbols": 0}

    def count(predicate) -> int:
        return sum(1 for record in records if predicate(record.get("metrics") or {}, record.get("technical_context") or {}))

    total = len(records)
    above_21 = count(lambda m, _t: m.get("current_price") is not None and m.get("sma21") is not None and m["current_price"] > m["sma21"])
    above_50 = count(lambda m, _t: m.get("current_price") is not None and m.get("sma50") is not None and m["current_price"] > m["sma50"])
    above_200 = count(lambda m, _t: m.get("current_price") is not None and m.get("sma200") is not None and m["current_price"] > m["sma200"])
    rs_rising = count(lambda _m, t: (t.get("relative_strength") or {}).get("trend_21d") == "RISING")
    accumulation = count(lambda _m, t: (t.get("volume") or {}).get("accumulation_distribution_estimate") == "POSITIVE")
    review_priority = count(lambda m, _t: m.get("quantitative_gate") == "CHART_REVIEW_PRIORITY")
    return {
        "status": "partial_evidence",
        "scope": "MarketSurge-derived review universe; not the full exchange breadth",
        "verified_symbols": total,
        "above_21d_pct": round(above_21 / total * 100, 1),
        "above_50d_pct": round(above_50 / total * 100, 1),
        "above_200d_pct": round(above_200 / total * 100, 1),
        "rs_rising_pct": round(rs_rising / total * 100, 1),
        "positive_accumulation_pct": round(accumulation / total * 100, 1),
        "chart_review_priority_count": review_priority,
    }


def _clamp(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 2)


def derive_candidates_from_chart(chart_payload: dict[str, Any], chart_dir: Path | None = None) -> list[dict[str, Any]]:
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
        candidate = {
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
                "company_name": (fmp.get("profile") or {}).get("company_name"),
                "sector": (fmp.get("profile") or {}).get("sector"),
                "current_price": current,
                "candidate_resistance": base.get("candidate_resistance"),
                "candidate_resistance_distance_pct": base.get("candidate_resistance_distance_pct"),
                "pct_from_52w_high": metrics.get("pct_from_52w_high"),
                "relative_volume": metrics.get("relative_volume"),
                "average_dollar_volume": metrics.get("avg_dollar_volume_50"),
                "quantitative_gate": metrics.get("quantitative_gate"),
                "gate_reasons": metrics.get("gate_reasons") or [],
                "rs_trend": relative.get("trend_21d"),
                "earnings_date": earnings.get("earnings_date"),
            }
        chart_path = chart_dir / "charts" / f"{metrics.get('ticker')}_daily.png" if chart_dir else None
        if chart_path and chart_path.exists():
            from .utils import sha256_file

            candidate["daily_chart_asset"] = {
                "file": f"charts/{metrics.get('ticker')}_daily.png",
                "sha256": sha256_file(chart_path),
            }
        candidates.append(candidate)
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
