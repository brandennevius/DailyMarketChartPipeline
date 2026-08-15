import json
from email.message import EmailMessage
from pathlib import Path

import pytest

from market_chart_pipeline.adapters import derive_candidates_from_chart, normalize_portfolio_snapshot
from market_chart_pipeline.core import ValidationError
from market_chart_pipeline.orchestrator import run_daily_review
from market_chart_pipeline.utils import sha256_file


SESSION = "2026-08-14"


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
    sources = []
    for label, path in [("portfolio_snapshot", portfolio), ("marketsurge_scan", scan), ("chart_packet_artifact", artifact)]:
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
