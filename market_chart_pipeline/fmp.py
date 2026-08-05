from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

import pandas as pd
import requests

BASE_URL = "https://financialmodelingprep.com/stable"


class FMPError(RuntimeError):
    pass


def _key() -> str:
    key = os.getenv("FMP_API_KEY")
    if not key:
        raise FMPError("FMP_API_KEY is not configured")
    return key


def _get(endpoint: str, **params: Any) -> list[dict]:
    headers = {"apikey": _key()}
    response = requests.get(f"{BASE_URL}/{endpoint}", params=params, headers=headers, timeout=45)
    if response.status_code != 200:
        raise FMPError(f"FMP {endpoint} failed ({response.status_code}): {response.text[:250]}")
    payload = response.json()
    if isinstance(payload, dict) and payload.get("Error Message"):
        raise FMPError(f"FMP {endpoint}: {payload['Error Message']}")
    if not isinstance(payload, list):
        raise FMPError(f"FMP {endpoint} returned unexpected payload type")
    return payload


def historical_eod(symbol: str, start: str, end: str) -> pd.DataFrame:
    rows = _get("historical-price-eod/full", symbol=symbol, **{"from": start, "to": end})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    rename = {"date":"Date","open":"Open","high":"High","low":"Low","close":"Close","volume":"Volume"}
    if not set(rename).issubset(df.columns):
        raise FMPError(f"{symbol}: FMP historical response missing OHLCV fields")
    df = df.rename(columns=rename)
    df["Date"] = pd.to_datetime(df["Date"]).dt.normalize()
    return df.set_index("Date")[["Open","High","Low","Close","Volume"]].sort_index()


def profile(symbol: str) -> dict:
    rows = _get("profile", symbol=symbol)
    row = rows[0] if rows else {}
    return {
        "company_name": row.get("companyName"),
        "sector": row.get("sector"),
        "industry": row.get("industry"),
        "market_cap": row.get("marketCap") or row.get("mktCap"),
        "exchange": row.get("exchange") or row.get("exchangeShortName"),
        "country": row.get("country"),
        "is_actively_trading": row.get("isActivelyTrading"),
    }


def quarterly_growth(symbol: str) -> dict:
    rows = _get("income-statement-growth", symbol=symbol, period="quarter", limit=5)
    row = rows[0] if rows else {}
    return {
        "growth_period_end": row.get("date"),
        "revenue_growth_pct": _pct(row.get("growthRevenue")),
        "eps_growth_pct": _pct(row.get("growthEPS") or row.get("growthEps")),
        "net_income_growth_pct": _pct(row.get("growthNetIncome")),
        "operating_income_growth_pct": _pct(row.get("growthOperatingIncome")),
    }


def annual_growth(symbol: str) -> dict:
    rows = _get("income-statement-growth", symbol=symbol, period="annual", limit=4)
    latest = rows[0] if rows else {}
    eps_values = [_pct(r.get("growthEPS") or r.get("growthEps")) for r in rows[:3]]
    eps_values = [v for v in eps_values if v is not None]
    return {
        "annual_growth_period_end": latest.get("date"),
        "annual_revenue_growth_pct": _pct(latest.get("growthRevenue")),
        "annual_eps_growth_pct": _pct(latest.get("growthEPS") or latest.get("growthEps")),
        "three_year_positive_eps_growth_count": sum(v > 0 for v in eps_values),
        "three_year_eps_growth_observations": len(eps_values),
    }


def next_earnings(symbol: str, session_date: str, lookahead_days: int = 120) -> dict:
    start = pd.Timestamp(session_date).date()
    end = start + timedelta(days=lookahead_days)
    rows = _get("earnings-calendar", **{"from": start.isoformat(), "to": end.isoformat()})
    candidates = [r for r in rows if str(r.get("symbol", "")).upper() == symbol.upper() and r.get("date")]
    candidates.sort(key=lambda r: r["date"])
    row = candidates[0] if candidates else {}
    if not row:
        return {"earnings_date": None, "days_to_earnings": None, "earnings_time": None, "eps_estimated": None, "revenue_estimated": None, "earnings_status": "UNVERIFIED"}
    event = pd.Timestamp(row["date"]).date()
    return {
        "earnings_date": event.isoformat(),
        "days_to_earnings": (event - start).days,
        "earnings_time": row.get("time"),
        "eps_estimated": row.get("epsEstimated"),
        "revenue_estimated": row.get("revenueEstimated"),
        "earnings_status": "VERIFIED",
    }


def aftermarket(symbol: str, regular_close: float) -> dict:
    rows = _get("aftermarket-trade", symbol=symbol)
    row = rows[0] if rows else {}
    price = row.get("price")
    if price is None:
        quote_rows = _get("aftermarket-quote", symbol=symbol)
        quote = quote_rows[0] if quote_rows else {}
        bid, ask = quote.get("bidPrice"), quote.get("askPrice")
        if bid is not None and ask is not None:
            price = (float(bid) + float(ask)) / 2
    change = None if price is None or regular_close <= 0 else (float(price) / regular_close - 1) * 100
    return {
        "aftermarket_price": None if price is None else float(price),
        "aftermarket_change_pct": change,
        "aftermarket_timestamp": row.get("timestamp"),
        "aftermarket_status": "AVAILABLE" if price is not None else "UNAVAILABLE",
    }


def compare_ohlcv(alpaca: pd.DataFrame, fmp: pd.DataFrame, session_date: str) -> dict:
    end = pd.Timestamp(session_date).normalize()
    common = alpaca.loc[alpaca.index <= end].join(fmp.loc[fmp.index <= end], lsuffix="_alpaca", rsuffix="_fmp", how="inner").tail(20)
    if common.empty:
        return {"status":"NO_OVERLAP","overlap_sessions":0,"latest_close_diff_pct":None,"latest_volume_diff_pct":None,"median_close_diff_pct_20d":None,"median_volume_diff_pct_20d":None,"material_discrepancy":True}
    close_diff = (common.Close_alpaca / common.Close_fmp - 1).abs() * 100
    volume_diff = (common.Volume_alpaca / common.Volume_fmp - 1).abs() * 100
    latest_close = float(close_diff.iloc[-1]); latest_volume = float(volume_diff.iloc[-1])
    median_close = float(close_diff.median()); median_volume = float(volume_diff.median())
    return {
        "status":"COMPARED",
        "overlap_sessions":len(common),
        "latest_close_diff_pct":latest_close,
        "latest_volume_diff_pct":latest_volume,
        "median_close_diff_pct_20d":median_close,
        "median_volume_diff_pct_20d":median_volume,
        "material_discrepancy": bool(latest_close > 0.5 or median_close > 0.25 or median_volume > 35),
    }


def rs_line(stock: pd.DataFrame, benchmark: pd.DataFrame, session_date: str) -> pd.Series:
    end = pd.Timestamp(session_date).normalize()
    joined = stock.loc[stock.index <= end, ["Close"]].join(benchmark.loc[benchmark.index <= end, ["Close"]], lsuffix="_stock", rsuffix="_benchmark", how="inner")
    series = joined.Close_stock / joined.Close_benchmark
    if series.empty:
        return series
    return series / series.iloc[0] * 100


def enrich_symbol(symbol: str, session_date: str, regular_close: float, alpaca_bars: pd.DataFrame, benchmark: pd.DataFrame) -> tuple[dict, pd.Series]:
    start = (pd.Timestamp(session_date) - pd.Timedelta(days=1100)).date().isoformat()
    fmp_bars = historical_eod(symbol, start, session_date)
    data = {
        "provider":"FMP",
        "profile":profile(symbol),
        "quarterly_growth":quarterly_growth(symbol),
        "annual_growth":annual_growth(symbol),
        "earnings":next_earnings(symbol, session_date),
        "aftermarket":aftermarket(symbol, regular_close),
        "ohlcv_crosscheck":compare_ohlcv(alpaca_bars, fmp_bars, session_date),
    }
    return data, rs_line(alpaca_bars, benchmark, session_date)


def _pct(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value) * 100
    except (TypeError, ValueError):
        return None
