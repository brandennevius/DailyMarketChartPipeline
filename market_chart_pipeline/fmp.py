from __future__ import annotations

import math
import os
import re
import threading
import time
from datetime import timedelta
from typing import Any

import pandas as pd
import requests

BASE_URL = "https://financialmodelingprep.com/stable"
HISTORICAL_ENDPOINT = "historical-price-eod/full"
MAX_HISTORY_CALENDAR_DAYS = 1_200
FX_PAIR_RE = re.compile(r"^[A-Z]{3}/[A-Z]{3}$")
EQUITY_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
BENCHMARK_TICKER_RE = re.compile(r"^\^[A-Z0-9.\-]{1,12}$")


class FMPError(RuntimeError):
    pass


_throttle_lock = threading.Lock()
_next_request_at = 0.0


def _key() -> str:
    key = os.getenv("FMP_API_KEY")
    if not key:
        raise FMPError("FMP_API_KEY is not configured")
    return key


def _positive_float(value: Any, *, field: str, symbol: str, date: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FMPError(f"{symbol}: FMP {field} is not numeric for {date}") from exc
    if not math.isfinite(number) or number <= 0:
        raise FMPError(f"{symbol}: FMP {field} must be positive for {date}")
    return number


def _nonnegative_float(value: Any, *, field: str, symbol: str, date: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FMPError(f"{symbol}: FMP {field} is not numeric for {date}") from exc
    if not math.isfinite(number) or number < 0:
        raise FMPError(f"{symbol}: FMP {field} must be nonnegative for {date}")
    return number


def _bounded_env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


def history_concurrency() -> int:
    return _bounded_env_int("FMP_HISTORY_CONCURRENCY", 4, 1, 8)


def _throttle() -> None:
    """Apply one process-wide request cadence across history and enrichment calls."""
    global _next_request_at
    interval = _bounded_env_float("FMP_MIN_REQUEST_INTERVAL_SECONDS", 0.15, 0.0, 10.0)
    if interval <= 0:
        return
    with _throttle_lock:
        now = time.monotonic()
        delay = max(0.0, _next_request_at - now)
        _next_request_at = max(now, _next_request_at) + interval
    if delay:
        time.sleep(delay)


def _retry_delay(response: requests.Response | None, attempt: int) -> float:
    retry_after = None if response is None else response.headers.get("Retry-After")
    try:
        requested = float(retry_after) if retry_after is not None else 0.0
    except ValueError:
        requested = 0.0
    base = _bounded_env_float("FMP_RETRY_BASE_SECONDS", 0.5, 0.0, 10.0)
    return min(60.0, max(requested, base * (2 ** max(0, attempt - 1))))


def _get(endpoint: str, **params: Any) -> list[dict]:
    attempts = _bounded_env_int("FMP_MAX_ATTEMPTS", 4, 1, 6)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        _throttle()
        response: requests.Response | None = None
        try:
            response = requests.get(
                f"{BASE_URL}/{endpoint}",
                params=params,
                headers={"apikey": _key()},
                timeout=45,
            )
        except requests.RequestException as exc:
            last_error = exc
        else:
            if response.status_code == 200:
                try:
                    payload = response.json()
                except Exception as exc:
                    raise FMPError(f"FMP {endpoint} returned non-JSON content") from exc
                if isinstance(payload, dict) and payload.get("Error Message"):
                    raise FMPError(f"FMP {endpoint}: {payload['Error Message']}")
                if not isinstance(payload, list):
                    raise FMPError(f"FMP {endpoint} returned unexpected payload type")
                if any(not isinstance(row, dict) for row in payload):
                    raise FMPError(f"FMP {endpoint} returned a non-object record")
                return payload
            if response.status_code not in {429, 500, 502, 503, 504}:
                raise FMPError(
                    f"FMP {endpoint} failed ({response.status_code}): {response.text[:250]}"
                )
            last_error = FMPError(f"FMP {endpoint} retryable status {response.status_code}")
        if attempt < attempts:
            time.sleep(_retry_delay(response, attempt))
    raise FMPError(f"FMP {endpoint} failed after {attempts} attempts: {last_error}")


def canonical_fmp_symbol(symbol: str) -> tuple[str, str]:
    original = str(symbol).strip().upper()
    if FX_PAIR_RE.fullmatch(original):
        return original.replace("/", ""), "FOREX"
    if EQUITY_TICKER_RE.fullmatch(original):
        return original, "EQUITY"
    if BENCHMARK_TICKER_RE.fullmatch(original):
        return original, "BENCHMARK"
    raise FMPError(f"{original!r}: unsupported FMP historical symbol")


def _normalized_date(value: str, *, field: str) -> pd.Timestamp:
    try:
        parsed = pd.Timestamp(value)
    except Exception as exc:
        raise FMPError(f"FMP historical {field} is invalid: {value!r}") from exc
    if parsed.tzinfo is not None or parsed.normalize() != parsed:
        raise FMPError(f"FMP historical {field} must be a date-only value")
    return parsed


def historical_eod(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Fetch bounded, exact daily FMP EOD history without quote substitution."""
    provider_symbol, asset_class = canonical_fmp_symbol(symbol)
    start_date = _normalized_date(start, field="from")
    end_date = _normalized_date(end, field="to")
    span = (end_date - start_date).days
    if span < 0 or span > MAX_HISTORY_CALENDAR_DAYS:
        raise FMPError(
            f"{symbol}: FMP historical range must be 0-{MAX_HISTORY_CALENDAR_DAYS} calendar days"
        )
    rows = _get(
        HISTORICAL_ENDPOINT,
        symbol=provider_symbol,
        **{"from": start_date.date().isoformat(), "to": end_date.date().isoformat()},
    )
    if len(rows) > span + 1:
        raise FMPError(f"{symbol}: FMP historical response exceeds the requested date bound")
    normalized: list[dict[str, Any]] = []
    seen_dates: set[pd.Timestamp] = set()
    adjusted_close_presence = [
        row.get("adjClose") is not None or row.get("adjustedClose") is not None for row in rows
    ]
    if any(adjusted_close_presence) and not all(adjusted_close_presence):
        raise FMPError(f"{symbol}: FMP adjusted-close coverage is partial")
    applied_adjustment = False
    volume_missing = False
    for row in rows:
        raw_date = row.get("date")
        try:
            date = pd.Timestamp(str(raw_date))
        except Exception as exc:
            raise FMPError(f"{symbol}: FMP historical response contains invalid date {raw_date!r}") from exc
        if date.tzinfo is not None or date.normalize() != date:
            raise FMPError(f"{symbol}: FMP historical date must be date-only: {raw_date!r}")
        if date < start_date or date > end_date:
            raise FMPError(f"{symbol}: FMP returned out-of-range session {date.date()}")
        if date in seen_dates:
            raise FMPError(f"{symbol}: FMP returned duplicate session {date.date()}")
        seen_dates.add(date)
        date_text = date.date().isoformat()
        open_price = _positive_float(row.get("open"), field="open", symbol=symbol, date=date_text)
        high = _positive_float(row.get("high"), field="high", symbol=symbol, date=date_text)
        low = _positive_float(row.get("low"), field="low", symbol=symbol, date=date_text)
        close = _positive_float(row.get("close"), field="close", symbol=symbol, date=date_text)
        if high < max(open_price, close) or low > min(open_price, close) or high < low:
            raise FMPError(f"{symbol}: FMP OHLC relationship is invalid for {date_text}")
        adjusted_close = row.get("adjClose")
        if adjusted_close is None:
            adjusted_close = row.get("adjustedClose")
        factor = 1.0
        if adjusted_close is not None:
            factor = _positive_float(
                adjusted_close, field="adjusted close", symbol=symbol, date=date_text
            ) / close
            applied_adjustment = applied_adjustment or not math.isclose(factor, 1.0, rel_tol=1e-12)
        raw_volume = row.get("volume")
        if raw_volume is None or raw_volume == "":
            if asset_class != "FOREX":
                raise FMPError(f"{symbol}: FMP volume is missing for {date_text}")
            volume = None
            volume_missing = True
        else:
            volume = _nonnegative_float(raw_volume, field="volume", symbol=symbol, date=date_text)
            if factor != 1.0:
                volume /= factor
        normalized.append(
            {
                "Date": date,
                "Open": open_price * factor,
                "High": high * factor,
                "Low": low * factor,
                "Close": close * factor,
                "Volume": volume,
            }
        )
    if asset_class == "FOREX" and normalized and all(
        item["Volume"] is None or item["Volume"] == 0 for item in normalized
    ):
        for item in normalized:
            item["Volume"] = None
        volume_missing = True
    available_volume_count = sum(item["Volume"] is not None for item in normalized)
    if available_volume_count == len(normalized) and normalized:
        volume_status = "AVAILABLE"
    elif available_volume_count == 0:
        volume_status = "UNAVAILABLE"
    else:
        volume_status = "PARTIAL"
    frame = pd.DataFrame(normalized, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    frame = frame.set_index("Date").sort_index()
    frame.attrs.update(
        {
            "provider": "FMP",
            "endpoint": f"stable/{HISTORICAL_ENDPOINT}",
            "original_symbol": str(symbol).strip().upper(),
            "provider_symbol": provider_symbol,
            "asset_class": asset_class,
            "from": start_date.date().isoformat(),
            "to": end_date.date().isoformat(),
            "adjustment": "ADJUSTED_CLOSE_FACTOR" if applied_adjustment else "FMP_EOD_AS_RETURNED",
            "volume_status": volume_status,
            "live_quote_substitution": False,
        }
    )
    return frame


def price_history_metadata(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "provider": frame.attrs.get("provider", "FMP"),
        "endpoint": frame.attrs.get("endpoint", f"stable/{HISTORICAL_ENDPOINT}"),
        "original_symbol": frame.attrs.get("original_symbol"),
        "provider_symbol": frame.attrs.get("provider_symbol"),
        "asset_class": frame.attrs.get("asset_class"),
        "requested_from": frame.attrs.get("from"),
        "requested_to": frame.attrs.get("to"),
        "first_session": None if frame.empty else frame.index[0].date().isoformat(),
        "last_session": None if frame.empty else frame.index[-1].date().isoformat(),
        "session_count": len(frame),
        "adjustment": frame.attrs.get("adjustment"),
        "volume_status": frame.attrs.get("volume_status"),
        "live_quote_substitution": False,
    }


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


def rs_line(stock: pd.DataFrame, benchmark: pd.DataFrame, session_date: str) -> pd.Series:
    end = pd.Timestamp(session_date).normalize()
    joined = stock.loc[stock.index <= end, ["Close"]].join(
        benchmark.loc[benchmark.index <= end, ["Close"]],
        lsuffix="_stock",
        rsuffix="_benchmark",
        how="inner",
    )
    series = joined.Close_stock / joined.Close_benchmark
    if series.empty:
        return series
    return series / series.iloc[0] * 100


def enrich_symbol(
    symbol: str,
    session_date: str,
    price_bars: pd.DataFrame,
    benchmark: pd.DataFrame,
) -> tuple[dict, pd.Series]:
    asset_class = price_bars.attrs.get("asset_class")
    price_evidence = price_history_metadata(price_bars)
    if asset_class == "FOREX":
        data = {
            "provider": "FMP",
            "profile": {"status": "NOT_APPLICABLE_FOR_FOREX"},
            "quarterly_growth": {"status": "NOT_APPLICABLE_FOR_FOREX"},
            "annual_growth": {"status": "NOT_APPLICABLE_FOR_FOREX"},
            "earnings": {"earnings_status": "NOT_APPLICABLE_FOR_FOREX"},
            "aftermarket": {
                "aftermarket_status": "UNAVAILABLE",
                "reason": "Live/current quote substitution is excluded from historical reviews.",
            },
            "historical_price_evidence": price_evidence,
        }
    else:
        data = {
            "provider": "FMP",
            "profile": profile(symbol),
            "quarterly_growth": quarterly_growth(symbol),
            "annual_growth": annual_growth(symbol),
            "earnings": next_earnings(symbol, session_date),
            "aftermarket": {
                "aftermarket_status": "UNAVAILABLE",
                "reason": "Live/current quote substitution is excluded from historical reviews.",
            },
            "historical_price_evidence": price_evidence,
        }
    return data, rs_line(price_bars, benchmark, session_date)


def _pct(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value) * 100
    except (TypeError, ValueError):
        return None
