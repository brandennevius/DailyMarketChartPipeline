from market_chart_pipeline.policy import load_policy
from market_chart_pipeline.rules import evaluate_position, evaluate_shakeout, score_candidate


def test_hard_capital_protection_overrides_rapid_advance_hold():
    policy = load_policy()
    result = evaluate_position(
        {
            "ticker": "NVDA",
            "entry_price": 100,
            "current_price": 91,
            "pivot_price": 75,
            "atr": 2,
            "breakout_date": "2026-08-01",
            "trading_days_since_breakout": 10,
            "highest_close_since_entry": 130,
        },
        policy,
        "2026-08-14",
    )

    assert result["action"] == "EXIT"
    assert result["trade_state"] == "BROKEN"
    assert result["events"][0]["rule"] == "hard_capital_protection"


def test_rapid_advance_creates_eight_week_hold_when_stop_passes():
    policy = load_policy()
    result = evaluate_position(
        {
            "ticker": "PLTR",
            "entry_price": 100,
            "current_price": 126,
            "pivot_price": 100,
            "atr": 3,
            "breakout_date": "2026-08-03",
            "trading_days_since_breakout": 9,
            "highest_close_since_entry": 127,
        },
        policy,
        "2026-08-14",
    )

    assert result["action"] == "HOLD"
    assert any(event["rule"] == "rapid_advance_hold" and event["status"] == "ACTIVE" for event in result["events"])


def test_peak_drawdown_after_stop_passes_reduces_not_exits():
    policy = load_policy()
    result = evaluate_position(
        {
            "ticker": "META",
            "entry_price": 100,
            "current_price": 112,
            "pivot_price": 100,
            "atr": 4,
            "breakout_date": "2026-07-01",
            "trading_days_since_breakout": 30,
            "highest_close_since_entry": 130,
        },
        policy,
        "2026-08-14",
    )

    assert result["action"] == "REDUCE"
    assert any(event["rule"] == "peak_drawdown_trail" and event["status"] == "TRIGGERED" for event in result["events"])


def test_shakeout_reentry_requires_reclaim_and_volume():
    policy = load_policy()
    result = evaluate_shakeout(
        {
            "ticker": "CRWD",
            "prior_support_price": 100,
            "current_price": 104,
            "days_since_break": 4,
            "volume_ratio": 1.3,
        },
        policy,
    )

    assert result["state"] == "REENTRY_READY"
    assert result["action"] == "ADD"


def test_candidate_origin_and_pivot_gate_prevent_actionable_output():
    policy = load_policy()

    bad_origin = score_candidate(
        {
            "ticker": "XYZ",
            "origin": "news",
            "pivot_verification_status": "verified",
            "inside_buy_zone": True,
            "fundamental_quality_score": 100,
            "relative_strength_group_score": 100,
            "technical_setup_score": 100,
            "accumulation_supply_score": 100,
            "new_catalyst_score": 100,
        },
        policy,
    )
    unverified = score_candidate(
        {
            "ticker": "ABC",
            "origin": "scanner",
            "pivot_verification_status": "unverified",
            "inside_buy_zone": True,
            "fundamental_quality_score": 100,
            "relative_strength_group_score": 100,
            "technical_setup_score": 100,
            "accumulation_supply_score": 100,
            "new_catalyst_score": 100,
        },
        policy,
    )

    assert bad_origin["action"] == "INSUFFICIENT_EVIDENCE"
    assert bad_origin["classification"] == "AVOID"
    assert unverified["action"] == "HOLD"
    assert unverified["classification"] == "WATCH"
