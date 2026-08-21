from __future__ import annotations

from pathlib import Path
from typing import Any

from .core import ValidationError


UNVERIFIED_PIVOT_GATES = [
    "prior_uptrend",
    "conventional_base_type",
    "base_duration",
    "base_depth",
    "base_stage",
    "handle_quality_where_applicable",
    "weekly_structure",
    "volume_contraction",
    "exact_pivot_price",
    "prebreakout_containment",
]


def _pivot_verification(base: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
    gates = base.get("gates") or {}
    structural_statuses = {
        name: (gates.get(name) or {}).get("status")
        for name in UNVERIFIED_PIVOT_GATES
        if name != "exact_pivot_price"
    }
    structural_statuses.update(
        {
            name: (gate or {}).get("status")
            for name, gate in gates.items()
            if name != "exact_pivot_price" and name not in structural_statuses
        }
    )
    handle_names = [name for name in gates if name.startswith("handle_")]
    structural_statuses["handle_quality_where_applicable"] = (
        "PASS" if not handle_names or all((gates.get(name) or {}).get("status") == "PASS" for name in handle_names)
        else "FAIL"
    )
    missing = [
        name
        for name, status in structural_statuses.items()
        if status != "PASS"
    ]
    if base.get("pivot_price") is None or base.get("pivot_status") != "VERIFIED_ALGORITHMIC_PIVOT":
        missing.append("exact_pivot_price")
    status = "verified" if not missing else "unverified"
    return status, sorted(set(missing)), structural_statuses


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


def _atr_at_entry(history: list[dict[str, Any]], entry_date: str, periods: int = 14) -> float | None:
    eligible = [row for row in history if row.get("date") and row["date"] <= entry_date]
    if len(eligible) < periods + 1:
        return None
    true_ranges = []
    for previous, current in zip(eligible, eligible[1:]):
        if any(current.get(field) is None for field in ["high", "low"]) or previous.get("close") is None:
            return None
        high = float(current["high"])
        low = float(current["low"])
        previous_close = float(previous["close"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return round(sum(true_ranges[-periods:]) / periods, 6)


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
        full_history = record.get("price_history") or []
        position_history = [
            row
            for row in full_history
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
        atr_at_entry = _atr_at_entry(full_history, entry_date) if entry_date else None
        position["price_history"] = full_history
        position["atr_at_entry"] = atr_at_entry
        position.update(
            {
                "atr_current": metrics.get("atr14"),
                "atr": atr_at_entry or position.get("atr") or metrics.get("atr14"),
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
    all_records = chart_payload.get("records") or []
    records = [
        record
        for record in all_records
        if (record.get("metrics") or {}).get("asset_class", "EQUITY") == "EQUITY"
    ]
    if not records:
        return {
            "status": "insufficient_evidence",
            "verified_symbols": 0,
            "excluded_non_equity_instruments": len(all_records),
        }

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
        "price_history_provider": chart_payload.get("chart_data_source"),
        "price_history_endpoint": chart_payload.get("chart_data_endpoint"),
        "live_quote_substitution": (chart_payload.get("chart_data_policy") or {}).get("live_quote_substitution"),
        "verified_symbols": total,
        "excluded_non_equity_instruments": len(all_records) - total,
        "above_21d_pct": round(above_21 / total * 100, 1),
        "above_50d_pct": round(above_50 / total * 100, 1),
        "above_200d_pct": round(above_200 / total * 100, 1),
        "rs_rising_pct": round(rs_rising / total * 100, 1),
        "positive_accumulation_pct": round(accumulation / total * 100, 1),
        "chart_review_priority_count": review_priority,
    }


def _clamp(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 2)


def _positive_points(value: Any, target: float, maximum: float) -> float:
    if value is None:
        return 0.0
    return max(0.0, min(maximum, float(value) / target * maximum))


def _candidate_component_scores(
    metrics: dict[str, Any],
    quarterly: dict[str, Any],
    annual: dict[str, Any],
    relative: dict[str, Any],
    volume: dict[str, Any],
    base: dict[str, Any],
    earnings: dict[str, Any],
) -> tuple[dict[str, float], list[str], list[str]]:
    """Score only sourced evidence; unavailable inputs contribute zero."""
    eps_growth = quarterly.get("eps_growth_pct")
    sales_growth = quarterly.get("revenue_growth_pct")
    annual_eps = annual.get("annual_eps_growth_pct")
    annual_positive = annual.get("three_year_positive_eps_growth_count")
    annual_observations = annual.get("three_year_eps_growth_observations")
    fundamental = _positive_points(eps_growth, 25, 40) + _positive_points(sales_growth, 25, 20)
    fundamental += _positive_points(annual_eps, 25, 25)
    if annual_positive is not None and annual_observations:
        fundamental += min(15.0, float(annual_positive) / float(annual_observations) * 15.0)

    rs_change = relative.get("change_21d_pct")
    pct_from_high = metrics.get("pct_from_52w_high")
    rs_group = 0.0
    if relative.get("status") == "VERIFIED":
        rs_group += {"RISING": 25.0, "FLAT": 10.0, "FALLING": 0.0}.get(relative.get("trend_21d"), 0.0)
        rs_group += _positive_points(rs_change, 10, 25)
        rs_group += 20.0 if relative.get("new_high_52w") is True else 0.0
        if pct_from_high is not None:
            rs_group += max(0.0, min(15.0, (10.0 + float(pct_from_high)) / 10.0 * 15.0))
    # The remaining 15 points require a sourced industry-leadership rank, which
    # the current FMP enrichment does not provide.

    current = metrics.get("current_price")
    sma21 = metrics.get("sma21")
    sma50 = metrics.get("sma50")
    sma200 = metrics.get("sma200")
    technical = 0.0
    if current is not None and sma21 is not None and float(current) > float(sma21):
        technical += 10.0
    if current is not None and sma50 is not None and float(current) > float(sma50):
        technical += 15.0
    if current is not None and sma200 is not None and float(current) > float(sma200):
        technical += 15.0
    if None not in (sma21, sma50, sma200) and float(sma21) > float(sma50) > float(sma200):
        technical += 15.0
    if base.get("status") == "VERIFIED_ALGORITHMIC_PIVOT":
        technical += 35.0
    elif base.get("status") == "BUILDING":
        technical += 20.0
    if base.get("status") in {"VERIFIED_ALGORITHMIC_PIVOT", "BUILDING"}:
        weeks = base.get("base_length_weeks")
        depth = base.get("base_depth_pct")
        if weeks is not None and depth is not None and 5 <= float(weeks) <= 35 and 3 <= float(depth) <= 35:
            technical += 10.0
        distance = base.get("candidate_resistance_distance_pct")
        if distance is not None and -8 <= float(distance) <= 2:
            technical += 10.0
    if metrics.get("quantitative_gate") == "CHART_REVIEW_PRIORITY":
        technical += 5.0

    supply = 0.0
    if volume.get("status") == "VERIFIED":
        rel_volume = metrics.get("relative_volume")
        ratio = volume.get("up_down_volume_ratio_20")
        if rel_volume is not None:
            supply += 30.0 if float(rel_volume) >= 1.5 else 20.0 if float(rel_volume) >= 1.0 else 5.0
        if ratio is not None:
            supply += 35.0 if float(ratio) >= 1.5 else 20.0 if float(ratio) >= 1.0 else 0.0
        supply += {"POSITIVE": 35.0, "NEUTRAL": 15.0, "NEGATIVE": 0.0}.get(
            volume.get("accumulation_distribution_estimate"), 0.0
        )

    new = 0.0
    if relative.get("new_high_52w") is True:
        new += 40.0
    if base.get("status") == "VERIFIED_ALGORITHMIC_PIVOT":
        new += 35.0
    resistance_distance = base.get("candidate_resistance_distance_pct")
    if resistance_distance is not None and -8 <= float(resistance_distance) <= 2:
        new += 40.0

    available_dimensions = []
    if any(value is not None for value in (eps_growth, sales_growth, annual_eps, annual_observations)):
        available_dimensions.append("C_A_fundamentals")
    if relative.get("status") == "VERIFIED":
        available_dimensions.append("relative_strength")
    if all(value is not None for value in (current, sma50, sma200)):
        available_dimensions.append("technical_trend")
    if volume.get("status") == "VERIFIED":
        available_dimensions.append("supply_demand")
    if relative.get("new_high_52w") is not None or resistance_distance is not None:
        available_dimensions.append("new_or_proximity_context")

    missing = []
    for field, value in (
        ("latest_quarter_eps_growth", eps_growth),
        ("latest_quarter_sales_growth", sales_growth),
        ("annual_eps_growth", annual_eps),
        ("annual_eps_growth_consistency", annual_observations),
    ):
        if value is None:
            missing.append(field)
    missing.extend(
        [
            "prior_three_quarter_eps_sales_growth",
            "estimates_and_revisions",
            "margin_trend",
            "industry_group_rank",
            "institutional_sponsorship",
        ]
    )
    if relative.get("status") != "VERIFIED":
        missing.append("relative_strength_history")
    if volume.get("status") != "VERIFIED":
        missing.append("volume_confirmation")
    if earnings.get("earnings_status") != "VERIFIED":
        missing.append("earnings_date_risk")
    if metrics.get("avg_dollar_volume_50") is None:
        missing.append("average_dollar_volume")
    if any(value is None for value in (current, sma50, sma200)):
        missing.append("long_term_trend_alignment")
    return (
        {
            "fundamental_quality": _clamp(fundamental),
            "relative_strength_group": _clamp(rs_group),
            "technical_setup": _clamp(technical),
            "accumulation_supply": _clamp(supply),
            "new_catalyst": _clamp(new),
        },
        sorted(set(missing)),
        available_dimensions,
    )


def derive_candidates_from_chart(chart_payload: dict[str, Any], chart_dir: Path | None = None) -> list[dict[str, Any]]:
    candidates = []
    seen = set()
    manifest_records = (chart_payload.get("source_manifest") or {}).get("records") or {}
    for record in chart_payload.get("records") or []:
        metrics = record.get("metrics") or {}
        ticker = str(metrics.get("ticker") or "").upper()
        if not ticker or ticker in seen:
            continue
        technical = record.get("technical_context") or {}
        fmp = record.get("fmp") or {}
        quarterly = fmp.get("quarterly_growth") or {}
        annual = fmp.get("annual_growth") or {}
        relative = technical.get("relative_strength") or {}
        volume = technical.get("volume") or {}
        base = technical.get("base_analysis") or {}
        sources = (manifest_records.get(ticker) or {}).get("sources") or record.get("sources") or []
        source_types = {str(item.get("source_type")) for item in sources if item.get("source_type")}
        origin = "open_position" if "PORTFOLIO" in source_types else "watchlist" if "BRANDENS_WATCHLIST" in source_types else "scanner"
        current = metrics.get("current_price")
        earnings = fmp.get("earnings") or {}
        components, missing_evidence, available_dimensions = _candidate_component_scores(
            metrics, quarterly, annual, relative, volume, base, earnings
        )
        pivot_status, pivot_missing, pivot_gate_statuses = _pivot_verification(base)
        market_surge_sources = [item for item in sources if item.get("source_type") in {"STANDARD_MARKETSURGE", "BRANDENS_WATCHLIST"}]
        profile = fmp.get("profile") or {}
        candidate = {
                "ticker": ticker,
                "origin": origin,
                "origin_categories": sorted(source_types),
                "market_surge_candidate": bool(market_surge_sources),
                "is_current_open_position": "PORTFOLIO" in source_types,
                "asset_class": metrics.get("asset_class") or "EQUITY",
                "chart_evidence_status": "VERIFIED",
                "scoring_evidence_version": "canslim_setup_evidence_v1",
                "pivot_verification_status": pivot_status,
                "pivot_structure_verification_status": "VERIFIED" if not [
                    name for name in pivot_missing if name not in {"exact_pivot_price", "breakout_volume_confirmation"}
                ] else "UNVERIFIED",
                "pivot_gate_statuses": pivot_gate_statuses,
                "exact_pivot_price": base.get("pivot_price"),
                "pivot_basis": base.get("pivot_basis"),
                "buy_zone_upper_bound": base.get("buy_zone_upper_bound"),
                "early_entry_verification_status": base.get("early_entry_verification_status") or "unverified",
                "early_entry_price": base.get("early_entry_price"),
                "breakout_volume_confirmation": base.get("breakout_volume_confirmation"),
                "breakout_status": base.get("breakout_status"),
                "breakout_date": base.get("breakout_date"),
                "breakout_volume_ratio_50d": base.get("breakout_volume_ratio_50d"),
                "inside_buy_zone": bool(base.get("inside_buy_zone")),
                "extended": bool(base.get("extended")),
                "fundamental_quality_score": components["fundamental_quality"],
                "relative_strength_group_score": components["relative_strength_group"],
                "technical_setup_score": components["technical_setup"],
                "accumulation_supply_score": components["accumulation_supply"],
                "new_catalyst_score": components["new_catalyst"],
                "available_dimensions": available_dimensions,
                "missing_evidence": missing_evidence,
                "source_labels": [item.get("label") for item in sources],
                "source_evidence": [
                    {
                        "source_type": item.get("source_type"),
                        "label": item.get("label"),
                        "pdf_page": item.get("pdf_page"),
                        "rank": item.get("rank"),
                    }
                    for item in sources
                ],
                "company_name": profile.get("company_name"),
                "sector": profile.get("sector"),
                "industry": profile.get("industry"),
                "market_cap": profile.get("market_cap"),
                "quarterly_eps_growth_pct": quarterly.get("eps_growth_pct"),
                "quarterly_sales_growth_pct": quarterly.get("revenue_growth_pct"),
                "annual_eps_growth_pct": annual.get("annual_eps_growth_pct"),
                "annual_positive_eps_growth_count": annual.get("three_year_positive_eps_growth_count"),
                "annual_eps_growth_observations": annual.get("three_year_eps_growth_observations"),
                "industry_group_rank": None,
                "institutional_sponsorship_status": "UNAVAILABLE",
                "current_price": current,
                "sma21": metrics.get("sma21"),
                "sma50": metrics.get("sma50"),
                "sma200": metrics.get("sma200"),
                "candidate_resistance": base.get("candidate_resistance"),
                "candidate_resistance_distance_pct": base.get("candidate_resistance_distance_pct"),
                "base_candidate_status": base.get("status") or "INSUFFICIENT_EVIDENCE",
                "base_length_weeks": base.get("base_length_weeks"),
                "base_depth_pct": base.get("base_depth_pct"),
                "pattern_type": base.get("pattern_type") or "UNKNOWN",
                "pattern_confidence": base.get("confidence") or "LOW",
                "pattern_confidence_score": base.get("confidence_score"),
                "pattern_algorithm_version": base.get("algorithm_version"),
                "pattern_policy_version": base.get("policy_version"),
                "base_start": base.get("base_start"),
                "base_end": base.get("base_end"),
                "left_side_high": base.get("left_side_high"),
                "base_low": base.get("base_low"),
                "handle": base.get("handle"),
                "pattern_gates": base.get("gates") or {},
                "pattern_evidence_bars": base.get("evidence_bars") or {},
                "invalidation_support": base.get("invalidation_support"),
                "pattern_reason": base.get("reason"),
                "pivot_missing_evidence": pivot_missing,
                "pct_from_52w_high": metrics.get("pct_from_52w_high"),
                "relative_volume": metrics.get("relative_volume"),
                "up_down_volume_ratio_20": volume.get("up_down_volume_ratio_20"),
                "accumulation_distribution_estimate": volume.get("accumulation_distribution_estimate"),
                "volume_evidence_status": volume.get("status") or "INSUFFICIENT_EVIDENCE",
                "average_dollar_volume": metrics.get("avg_dollar_volume_50"),
                "quantitative_gate": metrics.get("quantitative_gate"),
                "gate_reasons": metrics.get("gate_reasons") or [],
                "rs_trend": relative.get("trend_21d"),
                "rs_change_21d_pct": relative.get("change_21d_pct"),
                "rs_new_high_52w": relative.get("new_high_52w"),
                "earnings_date": earnings.get("earnings_date"),
                "days_to_earnings": earnings.get("days_to_earnings"),
                "earnings_status": earnings.get("earnings_status"),
                "setup_pattern_state": f"{base.get('pattern_type') or 'UNKNOWN'} | {str(base.get('status') or 'INSUFFICIENT_EVIDENCE').replace('_', ' ')}",
            }
        chart_path = chart_dir / "charts" / f"{metrics.get('ticker')}_daily.png" if chart_dir else None
        if chart_path and chart_path.exists():
            from .utils import sha256_file

            candidate["daily_chart_asset"] = {
                "file": f"charts/{metrics.get('ticker')}_daily.png",
                "sha256": sha256_file(chart_path),
            }
        pattern_chart_path = chart_dir / "charts" / f"{metrics.get('ticker')}_pattern.png" if chart_dir else None
        if pattern_chart_path and pattern_chart_path.exists():
            from .utils import sha256_file

            candidate["pattern_chart_asset"] = {
                "file": f"charts/{metrics.get('ticker')}_pattern.png",
                "sha256": sha256_file(pattern_chart_path),
            }
        candidates.append(candidate)
        seen.add(ticker)
    requested = {str(value).upper() for value in chart_payload.get("requested_tickers") or []}
    for ticker in sorted(requested - seen):
        sources = (manifest_records.get(ticker) or {}).get("sources") or []
        source_types = {str(item.get("source_type")) for item in sources if item.get("source_type")}
        origin = "open_position" if "PORTFOLIO" in source_types else "watchlist" if "BRANDENS_WATCHLIST" in source_types else "scanner"
        candidates.append(
            {
                "ticker": ticker,
                "origin": origin,
                "origin_categories": sorted(source_types),
                "market_surge_candidate": any(item.get("source_type") in {"STANDARD_MARKETSURGE", "BRANDENS_WATCHLIST"} for item in sources),
                "is_current_open_position": "PORTFOLIO" in source_types,
                "asset_class": "FOREX" if "/" in ticker else "EQUITY",
                "chart_evidence_status": "INSUFFICIENT_EVIDENCE",
                "scoring_evidence_version": "canslim_setup_evidence_v1",
                "pivot_verification_status": "unverified",
                "pivot_structure_verification_status": "UNVERIFIED",
                "pivot_gate_statuses": {name: None for name in UNVERIFIED_PIVOT_GATES if name not in {"exact_pivot_price", "breakout_volume_confirmation"}},
                "exact_pivot_price": None,
                "early_entry_verification_status": "unverified",
                "early_entry_price": None,
                "breakout_volume_confirmation": None,
                "breakout_status": "INSUFFICIENT_EVIDENCE",
                "breakout_date": None,
                "breakout_volume_ratio_50d": None,
                "inside_buy_zone": False,
                "extended": False,
                "fundamental_quality_score": 0,
                "relative_strength_group_score": 0,
                "technical_setup_score": 0,
                "accumulation_supply_score": 0,
                "new_catalyst_score": 0,
                "available_dimensions": [],
                "missing_evidence": [
                    "current_session_chart_history",
                    "C_A_fundamentals",
                    "relative_strength_history",
                    "volume_confirmation",
                    "industry_group_rank",
                    "institutional_sponsorship",
                ],
                "source_labels": [item.get("label") for item in sources],
                "source_evidence": [
                    {
                        "source_type": item.get("source_type"),
                        "label": item.get("label"),
                        "pdf_page": item.get("pdf_page"),
                        "rank": item.get("rank"),
                    }
                    for item in sources
                ],
                "base_candidate_status": "INSUFFICIENT_EVIDENCE",
                "setup_pattern_state": "INSUFFICIENT EVIDENCE",
                "pattern_chart_asset": None,
                "pivot_missing_evidence": ["current_session_chart_history", *UNVERIFIED_PIVOT_GATES],
                "chart_error": (chart_payload.get("errors") or {}).get(ticker),
            }
        )
    return candidates
