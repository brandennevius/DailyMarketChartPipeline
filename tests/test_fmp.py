from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from market_chart_pipeline import fmp
from market_chart_pipeline.core import fetch_bars, serialize_price_history, validate_bars
from market_chart_pipeline.fmp import FMPError, canonical_fmp_symbol, historical_eod, rs_line
from market_chart_pipeline.technicals import calculate_technical_context


SESSION = "2026-08-17"


class Response:
    def __init__(self, status_code: int, payload, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


def history_rows(periods=260, end=SESSION, *, volume=1_000_000):
    rows = []
    for index, date in enumerate(pd.bdate_range(end=end, periods=periods)):
        close = 100 + index * 0.1
        row = {
            "date": date.date().isoformat(),
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
        }
        if volume is not None:
            row["volume"] = volume
        rows.append(row)
    return rows


def frame(closes, volumes, start="2026-07-01"):
    idx = pd.bdate_range(start=start, periods=len(closes))
    result = pd.DataFrame({
        "Open": closes,
        "High": [x * 1.01 for x in closes],
        "Low": [x * 0.99 for x in closes],
        "Close": closes,
        "Volume": volumes,
    }, index=idx)
    result.attrs.update({"asset_class": "EQUITY", "volume_status": "AVAILABLE"})
    return result


@pytest.fixture(autouse=True)
def fmp_env(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    monkeypatch.setenv("FMP_MIN_REQUEST_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("FMP_RETRY_BASE_SECONDS", "0")


def test_fx_symbol_is_canonicalized_without_changing_display_symbol():
    assert canonical_fmp_symbol("AUD/USD") == ("AUDUSD", "FOREX")
    assert canonical_fmp_symbol("usd/cad") == ("USDCAD", "FOREX")


def test_historical_eod_uses_stable_bounded_endpoint_and_strict_schema(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response(200, history_rows())

    monkeypatch.setattr(fmp.requests, "get", fake_get)
    result = historical_eod("AAPL", "2025-08-17", SESSION)
    assert len(calls) == 1
    assert calls[0][0].endswith("/stable/historical-price-eod/full")
    assert calls[0][1]["params"] == {"symbol": "AAPL", "from": "2025-08-17", "to": SESSION}
    assert result.index[-1].date().isoformat() == SESSION
    assert result.attrs["provider"] == "FMP"
    assert result.attrs["live_quote_substitution"] is False


def test_historical_eod_applies_adjusted_close_factor_to_ohlcv(monkeypatch):
    rows = [
        {"date": SESSION, "open": 398, "high": 404, "low": 396, "close": 400, "adjClose": 100, "volume": 1_000_000},
    ]
    monkeypatch.setattr(fmp.requests, "get", lambda *a, **k: Response(200, rows))
    result = historical_eod("AAPL", SESSION, SESSION)
    assert result.iloc[0].Close == 100
    assert result.iloc[0].Open == 99.5
    assert result.iloc[0].Volume == 4_000_000
    assert result.attrs["adjustment"] == "ADJUSTED_CLOSE_FACTOR"


def test_fx_history_preserves_missing_volume_as_insufficient_evidence(monkeypatch):
    observed = {}

    def fake_get(_url, **kwargs):
        observed.update(kwargs["params"])
        return Response(200, history_rows(volume=None))

    monkeypatch.setattr(fmp.requests, "get", fake_get)
    result = historical_eod("AUD/USD", "2025-08-17", SESSION)
    assert observed["symbol"] == "AUDUSD"
    assert result.attrs["original_symbol"] == "AUD/USD"
    assert result.attrs["volume_status"] == "UNAVAILABLE"
    assert result.Volume.isna().all()
    validated = validate_bars("AUD/USD", result, SESSION)
    technical = calculate_technical_context(validated, pd.Series(dtype=float))
    assert technical["volume"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert serialize_price_history(validated)[-1]["volume"] is None


def test_fx_zero_placeholder_volume_is_not_treated_as_real_volume(monkeypatch):
    monkeypatch.setattr(fmp.requests, "get", lambda *a, **k: Response(200, history_rows(volume=0)))
    result = historical_eod("USD/CAD", "2025-08-17", SESSION)
    assert result.attrs["volume_status"] == "UNAVAILABLE"
    assert result.Volume.isna().all()


def test_equity_history_rejects_missing_volume(monkeypatch):
    monkeypatch.setattr(fmp.requests, "get", lambda *a, **k: Response(200, history_rows(volume=None)))
    with pytest.raises(FMPError, match="volume is missing"):
        historical_eod("AAPL", "2025-08-17", SESSION)


def test_historical_eod_retries_429_and_5xx(monkeypatch):
    responses = [Response(429, [], {"Retry-After": "0"}), Response(503, []), Response(200, history_rows())]
    monkeypatch.setattr(fmp.requests, "get", lambda *a, **k: responses.pop(0))
    result = historical_eod("AAPL", "2025-08-17", SESSION)
    assert len(result) == 260
    assert responses == []


def test_history_range_and_returned_dates_are_bounded(monkeypatch):
    monkeypatch.setattr(fmp.requests, "get", lambda *a, **k: Response(200, [
        {"date": "2026-08-18", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100}
    ]))
    with pytest.raises(FMPError, match="out-of-range"):
        historical_eod("AAPL", SESSION, SESSION)
    with pytest.raises(FMPError, match="0-1200 calendar days"):
        historical_eod("AAPL", "2020-01-01", SESSION)


def test_exact_session_and_minimum_history_fail_closed(monkeypatch):
    monkeypatch.setattr(fmp.requests, "get", lambda *a, **k: Response(200, history_rows(199, end="2026-08-14")))
    result = historical_eod("AAPL", "2025-08-17", SESSION)
    with pytest.raises(Exception, match="199 bars available"):
        validate_bars("AAPL", result, SESSION)


def test_production_shaped_universe_fetch_is_complete_deterministic_and_includes_fx(monkeypatch):
    equities = [f"T{index:03d}" for index in range(175)]
    universe = equities + ["AUD/USD", "USD/CAD"]
    calls = []

    def fake_history(symbol, start, end):
        calls.append((symbol, start, end))
        values = history_rows()
        provider_symbol, asset_class = canonical_fmp_symbol(symbol)
        data = pd.DataFrame(values).rename(columns={
            "date": "Date", "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"
        })
        data["Date"] = pd.to_datetime(data["Date"])
        if asset_class == "FOREX":
            data["Volume"] = pd.NA
        data = data.set_index("Date")
        data.attrs.update({
            "provider": "FMP", "provider_symbol": provider_symbol, "original_symbol": symbol,
            "asset_class": asset_class, "volume_status": "UNAVAILABLE" if asset_class == "FOREX" else "AVAILABLE",
        })
        return data

    monkeypatch.setattr("market_chart_pipeline.core.historical_eod", fake_history)
    first, first_errors = fetch_bars(universe, SESSION)
    second, second_errors = fetch_bars(list(reversed(universe)), SESSION)
    assert len(first) == 177
    assert first_errors == second_errors == {}
    assert list(first) == list(second) == sorted(universe)
    assert first["AUD/USD"].attrs["provider_symbol"] == "AUDUSD"
    assert first["USD/CAD"].attrs["provider_symbol"] == "USDCAD"
    first_json = json.dumps({key: serialize_price_history(value, 2) for key, value in first.items()}, sort_keys=True)
    second_json = json.dumps({key: serialize_price_history(value, 2) for key, value in second.items()}, sort_keys=True)
    assert first_json == second_json
    assert len(calls) == 354


def test_fmp_is_only_required_history_secret(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    monkeypatch.setattr(fmp.requests, "get", lambda *a, **k: Response(200, history_rows()))
    assert not historical_eod("AAPL", "2025-08-17", SESSION).empty
    workflow = Path(".github/workflows/chart-packet.yml").read_text()
    daily = Path(".github/workflows/daily-review.yml").read_text()
    assert "ALPACA_API" not in workflow + daily
    assert "FMP_API_KEY" in workflow and "FMP_API_KEY" in daily


def test_rs_line_is_rebased_to_100():
    stock = frame([100, 110, 121], [1, 1, 1])
    benchmark = frame([100, 105, 110], [1, 1, 1])
    result = rs_line(stock, benchmark, stock.index[-1].date().isoformat())
    assert round(result.iloc[0], 8) == 100
    assert result.iloc[-1] > 100
