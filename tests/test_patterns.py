import os

import pandas as pd
import pytest

from market_chart_pipeline.candidate_charts import render_pattern_chart
from market_chart_pipeline.fmp import historical_eod
from market_chart_pipeline.patterns import PATTERN_ALGORITHM_VERSION, PATTERN_POLICY_VERSION, analyze_ohlcv_patterns
from market_chart_pipeline.utils import sha256_file


def _daily_from_weekly(
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    weekly_volumes: list[float] | None = None,
) -> pd.DataFrame:
    start = pd.Timestamp("2024-01-01")
    dates = pd.bdate_range(start, periods=len(closes) * 5)
    highs = highs or [value * 1.015 for value in closes]
    lows = lows or [value * 0.985 for value in closes]
    weekly_volumes = weekly_volumes or [5_000_000.0] * len(closes)
    rows = []
    previous = closes[0] * 0.99
    for week, close in enumerate(closes):
        interpolated = [previous + (close - previous) * (day + 1) / 5 for day in range(5)]
        for day, day_close in enumerate(interpolated):
            day_open = previous if day == 0 else interpolated[day - 1]
            rows.append(
                {
                    "Open": day_open,
                    "High": max(day_open, day_close) * 1.003,
                    "Low": min(day_open, day_close) * 0.997,
                    "Close": day_close,
                    "Volume": weekly_volumes[week] / 5.0,
                }
            )
        rows[-4]["High"] = max(rows[-4]["High"], highs[week])
        rows[-3]["Low"] = min(rows[-3]["Low"], lows[week])
        previous = close
    return pd.DataFrame(rows, index=dates)


def _prior_uptrend(weeks: int = 60) -> list[float]:
    return [40.0 + index * (60.0 / (weeks - 1)) for index in range(weeks)]


def _flat_base_series(*, breakout_volume: float = 12_000_000.0) -> pd.DataFrame:
    prior = _prior_uptrend()
    base = [99.0, 95.0, 92.0, 94.0, 97.0, 99.0]
    breakout = [102.5]
    closes = prior + base + breakout
    highs = [value * 1.01 for value in prior] + [101.0, 99.0, 96.0, 97.0, 99.5, 100.0] + [103.0]
    lows = [value * 0.99 for value in prior] + [97.0, 93.0, 90.0, 92.0, 95.0, 97.0] + [100.0]
    volumes = [5_000_000.0] * len(prior) + [7_000_000.0, 6_500_000.0, 5_500_000.0, 4_500_000.0, 4_000_000.0, 3_500_000.0] + [breakout_volume]
    return _daily_from_weekly(closes, highs=highs, lows=lows, weekly_volumes=volumes)


def _pattern_series(pattern_type: str) -> pd.DataFrame:
    prior = _prior_uptrend()
    patterns = {
        "CUP_WITHOUT_HANDLE": (
            [100, 97, 92, 85, 78, 72, 70, 74, 80, 86, 91, 95, 97, 98, 99, 100],
            [102, 100, 97, 92, 86, 80, 76, 78, 84, 89, 94, 98, 99, 100, 101, 101],
            [98, 94, 88, 80, 74, 69, 68, 70, 76, 82, 88, 92, 95, 96, 97, 98],
            (103, 104, 100),
        ),
        "CUP_WITH_HANDLE": (
            [100, 97, 92, 85, 78, 72, 70, 74, 80, 86, 91, 95, 98, 100, 99, 98],
            [102, 100, 97, 92, 86, 80, 76, 78, 84, 89, 94, 98, 100, 101, 100, 99.5],
            [98, 94, 88, 80, 74, 69, 68, 70, 76, 82, 88, 92, 95, 97, 96, 96.5],
            (101, 102, 98.5),
        ),
        "DOUBLE_BOTTOM": (
            [100, 96, 92, 88, 80, 84, 90, 94, 88, 81, 85, 88, 91, 93, 94, 94.5],
            [102, 99, 96, 92, 84, 88, 93, 95, 92, 85, 89, 92, 94, 95, 95.5, 95.8],
            [98, 93, 89, 85, 78, 81, 86, 90, 84, 79, 82, 85, 88, 90, 92, 93],
            (98, 99, 94),
        ),
    }
    closes, highs, lows, breakout = patterns[pattern_type]
    volumes = [5_000_000.0] * len(prior) + [7_000_000.0 - index * 250_000.0 for index in range(len(closes))] + [12_000_000.0]
    return _daily_from_weekly(
        prior + closes + [breakout[0]],
        highs=[value * 1.01 for value in prior] + highs + [breakout[1]],
        lows=[value * 0.99 for value in prior] + lows + [breakout[2]],
        weekly_volumes=volumes,
    )


def _assert_evidence_is_session_bounded(result: dict, session: str) -> None:
    dated = [
        result.get("base_start"),
        result.get("base_end"),
        result.get("breakout_date"),
        (result.get("evidence_bars") or {}).get("session_end"),
    ]
    assert all(not value or value <= session for value in dated)


def test_flat_base_verified_pivot_and_confirmed_breakout():
    frame = _flat_base_series()
    result = analyze_ohlcv_patterns(frame)
    session = frame.index[-1].date().isoformat()
    assert result["status"] == "VERIFIED_ALGORITHMIC_PIVOT"
    assert result["algorithm_version"] == PATTERN_ALGORITHM_VERSION
    assert result["policy_version"] == PATTERN_POLICY_VERSION
    assert result["pattern_type"] == "FLAT_BASE"
    assert result["pivot_status"] == "VERIFIED_ALGORITHMIC_PIVOT"
    assert result["pivot_price"] == pytest.approx(101.1)
    assert result["buy_zone_upper_bound"] == pytest.approx(106.155)
    assert result["breakout_status"] == "CONFIRMED"
    assert result["breakout_volume_confirmation"] is True
    assert all(result["gates"][name]["status"] == "PASS" for name in result["hard_gate_names"])
    _assert_evidence_is_session_bounded(result, session)


def test_insufficient_breakout_volume_never_becomes_confirmed():
    result = analyze_ohlcv_patterns(_flat_base_series(breakout_volume=4_000_000.0))
    assert result["pivot_status"] == "VERIFIED_ALGORITHMIC_PIVOT"
    assert result["breakout_status"] == "PRICE_ONLY"
    assert result["breakout_volume_confirmation"] is False


@pytest.mark.parametrize("pattern_type", ["CUP_WITHOUT_HANDLE", "CUP_WITH_HANDLE", "DOUBLE_BOTTOM"])
def test_supported_pattern_geometries_have_positive_synthetic_corpus_cases(pattern_type):
    result = analyze_ohlcv_patterns(_pattern_series(pattern_type))
    assert result["pattern_type"] == pattern_type
    assert result["status"] == "VERIFIED_ALGORITHMIC_PIVOT"
    assert result["breakout_status"] == "CONFIRMED"
    assert result["pivot_price"] is not None
    assert not result["missing_evidence"]
    if pattern_type == "CUP_WITH_HANDLE":
        assert result["handle"]["low_date"]
        assert result["handle"]["high_date"]


def test_lower_half_handle_is_not_verified_as_cup_with_handle():
    frame = _pattern_series("CUP_WITH_HANDLE").copy()
    # Force the two pre-breakout handle weeks into the lower half of the cup.
    base_end = frame.index[-6]
    handle_rows = frame.loc[(frame.index > base_end - pd.Timedelta(days=14)) & (frame.index <= base_end)]
    frame.loc[handle_rows.index, "Low"] = 75.0
    result = analyze_ohlcv_patterns(frame)
    assert not (
        result["pattern_type"] == "CUP_WITH_HANDLE"
        and result["status"] == "VERIFIED_ALGORITHMIC_PIVOT"
    )


def test_too_short_consolidation_does_not_verify_a_base():
    prior = _prior_uptrend()
    frame = _daily_from_weekly(prior + [98.0, 94.0, 99.0, 103.0])
    result = analyze_ohlcv_patterns(frame)
    assert result["status"] != "VERIFIED_ALGORITHMIC_PIVOT"


def test_failed_breakout_and_extended_buy_are_explicit():
    confirmed = _flat_base_series()
    failed_week = _daily_from_weekly([98.0], highs=[100.0], lows=[96.0], weekly_volumes=[5_000_000.0])
    failed_week.index = pd.bdate_range(confirmed.index[-1] + pd.offsets.BDay(1), periods=5)
    failed = analyze_ohlcv_patterns(pd.concat([confirmed, failed_week]))
    assert failed["breakout_status"] == "FAILED"
    assert failed["inside_buy_zone"] is False

    extended = _flat_base_series().copy()
    extended.iloc[-1, extended.columns.get_loc("Close")] = 108.0
    extended.iloc[-1, extended.columns.get_loc("High")] = 109.0
    extended_result = analyze_ohlcv_patterns(extended)
    assert extended_result["breakout_status"] == "EXTENDED"
    assert extended_result["extended"] is True


def test_directional_advance_is_not_misclassified_as_verified_flat_base():
    closes = [40.0 + index * 0.8 for index in range(70)]
    result = analyze_ohlcv_patterns(_daily_from_weekly(closes))
    assert result["status"] != "VERIFIED_ALGORITHMIC_PIVOT"
    assert result["pivot_price"] is None


def test_too_deep_base_fails_closed():
    prior = _prior_uptrend()
    base = [99.0, 85.0, 65.0, 50.0, 68.0, 84.0, 97.0]
    frame = _daily_from_weekly(prior + base + [102.0])
    result = analyze_ohlcv_patterns(frame)
    assert result["status"] != "VERIFIED_ALGORITHMIC_PIVOT"
    assert result["pivot_price"] is None


def test_same_exact_session_input_is_deterministic_and_future_bars_do_not_leak():
    frame = _flat_base_series()
    first = analyze_ohlcv_patterns(frame)
    second = analyze_ohlcv_patterns(frame.copy())
    assert first == second
    future = _daily_from_weekly([104.0, 106.0])
    future.index = pd.bdate_range(frame.index[-1] + pd.offsets.BDay(1), periods=len(future))
    combined = pd.concat([frame, future])
    assert analyze_ohlcv_patterns(combined.loc[: frame.index[-1]]) == first


def test_pattern_overlay_is_hashable_and_exact_session_bounded(tmp_path):
    frame = _flat_base_series()
    result = analyze_ohlcv_patterns(frame)
    target = tmp_path / "ACME_pattern.png"
    render_pattern_chart("ACME", frame, frame.index[-1].date().isoformat(), result, target)
    assert target.stat().st_size > 5_000
    assert len(sha256_file(target)) == 64


@pytest.mark.skipif(
    os.getenv("RUN_FMP_PATTERN_INTEGRATION") != "1" or not os.getenv("FMP_API_KEY"),
    reason="Explicit FMP public-price integration opt-in and FMP_API_KEY are required",
)
def test_public_fmp_price_series_is_session_bounded_and_deterministic():
    """Optional public-price corpus case; never assumes that a named pattern must exist."""
    frame = historical_eod("AAPL", "2024-01-01", "2025-08-15")
    first = analyze_ohlcv_patterns(frame)
    second = analyze_ohlcv_patterns(frame.copy())
    assert first == second
    assert first["algorithm_version"] == PATTERN_ALGORITHM_VERSION
    _assert_evidence_is_session_bounded(first, "2025-08-15")
