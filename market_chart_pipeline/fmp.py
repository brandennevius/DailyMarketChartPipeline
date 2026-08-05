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
    try:
        payload = response.json()
    except Exception as exc:
        raise FMPError(f"FMP {endpoint} returned non-JSON content") from exc
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
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.normalize()
    if df["Date"].isna().any():
        raise FMPError(f"{symbol}: FMP historical response contains invalid dates")
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


def _parse_earnings_rows(rows: list[dict], symbol: str, start) -> dict | None:
    candidates = []
    for row in rows:
        row_symbol = str(row.get("symbol", symbol)).upper()
        if row_symbol != symbol.upper():
            continue
        raw_date = row.get("date")
        if not raw_date:
            continue
        try:
            event = pd.Timestamp(raw_date).date()
        except Exception as exc:
            raise ValueError(f"invalid earnings date {raw_date!r}") from exc
        if event >= start:
            candidates.append((event, row))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    event, row = candidates[0]
    return {
        "earnings_date": event.isoformat(),
        "days_to_earnings": (event - start).days,
        "earnings_time": row.get("time") or row.get("when"),
        "eps_estimated": row.get("epsEstimated") if row.get("epsEstimated") is not None else row.get("epsEstimatedFuture"),
        "revenue_estimated": row.get("revenueEstimated") if row.get("revenueEstimated") is not None else row.get("revenueEstimatedFuture"),
        "earnings_status": "VERIFIED",
    }


def next_earnings(symbol: str, session_date: str, lookahead_days: int = 180) -> dict:
    start = pd.Timestamp(session_date).date()
    end = start + timedelta(days=lookahead_days)
    endpoint_errors: list[str] = []

    # Prefer the symbol-specific endpoint, then fall back to the broad calendar.
    for endpoint, params in (
        ("earnings", {"symbol": symbol, "limit": 20}),
        ("earnings-calendar", {"from": start.isoformat(), "to": end.isoformat()}),
    ):
        try:
            rows = _get(endpoint, **params)
        except Exception as exc:
            endpoint_errors.append(f"{endpoint}: {exc}")
            continue
        try:
            parsed = _parse_earnings_rows(rows, symbol, start)
        except Exception as exc:
            return {
                "earnings_date": None,
                "days_to_earnings": None,
                "earnings_time": None,
                "eps_estimated": None,
                "revenue_estimated": None,
                "earnings_status": "PARSING_ERROR",
                "earnings_source": endpoint,
                "earnings_error": str(exc),
            }
        if parsed:
            parsed["earnings_source"] = endpoint
            parsed["earnings_error"] = None
            return parsed

    if len(endpoint_errors) == 2:
        return {
            "earnings_date": None,
            "days_to_earnings": None,
            "earnings_time": None,
            "eps_estimated": None,
            "revenue_estimated": None,
            "earnings_status": "ENDPOINT_ERROR",
            "earnings_source": None,
            "earnings_error": " | ".join(endpoint_errors),
        }
    return {
        "earnings_date": None,
        "days_to_earnings": None,
        "earnings_time": None,
        "eps_estimated": None,
        "revenue_estimated": None,
        "earnings_status": "NO_UPCOMING_DATE_RETURNED",
        "earnings_source": "earnings + earnings-calendar",
        "earnings_error": " | ".join(endpoint_errors) if endpoint_errors else None,
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


def _pct_diff(left: pd.Series, right: pd.Series) -> pd.Series:
    denominator = right.abs().replace(0, pd.NA)
    return ((left - right).abs() / denominator * 100).astype(float)


def compare_ohlcv(alpaca: pd.DataFrame, fmp: pd.DataFrame, session_date: str) -> dict:
    end = pd.Timestamp(session_date).normalize()
    common = alpaca.loc[alpaca.index <= end].join(
        fmp.loc[fmp.index <= end], lsuffix="_alpaca", rsuffix="_fmp", how="inner"
    ).tail(20)
    if common.empty:
        return {
            "status":"NO_OVERLAP",
            "severity":"CRITICAL",
            "classification":"NO_COMPARABLE_SESSIONS",
            "overlap_sessions":0,
            "latest_close_diff_pct":None,
            "latest_high_diff_pct":None,
            "latest_low_diff_pct":None,
            "latest_volume_diff_pct":None,
            "median_close_diff_pct_20d":None,
            "median_high_diff_pct_20d":None,
            "median_low_diff_pct_20d":None,
            "median_volume_diff_pct_20d":None,
            "price_mismatch_sessions":0,
            "volume_mismatch_sessions":0,
            "price_critical":True,
            "volume_only":False,
        }

    close_diff = _pct_diff(common.Close_alpaca, common.Close_fmp)
    high_diff = _pct_diff(common.High_alpaca, common.High_fmp)
    low_diff = _pct_diff(common.Low_alpaca, common.Low_fmp)
    volume_diff = _pct_diff(common.Volume_alpaca, common.Volume_fmp)

    latest_close = float(close_diff.iloc[-1]); latest_high = float(high_diff.iloc[-1]); latest_low = float(low_diff.iloc[-1]); latest_volume = float(volume_diff.iloc[-1])
    med_close = float(close_diff.median()); med_high = float(high_diff.median()); med_low = float(low_diff.median()); med_volume = float(volume_diff.median())
    price_mismatch_sessions = int(((close_diff > 0.5) | (high_diff > 0.75) | (low_diff > 0.75)).sum())
    volume_mismatch_sessions = int((volume_diff > 35).sum())

    price_critical = bool(latest_close > 1.0 or med_close > 0.5 or max(latest_high, latest_low) > 1.5 or price_mismatch_sessions >= 3)
    price_warning = bool(latest_close > 0.25 or med_close > 0.15 or max(latest_high, latest_low) > 0.75 or price_mismatch_sessions > 0)
    volume_warning = bool(latest_volume > 35 or med_volume > 35 or volume_mismatch_sessions > 0)

    if price_critical:
        severity = "CRITICAL"
        classification = "PRICE_HISTORY_CONFLICT"
    elif price_warning:
        severity = "WARNING"
        classification = "MINOR_PRICE_DIFFERENCE"
    elif volume_warning:
        severity = "INFO"
        classification = "VOLUME_SOURCE_MISMATCH"
    else:
        severity = "NONE"
        classification = "MATCH"

    return {
        "status":"COMPARED",
        "severity":severity,
        "classification":classification,
        "overlap_sessions":len(common),
        "latest_close_diff_pct":latest_close,
        "latest_high_diff_pct":latest_high,
        "latest_low_diff_pct":latest_low,
        "latest_volume_diff_pct":latest_volume,
        "median_close_diff_pct_20d":med_close,
        "median_high_diff_pct_20d":med_high,
        "median_low_diff_pct_20d":med_low,
        "median_volume_diff_pct_20d":med_volume,
        "price_mismatch_sessions":price_mismatch_sessions,
        "volume_mismatch_sessions":volume_mismatch_sessions,
        "price_critical":price_critical,
        "volume_only":bool(classification == "VOLUME_SOURCE_MISMATCH"),
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
