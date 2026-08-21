import json

import pytest

from market_chart_pipeline.audit import audit_packet
from market_chart_pipeline.core import ValidationError
from market_chart_pipeline.orchestrator import run_daily_review
from market_chart_pipeline.packet import build_review_packet, freeze_packet, llm_non_influence_record
from market_chart_pipeline.policy import load_policy
from market_chart_pipeline.rules import evaluate_position
from market_chart_pipeline.utils import sha256_file


def test_packet_freeze_hash_is_canonical_and_stable():
    policy = load_policy()
    packet_one = build_review_packet(
        requested_date="2026-08-14",
        session_date="2026-08-14",
        policy=policy,
        candidates=[
            {
                "ticker": "AAPL",
                "origin": "scanner",
                "pivot_verification_status": "unverified",
                "inside_buy_zone": True,
                "fundamental_quality_score": 90,
                "relative_strength_group_score": 80,
                "technical_setup_score": 90,
                "accumulation_supply_score": 70,
                "new_catalyst_score": 50,
            }
        ],
    )
    packet_two = json.loads(json.dumps(packet_one, sort_keys=True))

    assert packet_one["packet_sha256"] == packet_two["packet_sha256"]
    assert audit_packet(packet_one)[0]["gate"] == "packet_hash"


def test_audit_fails_on_hash_tampering():
    policy = load_policy()
    packet = build_review_packet(requested_date="2026-08-14", session_date="2026-08-14", policy=policy)
    packet["session_date"] = "2026-08-13"

    with pytest.raises(ValidationError, match="Packet hash"):
        audit_packet(packet)


def test_audit_fails_on_candidate_score_arithmetic():
    policy = load_policy()
    packet = build_review_packet(
        requested_date="2026-08-14",
        session_date="2026-08-14",
        policy=policy,
        candidates=[
            {
                "ticker": "AAPL",
                "origin": "scanner",
                "pivot_verification_status": "unverified",
                "inside_buy_zone": True,
                "fundamental_quality_score": 90,
                "relative_strength_group_score": 80,
                "technical_setup_score": 90,
                "accumulation_supply_score": 70,
                "new_catalyst_score": 50,
            }
        ],
    )
    packet["candidate_results"][0]["internal_canslim_score"] = 1
    from market_chart_pipeline.packet import freeze_packet

    packet = freeze_packet(packet)

    with pytest.raises(ValidationError, match="Candidate score arithmetic"):
        audit_packet(packet)


def test_audit_rejects_actionable_candidate_without_verified_chart():
    policy = load_policy()
    packet = build_review_packet(
        requested_date="2026-08-14",
        session_date="2026-08-14",
        policy=policy,
        candidates=[
            {
                "ticker": "AAPL",
                "origin": "scanner",
                "pivot_verification_status": "verified",
                "inside_buy_zone": True,
                "fundamental_quality_score": 100,
                "relative_strength_group_score": 100,
                "technical_setup_score": 100,
                "accumulation_supply_score": 100,
                "new_catalyst_score": 100,
            }
        ],
    )

    with pytest.raises(ValidationError, match="lack current verified charts"):
        audit_packet(packet)


def test_strict_audit_accepts_hpe_hard_exit_with_verified_sandbox(tmp_path):
    policy = load_policy()
    result = evaluate_position(
        {
            "ticker": "HPE",
            "entry_price": 54.79,
            "entry_date": "2026-05-27",
            "current_price": 52.89,
            "stop_price": 48.97,
            "atr": 2.89,
            "highest_close_since_entry": 63.50,
            "sell_sandbox_status": "verified",
            "sell_sandbox_asset": {
                "status": "verified",
                "file": "assets/HPE_sell_sandbox.png",
                "sha256": "c19c0c3db45a66b811b6b1ff268dd8d2ea427541e2b38f7df109e2dc32004348",
            },
        },
        policy,
        "2026-08-20",
    )
    required_sources = {
        "portfolio_snapshot",
        "marketsurge_scan",
        "market_gauge_json",
        "chart_packet_artifact",
        "chart_packet_json",
        "chart_packet_pdf",
    }
    sources = []
    for label in sorted(required_sources):
        path = tmp_path / label
        path.write_text(label, encoding="utf-8")
        sources.append({"label": label, "path": str(path), "sha256": sha256_file(path), "status": "verified"})
    packet_body = {
            "audit_profile": "strict-core",
            "sources": sources,
            "chart_verification": {"status": "verified", "requested_tickers": [], "verified_tickers": []},
            "portfolio_risk": {"status": "calculated"},
            "sell_rule_results": [result],
            "candidate_results": [],
            "shakeout_results": [],
            "input_sets": {
                "portfolio_tickers": ["HPE"],
                "candidate_tickers": [],
                "watchlist_tickers": [],
            },
        }
    packet_body["llm_non_influence"] = llm_non_influence_record(packet_body)
    packet = freeze_packet(packet_body)

    evidence = audit_packet(packet)

    sandbox_gate = next(item for item in evidence if item["gate"] == "sell_sandbox_charts")
    assert sandbox_gate["verified"] == ["HPE"]
    assert result["action"] == "EXIT"


def test_orchestrator_writes_packet_markdown_and_pdf_from_packet_only(tmp_path):
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(
        json.dumps(
            [
                {
                    "ticker": "MSFT",
                    "entry_price": 100,
                    "current_price": 110,
                    "pivot_price": 100,
                    "atr": 3,
                    "breakout_date": "2026-07-30",
                    "trading_days_since_breakout": 11,
                    "highest_close_since_entry": 111,
                }
            ]
        ),
        encoding="utf-8",
    )

    result = run_daily_review(
        requested_date="2026-08-14",
        session_date=None,
        mode="read-only",
        output_dir=tmp_path / "reports",
        portfolio_path=str(portfolio),
    )

    json_path = tmp_path / "reports" / "2026-08-14" / "2026-08-14-market-review.json"
    md_path = tmp_path / "reports" / "2026-08-14" / "2026-08-14-market-review.md"
    pdf_path = tmp_path / "reports" / "2026-08-14" / "2026-08-14-market-review.pdf"
    assert json_path.exists()
    assert md_path.exists()
    assert pdf_path.exists()
    assert pdf_path.read_bytes().startswith(b"%PDF-")
    assert result["packet"]["sell_rule_results"][0]["ticker"] == "MSFT"
    assert result["packet"]["packet_sha256"] in md_path.read_text(encoding="utf-8")
