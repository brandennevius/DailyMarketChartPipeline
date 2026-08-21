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


def test_hpe_hard_stop_preserves_verified_sell_sandbox_evidence():
    policy = load_policy()
    sandbox = {
        "status": "verified",
        "file": "assets/HPE_sell_sandbox.png",
        "sha256": "c19c0c3db45a66b811b6b1ff268dd8d2ea427541e2b38f7df109e2dc32004348",
    }
    result = evaluate_position(
        {
            "ticker": "HPE",
            "entry_price": 54.79,
            "entry_date": "2026-05-27",
            "current_price": 52.89,
            "stop_price": 48.97,
            "atr": 2.89,
            "highest_close_since_entry": 63.50,
            "sell_sandbox_status": "verified",
            "sell_sandbox_asset": sandbox,
        },
        policy,
        "2026-08-20",
    )

    assert result["trade_state"] == "BROKEN"
    assert result["action"] == "EXIT"
    assert result["position_snapshot"]["sell_sandbox_status"] == "verified"
    assert result["position_snapshot"]["sell_sandbox_asset"] == sandbox
    hard_stop = result["events"][0]
    assert hard_stop["rule"] == "hard_capital_protection"
    assert hard_stop["status"] == "TRIGGERED"
    assert hard_stop["values"]["current_price"] == 52.89
    assert hard_stop["values"]["effective_stop"] == 54.79


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


def test_seven_percent_advance_activates_five_percent_loss_floor():
    policy = load_policy()
    result = evaluate_position(
        {
            "ticker": "TEST",
            "entry_price": 100,
            "current_price": 94.9,
            "highest_close_since_entry": 107.5,
            "entry_date": "2026-08-03",
        },
        policy,
        "2026-08-14",
    )

    assert result["action"] == "EXIT"
    hard = result["events"][0]
    assert hard["rule"] == "hard_capital_protection"
    assert hard["values"]["protected_loss_floor"] == 95.0


def test_profit_zone_reduces_only_after_minimum_hold():
    policy = load_policy()
    common = {
        "ticker": "TEST",
        "entry_price": 100,
        "current_price": 124,
        "pivot_price": 100,
        "highest_close_since_entry": 125,
        "entry_date": "2026-06-01",
    }

    early = evaluate_position({**common, "trading_days_since_breakout": 30}, policy, "2026-08-14")
    mature = evaluate_position({**common, "trading_days_since_breakout": 40}, policy, "2026-08-14")

    assert early["action"] == "HOLD"
    assert any(event["rule"] == "profit_zone_minimum_hold" and event["status"] == "ACTIVE" for event in early["events"])
    assert mature["action"] == "REDUCE"
    assert "profit zone" in mature["rationale"]


def test_rapid_advance_trigger_persists_until_eight_weeks():
    policy = load_policy()
    position = {
        "ticker": "TEST",
        "entry_price": 100,
        "current_price": 124,
        "pivot_price": 100,
        "highest_close_since_entry": 125,
        "entry_date": "2026-06-01",
        "trading_days_to_rapid_advance": 10,
    }

    active = evaluate_position({**position, "trading_days_since_breakout": 30}, policy, "2026-08-14")
    finished = evaluate_position({**position, "trading_days_since_breakout": 40}, policy, "2026-08-14")

    assert active["action"] == "HOLD"
    assert any(event["rule"] == "rapid_advance_hold" and event["status"] == "ACTIVE" for event in active["events"])
    assert finished["action"] == "REDUCE"
    assert any(event["rule"] == "rapid_advance_hold" and event["status"] == "SATISFIED" for event in finished["events"])


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
