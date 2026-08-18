from pathlib import Path

import pytest

from market_chart_pipeline.adapters import derive_market_breadth, enrich_positions_from_charts, normalize_portfolio_snapshot
from market_chart_pipeline.core import ValidationError
from market_chart_pipeline.render import audit_rendered_pdf, render_markdown, render_pdf


SESSION = "2026-08-14"


def test_portfolio_adapter_preserves_decision_metrics():
    snapshot = {
        "metadata": {
            "latest_completed_market_session": SESSION,
            "broker_import_complete": True,
            "price_data_as_of": f"{SESSION}T16:00:00-04:00",
            "account_value": 100_000,
            "price_source": "closing-feed",
        },
        "portfolio_summary": {
            "open_position_count": 1,
            "gross_exposure_pct": 8.5,
            "total_open_pnl": 450,
            "total_remaining_risk_to_stops": 300,
            "total_remaining_risk_pct": 0.3,
        },
        "open_positions": [
            {
                "ticker": "MSFT",
                "side": "LONG",
                "average_entry": 100,
                "entry_date": "2026-08-01",
                "current_price": 110,
                "current_stop": 102,
                "shares": 10,
                "market_value": 1100,
                "position_weight_pct": 1.1,
                "unrealized_pnl": 100,
                "open_r_multiple": 1.25,
                "remaining_risk_to_stop_dollars": 80,
                "take_profit": 125,
                "setup": "CANSLIM",
                "grade": "A",
            }
        ],
    }

    positions, risk = normalize_portfolio_snapshot(snapshot, SESSION)
    assert positions[0]["open_r_multiple"] == 1.25
    assert positions[0]["take_profit"] == 125
    assert positions[0]["grade"] == "A"
    assert risk["total_open_pnl"] == 450
    assert risk["total_remaining_risk_to_stops"] == 300
    assert risk["price_source"] == "closing-feed"


def test_market_breadth_is_labeled_partial_and_reconciles():
    payload = {
        "records": [
            {
                "metrics": {"current_price": 110, "sma21": 100, "sma50": 90, "sma200": 80, "quantitative_gate": "CHART_REVIEW_PRIORITY"},
                "technical_context": {"relative_strength": {"trend_21d": "RISING"}, "volume": {"accumulation_distribution_estimate": "POSITIVE"}},
            },
            {
                "metrics": {"current_price": 90, "sma21": 100, "sma50": 95, "sma200": 80, "quantitative_gate": "DEPRIORITIZE"},
                "technical_context": {"relative_strength": {"trend_21d": "FALLING"}, "volume": {"accumulation_distribution_estimate": "NEUTRAL"}},
            },
        ]
    }

    breadth = derive_market_breadth(payload)
    assert breadth["status"] == "partial_evidence"
    assert breadth["verified_symbols"] == 2
    assert breadth["above_21d_pct"] == 50.0
    assert breadth["above_200d_pct"] == 100.0
    assert breadth["chart_review_priority_count"] == 1


def test_chart_history_supplies_position_trailing_evidence(tmp_path: Path):
    positions = [{"ticker": "MSFT", "entry_price": 100, "entry_date": "2026-08-10", "pivot_price": 100}]
    payload = {
        "records": [
            {
                "metrics": {"ticker": "MSFT"},
                "technical_context": {},
                "fmp": {},
                "price_history": [
                    {"date": "2026-08-07", "close": 99, "high": 100},
                    {"date": "2026-08-10", "close": 100, "high": 101},
                    {"date": "2026-08-11", "close": 121, "high": 122},
                    {"date": "2026-08-12", "close": 119, "high": 121},
                ],
            }
        ]
    }

    enriched = enrich_positions_from_charts(positions, payload, tmp_path)
    assert enriched[0]["highest_close_since_entry"] == 121
    assert enriched[0]["trading_days_since_breakout"] == 2
    assert enriched[0]["trading_days_to_rapid_advance"] == 1


def test_report_is_decision_first_and_renders_pdf():
    packet = {
        "session_date": SESSION,
        "policy_version": "test-policy",
        "packet_sha256": "a" * 64,
        "audit_profile": "standard",
        "market_regime": {"classification": "INSUFFICIENT_EVIDENCE"},
        "market_breadth": {"verified_symbols": 0},
        "portfolio_risk": {"account_value": 100_000, "normalized_long_position_count": 0},
        "sell_rule_results": [],
        "candidate_results": [],
        "chart_verification": {"verified_tickers": [], "requested_tickers": []},
        "input_sets": {"portfolio_tickers": []},
    }

    markdown = render_markdown(packet)
    pdf = render_pdf(packet)
    assert "## Decision Summary" in markdown
    assert "Validation Evidence" not in markdown
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 3_000


def test_pdf_rejects_tampered_sell_sandbox_asset(tmp_path: Path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "TEST_sell_sandbox.png").write_bytes(b"tampered")
    packet = {
        "session_date": SESSION,
        "policy_version": "test-policy",
        "packet_sha256": "a" * 64,
        "audit_profile": "standard",
        "market_regime": {"classification": "INSUFFICIENT_EVIDENCE"},
        "market_breadth": {"verified_symbols": 0},
        "portfolio_risk": {"account_value": 100_000, "normalized_long_position_count": 1},
        "sell_rule_results": [
            {
                "ticker": "TEST",
                "action": "HOLD",
                "rationale": "No rule fired.",
                "gain_pct": 1,
                "events": [{"rule": "hard_capital_protection", "status": "PASS", "values": {}}],
                "position_snapshot": {
                    "sell_sandbox_asset": {"file": "assets/TEST_sell_sandbox.png", "sha256": "bad"},
                },
            }
        ],
        "candidate_results": [],
        "chart_verification": {"verified_tickers": [], "requested_tickers": []},
        "input_sets": {"portfolio_tickers": ["TEST"]},
    }

    with pytest.raises(ValidationError, match="hash mismatch"):
        render_pdf(packet, report_dir=tmp_path)


def test_complete_watchlist_and_position_failure_pages_are_rendered(tmp_path: Path):
    watchlist = [
        {
            "ticker": f"W{index:02d}",
            "origin": "watchlist",
            "internal_canslim_score": 10,
            "classification": "WATCH",
            "action": "HOLD",
            "rationale": "Pivot is not verified.",
            "snapshot": {"source_labels": ["BRANDENS WATCHLIST"]},
        }
        for index in range(1, 16)
    ]
    packet = {
        "session_date": SESSION,
        "policy_version": "test-policy",
        "packet_sha256": "a" * 64,
        "audit_profile": "standard",
        "market_regime": {"classification": "INSUFFICIENT_EVIDENCE", "dashboard_market_gauge_posture": "Neutral"},
        "market_breadth": {"verified_symbols": 0},
        "portfolio_risk": {"account_value": 100_000, "normalized_long_position_count": 1},
        "sell_rule_results": [{
            "ticker": "FAIL",
            "action": "INSUFFICIENT_EVIDENCE",
            "rationale": "Sandbox evidence unavailable.",
            "events": [{"rule": "critical_evidence", "status": "INSUFFICIENT_EVIDENCE", "values": {}}],
            "position_snapshot": {"sell_sandbox_status": "insufficient_evidence", "sell_sandbox_error": "Missing verified price history."},
        }],
        "candidate_results": watchlist,
        "chart_verification": {"verified_tickers": [], "requested_tickers": [item["ticker"] for item in watchlist]},
        "input_sets": {"portfolio_tickers": ["FAIL"], "watchlist_tickers": [item["ticker"] for item in watchlist]},
    }
    markdown = render_markdown(packet)
    assert "Complete Results (15)" in markdown
    assert all(f"**{item['ticker']}**" in markdown for item in watchlist)
    pdf_path = tmp_path / "review.pdf"
    pdf_path.write_bytes(render_pdf(packet, report_dir=tmp_path))
    evidence = audit_rendered_pdf(pdf_path, packet)
    assert {item["gate"] for item in evidence} == {"position_sandbox_page", "complete_watchlist_render"}
    from pypdf import PdfReader
    text = "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages)
    assert "SELL-RULE SANDBOX UNAVAILABLE - FAIL" in text
    assert "FAIL daily chart through" not in text
