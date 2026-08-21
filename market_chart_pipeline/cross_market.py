from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .fmp import _get
from .utils import canonical_json, sha256_text


NEWS_ENDPOINTS = {
    "general": "news/general-latest",
    "stock": "news/stock-latest",
    "forex": "news/forex-latest",
    "crypto": "news/crypto-latest",
}
CONTEXT_ENDPOINTS = {
    **NEWS_ENDPOINTS,
    "economic_calendar": "economic-calendar",
    "treasury_rates": "treasury-rates",
}
THEME_TERMS = {
    "central_banks_rates": ("federal reserve", "fed ", "interest rate", "rate cut", "rate hike", "treasury", "yield"),
    "inflation_growth": ("inflation", "cpi", "ppi", "gdp", "jobs", "payroll", "unemployment", "retail sales"),
    "energy_commodities": ("oil", "crude", "natural gas", "gold", "commodity"),
    "foreign_exchange": ("forex", "currency", "dollar", "yen", "euro", "sterling"),
    "digital_assets": ("bitcoin", "crypto", "ethereum", "digital asset"),
    "corporate_risk": ("earnings", "guidance", "merger", "acquisition", "bankruptcy", "default"),
}


def _date_only(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("cross-market session must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("cross-market session must use YYYY-MM-DD")
    return parsed


def _window(session_date: str, lookback_calendar_days: int = 3) -> dict[str, Any]:
    session = _date_only(session_date)
    start = session - timedelta(days=max(1, lookback_calendar_days) - 1)
    return {
        "start_date": start.isoformat(),
        "end_date": session.isoformat(),
        "lookback_calendar_days": max(1, lookback_calendar_days),
        "timezone": "America/New_York",
        "rule": "Inclusive New York calendar dates ending on the completed session.",
    }


def _endpoint_result(
    fetcher: Callable[..., list[dict]], endpoint: str, params: dict[str, Any]
) -> dict[str, Any]:
    try:
        rows = fetcher(endpoint, **params)
    except Exception as exc:
        message = str(exc).replace("\n", " ")[:300]
        return {
            "endpoint": f"stable/{endpoint}",
            "params": params,
            "status": "INSUFFICIENT_EVIDENCE",
            "records": [],
            "error": message or "FMP endpoint unavailable",
        }
    return {
        "endpoint": f"stable/{endpoint}",
        "params": params,
        "status": "AVAILABLE" if rows else "INSUFFICIENT_EVIDENCE",
        "records": rows,
        "error": None if rows else "FMP returned no records.",
    }


def collect_fmp_cross_market_raw(
    session_date: str,
    *,
    fetcher: Callable[..., list[dict]] | None = None,
    retrieved_at: str | None = None,
    lookback_calendar_days: int = 3,
) -> dict[str, Any]:
    """Collect bounded FMP context. Failures become frozen evidence gaps, never trading signals."""
    source = fetcher or _get
    window = _window(session_date, lookback_calendar_days)
    endpoint_results: dict[str, Any] = {}
    for category, endpoint in NEWS_ENDPOINTS.items():
        endpoint_results[category] = _endpoint_result(source, endpoint, {"page": 0, "limit": 100})
    exact_params = {"from": session_date, "to": session_date}
    endpoint_results["economic_calendar"] = _endpoint_result(
        source, CONTEXT_ENDPOINTS["economic_calendar"], exact_params
    )
    endpoint_results["treasury_rates"] = _endpoint_result(
        source, CONTEXT_ENDPOINTS["treasury_rates"], exact_params
    )
    return {
        "schema_version": "fmp_cross_market_raw_v1",
        "provider": "FMP",
        "session_date": session_date,
        "retrieved_at": retrieved_at or datetime.now(timezone.utc).isoformat(),
        "lookback_window": window,
        "endpoint_results": endpoint_results,
        "api_key_in_payload": False,
    }


def _article_timestamp(row: dict[str, Any]) -> str | None:
    value = row.get("publishedDate") or row.get("publishedAt") or row.get("date")
    text = str(value).strip() if value is not None else ""
    return text or None


def _article_url(row: dict[str, Any]) -> str | None:
    value = str(row.get("url") or row.get("link") or "").strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def _dedupe_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def _themes(title: str) -> list[str]:
    haystack = f" {title.lower()} "
    return sorted(label for label, terms in THEME_TERMS.items() if any(term in haystack for term in terms))


def _normalize_news(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    window = raw["lookback_window"]
    start_date = window["start_date"]
    end_date = window["end_date"]
    accepted: list[dict[str, Any]] = []
    rejected = {"missing_required_fields": 0, "outside_window": 0, "duplicate": 0}
    seen: set[str] = set()
    for category in NEWS_ENDPOINTS:
        endpoint = raw["endpoint_results"][category]
        for row in endpoint.get("records") or []:
            title = str(row.get("title") or "").strip()
            publisher = str(row.get("site") or row.get("publisher") or row.get("source") or "").strip()
            published_at = _article_timestamp(row)
            url = _article_url(row)
            if not title or not publisher or not published_at or not url or len(published_at) < 10:
                rejected["missing_required_fields"] += 1
                continue
            published_date = published_at[:10]
            if published_date < start_date or published_date > end_date:
                rejected["outside_window"] += 1
                continue
            key = _dedupe_url(url) or f"{publisher.lower()}|{title.lower()}"
            if key in seen:
                rejected["duplicate"] += 1
                continue
            seen.add(key)
            symbols = row.get("symbol") or row.get("symbols") or []
            if isinstance(symbols, str):
                symbols = [item.strip().upper() for item in symbols.split(",") if item.strip()]
            elif not isinstance(symbols, list):
                symbols = []
            accepted.append(
                {
                    "category": category,
                    "title": title,
                    "publisher": publisher,
                    "published_at": published_at,
                    "published_timezone": "PROVIDER_VALUE" if published_at.endswith("Z") or "+" in published_at[10:] else "UNSPECIFIED",
                    "url": url,
                    "symbols": sorted({str(item).upper() for item in symbols if item}),
                    "themes": _themes(title),
                }
            )
    accepted.sort(key=lambda item: (item["published_at"], item["category"], item["title"], item["url"]), reverse=True)
    return accepted, rejected


def _exact_session_records(result: dict[str, Any], session_date: str) -> list[dict[str, Any]]:
    records = []
    for row in result.get("records") or []:
        raw_date = str(row.get("date") or "")[:10]
        if raw_date == session_date:
            records.append(row)
    return records


def _economic_events(raw: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _exact_session_records(raw["endpoint_results"]["economic_calendar"], raw["session_date"])
    events = []
    for row in rows:
        event = str(row.get("event") or row.get("name") or "").strip()
        if not event:
            continue
        events.append(
            {
                "date": str(row.get("date")),
                "event": event,
                "country": row.get("country"),
                "impact": row.get("impact"),
                "actual": row.get("actual"),
                "estimate": row.get("estimate"),
                "previous": row.get("previous"),
                "unit": row.get("unit"),
            }
        )
    return sorted(events, key=lambda item: (item["date"], item["country"] or "", item["event"]))


def _treasury_context(raw: dict[str, Any]) -> dict[str, Any] | None:
    rows = _exact_session_records(raw["endpoint_results"]["treasury_rates"], raw["session_date"])
    if not rows:
        return None
    row = sorted(rows, key=lambda item: str(item.get("date")))[-1]
    maturities = {
        key: row.get(key)
        for key in ("month1", "month2", "month3", "month6", "year1", "year2", "year5", "year10", "year20", "year30")
        if row.get(key) is not None
    }
    return {"date": str(row.get("date")), "maturities_pct": maturities}


def normalize_cross_market_context(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("schema_version") != "fmp_cross_market_raw_v1":
        raise ValueError("cross-market raw schema is unsupported")
    session_date = str(raw.get("session_date") or "")
    _date_only(session_date)
    news, rejected = _normalize_news(raw)
    economic = _economic_events(raw)
    treasury = _treasury_context(raw)
    for index, article in enumerate(news, start=1):
        article["evidence_id"] = f"FMP_NEWS_{index:03d}"
    for index, event in enumerate(economic, start=1):
        event["evidence_id"] = f"FMP_ECON_{index:03d}"
    if treasury:
        treasury["evidence_id"] = "FMP_TREASURY_001"
    cited: list[dict[str, Any]] = []
    for category in NEWS_ENDPOINTS:
        cited.extend([item for item in news if item["category"] == category][:2])

    stream_status = {
        category: "AVAILABLE" if any(item["category"] == category for item in news) else "INSUFFICIENT_EVIDENCE"
        for category in NEWS_ENDPOINTS
    }
    stream_status["economic_calendar"] = "AVAILABLE" if economic else "INSUFFICIENT_EVIDENCE"
    stream_status["treasury_rates"] = "AVAILABLE" if treasury else "INSUFFICIENT_EVIDENCE"
    available_count = sum(status == "AVAILABLE" for status in stream_status.values())
    status = "AVAILABLE" if available_count == len(stream_status) else "PARTIAL" if available_count else "INSUFFICIENT_EVIDENCE"
    gaps = [f"{label}: insufficient evidence" for label, value in stream_status.items() if value != "AVAILABLE"]
    raw_hash = sha256_text(canonical_json(raw))
    return {
        "schema_version": "fmp_cross_market_context_v1",
        "status": status,
        "provider": "FMP",
        "session_date": session_date,
        "lookback_window": raw["lookback_window"],
        "retrieved_at": raw.get("retrieved_at"),
        "raw_input_sha256": raw_hash,
        "raw_inputs": raw,
        "stream_status": stream_status,
        "news": news,
        "cited_context": cited,
        "economic_calendar": economic,
        "treasury_context": treasury,
        "rejected_article_counts": rejected,
        "evidence_gaps": gaps,
        "interpretation": {
            "decision_influence": "INTERPRETATION_ONLY",
            "may_override_deterministic_outputs": False,
            "statement": "Cross-market context may explain measured conditions but cannot change regime, exposure, portfolio, candidate, or sell-rule actions.",
        },
        "llm_synthesis_boundary": {
            "status": "PENDING",
            "network_access_scope": "OPENAI_RESPONSES_API_ONLY",
            "autonomous_web_or_tool_access": False,
            "allowed_frozen_source_sha256": raw_hash,
            "instruction": "The optional synthesis may consume only this normalized frozen FMP context plus the frozen Dashboard Market Gauge.",
        },
    }


def collect_fmp_cross_market_context(
    session_date: str,
    *,
    fetcher: Callable[..., list[dict]] | None = None,
    retrieved_at: str | None = None,
    lookback_calendar_days: int = 3,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = collect_fmp_cross_market_raw(
        session_date,
        fetcher=fetcher,
        retrieved_at=retrieved_at,
        lookback_calendar_days=lookback_calendar_days,
    )
    return raw, normalize_cross_market_context(raw)
