from pathlib import Path

import pandas as pd

from market_chart_pipeline.policy import load_policy
from market_chart_pipeline.sell_charts import build_sell_sandbox_chart


def _position(pivot=100):
    dates = pd.bdate_range(end="2026-08-14", periods=100)
    rows = []
    for index, day in enumerate(dates):
        close = 85 + index * 0.3
        rows.append(
            {
                "date": day.date().isoformat(),
                "open": close - 0.2,
                "high": close + 0.8,
                "low": close - 0.8,
                "close": close,
                "volume": 1_000_000 + index * 1_000,
            }
        )
    return {
        "ticker": "TEST",
        "entry_price": 100,
        "entry_date": dates[50].date().isoformat(),
        "current_price": rows[-1]["close"],
        "stop_price": 95,
        "pivot_price": pivot,
        "atr": 2,
        "price_history": rows,
    }


def test_sell_sandbox_renders_all_policy_boundaries(tmp_path: Path):
    output = tmp_path / "TEST_sell_sandbox.png"
    asset = build_sell_sandbox_chart(_position(), load_policy(), "2026-08-14", output)

    assert output.stat().st_size > 10_000
    assert asset["status"] == "verified"
    assert asset["sha256"]
    assert asset["levels"] == {
        "entry": 100.0,
        "loss_limit": 92.0,
        "protected_loss_floor": 95.0,
        "atr_stop": 96.0,
        "working_stop": 95.0,
        "effective_stop": 100.0,
        "pivot": 100.0,
        "profit_zone_lower": 120.0,
        "profit_zone_upper": 125.0,
    }


def test_sell_sandbox_never_draws_unverified_pivot_zone(tmp_path: Path):
    output = tmp_path / "TEST_sell_sandbox.png"
    asset = build_sell_sandbox_chart(_position(pivot=None), load_policy(), "2026-08-14", output)

    assert asset["levels"]["pivot"] is None
    assert asset["levels"]["profit_zone_lower"] is None
    assert asset["levels"]["profit_zone_upper"] is None
