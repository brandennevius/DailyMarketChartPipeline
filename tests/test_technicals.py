import pandas as pd

from market_chart_pipeline.technicals import calculate_technical_context


def bars(periods=300, start="2025-01-02"):
    idx = pd.bdate_range(start=start, periods=periods)
    close = pd.Series([50 + i * 0.15 for i in range(periods)], index=idx)
    return pd.DataFrame({
        "Open": close - 0.1,
        "High": close + 0.5,
        "Low": close - 0.5,
        "Close": close,
        "Volume": [1_000_000 + (i % 5) * 50_000 for i in range(periods)],
    }, index=idx)


def test_objective_distances_and_weekly_slopes():
    df = bars()
    rs = pd.Series(range(100, 100 + len(df)), index=df.index, dtype=float)
    result = calculate_technical_context(df, rs)
    assert result["distance_from_21d_pct"] > 0
    assert result["distance_from_50d_pct"] > 0
    assert result["ma_10w_trend"] == "RISING"
    assert result["ma_40w_trend"] == "RISING"


def test_rs_new_high_and_volume_metrics():
    df = bars()
    rs = pd.Series(range(100, 100 + len(df)), index=df.index, dtype=float)
    result = calculate_technical_context(df, rs)
    assert result["relative_strength"]["status"] == "VERIFIED"
    assert result["relative_strength"]["new_high_52w"] is True
    assert "up_down_volume_ratio_20" in result["volume"]


def test_base_fields_are_conservative():
    df = bars()
    rs = pd.Series(range(100, 100 + len(df)), index=df.index, dtype=float)
    result = calculate_technical_context(df, rs)
    base = result["base_analysis"]
    assert base["status"] != "VERIFIED_ALGORITHMIC_PIVOT"
    assert base["pivot_price"] is None
    assert base["algorithm_version"] == "oneil_style_ohlcv_patterns_v1"
