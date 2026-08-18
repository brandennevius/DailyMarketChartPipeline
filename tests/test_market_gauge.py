import pytest

from market_chart_pipeline.core import ValidationError
from market_chart_pipeline.market_gauge import normalize_dashboard_market_gauge


SESSION = "2026-08-14"


def payload():
    return {
        "schema_version": "dashboard_market_gauge_v1",
        "session_date": SESSION,
        "generated_at": "2026-08-14T21:00:00Z",
        "overall_state": "Grow",
        "overall_score": 80,
        "components": [{"label": "Short term", "state": "Grow", "detail": "Indexes vs 21EMA"}],
        "index_regimes": [
            {
                "symbol": symbol,
                "date": SESSION,
                "close": 100 + index,
                "ema21": 99 + index,
                "sma50": 96 + index,
                "sma200": 90 + index,
                "shortTerm": "Up",
                "mediumTerm": "Up",
                "longTerm": "Up",
                "rawShortTerm": "Up",
                "rawMediumTerm": "Up",
                "rawLongTerm": "Up",
                "above21Percent": 1.01,
                "above50Percent": 4.17,
                "extension": "Caution",
            }
            for index, symbol in enumerate(["SPY", "QQQ", "IWM"])
        ],
        "universe": {"indexes": ["SPY", "QQQ", "IWM"], "leaders": ["NVDA"]},
        "providers": ["Stooq"],
    }


def test_market_gauge_is_supporting_evidence_not_an_invented_oneil_regime():
    result = normalize_dashboard_market_gauge(payload(), SESSION)
    assert result["market_regime"]["classification"] == "INSUFFICIENT_EVIDENCE"
    assert result["market_regime"]["dashboard_market_gauge_posture"] == "Grow"
    assert [item["symbol"] for item in result["market_regime"]["dashboard_market_gauge_indexes"]] == ["SPY", "QQQ", "IWM"]
    assert result["market_regime"]["dashboard_market_gauge_indexes"][0]["price_session"] == SESSION
    assert result["market_regime"]["trend_extension_posture"]["extension_counts"]["Caution"] == 3
    assert result["market_regime"]["dashboard_market_gauge_providers"] == ["Stooq"]
    assert result["exposure_guidance"]["exact_exposure"] == "indeterminate"
    assert "distribution" in " ".join(result["market_regime"]["missing_evidence"])


def test_market_gauge_fails_closed_on_wrong_session():
    with pytest.raises(ValidationError, match="session"):
        normalize_dashboard_market_gauge(payload(), "2026-08-13")


def test_market_gauge_fails_closed_when_component_evidence_is_missing():
    incomplete = payload()
    incomplete["index_regimes"][0]["ema21"] = None
    with pytest.raises(ValidationError, match="SPY evidence is incomplete"):
        normalize_dashboard_market_gauge(incomplete, SESSION)
