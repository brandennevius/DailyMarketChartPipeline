from __future__ import annotations

from datetime import date
from typing import Any

ACTION_HOLD = "HOLD"
ACTION_ADD = "ADD"
ACTION_REDUCE = "REDUCE"
ACTION_EXIT = "EXIT"
ACTION_REPAIR = "REPAIR"
ACTION_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"


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


def _pct(current: float, base: float) -> float:
    return ((current - base) / base) * 100.0


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


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
            )
        )
        return {
            "ticker": ticker,
            "trade_state": "BROKEN",
            "action": ACTION_EXIT,
            "rationale": "Hard capital-protection stop was violated.",
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

    pivot = position.get("pivot_price")
    if pivot is not None:
        pivot_gain = _pct(current, float(pivot))
        zone = policy["profit_zone"]
        if pivot_gain >= float(zone["upper_pct_from_pivot"]):
            events.append(_event("profit_zone", "UPPER_ZONE_REACHED", 2, "Price is above the upper profit-zone threshold", gain_from_pivot_pct=round(pivot_gain, 2)))
        elif pivot_gain >= float(zone["lower_pct_from_pivot"]):
            events.append(_event("profit_zone", "LOWER_ZONE_REACHED", 2, "Price is in the 20%-25% profit zone", gain_from_pivot_pct=round(pivot_gain, 2)))
        else:
            events.append(_event("profit_zone", "NOT_REACHED", 2, "Price has not reached the profit zone", gain_from_pivot_pct=round(pivot_gain, 2)))
    else:
        events.append(_event("profit_zone", "INSUFFICIENT_EVIDENCE", 2, "Pivot price is unavailable"))

    breakout_date = position.get("breakout_date") or position.get("entry_date")
    rapid_active = False
    if breakout_date and pivot is not None:
        trading_days_held = int(position.get("trading_days_since_breakout") or _days(str(breakout_date), session_date))
        weeks_held = trading_days_held / 5.0
        pivot_gain = _pct(current, float(pivot))
        rapid = policy["rapid_advance"]
        if pivot_gain >= float(rapid["gain_pct"]) and trading_days_held <= int(rapid["max_trading_days"]):
            rapid_active = weeks_held < float(rapid["minimum_hold_weeks"])
            events.append(
                _event(
                    "rapid_advance_hold",
                    "ACTIVE" if rapid_active else "SATISFIED",
                    1,
                    "20% advance within three weeks qualifies for the eight-week hold rule",
                    trading_days_since_breakout=trading_days_held,
                    weeks_held=round(weeks_held, 2),
                    gain_from_pivot_pct=round(pivot_gain, 2),
                )
            )
        else:
            events.append(_event("rapid_advance_hold", "NOT_TRIGGERED", 1, "Rapid advance threshold was not met", trading_days_since_breakout=trading_days_held, gain_from_pivot_pct=round(pivot_gain, 2)))
    else:
        events.append(_event("rapid_advance_hold", "INSUFFICIENT_EVIDENCE", 1, "Breakout date or pivot price is unavailable"))

    if high:
        drawdown = ((float(high) - current) / float(high)) * 100.0
        threshold = float(policy["trailing"]["peak_drawdown_exit_pct"])
        status = "TRIGGERED" if drawdown >= threshold else "PASS"
        events.append(_event("peak_drawdown_trail", status, 1, "Measured decline from highest close since entry", drawdown_pct=round(drawdown, 2), threshold_pct=threshold))
    else:
        drawdown = None
        events.append(_event("peak_drawdown_trail", "INSUFFICIENT_EVIDENCE", 1, "Highest close since entry is unavailable"))

    patience_status = "NOT_EVALUATED"
    trading_days_held = None
    if breakout_date:
        trading_days_held = int(position.get("trading_days_since_breakout") or _days(str(breakout_date), session_date))
        weeks_held = trading_days_held / 5.0
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
        rationale = "Rapid-advance eight-week hold is active and no hard exit rule fired."
    elif drawdown is not None and drawdown >= float(policy["trailing"]["peak_drawdown_exit_pct"]):
        action = ACTION_REDUCE
        rationale = "Peak drawdown trail fired after capital-protection checks passed."
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
        "position_snapshot": {
            key: position.get(key)
            for key in [
                "company_name", "sector", "entry_price", "entry_date", "current_price", "shares",
                "market_value", "position_weight_pct", "unrealized_pnl", "open_r_multiple",
                "stop_price", "remaining_risk_to_stop_dollars", "take_profit", "setup", "grade",
                "setup_criteria_score", "setup_criteria_max", "earnings_date", "sma21", "sma50",
                "sma200", "pct_from_sma50", "pct_from_52w_high", "relative_strength_trend",
                "accumulation_distribution", "chart_gate", "chart_gate_reasons", "daily_chart_asset",
            ]
        },
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


def score_candidate(candidate: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    weights = policy["candidate_scoring"]
    components = {
        "fundamental_quality": float(candidate.get("fundamental_quality_score") or 0),
        "relative_strength_group": float(candidate.get("relative_strength_group_score") or 0),
        "technical_setup": float(candidate.get("technical_setup_score") or 0),
        "accumulation_supply": float(candidate.get("accumulation_supply_score") or 0),
        "new_catalyst": float(candidate.get("new_catalyst_score") or 0),
    }
    total = sum(max(0.0, min(100.0, components[key])) * float(weight) for key, weight in weights.items())
    critical = [field for field in ["origin", "ticker", "pivot_verification_status"] if not candidate.get(field)]
    classification = "WATCH"
    action = ACTION_HOLD
    rationale = "Candidate is tracked but not actionable."
    if candidate.get("origin") not in {"scanner", "watchlist", "open_position"}:
        classification = "AVOID"
        action = ACTION_INSUFFICIENT
        rationale = "Candidate origin is not allowed by policy."
    elif critical:
        classification = "AVOID"
        action = ACTION_INSUFFICIENT
        rationale = f"Missing critical candidate fields: {', '.join(critical)}"
    elif candidate.get("pivot_verification_status") != "verified":
        classification = "WATCH"
        rationale = "Pivot is not verified, so no actionable entry is allowed."
    elif candidate.get("inside_buy_zone") is True and total >= 75:
        classification = "BUY_NOW"
        action = ACTION_ADD
        rationale = "Verified pivot, buy-zone location, and deterministic score pass."
    elif candidate.get("extended") is True:
        classification = "EXTENDED"
        action = ACTION_HOLD
        rationale = "Candidate is extended beyond the configured buy zone."
    return {
        "ticker": candidate.get("ticker", "UNKNOWN"),
        "origin": candidate.get("origin"),
        "internal_canslim_score": round(total, 2),
        "score_components": components,
        "score_weights": weights,
        "classification": classification,
        "action": action,
        "rationale": rationale,
        "snapshot": {
            key: candidate.get(key)
            for key in [
                "company_name", "sector", "current_price", "candidate_resistance",
                "candidate_resistance_distance_pct", "pct_from_52w_high", "relative_volume",
                "average_dollar_volume", "quantitative_gate", "gate_reasons", "rs_trend",
                "earnings_date", "source_labels",
                "daily_chart_asset",
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
            )
        ],
    }
