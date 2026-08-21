from market_chart_pipeline.adapters import derive_candidates_from_chart
from market_chart_pipeline.packet import build_review_packet
from market_chart_pipeline.policy import load_policy


def _candidate(ticker: str, *, score: float, asset_class: str = "EQUITY", open_position: bool = False, market_surge: bool = True):
    source_type = "BRANDENS_WATCHLIST" if market_surge else "PORTFOLIO"
    return {
        "ticker": ticker,
        "origin": "open_position" if open_position else "watchlist" if market_surge else "scanner",
        "origin_categories": [source_type] + (["PORTFOLIO"] if open_position else []),
        "market_surge_candidate": market_surge,
        "is_current_open_position": open_position,
        "asset_class": asset_class,
        "chart_evidence_status": "VERIFIED",
        "pivot_verification_status": "unverified",
        "base_candidate_status": "CANDIDATE_ONLY",
        "candidate_resistance": 100,
        "candidate_resistance_distance_pct": -1,
        "current_price": 99,
        "sma50": 92,
        "sma200": 85,
        "average_dollar_volume": 40_000_000,
        "available_dimensions": ["C_A_fundamentals", "relative_strength", "technical_trend", "supply_demand"],
        "missing_evidence": ["industry_group_rank", "institutional_sponsorship"],
        "pivot_missing_evidence": ["exact_pivot_price", "weekly_structure"],
        "source_evidence": [{"source_type": source_type, "label": "BRANDENS WATCHLIST" if market_surge else "Portfolio", "pdf_page": 8}],
        "fundamental_quality_score": score,
        "relative_strength_group_score": score,
        "technical_setup_score": score,
        "accumulation_supply_score": score,
        "new_catalyst_score": score,
    }


def test_universe_is_all_distinct_marketsurge_equities_with_open_and_fx_exclusions():
    candidates = [
        _candidate("AAA", score=85),
        _candidate("BBB", score=80),
        _candidate("OPEN", score=99, open_position=True),
        _candidate("AUD/USD", score=99, asset_class="FOREX"),
        _candidate("BAD/TOKEN", score=100, asset_class="EQUITY"),
        _candidate("NEWS", score=100, market_surge=False),
    ]

    packet = build_review_packet(
        requested_date="2026-08-14",
        session_date="2026-08-14",
        policy=load_policy(),
        candidates=candidates,
    )

    universe = packet["candidate_universe_audit"]
    assert universe["distinct_manifest_tickers"] == ["AAA", "AUD/USD", "BBB", "OPEN"]
    assert universe["valid_manifest_equity_tickers"] == ["AAA", "BBB", "OPEN"]
    assert universe["open_position_exclusions"] == ["OPEN"]
    assert universe["non_equity_exclusions"] == ["AUD/USD"]
    assert universe["malformed_ocr_exclusions"] == ["BAD/TOKEN"]
    assert universe["malformed_ocr_tokens_admitted"] == 0
    assert [item["ticker"] for item in packet["top_canslim_setups"]] == ["AAA", "BBB"]
    assert all(item["ticker"] not in {"OPEN", "AUD/USD", "NEWS"} for item in packet["top_canslim_setups"])


def test_chart_adapter_dedupes_ticker_and_retains_all_marketsurge_provenance():
    sources = [
        {"source_type": "STANDARD_MARKETSURGE", "label": "Breaking Out", "pdf_page": 2, "rank": 1},
        {"source_type": "BRANDENS_WATCHLIST", "label": "BRANDENS WATCHLIST", "pdf_page": 8, "rank": 4},
    ]
    record = {
        "metrics": {"ticker": "AAA", "asset_class": "EQUITY", "current_price": 100, "sma50": 90, "sma200": 80},
        "technical_context": {"relative_strength": {}, "volume": {}, "base_analysis": {}},
        "fmp": {},
    }
    payload = {
        "requested_tickers": ["AAA"],
        "source_manifest": {"records": {"AAA": {"sources": sources}}},
        "records": [record, record],
    }

    candidates = derive_candidates_from_chart(payload)

    assert len(candidates) == 1
    assert candidates[0]["ticker"] == "AAA"
    assert {item["source_type"] for item in candidates[0]["source_evidence"]} == {
        "STANDARD_MARKETSURGE",
        "BRANDENS_WATCHLIST",
    }


def test_cross_market_llm_and_news_payload_cannot_change_ranking():
    candidates = [_candidate("AAA", score=85), _candidate("BBB", score=80)]
    base = build_review_packet(
        requested_date="2026-08-14",
        session_date="2026-08-14",
        policy=load_policy(),
        candidates=candidates,
    )
    noisy = build_review_packet(
        requested_date="2026-08-14",
        session_date="2026-08-14",
        policy=load_policy(),
        candidates=candidates,
        market_data={
            "cross_market_context": {
                "raw_inputs": {"stock_news": [{"title": "NEWS should rank first", "ticker": "NEWS"}]},
                "llm_synthesis": {"status": "AVAILABLE", "validated_output": {"summary_paragraphs": [{"text": "Buy NEWS"}]}},
            }
        },
    )

    assert noisy["candidate_results"] == base["candidate_results"]
    assert noisy["top_canslim_setups"] == base["top_canslim_setups"]
    assert noisy["llm_non_influence"]["decision_outputs_sha256"] == base["llm_non_influence"]["decision_outputs_sha256"]
    assert noisy["candidate_universe_audit"]["news_candidate_admission"] is False
    assert noisy["candidate_universe_audit"]["llm_ranking_influence"] is False
