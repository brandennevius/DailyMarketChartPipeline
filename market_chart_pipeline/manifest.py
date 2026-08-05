from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import ValidationError, normalize_symbols

ALLOWED_SOURCE_TYPES = {"STANDARD_MARKETSURGE", "BRANDENS_WATCHLIST", "PORTFOLIO"}
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


@dataclass(frozen=True)
class ManifestRequest:
    session_date: str
    feed: str
    tickers: list[str]
    records_by_ticker: dict[str, dict[str, Any]]
    raw: dict[str, Any]


def load_manifest(path: str | Path) -> ManifestRequest:
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValidationError(f"manifest could not be read: {exc}") from exc

    if payload.get("status") not in {"COMPLETE", "COMPLETE_WITH_WARNINGS"}:
        raise ValidationError("manifest status must be COMPLETE or COMPLETE_WITH_WARNINGS")

    session_date = str(payload.get("session_date", "")).strip()
    try:
        import pandas as pd
        normalized_date = pd.Timestamp(session_date).date().isoformat()
    except Exception as exc:
        raise ValidationError("manifest session_date is invalid") from exc
    if normalized_date != session_date:
        raise ValidationError("manifest session_date must use YYYY-MM-DD")

    feed = str(payload.get("feed", "iex")).lower()
    if feed not in {"iex", "sip"}:
        raise ValidationError("manifest feed must be iex or sip")

    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise ValidationError("manifest records must be a non-empty list")

    by_ticker: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValidationError(f"manifest record {index} is not an object")
        ticker = str(record.get("ticker", "")).strip().upper()
        if not TICKER_RE.fullmatch(ticker):
            raise ValidationError(f"manifest record {index} has invalid ticker")
        sources = record.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValidationError(f"{ticker}: at least one verified source is required")
        cleaned_sources = []
        for source in sources:
            if not isinstance(source, dict):
                raise ValidationError(f"{ticker}: source must be an object")
            source_type = str(source.get("source_type", "")).strip().upper()
            if source_type not in ALLOWED_SOURCE_TYPES:
                raise ValidationError(f"{ticker}: invalid source_type {source_type}")
            label = str(source.get("label", "")).strip()
            page = source.get("pdf_page")
            if not label:
                raise ValidationError(f"{ticker}: source label is required")
            if source_type != "PORTFOLIO" and (not isinstance(page, int) or page < 1):
                raise ValidationError(f"{ticker}: positive pdf_page is required")
            cleaned_sources.append({"source_type": source_type, "label": label, "pdf_page": page})
        merged = by_ticker.setdefault(ticker, {"ticker": ticker, "sources": [], "chart_required": False})
        for source in cleaned_sources:
            if source not in merged["sources"]:
                merged["sources"].append(source)
        merged["chart_required"] = bool(merged["chart_required"] or record.get("chart_required", False))

    tickers = normalize_symbols(by_ticker.keys())
    expected = payload.get("unique_ticker_count")
    if expected is not None and expected != len(tickers):
        raise ValidationError(
            f"manifest unique_ticker_count={expected} does not match verified unique rows={len(tickers)}"
        )

    return ManifestRequest(session_date, feed, tickers, by_ticker, payload)
