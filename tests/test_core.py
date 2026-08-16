import pandas as pd
import pytest
from market_chart_pipeline.core import ValidationError, calculate_metrics, normalize_symbols, serialize_price_history, validate_bars


def bars(periods=260,end="2026-08-04"):
    idx=pd.bdate_range(end=end,periods=periods); close=pd.Series([50+i*.2 for i in range(periods)],index=idx)
    return pd.DataFrame({"Open":close-.1,"High":close+.5,"Low":close-.5,"Close":close,"Volume":1_000_000},index=idx)


def test_manifest_deduplicates(): assert normalize_symbols(["aapl","AAPL"," msft "]) == ["AAPL","MSFT"]
def test_empty_manifest_fails():
    with pytest.raises(ValidationError): normalize_symbols([])
def test_stale_session_fails():
    with pytest.raises(ValidationError): validate_bars("TEST",bars(end="2026-08-03"),"2026-08-04")
def test_short_history_fails():
    with pytest.raises(ValidationError): validate_bars("TEST",bars(199),"2026-08-04")
def test_metrics_are_computed():
    m=calculate_metrics("TEST",bars(),"2026-08-04")
    assert m.current_price > m.sma50 > m.sma200
    assert m.quantitative_gate in {"CHART_REVIEW","CHART_REVIEW_PRIORITY"}


def test_price_history_is_bounded_and_serializable():
    history = serialize_price_history(bars(260), limit=20)
    assert len(history) == 20
    assert set(history[-1]) == {"date", "high", "close"}
    assert history[-1]["date"] == "2026-08-04"
