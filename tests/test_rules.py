from market_chart_pipeline.policy import load_policy
from market_chart_pipeline.rules import evaluate_position, evaluate_shakeout, rank_top_canslim_setups, score_candidate


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

    assert bad_origin["action"] == "AVOID"
    assert bad_origin["classification"] == "AVOID"
    assert unverified["action"] == "INSUFFICIENT EVIDENCE"
    assert unverified["classification"] == "INSUFFICIENT_EVIDENCE"


def _rankable_candidate(ticker: str, *, score: float = 80, technical: float | None = None, rs: float | None = None):
    return {
        "ticker": ticker,
        "origin": "watchlist",
        "origin_categories": ["BRANDENS_WATCHLIST"],
        "market_surge_candidate": True,
        "is_current_open_position": False,
        "asset_class": "EQUITY",
        "chart_evidence_status": "VERIFIED",
        "pivot_verification_status": "unverified",
        "pivot_structure_verification_status": "UNVERIFIED",
        "base_candidate_status": "BUILDING",
        "pattern_algorithm_version": "oneil_style_ohlcv_patterns_v1",
        "pattern_policy_version": "oneil_style_pattern_policy_v1",
        "pattern_type": "UNKNOWN",
        "pivot_gate_statuses": {"prior_uptrend": "UNKNOWN"},
        "candidate_resistance": 100,
        "candidate_resistance_distance_pct": -2,
        "current_price": 98,
        "sma50": 90,
        "sma200": 80,
        "average_dollar_volume": 50_000_000,
        "available_dimensions": ["C_A_fundamentals", "relative_strength", "technical_trend", "supply_demand"],
        "missing_evidence": ["industry_group_rank", "institutional_sponsorship"],
        "pivot_missing_evidence": ["exact_pivot_price", "weekly_structure"],
        "source_evidence": [{"source_type": "BRANDENS_WATCHLIST", "label": "BRANDENS WATCHLIST", "pdf_page": 8}],
        "fundamental_quality_score": score,
        "relative_strength_group_score": rs if rs is not None else score,
        "technical_setup_score": technical if technical is not None else score,
        "accumulation_supply_score": score,
        "new_catalyst_score": score,
    }


def test_global_top10_order_is_deterministic_and_capped():
    policy = load_policy()
    candidates = [_rankable_candidate(f"T{index:02d}", score=95 - index) for index in range(12)]
    results = [score_candidate(candidate, policy) for candidate in reversed(candidates)]

    ranked = rank_top_canslim_setups(results, policy)

    assert len(ranked) == 10
    assert [item["ticker"] for item in ranked] == [f"T{index:02d}" for index in range(10)]
    assert [item["rank"] for item in ranked] == list(range(1, 11))


def test_top10_tie_breaks_by_technical_then_rs_then_ticker():
    policy = load_policy()
    # Keep weighted totals equal while varying the documented deterministic tie-break fields.
    candidates = [
        _rankable_candidate("ZZZ", score=80, technical=80, rs=80),
        _rankable_candidate("BBB", score=80, technical=90, rs=68),
        _rankable_candidate("AAA", score=80, technical=90, rs=68),
    ]
    for candidate in candidates[1:]:
        candidate["fundamental_quality_score"] = 80
        candidate["accumulation_supply_score"] = 80
        candidate["new_catalyst_score"] = 80
        # 0.30*90 + 0.25*68 equals 0.30*80 + 0.25*80.
    results = [score_candidate(candidate, policy) for candidate in candidates]

    ranked = rank_top_canslim_setups(results, policy)

    assert [item["ticker"] for item in ranked] == ["AAA", "BBB", "ZZZ"]


def test_buy_now_requires_every_verified_entry_and_market_gate():
    policy = load_policy()
    candidate = _rankable_candidate("BUY", score=90)
    candidate.update(
        {
            "pivot_verification_status": "verified",
            "pivot_structure_verification_status": "VERIFIED",
            "pattern_algorithm_version": "oneil_style_ohlcv_patterns_v1",
            "pivot_gate_statuses": {
                "prior_uptrend": "PASS",
                "base_duration": "PASS",
                "base_depth": "PASS",
                "weekly_structure": "PASS",
                "volume_contraction": "PASS",
            },
            "breakout_status": "CONFIRMED",
            "exact_pivot_price": 98,
            "inside_buy_zone": True,
            "breakout_volume_confirmation": True,
            "breakout_volume_ratio_50d": 1.8,
            "relative_volume": 1.8,
            "industry_group_rank": 5,
            "institutional_sponsorship_status": "VERIFIED_SUPPORTIVE",
            "earnings_status": "VERIFIED",
            "days_to_earnings": 20,
            "pivot_missing_evidence": [],
        }
    )
    permissive = {"classification": "Confirmed uptrend", "dashboard_market_gauge_posture": "Grow"}

    actionable = score_candidate(candidate, policy, permissive)
    unverified = score_candidate({**candidate, "pivot_verification_status": "unverified"}, policy, permissive)

    assert actionable["action"] == "BUY NOW"
    assert actionable["classification"] == "BUY_NOW"
    assert unverified["action"] != "BUY NOW"


def test_confirmed_breakout_inside_buy_zone_waits_for_market_permission_not_building():
    policy = load_policy()
    candidate = _rankable_candidate("RELY", score=90)
    candidate.update(
        {
            "pivot_verification_status": "verified",
            "pivot_structure_verification_status": "VERIFIED",
            "base_candidate_status": "VERIFIED_ALGORITHMIC_PIVOT",
            "pattern_algorithm_version": "oneil_style_ohlcv_patterns_v1",
            "pattern_policy_version": "oneil_style_pattern_policy_v1",
            "pivot_gate_statuses": {
                "prior_uptrend": "PASS",
                "base_duration": "PASS",
                "base_depth": "PASS",
                "weekly_structure": "PASS",
                "volume_contraction": "PASS",
            },
            "breakout_status": "CONFIRMED",
            "exact_pivot_price": 25.85,
            "inside_buy_zone": True,
            "candidate_resistance_distance_pct": 3.2,
            "breakout_volume_confirmation": True,
            "breakout_volume_ratio_50d": 2.28,
            "industry_group_rank": 5,
            "institutional_sponsorship_status": "VERIFIED_SUPPORTIVE",
            "earnings_status": "VERIFIED",
            "days_to_earnings": 20,
            "pivot_missing_evidence": [],
        }
    )

    result = score_candidate(candidate, policy, {"classification": "INSUFFICIENT_EVIDENCE", "dashboard_market_gauge_posture": "Neutral"})

    assert result["action"] == "WAIT FOR CONFIRMATION"
    assert result["classification"] == "WAIT_FOR_CONFIRMATION"
    assert "market permission" in result["rationale"].lower()
    assert "building" not in result["rationale"].lower()
    assert "buying_permissive_market_regime" in result["missing_evidence"]


def test_failed_or_price_only_breakout_waits_instead_of_recycling_to_near_pivot():
    policy = load_policy()
    common = _rankable_candidate("FAIL", score=90)
    common.update(
        {
            "pivot_verification_status": "verified",
            "pivot_structure_verification_status": "VERIFIED",
            "base_candidate_status": "VERIFIED_ALGORITHMIC_PIVOT",
            "exact_pivot_price": 100,
            "candidate_resistance": 100,
            "candidate_resistance_distance_pct": -1,
            "pivot_gate_statuses": {
                "prior_uptrend": "PASS",
                "base_duration": "PASS",
                "base_depth": "PASS",
                "weekly_structure": "PASS",
                "volume_contraction": "PASS",
            },
        }
    )

    failed = score_candidate({**common, "breakout_status": "FAILED"}, policy)
    price_only = score_candidate({**common, "breakout_status": "PRICE_ONLY"}, policy)

    assert failed["action"] == "WAIT FOR CONFIRMATION"
    assert "failed" in failed["rationale"].lower()
    assert price_only["action"] == "WAIT FOR CONFIRMATION"
    assert "volume" in price_only["rationale"].lower()


def test_early_entry_requires_verified_structure_trigger_volume_and_market_gate():
    policy = load_policy()
    candidate = _rankable_candidate("EARLY", score=90)
    candidate.update(
        {
            "pivot_structure_verification_status": "VERIFIED",
            "pattern_algorithm_version": "oneil_style_ohlcv_patterns_v1",
            "pivot_gate_statuses": {
                "prior_uptrend": "PASS",
                "base_duration": "PASS",
                "base_depth": "PASS",
                "weekly_structure": "PASS",
                "volume_contraction": "PASS",
            },
            "early_entry_verification_status": "verified",
            "early_entry_price": 97,
            "breakout_volume_confirmation": True,
            "breakout_volume_ratio_50d": 1.8,
            "relative_volume": 1.8,
            "industry_group_rank": 5,
            "institutional_sponsorship_status": "VERIFIED_SUPPORTIVE",
            "earnings_status": "VERIFIED",
            "days_to_earnings": 20,
        }
    )
    permissive = {"classification": "Confirmed uptrend", "dashboard_market_gauge_posture": "Grow"}

    actionable = score_candidate(candidate, policy, permissive)
    no_volume = score_candidate({**candidate, "breakout_volume_confirmation": False}, policy, permissive)

    assert actionable["action"] == "EARLY ENTRY"
    assert actionable["classification"] == "EARLY_ENTRY"
    assert no_volume["action"] != "EARLY ENTRY"


def test_missing_evidence_lowers_confidence_without_fabricating_score():
    policy = load_policy()
    candidate = _rankable_candidate("LOW", score=0)
    candidate["available_dimensions"] = ["technical_trend", "relative_strength", "supply_demand"]
    candidate["missing_evidence"] = ["latest_quarter_eps_growth", "industry_group_rank", "institutional_sponsorship"]
    result = score_candidate(candidate, policy)

    assert result["confidence"] == "LOW"
    assert result["score_components"]["fundamental_quality"] == 0
    assert "latest_quarter_eps_growth" in result["missing_evidence"]
