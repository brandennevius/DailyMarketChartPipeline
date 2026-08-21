import json
from email.message import EmailMessage
from pathlib import Path

import pytest
import pandas as pd

from market_chart_pipeline.adapters import derive_candidates_from_chart, normalize_portfolio_snapshot
from market_chart_pipeline.core import ValidationError
from market_chart_pipeline.cross_market import normalize_cross_market_context
from market_chart_pipeline.market_gauge import normalize_dashboard_market_gauge
from market_chart_pipeline.llm_context import synthesize_cross_market_context
from market_chart_pipeline.orchestrator import run_daily_review
from market_chart_pipeline.utils import sha256_file


SESSION = "2026-08-14"


def _price_history():
    dates = pd.bdate_range(end=SESSION, periods=80)
    return [
        {
            "date": day.date().isoformat(),
            "open": 90 + index * 0.24,
            "high": 91 + index * 0.24,
            "low": 89 + index * 0.24,
            "close": 90.5 + index * 0.24,
            "volume": 1_000_000 + index * 1_000,
        }
        for index, day in enumerate(dates)
    ]


def _snapshot():
    return {
        "metadata": {
            "latest_completed_market_session": SESSION,
            "broker_import_complete": True,
            "price_data_as_of": f"{SESSION}T16:00:00-04:00",
            "account_value": 100000,
        },
        "portfolio_summary": {"open_position_count": 1, "gross_exposure_pct": 2, "total_remaining_risk_pct": 0.2},
        "open_positions": [
            {
                "ticker": "MSFT",
                "side": "LONG",
                "average_entry": 100,
                "entry_date": "2026-08-01",
                "current_price": 110,
                "current_stop": 95,
                "trade_id": "trade-1",
            }
        ],
    }


def _chart_payload(pdf_hash):
    return {
        "session_date": SESSION,
        "chart_data_source": "FMP",
        "chart_data_endpoint": "stable/historical-price-eod/full",
        "chart_data_policy": {"live_quote_substitution": False},
        "status": "COMPLETE_WITH_WARNINGS",
        "requested_tickers": ["MSFT", "MISS"],
        "verified_count": 1,
        "error_count": 1,
        "errors": {"MISS": "no bars"},
        "source_manifest": {
            "records": {
                "MSFT": {"sources": [{"source_type": "PORTFOLIO", "label": "Current Portfolio"}]},
                "MISS": {"sources": [{"source_type": "STANDARD_MARKETSURGE", "label": "Near Pivot"}]},
            }
        },
        "artifacts": {"pdf_sha256": pdf_hash},
        "records": [
            {
                "metrics": {"ticker": "MSFT", "current_price": 110, "sma50": 100, "sma200": 90},
                "technical_context": {
                    "relative_strength": {"trend_21d": "RISING", "new_high_52w": True},
                    "volume": {"up_down_volume_ratio_20": 1.5},
                    "base_analysis": {"status": "CANDIDATE_ONLY", "pivot_price": None, "pivot_status": "VISUAL_CONFIRMATION_REQUIRED"},
                },
                "fmp": {},
                "sources": [{"source_type": "PORTFOLIO", "label": "Current Portfolio"}],
                "latest_bar_date": SESSION,
                "daily_chart": "daily.png",
                "weekly_chart": "weekly.png",
                "price_history": _price_history(),
            }
        ],
    }


def test_portfolio_snapshot_adapter_rejects_stale_prices():
    snapshot = _snapshot()
    snapshot["metadata"]["price_data_as_of"] = "2026-08-13T16:00:00-04:00"
    with pytest.raises(ValidationError, match="prices"):
        normalize_portfolio_snapshot(snapshot, SESSION)


def test_chart_adapter_preserves_failed_tickers_as_non_actionable_candidates(tmp_path):
    candidates = derive_candidates_from_chart(_chart_payload("unused"))
    assert {item["ticker"] for item in candidates} == {"MSFT", "MISS"}
    missing = next(item for item in candidates if item["ticker"] == "MISS")
    assert missing["pivot_verification_status"] == "unverified"
    assert missing["chart_error"] == "no bars"


def test_strict_core_run_checks_sources_hashes_and_set_relationships(tmp_path):
    chart_dir = tmp_path / "chart"
    chart_dir.mkdir()
    pdf = chart_dir / f"Market_Chart_Packet_{SESSION}.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    chart = _chart_payload(sha256_file(pdf))
    (chart_dir / f"Market_Chart_Data_{SESSION}.json").write_text(json.dumps(chart), encoding="utf-8")

    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(json.dumps(_snapshot()), encoding="utf-8")
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(b"scan")
    artifact = tmp_path / "artifact.zip"
    artifact.write_bytes(b"artifact")
    gauge = tmp_path / "market-gauge.json"
    gauge.write_text("{}", encoding="utf-8")
    sources = []
    for label, path in [("portfolio_snapshot", portfolio), ("marketsurge_scan", scan), ("market_gauge_json", gauge), ("chart_packet_artifact", artifact)]:
        sources.append({"label": label, "path": str(path), "sha256": sha256_file(path), "status": "verified"})
    manifest = tmp_path / "source-manifest.json"
    manifest.write_text(json.dumps({"session_date": SESSION, "sources": sources}), encoding="utf-8")

    result = run_daily_review(
        requested_date=SESSION,
        session_date=SESSION,
        mode="read-only",
        output_dir=tmp_path / "reports",
        portfolio_path=str(portfolio),
        chart_packet_dir=str(chart_dir),
        source_manifest_path=str(manifest),
        audit_profile="strict-core",
    )

    gates = {item["gate"] for item in result["packet"]["validation_evidence"]}
    assert "strict_core_sources" in gates
    assert "set_relationships" in gates
    assert {item["ticker"] for item in result["packet"]["candidate_results"]} == {"MSFT", "MISS"}
    assert result["packet"]["sell_rule_results"][0]["ticker"] == "MSFT"
    assert "sell_sandbox_charts" in gates
    sandbox = result["packet"]["sell_rule_results"][0]["position_snapshot"]["sell_sandbox_asset"]
    assert sandbox["status"] == "verified"
    assert sandbox["sha256"]


def test_production_shaped_review_ranks_global_marketsurge_top10_and_preserves_full_json_coverage(tmp_path):
    tickers = [f"WL{index:02d}" for index in range(1, 16)]
    chart_dir = tmp_path / "chart"
    chart_dir.mkdir()
    pdf = chart_dir / f"Market_Chart_Packet_{SESSION}.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    records = [_chart_payload("unused")["records"][0]]
    manifest_records = {
        "MSFT": {"sources": [{"source_type": "PORTFOLIO", "label": "Current Portfolio"}]}
    }
    for index, ticker in enumerate(tickers, start=1):
        source = {
            "source_type": "BRANDENS_WATCHLIST" if index % 2 else "STANDARD_MARKETSURGE",
            "label": "BRANDENS WATCHLIST" if index % 2 else "BREAKING OUT TODAY",
            "pdf_page": 8 if index % 2 else 3,
            "rank": index,
        }
        manifest_records[ticker] = {"sources": [source]}
        records.append({
            "metrics": {"ticker": ticker, "asset_class": "EQUITY", "current_price": 25 + index / 10, "sma21": 24, "sma50": 23, "sma200": 20, "avg_dollar_volume_50": 30_000_000, "quantitative_gate": "CHART_REVIEW"},
            "technical_context": {
                "relative_strength": {"status": "VERIFIED", "trend_21d": "RISING", "change_21d_pct": 1 + index / 10, "new_high_52w": index % 3 == 0},
                "volume": {"status": "VERIFIED", "up_down_volume_ratio_20": 1.2, "accumulation_distribution_estimate": "POSITIVE"},
                "base_analysis": {"status": "CANDIDATE_ONLY", "pivot_status": "VISUAL_CONFIRMATION_REQUIRED", "candidate_resistance": 26, "candidate_resistance_distance_pct": -2 + index / 20, "base_length_weeks": 7, "base_depth_pct": 18},
            },
            "fmp": {},
            "sources": [source],
            "latest_bar_date": SESSION,
            "daily_chart": f"{ticker}-daily.png",
            "weekly_chart": f"{ticker}-weekly.png",
            "price_history": _price_history(),
        })
    requested = ["MSFT", *tickers]
    payload = {
        "session_date": SESSION,
        "chart_data_source": "FMP",
        "chart_data_endpoint": "stable/historical-price-eod/full",
        "chart_data_policy": {"live_quote_substitution": False},
        "status": "COMPLETE",
        "requested_tickers": requested,
        "verified_count": len(requested),
        "error_count": 0,
        "errors": {},
        "source_manifest": {"records": manifest_records},
        "artifacts": {"pdf_sha256": sha256_file(pdf)},
        "records": records,
    }
    (chart_dir / f"Market_Chart_Data_{SESSION}.json").write_text(json.dumps(payload), encoding="utf-8")
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(json.dumps(_snapshot()), encoding="utf-8")
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(b"scan")
    gauge_payload = {
        "schema_version": "dashboard_market_gauge_v1",
        "session_date": SESSION,
        "generated_at": "2026-08-14T21:00:00Z",
        "overall_state": "Neutral",
        "overall_score": 50,
        "components": [{"label": "Short term", "state": "Neutral", "detail": "Mixed indexes vs 21EMA"}],
        "index_regimes": [
            {
                "symbol": symbol,
                "date": SESSION,
                "close": 100 + index,
                "ema21": 99 + index,
                "sma50": 98 + index,
                "sma200": 90 + index,
                "shortTerm": "Up",
                "mediumTerm": "Neutral",
                "longTerm": "Up",
                "rawShortTerm": "Up",
                "rawMediumTerm": "Up",
                "rawLongTerm": "Up",
                "above21Percent": 1.0,
                "above50Percent": 2.0,
                "extension": "Normal",
            }
            for index, symbol in enumerate(["SPY", "QQQ", "IWM"])
        ],
        "universe": {"indexes": ["SPY", "QQQ", "IWM"]},
        "providers": ["Stooq", "Yahoo fallback"],
    }
    gauge = tmp_path / "market-gauge.json"
    gauge.write_text(json.dumps(gauge_payload), encoding="utf-8")
    cross_raw = {
        "schema_version": "fmp_cross_market_raw_v1",
        "provider": "FMP",
        "session_date": SESSION,
        "retrieved_at": "2026-08-14T21:05:00Z",
        "lookback_window": {
            "start_date": "2026-08-12",
            "end_date": SESSION,
            "lookback_calendar_days": 3,
            "timezone": "America/New_York",
            "rule": "Inclusive New York calendar dates ending on the completed session.",
        },
        "endpoint_results": {
            "general": {"endpoint": "stable/news/general-latest", "params": {"page": 0, "limit": 100}, "status": "AVAILABLE", "error": None, "records": [{"title": "Treasury yields move", "site": "Example Wire", "publishedDate": "2026-08-14T14:00:00Z", "url": "https://example.com/rates"}]},
            "stock": {"endpoint": "stable/news/stock-latest", "params": {"page": 0, "limit": 100}, "status": "INSUFFICIENT_EVIDENCE", "error": "No records", "records": []},
            "forex": {"endpoint": "stable/news/forex-latest", "params": {"page": 0, "limit": 100}, "status": "INSUFFICIENT_EVIDENCE", "error": "No records", "records": []},
            "crypto": {"endpoint": "stable/news/crypto-latest", "params": {"page": 0, "limit": 100}, "status": "INSUFFICIENT_EVIDENCE", "error": "No records", "records": []},
            "economic_calendar": {"endpoint": "stable/economic-calendar", "params": {"from": SESSION, "to": SESSION}, "status": "AVAILABLE", "error": None, "records": [{"date": f"{SESSION} 08:30:00", "event": "CPI", "country": "US", "impact": "High", "actual": 2.7, "estimate": 2.8}]},
            "treasury_rates": {"endpoint": "stable/treasury-rates", "params": {"from": SESSION, "to": SESSION}, "status": "AVAILABLE", "error": None, "records": [{"date": SESSION, "year2": 4.0, "year10": 4.2, "year30": 4.7}]},
        },
        "api_key_in_payload": False,
    }
    cross_source = tmp_path / "fmp-cross-market-context.json"
    cross_source.write_text(json.dumps(cross_raw), encoding="utf-8")
    market_data = normalize_dashboard_market_gauge(gauge_payload, SESSION)
    cross_context = normalize_cross_market_context(cross_raw)
    synthesis = synthesize_cross_market_context(
        SESSION,
        cross_context,
        market_data["market_regime"],
        api_key="",
        generated_at="2026-08-14T21:06:00Z",
    )
    cross_context["llm_synthesis"] = synthesis
    market_data["cross_market_context"] = cross_context
    synthesis_source = tmp_path / "openai-cross-market-synthesis.json"
    synthesis_source.write_text(json.dumps(synthesis), encoding="utf-8")
    market_data_path = tmp_path / "market-data.json"
    market_data_path.write_text(json.dumps(market_data), encoding="utf-8")
    archive = tmp_path / "chart.zip"
    archive.write_bytes(b"chart archive")
    sources = [
        {"label": label, "path": str(path), "sha256": sha256_file(path), "status": "verified"}
        for label, path in [
            ("portfolio_snapshot", portfolio),
            ("marketsurge_scan", scan),
            ("market_gauge_json", gauge),
            ("fmp_cross_market_context", cross_source),
            ("openai_cross_market_synthesis", synthesis_source),
            ("chart_packet_artifact", archive),
        ]
    ]
    manifest = tmp_path / "sources.json"
    manifest.write_text(json.dumps({"sources": sources}), encoding="utf-8")
    result = run_daily_review(
        requested_date=SESSION,
        session_date=SESSION,
        mode="read-only",
        output_dir=tmp_path / "reports",
        portfolio_path=str(portfolio),
        market_data_path=str(market_data_path),
        chart_packet_dir=str(chart_dir),
        source_manifest_path=str(manifest),
        audit_profile="strict-core",
    )
    market_surge_results = [item for item in result["packet"]["candidate_results"] if item["snapshot"]["market_surge_candidate"]]
    assert {item["ticker"] for item in market_surge_results} == set(tickers)
    top = result["packet"]["top_canslim_setups"]
    assert len(top) == 10
    assert {item["ticker"] for item in top}.issubset(set(tickers))
    assert {"BRANDENS_WATCHLIST", "STANDARD_MARKETSURGE"}.issubset(
        {evidence["source_type"] for item in top for evidence in item["snapshot"]["source_evidence"]}
    )
    gates = result["packet"]["validation_evidence"]
    assert next(item for item in gates if item["gate"] == "top_canslim_render")["ticker_count"] == 10
    assert next(item for item in gates if item["gate"] == "top_canslim_setups")["ranked_tickers"] == [item["ticker"] for item in top]
    assert next(item for item in gates if item["gate"] == "position_sandbox_page")["ticker"] == "MSFT"
    from pypdf import PdfReader
    pdf_pages = PdfReader(result["pdf_path"]).pages
    page_text = [page.extract_text() or "" for page in pdf_pages]
    text = "\n".join(page_text)
    assert len([value for value in page_text if "Top 10 CANSLIM Setups" in value]) == 2
    assert all(item["ticker"] in text for item in top)
    assert "Brandens Watchlist - Complete Results" not in text
    assert "Visual Review Queue" not in text
    assert "First Charts to Review" not in text
    assert "VERIFIED SELL-RULE SANDBOX - MSFT" in text
    assert "Dashboard Gauge posture: Neutral" in text
    assert "Cross-Market Context" in text
    assert "Treasury yields move" in text
    assert next(item for item in gates if item["gate"] == "cross_market_frozen_context")["context_status"] == "PARTIAL"
    assert next(item for item in gates if item["gate"] == "cross_market_llm_synthesis")["synthesis_status"] == "INSUFFICIENT_EVIDENCE"


def test_strict_core_run_rejects_tampered_source(tmp_path):
    source = tmp_path / "portfolio.json"
    source.write_text("{}", encoding="utf-8")
    from market_chart_pipeline.audit import audit_packet
    from market_chart_pipeline.packet import build_review_packet
    from market_chart_pipeline.policy import load_policy

    packet = build_review_packet(
        requested_date=SESSION,
        session_date=SESSION,
        policy=load_policy(),
        source_manifest={"sources": [{"label": "portfolio_snapshot", "path": str(source), "sha256": "bad", "status": "verified"}]},
        audit_profile="strict-core",
    )
    with pytest.raises(ValidationError, match="missing required sources"):
        audit_packet(packet)


def test_failure_notice_is_not_reported_as_success(monkeypatch):
    from market_chart_pipeline import review_mailer

    monkeypatch.setenv("GMAIL_ADDRESS", "owner@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "password")
    monkeypatch.setattr(review_mailer, "_send", lambda *args: None)
    result = review_mailer.send_failure(SESSION, "source audit failed")
    assert result.status == "FAILURE_NOTICE_SENT"
    assert result.status != "SUCCESS"


def test_pdf_attachment_can_be_selected_by_mime_without_filename(tmp_path):
    from market_chart_pipeline.source_acquisition import _write_matching_attachment

    message = EmailMessage()
    message.set_content("scan attached")
    message.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf")
    part = list(message.iter_attachments())[0]
    del part["Content-Disposition"]

    path = _write_matching_attachment(message, tmp_path, ".pdf")
    assert path.name == "source-attachment.pdf"
    assert path.read_bytes() == b"%PDF-1.4"
