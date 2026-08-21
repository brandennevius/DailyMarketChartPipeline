from copy import deepcopy

from market_chart_pipeline.cross_market import (
    collect_fmp_cross_market_context,
    normalize_cross_market_context,
)


SESSION = "2026-08-17"


def _fetcher(endpoint, **params):
    news = {
        "news/general-latest": [
            {
                "title": "Treasury yields move before the Federal Reserve minutes",
                "site": "Example Wire",
                "publishedDate": "2026-08-17T14:30:00Z",
                "url": "https://example.com/rates?campaign=one",
            },
            {
                "title": "Duplicate rates story",
                "site": "Example Wire",
                "publishedDate": "2026-08-17T14:31:00Z",
                "url": "https://example.com/rates?campaign=two",
            },
            {
                "title": "Outside the frozen window",
                "site": "Old Wire",
                "publishedDate": "2026-08-10T10:00:00Z",
                "url": "https://example.com/old",
            },
        ],
        "news/stock-latest": [
            {
                "title": "Company raises earnings guidance",
                "publisher": "Stock Desk",
                "publishedDate": "2026-08-16T18:00:00Z",
                "url": "https://stock.example/guidance",
                "symbol": "ACME",
            }
        ],
        "news/forex-latest": [
            {
                "title": "Dollar and euro react to rate outlook",
                "source": "FX Desk",
                "publishedDate": "2026-08-15T13:00:00Z",
                "url": "https://fx.example/outlook",
            }
        ],
        "news/crypto-latest": [
            {
                "title": "Bitcoin steadies after digital asset volatility",
                "site": "Crypto Desk",
                "publishedDate": "2026-08-17T08:00:00Z",
                "url": "https://crypto.example/bitcoin",
            }
        ],
    }
    if endpoint in news:
        assert params == {"page": 0, "limit": 100}
        return deepcopy(news[endpoint])
    if endpoint == "economic-calendar":
        assert params == {"from": SESSION, "to": SESSION}
        return [
            {
                "date": "2026-08-17 08:30:00",
                "event": "Retail Sales",
                "country": "US",
                "impact": "High",
                "actual": 0.7,
                "estimate": 0.4,
                "previous": 0.2,
                "unit": "%",
            }
        ]
    if endpoint == "treasury-rates":
        assert params == {"from": SESSION, "to": SESSION}
        return [{"date": SESSION, "year2": 4.01, "year10": 4.22, "year30": 4.8}]
    raise AssertionError(endpoint)


def test_cross_market_context_is_bounded_deduplicated_sourced_and_deterministic():
    raw, context = collect_fmp_cross_market_context(
        SESSION,
        fetcher=_fetcher,
        retrieved_at="2026-08-17T21:00:00Z",
    )

    assert context["status"] == "AVAILABLE"
    assert context["lookback_window"]["start_date"] == "2026-08-15"
    assert context["lookback_window"]["end_date"] == SESSION
    assert len(context["news"]) == 4
    assert context["rejected_article_counts"] == {
        "missing_required_fields": 0,
        "outside_window": 1,
        "duplicate": 1,
    }
    assert all(
        set(("category", "title", "publisher", "published_at", "url")) <= set(article)
        for article in context["news"]
    )
    assert context["economic_calendar"][0]["event"] == "Retail Sales"
    assert context["treasury_context"]["maturities_pct"]["year10"] == 4.22
    assert context["interpretation"]["may_override_deterministic_outputs"] is False
    assert context["llm_synthesis_boundary"]["autonomous_web_or_tool_access"] is False
    assert context["news"][0]["evidence_id"].startswith("FMP_NEWS_")
    assert context["economic_calendar"][0]["evidence_id"] == "FMP_ECON_001"
    assert context["treasury_context"]["evidence_id"] == "FMP_TREASURY_001"
    assert normalize_cross_market_context(deepcopy(raw)) == context


def test_cross_market_entitlement_failures_are_visible_insufficient_evidence():
    def unavailable(endpoint, **params):
        raise RuntimeError(f"{endpoint} is not entitled")

    raw, context = collect_fmp_cross_market_context(
        SESSION,
        fetcher=unavailable,
        retrieved_at="2026-08-17T21:00:00Z",
    )

    assert context["status"] == "INSUFFICIENT_EVIDENCE"
    assert len(context["evidence_gaps"]) == 6
    assert not context["cited_context"]
    assert all(result["status"] == "INSUFFICIENT_EVIDENCE" for result in raw["endpoint_results"].values())
    assert all("not entitled" in result["error"] for result in raw["endpoint_results"].values())
