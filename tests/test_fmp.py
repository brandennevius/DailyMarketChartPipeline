import pandas as pd

from market_chart_pipeline.fmp import compare_ohlcv, rs_line


def frame(closes, volumes, start="2026-07-01"):
    idx = pd.bdate_range(start=start, periods=len(closes))
    return pd.DataFrame({
        "Open": closes,
        "High": [x * 1.01 for x in closes],
        "Low": [x * 0.99 for x in closes],
        "Close": closes,
        "Volume": volumes,
    }, index=idx)


def test_crosscheck_flags_material_volume_difference():
    alpaca = frame([100 + i for i in range(20)], [1_000_000] * 20)
    fmp = frame([100 + i for i in range(20)], [2_000_000] * 20)
    result = compare_ohlcv(alpaca, fmp, alpaca.index[-1].date().isoformat())
    assert result["status"] == "COMPARED"
    assert result["material_discrepancy"] is True
    assert result["latest_close_diff_pct"] == 0


def test_rs_line_is_rebased_to_100():
    stock = frame([100, 110, 121], [1, 1, 1])
    benchmark = frame([100, 105, 110], [1, 1, 1])
    result = rs_line(stock, benchmark, stock.index[-1].date().isoformat())
    assert round(result.iloc[0], 8) == 100
    assert result.iloc[-1] > 100
