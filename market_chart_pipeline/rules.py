from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .manifest import EQUITY_TICKER_RE, FX_PAIR_RE

ACTION_HOLD = "HOLD"
ACTION_ADD = "ADD"
ACTION_REDUCE = "REDUCE"
ACTION_EXIT = "EXIT"
ACTION_REPAIR = "REPAIR"
ACTION_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

CANDIDATE_ACTION_BUY = "BUY NOW"
CANDIDATE_ACTION_EARLY = "EARLY ENTRY"
CANDIDATE_ACTION_NEAR = "WATCH NEAR PIVOT"
CANDIDATE_ACTION_BUILDING = "WATCH / BUILDING"
CANDIDATE_ACTION_WAIT = "WAIT FOR CONFIRMATION"
CANDIDATE_ACTION_AVOID = "AVOID"
CANDIDATE_ACTION_INSUFFICIENT = "INSUFFICIENT EVIDENCE"
CANDIDATE_ACTION_OPEN_POSITION = "EXCLUDED - OPEN POSITION"
CANDIDATE_ACTION_NON_EQUITY = "EXCLUDED - NON-EQUITY"

_POSITION_SNAPSHOT_FIELDS = (
    "company_name", "sector", "entry_price", "entry_date", "current_price", "shares",
    "market_value", "position_weight_pct", "unrealized_pnl", "open_r_multiple",
    "stop_price", "remaining_risk_to_stop_dollars", "take_profit", "setup", "grade",
    "setup_criteria_score", "setup_criteria_max", "earnings_date", "sma21", "sma50",
    "sma200", "pct_from_sma50", "pct_from_52w_high", "relative_strength_trend",
    "accumulation_distribution", "chart_gate", "chart_gate_reasons", "daily_chart_asset",
    "highest_close_since_entry", "trading_days_since_breakout", "trading_days_to_rapid_advance",
    "atr", "atr_at_entry", "atr_current", "sell_sandbox_asset", "sell_sandbox_status",
    "sell_sandbox_error",
)


def _event(rule: str, status: str, priority: int, rationale: str, **values: Any) -> dict[str, Any]:
    return {
        "rule": rule,
        "status": status,
        "priority": priority,
        "rationale": rationale,
        "values": values,
    }


def _missing(position: dict[str, Any], fields: list[str]) -> list[str]:
    return [field for field in fields if position.get(field) is None]


def _position_snapshot(position: dict[str, Any]) -> dict[str, Any]:
    """Preserve immutable position evidence on every deterministic outcome path."""
    return {key: position.get(key) for key in _POSITION_SNAPSHOT_FIELDS}


def _pct(current: float, base: float) -> float:
    return ((current - base) / base) * 100.0


def _trading_days(start: str, end: str) -> int:
    cursor = date.fromisoformat(start) + timedelta(days=1)
    finish = date.fromisoformat(end)
    count = 0
    while cursor <= finish:
        if cursor.weekday() < 5:
            count += 1
        cursor += timedelta(days=1)
    return count


def evaluate_position(position: dict[str, Any], policy: dict[str, Any], session_date: str) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    critical_missing = _missing(position, ["ticker", "entry_price", "current_price"])
    ticker = position.get("ticker", "UNKNOWN")
    if critical_missing:
        return {
            "ticker": ticker,
            "trade_state": "UNKNOWN",
            "action": ACTION_INSUFFICIENT,
            "rationale": f"Missing critical position fields: {', '.join(critical_missing)}",
            "position_snapshot": _position_snapshot(position),
            "events": [_event("critical_evidence", "INSUFFICIENT_EVIDENCE", 0, "Critical inputs are unavailable", missing=critical_missing)],
        }

    entry = float(position["entry_price"])
    current = float(position["current_price"])
    gain_pct = _pct(current, entry)
    hard = policy["hard_rules"]
    loss_limit_price = entry * (1.0 - float(hard["max_initial_loss_pct"]) / 100.0)
    structural_stop = position.get("stop_price")
    atr = position.get("atr")
    atr_stop = entry - float(atr) * float(hard["atr_stop_multiple"]) if atr is not None else None
    stop_candidates = [loss_limit_price]
    if structural_stop is not None:
        stop_candidates.append(float(structural_stop))
    if atr_stop is not None:
        stop_candidates.append(atr_stop)
    initial_stop = max(stop_candidates)
    initial_risk = entry - initial_stop
    high = position.get("highest_close_since_entry") or position.get("highest_price_since_entry")
    trailing = policy["trailing"]
    high_gain_pct = _pct(float(high), entry) if high is not None else None
    gain_protection_active = bool(
        high_gain_pct is not None and high_gain_pct >= float(trailing["activation_gain_pct"])
    )
    protected_loss_floor = None
    if gain_protection_active:
        protected_loss_floor = entry * (1.0 - float(trailing["protected_loss_floor_pct"]) / 100.0)
        stop_candidates.append(protected_loss_floor)
    break_even_active = bool(
        high is not None
        and initial_risk > 0
        and float(high) >= entry + initial_risk * float(hard["break_even_after_r"])
    )
    break_even_event = None
    if break_even_active:
        stop_candidates.append(entry)
        break_even_event = _event(
            "break_even_protection",
            "ACTIVE",
            0,
            "The position reached the configured R multiple; the effective stop is no lower than entry",
            trigger_r=float(hard["break_even_after_r"]),
            initial_risk=round(initial_risk, 4),
        )
    effective_stop = max(stop_candidates)

    if current <= effective_stop:
        events.append(
            _event(
                "hard_capital_protection",
                "TRIGGERED",
                0,
                "Current price violated the effective stop; capital protection overrides all soft signals",
                current_price=current,
                effective_stop=round(effective_stop, 4),
                max_initial_loss_price=round(loss_limit_price, 4),
                atr_stop=round(atr_stop, 4) if atr_stop is not None else None,
                structural_stop=structural_stop,
                protected_loss_floor=round(protected_loss_floor, 4) if protected_loss_floor is not None else None,
            )
        )
        return {
            "ticker": ticker,
            "trade_state": "BROKEN",
            "action": ACTION_EXIT,
            "rationale": "Hard capital-protection stop was violated.",
            "position_snapshot": _position_snapshot(position),
            "events": events,
        }
    events.append(
        _event(
            "hard_capital_protection",
            "PASS",
            0,
            "Current price is above the effective stop",
            current_price=current,
            effective_stop=round(effective_stop, 4),
        )
    )
    if break_even_event:
        events.append(break_even_event)
    if high is None:
        events.append(
            _event(
                "seven_percent_gain_protection",
                "INSUFFICIENT_EVIDENCE",
                1,
                "Highest close since entry is unavailable",
            )
        )
    elif gain_protection_active:
        events.append(
            _event(
                "seven_percent_gain_protection",
                "ACTIVE",
                1,
                "A 7% advance activated the protected loss floor",
                highest_close_since_entry=float(high),
                highest_gain_pct=round(float(high_gain_pct), 2),
                protected_loss_floor=round(float(protected_loss_floor), 4),
            )
        )
    else:
        events.append(
            _event(
                "seven_percent_gain_protection",
                "NOT_TRIGGERED",
                1,
                "The position has not yet advanced 7% from entry",
                highest_gain_pct=round(float(high_gain_pct), 2),
                activation_gain_pct=float(trailing["activation_gain_pct"]),
            )
        )

    pivot = position.get("pivot_price")
    pivot_gain = None
    profit_zone_reached = False
    if pivot is not None:
        pivot_gain = _pct(current, float(pivot))
        zone = policy["profit_zone"]
        profit_zone_reached = pivot_gain >= float(zone["lower_pct_from_pivot"])
        if pivot_gain >= float(zone["upper_pct_from_pivot"]):
            events.append(_event("profit_zone", "UPPER_ZONE_REACHED", 2, "Price is above the upper profit-zone threshold", gain_from_pivot_pct=round(pivot_gain, 2)))
        elif pivot_gain >= float(zone["lower_pct_from_pivot"]):
            events.append(_event("profit_zone", "LOWER_ZONE_REACHED", 2, "Price is in the 20%-25% profit zone", gain_from_pivot_pct=round(pivot_gain, 2)))
        else:
            events.append(_event("profit_zone", "NOT_REACHED", 2, "Price has not reached the profit zone", gain_from_pivot_pct=round(pivot_gain, 2)))
    else:
        events.append(_event("profit_zone", "INSUFFICIENT_EVIDENCE", 2, "Pivot price is unavailable"))

    breakout_date = position.get("breakout_date") or position.get("entry_date")
    trading_days_held = None
    weeks_held = None
    if breakout_date:
        supplied_days = position.get("trading_days_since_breakout")
        trading_days_held = int(supplied_days) if supplied_days is not None else _trading_days(str(breakout_date), session_date)
        weeks_held = trading_days_held / 5.0
    rapid_active = False
    if breakout_date and pivot is not None:
        rapid = policy["rapid_advance"]
        trigger_days = position.get("trading_days_to_rapid_advance")
        if trigger_days is None and pivot_gain >= float(rapid["gain_pct"]) and trading_days_held <= int(rapid["max_trading_days"]):
            trigger_days = trading_days_held
        if trigger_days is not None and int(trigger_days) <= int(rapid["max_trading_days"]):
            rapid_active = weeks_held < float(rapid["minimum_hold_weeks"])
            events.append(
                _event(
                    "rapid_advance_hold",
                    "ACTIVE" if rapid_active else "SATISFIED",
                    1,
                    "20% advance within three weeks qualifies for the eight-week hold rule",
                    trading_days_to_20_pct=int(trigger_days),
                    trading_days_since_breakout=trading_days_held,
                    weeks_held=round(weeks_held, 2),
                    gain_from_pivot_pct=round(pivot_gain, 2),
                )
            )
        else:
            events.append(_event("rapid_advance_hold", "NOT_TRIGGERED", 1, "Rapid advance threshold was not met", trading_days_since_breakout=trading_days_held, gain_from_pivot_pct=round(pivot_gain, 2)))
    else:
        events.append(_event("rapid_advance_hold", "INSUFFICIENT_EVIDENCE", 1, "Breakout date or pivot price is unavailable"))

    if profit_zone_reached and weeks_held is not None:
        minimum_weeks = float(policy["profit_zone"]["minimum_hold_weeks"])
        events.append(
            _event(
                "profit_zone_minimum_hold",
                "ACTIVE" if weeks_held < minimum_weeks else "SATISFIED",
                2,
                "Profit-zone action waits for the configured minimum hold unless a harder rule fires",
                weeks_held=round(weeks_held, 2),
                minimum_hold_weeks=minimum_weeks,
            )
        )

    trail_triggered = False
    if high is not None and gain_protection_active:
        drawdown = ((float(high) - current) / float(high)) * 100.0
        threshold = float(policy["trailing"]["peak_drawdown_exit_pct"])
        trail_stop_price = float(high) * (1.0 - threshold / 100.0)
        status = "TRIGGERED" if drawdown >= threshold else "PASS"
        trail_triggered = status == "TRIGGERED"
        events.append(_event("peak_drawdown_trail", status, 1, "Measured decline from highest close since entry", drawdown_pct=round(drawdown, 2), threshold_pct=threshold, trail_stop_price=round(trail_stop_price, 4)))
    elif high is not None:
        drawdown = ((float(high) - current) / float(high)) * 100.0
        events.append(
            _event(
                "peak_drawdown_trail",
                "NOT_ACTIVE",
                1,
                "The 11% peak trail activates only after a 7% advance from entry",
                drawdown_pct=round(drawdown, 2),
                highest_gain_pct=round(float(high_gain_pct), 2),
            )
        )
    else:
        drawdown = None
        events.append(_event("peak_drawdown_trail", "INSUFFICIENT_EVIDENCE", 1, "Highest close since entry is unavailable"))

    patience_status = "NOT_EVALUATED"
    if breakout_date:
        patience = policy["patience"]
        if weeks_held >= float(patience["slow_leader_patience_weeks"]) and gain_pct < float(
            patience["minimum_gain_pct_for_patience"]
        ):
            patience_status = "TRIGGERED"
        elif weeks_held >= float(patience["eight_week_hold_weeks"]):
            patience_status = "REVIEW"
        else:
            patience_status = "PASS"
        events.append(
            _event(
                "eight_thirteen_week_patience",
                patience_status,
                4,
                "Evaluated opportunity cost at the configured eight- and thirteen-week checkpoints",
                weeks_held=round(weeks_held, 2),
                gain_pct=round(gain_pct, 2),
            )
        )
    else:
        events.append(
            _event(
                "eight_thirteen_week_patience",
                "INSUFFICIENT_EVIDENCE",
                4,
                "Breakout or entry date is unavailable",
            )
        )

    if rapid_active:
        action = ACTION_HOLD
        rationale = "Rapid-advance eight-week hold is active; hard capital protection remains enforced."
    elif trail_triggered:
        action = trailing["action"]
        rationale = "The 11% peak trail fired after the position had activated gain protection."
    elif profit_zone_reached and weeks_held is not None and weeks_held >= float(policy["profit_zone"]["minimum_hold_weeks"]):
        action = policy["profit_zone"]["action"]
        rationale = "The 20%-25% pivot profit zone is active after the minimum eight-week hold."
    elif patience_status == "TRIGGERED":
        action = ACTION_REDUCE
        rationale = "The thirteen-week patience threshold fired without the configured minimum progress."
    elif gain_pct < 0:
        action = ACTION_REPAIR
        rationale = "Position is below entry but above the hard stop."
    else:
        action = ACTION_HOLD
        rationale = "No deterministic sell rule requires action."

    trade_state = "ADVANCING" if gain_pct > 0 else "DEFENSIVE" if gain_pct < 0 else "FLAT"
    return {
        "ticker": ticker,
        "trade_state": trade_state,
        "action": action,
        "rationale": rationale,
        "gain_pct": round(gain_pct, 2),
        "position_snapshot": _position_snapshot(position),
        "events": events,
    }


def evaluate_shakeout(record: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    cfg = policy["shakeout"]
    ticker = record.get("ticker", "UNKNOWN")
    missing = _missing(record, ["prior_support_price", "current_price"])
    if missing:
        return {"ticker": ticker, "state": "INSUFFICIENT_EVIDENCE", "action": ACTION_INSUFFICIENT, "rationale": "Required shakeout evidence is missing.", "events": [_event("shakeout_evidence", "INSUFFICIENT_EVIDENCE", 1, "Missing shakeout inputs", missing=missing)]}
    support = float(record["prior_support_price"])
    current = float(record["current_price"])
    drawdown = ((support - current) / support) * 100.0
    days = int(record.get("days_since_break") or 0)
    volume_ratio = float(record.get("volume_ratio") or 0)
    reclaimed = current >= support * (1.0 + float(cfg["reentry_reclaim_pct"]) / 100.0)
    events = [_event("shakeout_watch", "WATCH" if drawdown >= float(cfg["watch_drawdown_pct"]) else "NO_WATCH", 2, "Evaluated support break depth", drawdown_pct=round(drawdown, 2))]
    if reclaimed and volume_ratio >= float(cfg["minimum_reclaim_volume_ratio"]):
        return {"ticker": ticker, "state": "REENTRY_READY", "action": ACTION_ADD, "rationale": "Price reclaimed support with sufficient volume.", "events": [*events, _event("reentry_reclaim", "TRIGGERED", 1, "Price reclaimed support with sufficient volume", volume_ratio=volume_ratio)]}
    if days > int(cfg["max_watch_days"]):
        return {"ticker": ticker, "state": "EXPIRED", "action": ACTION_REPAIR, "rationale": "The shakeout watch expired without a valid reclaim.", "events": [*events, _event("shakeout_expiry", "TRIGGERED", 1, "Shakeout watch exceeded maximum age", days_since_break=days)]}
    return {"ticker": ticker, "state": "WATCH", "action": ACTION_HOLD, "rationale": "The reclaim requirements are not yet satisfied.", "events": events}


def _candidate_market_permission(market_regime: dict[str, Any], ranking: dict[str, Any]) -> dict[str, Any]:
    classification = market_regime.get("classification")
    posture = market_regime.get("dashboard_market_gauge_posture")
    permitted = (
        classification in set(ranking.get("buying_permissive_internal_regimes") or [])
        and posture in set(ranking.get("buying_permissive_dashboard_postures") or [])
    )
    status = "PERMITTED" if permitted else "INSUFFICIENT_EVIDENCE" if classification == "INSUFFICIENT_EVIDENCE" else "NOT_PERMITTED"
    return {
        "status": status,
        "internal_regime": classification,
        "dashboard_market_gauge_posture": posture,
        "reason": (
            "Internal regime and frozen Dashboard Gauge are buying-permissive."
            if permitted
            else "A buying-permissive internal regime plus Grow Dashboard Gauge posture is not verified."
        ),
    }


def _candidate_confidence(candidate: dict[str, Any]) -> str:
    available = len(candidate.get("available_dimensions") or [])
    critical_missing = {
        "prior_three_quarter_eps_sales_growth",
        "estimates_and_revisions",
        "industry_group_rank",
        "institutional_sponsorship",
        "exact_pivot_price",
        "base_duration",
        "base_depth",
        "base_stage",
        "breakout_volume_confirmation",
    }
    missing = set(candidate.get("missing_evidence") or []) | set(candidate.get("pivot_missing_evidence") or [])
    if available >= 5 and not (missing & critical_missing):
        return "HIGH"
    if available >= 4 and len(missing & critical_missing) <= 2:
        return "MEDIUM"
    return "LOW"


def _candidate_trigger(candidate: dict[str, Any], action: str, ranking: dict[str, Any]) -> str:
    if action == CANDIDATE_ACTION_BUY and candidate.get("exact_pivot_price") is not None:
        return (
            f"Verified breakout through {float(candidate['exact_pivot_price']):.2f} with relative volume "
            f">= {float(ranking['minimum_breakout_relative_volume']):.2f}x."
        )
    if action == CANDIDATE_ACTION_EARLY and candidate.get("early_entry_price") is not None:
        return f"Verified early-entry trigger through {float(candidate['early_entry_price']):.2f} with confirmed volume."
    if candidate.get("pivot_verification_status") == "verified" and candidate.get("exact_pivot_price") is not None:
        return (
            f"Watch the verified algorithmic pivot at {float(candidate['exact_pivot_price']):.2f}; require a close through it "
            f"with breakout-day volume >= {float(ranking['minimum_breakout_relative_volume']):.2f}x the prior 50-day average."
        )
    resistance = candidate.get("candidate_resistance")
    if resistance is not None:
        return (
            f"Visually verify a proper base and exact pivot near {float(resistance):.2f}; then require a valid breakout "
            f"with relative volume >= {float(ranking['minimum_breakout_relative_volume']):.2f}x."
        )
    return "No verified entry trigger; establish a proper base, exact pivot, and confirming volume first."


def _candidate_risk(candidate: dict[str, Any]) -> str:
    parts = []
    invalidation = candidate.get("invalidation_support") or {}
    if invalidation.get("primary") is not None:
        parts.append(
            f"pattern invalidation below {float(invalidation['primary']):.2f} "
            f"({invalidation.get('basis') or 'algorithmic support'})"
        )
    if candidate.get("sma50") is not None:
        parts.append(f"technical deterioration below 50-day {float(candidate['sma50']):.2f}")
    if candidate.get("sma200") is not None:
        parts.append(f"major trend failure below 200-day {float(candidate['sma200']):.2f}")
    if candidate.get("earnings_date"):
        parts.append(f"earnings risk {candidate['earnings_date']}")
    if candidate.get("pivot_verification_status") != "verified":
        parts.append("no exact setup invalidation until the pivot/base is verified")
    return "; ".join(parts) or "Exact risk/invalidation is insufficient evidence."


def _why_ranked(components: dict[str, float], candidate: dict[str, Any]) -> str:
    labels = {
        "fundamental_quality": "C/A fundamentals",
        "relative_strength_group": "RS/group",
        "technical_setup": "technical setup",
        "accumulation_supply": "supply/demand",
        "new_catalyst": "new/proximity",
    }
    leaders = sorted(components, key=lambda key: (-components[key], key))[:3]
    evidence = ", ".join(f"{labels[key]} {components[key]:.0f}/100" for key in leaders)
    gate = candidate.get("quantitative_gate") or "gate unavailable"
    return f"Highest sourced components: {evidence}; technical screen {gate}."


def score_candidate(
    candidate: dict[str, Any],
    policy: dict[str, Any],
    market_regime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    weights = policy["candidate_scoring"]
    ranking = policy.get("candidate_ranking") or {}
    components = {
        "fundamental_quality": float(candidate.get("fundamental_quality_score") or 0),
        "relative_strength_group": float(candidate.get("relative_strength_group_score") or 0),
        "technical_setup": float(candidate.get("technical_setup_score") or 0),
        "accumulation_supply": float(candidate.get("accumulation_supply_score") or 0),
        "new_catalyst": float(candidate.get("new_catalyst_score") or 0),
    }
    total = sum(max(0.0, min(100.0, components[key])) * float(weight) for key, weight in weights.items())
    critical = [field for field in ["origin", "ticker", "pivot_verification_status"] if not candidate.get(field)]
    market_permission = _candidate_market_permission(market_regime or {}, ranking)
    missing = sorted(set(candidate.get("missing_evidence") or []) | set(candidate.get("pivot_missing_evidence") or []))
    if market_permission["status"] != "PERMITTED":
        missing.append("buying_permissive_market_regime")
    missing = sorted(set(missing))
    classification = "WAIT_FOR_CONFIRMATION"
    action = CANDIDATE_ACTION_WAIT
    rationale = "Setup evidence is incomplete; wait for a verified entry and confirming evidence."
    rejection_reasons: list[str] = []
    available_count = len(candidate.get("available_dimensions") or [])
    ranking_eligible = True
    if candidate.get("origin") not in {"scanner", "watchlist", "open_position"}:
        classification = "AVOID"
        action = CANDIDATE_ACTION_AVOID
        rationale = "Candidate origin is not allowed by policy."
        rejection_reasons.append("DISALLOWED_ORIGIN")
        ranking_eligible = False
    elif critical:
        classification = "INSUFFICIENT_EVIDENCE"
        action = CANDIDATE_ACTION_INSUFFICIENT
        rationale = f"Missing critical candidate fields: {', '.join(critical)}"
        rejection_reasons.append("MISSING_CRITICAL_FIELDS")
        ranking_eligible = False
    elif not (
        (candidate.get("asset_class") == "EQUITY" and EQUITY_TICKER_RE.fullmatch(str(candidate.get("ticker") or "")))
        or (candidate.get("asset_class") == "FOREX" and FX_PAIR_RE.fullmatch(str(candidate.get("ticker") or "")))
    ):
        classification = "INSUFFICIENT_EVIDENCE"
        action = CANDIDATE_ACTION_INSUFFICIENT
        rationale = "Ticker does not satisfy the validated equity or strict AAA/BBB identifier policy."
        rejection_reasons.append("MALFORMED_TICKER")
        ranking_eligible = False
    elif candidate.get("is_current_open_position") is True:
        classification = "PORTFOLIO_POSITION"
        action = CANDIDATE_ACTION_OPEN_POSITION
        rationale = "Current open positions are excluded from new-entry ranking and remain in sell-sandbox analysis."
        rejection_reasons.append("CURRENT_OPEN_POSITION")
        ranking_eligible = False
    elif candidate.get("market_surge_candidate") is not True:
        classification = "INSUFFICIENT_EVIDENCE"
        action = CANDIDATE_ACTION_INSUFFICIENT
        rationale = "Ticker did not originate from a validated MarketSurge PDF row."
        rejection_reasons.append("NOT_IN_MARKETSURGE_MANIFEST")
        ranking_eligible = False
    elif candidate.get("asset_class") != "EQUITY":
        classification = "NON_EQUITY"
        action = CANDIDATE_ACTION_NON_EQUITY
        rationale = "Only valid equity securities are eligible for the CANSLIM setup ranking."
        rejection_reasons.append("NON_EQUITY_INSTRUMENT")
        ranking_eligible = False
    elif candidate.get("chart_evidence_status") != "VERIFIED":
        classification = "INSUFFICIENT_EVIDENCE"
        action = CANDIDATE_ACTION_INSUFFICIENT
        rationale = "Exact-session chart and technical evidence are unavailable."
        rejection_reasons.append("CURRENT_SESSION_CHART_UNAVAILABLE")
        ranking_eligible = False
    elif available_count < int(ranking.get("minimum_available_dimensions", 3)):
        classification = "INSUFFICIENT_EVIDENCE"
        action = CANDIDATE_ACTION_INSUFFICIENT
        rationale = "Too few sourced CANSLIM/setup dimensions are available for ranking."
        rejection_reasons.append("MINIMUM_EVIDENCE_NOT_MET")
        ranking_eligible = False
    elif candidate.get("average_dollar_volume") is None:
        classification = "INSUFFICIENT_EVIDENCE"
        action = CANDIDATE_ACTION_INSUFFICIENT
        rationale = "Average daily dollar-volume evidence is unavailable."
        rejection_reasons.append("LIQUIDITY_UNAVAILABLE")
        ranking_eligible = False
    elif any(candidate.get(field) is None for field in ["current_price", "sma50", "sma200"]):
        classification = "INSUFFICIENT_EVIDENCE"
        action = CANDIDATE_ACTION_INSUFFICIENT
        rationale = "Current price and 50/200-day trend-alignment evidence are required for ranking."
        rejection_reasons.append("LONG_TERM_TREND_UNAVAILABLE")
        ranking_eligible = False
    elif (
        float(candidate["average_dollar_volume"]) < float(ranking.get("minimum_average_dollar_volume", 20_000_000))
    ) or (
        candidate.get("current_price") is not None
        and candidate.get("sma200") is not None
        and float(candidate["current_price"]) < float(candidate["sma200"])
    ):
        classification = "AVOID"
        action = CANDIDATE_ACTION_AVOID
        rationale = "Liquidity or long-term trend evidence fails the ranking policy."
        rejection_reasons.append("LIQUIDITY_OR_LONG_TERM_TREND_FAILURE")
        ranking_eligible = False
    elif (
        candidate.get("pivot_verification_status") == "verified"
        and candidate.get("pivot_structure_verification_status") == "VERIFIED"
        and candidate.get("pattern_algorithm_version") == "oneil_style_ohlcv_patterns_v1"
        and candidate.get("pattern_policy_version") == "oneil_style_pattern_policy_v1"
        and candidate.get("breakout_status") == "CONFIRMED"
        and bool(candidate.get("pivot_gate_statuses"))
        and all(status == "PASS" for status in candidate["pivot_gate_statuses"].values())
        and candidate.get("exact_pivot_price") is not None
        and candidate.get("inside_buy_zone") is True
        and candidate.get("breakout_volume_confirmation") is True
        and candidate.get("breakout_volume_ratio_50d") is not None
        and float(candidate["breakout_volume_ratio_50d"]) >= float(ranking.get("minimum_breakout_relative_volume", 1.5))
        and components["fundamental_quality"] >= float(ranking.get("minimum_fundamental_score_for_action", 70))
        and components["relative_strength_group"] >= float(ranking.get("minimum_rs_group_score_for_action", 65))
        and components["technical_setup"] >= float(ranking.get("minimum_technical_score_for_action", 70))
        and candidate.get("industry_group_rank") is not None
        and int(candidate["industry_group_rank"]) <= int(ranking.get("maximum_industry_group_rank_for_action", 40))
        and candidate.get("institutional_sponsorship_status") == "VERIFIED_SUPPORTIVE"
        and candidate.get("earnings_status") == "VERIFIED"
        and candidate.get("days_to_earnings") is not None
        and int(candidate["days_to_earnings"]) >= int(ranking.get("minimum_days_to_earnings_for_action", 5))
        and market_permission["status"] == "PERMITTED"
    ):
        classification = "BUY_NOW"
        action = CANDIDATE_ACTION_BUY
        rationale = "All verified pivot, buy-zone, volume, C/A, leadership, sponsorship, earnings-risk and market-permission gates pass."
    elif (
        candidate.get("early_entry_verification_status") == "verified"
        and candidate.get("pivot_structure_verification_status") == "VERIFIED"
        and candidate.get("pattern_algorithm_version") == "oneil_style_ohlcv_patterns_v1"
        and candidate.get("pattern_policy_version") == "oneil_style_pattern_policy_v1"
        and bool(candidate.get("pivot_gate_statuses"))
        and all(status == "PASS" for status in candidate["pivot_gate_statuses"].values())
        and candidate.get("early_entry_price") is not None
        and candidate.get("breakout_volume_confirmation") is True
        and candidate.get("breakout_volume_ratio_50d") is not None
        and float(candidate["breakout_volume_ratio_50d"]) >= float(ranking.get("minimum_breakout_relative_volume", 1.5))
        and components["fundamental_quality"] >= float(ranking.get("minimum_fundamental_score_for_action", 70))
        and components["relative_strength_group"] >= float(ranking.get("minimum_rs_group_score_for_action", 65))
        and components["technical_setup"] >= float(ranking.get("minimum_technical_score_for_action", 70))
        and candidate.get("industry_group_rank") is not None
        and int(candidate["industry_group_rank"]) <= int(ranking.get("maximum_industry_group_rank_for_action", 40))
        and candidate.get("institutional_sponsorship_status") == "VERIFIED_SUPPORTIVE"
        and candidate.get("earnings_status") == "VERIFIED"
        and candidate.get("days_to_earnings") is not None
        and int(candidate["days_to_earnings"]) >= int(ranking.get("minimum_days_to_earnings_for_action", 5))
        and market_permission["status"] == "PERMITTED"
    ):
        classification = "EARLY_ENTRY"
        action = CANDIDATE_ACTION_EARLY
        rationale = "All verified early-entry, volume, C/A, leadership, sponsorship, earnings-risk and market-permission gates pass."
    elif candidate.get("extended") is True:
        classification = "WAIT_FOR_CONFIRMATION"
        action = CANDIDATE_ACTION_WAIT
        rationale = "Price is beyond the configured verified-entry buy zone; wait for a new setup."
    elif candidate.get("breakout_status") in {"PRICE_ONLY", "FAILED"}:
        classification = "WAIT_FOR_CONFIRMATION"
        action = CANDIDATE_ACTION_WAIT
        rationale = (
            "The calculated pivot was crossed without the required breakout-volume confirmation; wait for a new valid trigger."
            if candidate.get("breakout_status") == "PRICE_ONLY"
            else "The prior breakout failed back below the calculated pivot; wait for a repaired base and new valid trigger."
        )
    elif (
        candidate.get("base_candidate_status") == "VERIFIED_ALGORITHMIC_PIVOT"
        and candidate.get("candidate_resistance_distance_pct") is not None
        and float(ranking.get("near_resistance_lower_pct", -5))
        <= float(candidate["candidate_resistance_distance_pct"])
        <= float(ranking.get("near_resistance_upper_pct", 2))
    ):
        classification = "WATCH_NEAR_PIVOT"
        action = CANDIDATE_ACTION_NEAR
        rationale = "A deterministic O'Neil-style base and algorithmic pivot passed every structural gate; wait for the verified breakout and volume trigger."
    elif candidate.get("base_candidate_status") in {"VERIFIED_ALGORITHMIC_PIVOT", "BUILDING"}:
        classification = "WATCH_BUILDING"
        action = CANDIDATE_ACTION_BUILDING
        rationale = "A deterministic pattern candidate exists, but it is still building or is not near its verified algorithmic trigger."
    if action == CANDIDATE_ACTION_BUY and candidate.get("pivot_verification_status") != "verified":
        raise ValueError("BUY NOW cannot be assigned without a verified pivot")
    if action == CANDIDATE_ACTION_EARLY and candidate.get("early_entry_verification_status") != "verified":
        raise ValueError("EARLY ENTRY cannot be assigned without a verified early-entry safeguard")
    confidence = _candidate_confidence({**candidate, "missing_evidence": missing})
    trigger = _candidate_trigger(candidate, action, ranking)
    risk = _candidate_risk(candidate)
    return {
        "ticker": candidate.get("ticker", "UNKNOWN"),
        "origin": candidate.get("origin"),
        "internal_canslim_score": round(total, 2),
        "score_components": components,
        "score_weights": weights,
        "classification": classification,
        "action": action,
        "rationale": rationale,
        "confidence": confidence,
        "ranking_eligibility": {
            "status": "ELIGIBLE" if ranking_eligible else "REJECTED",
            "rejection_reasons": rejection_reasons,
            "available_dimension_count": available_count,
            "minimum_available_dimensions": int(ranking.get("minimum_available_dimensions", 3)),
        },
        "why_ranked": _why_ranked(components, candidate),
        "missing_evidence": missing,
        "trigger": trigger,
        "risk_invalidates": risk,
        "market_permission": market_permission,
        "snapshot": {
            key: candidate.get(key)
            for key in [
                "company_name", "sector", "industry", "market_cap", "asset_class", "current_price", "candidate_resistance",
                "candidate_resistance_distance_pct", "pct_from_52w_high", "relative_volume",
                "average_dollar_volume", "volume_evidence_status", "up_down_volume_ratio_20",
                "accumulation_distribution_estimate", "quantitative_gate", "gate_reasons", "rs_trend",
                "rs_change_21d_pct", "rs_new_high_52w", "earnings_date", "days_to_earnings", "earnings_status",
                "quarterly_eps_growth_pct", "quarterly_sales_growth_pct", "annual_eps_growth_pct",
                "annual_positive_eps_growth_count", "annual_eps_growth_observations", "industry_group_rank",
                "institutional_sponsorship_status", "source_labels", "source_evidence", "origin_categories",
                "market_surge_candidate", "is_current_open_position", "chart_evidence_status", "setup_pattern_state",
                "base_candidate_status", "base_length_weeks", "base_depth_pct", "pivot_missing_evidence",
                "pivot_verification_status", "pivot_structure_verification_status", "pivot_gate_statuses",
                "exact_pivot_price", "pivot_basis", "buy_zone_upper_bound", "early_entry_price", "early_entry_verification_status",
                "breakout_status", "breakout_date", "breakout_volume_ratio_50d", "breakout_volume_confirmation",
                "inside_buy_zone", "extended", "pattern_type", "pattern_confidence", "pattern_confidence_score",
                "pattern_algorithm_version", "pattern_policy_version", "base_start", "base_end", "left_side_high",
                "base_low", "handle", "pattern_gates", "pattern_evidence_bars", "invalidation_support", "pattern_reason",
                "sma21", "sma50", "sma200", "available_dimensions", "daily_chart_asset", "pattern_chart_asset",
            ]
        },
        "events": [
            _event(
                "candidate_classification",
                classification,
                3,
                rationale,
                score=round(total, 2),
                pivot_verification_status=candidate.get("pivot_verification_status"),
                action=action,
                confidence=confidence,
                trigger=trigger,
                risk_invalidates=risk,
                missing_evidence=missing,
            )
        ],
    }


def rank_top_canslim_setups(
    candidate_results: list[dict[str, Any]], policy: dict[str, Any]
) -> list[dict[str, Any]]:
    maximum = int((policy.get("candidate_ranking") or {}).get("maximum_ranked_setups", 10))
    eligible = [
        item
        for item in candidate_results
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
    return [{**item, "rank": index} for index, item in enumerate(eligible[:maximum], start=1)]
