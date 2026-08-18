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
        "index_regimes": [{"symbol": symbol, "date": SESSION} for symbol in ["SPY", "QQQ", "IWM"]],
        "universe": {"indexes": ["SPY", "QQQ", "IWM"], "leaders": ["NVDA"]},
        "providers": ["Stooq"],
    }


def test_market_gauge_is_supporting_evidence_not_an_invented_oneil_regime():
    result = normalize_dashboard_market_gauge(payload(), SESSION)
    assert result["market_regime"]["classification"] == "INSUFFICIENT_EVIDENCE"
    assert result["market_regime"]["dashboard_market_gauge_posture"] == "Grow"
    assert result["exposure_guidance"]["exact_exposure"] == "indeterminate"
    assert "distribution" in " ".join(result["market_regime"]["missing_evidence"])


def test_market_gauge_fails_closed_on_wrong_session():
    with pytest.raises(ValidationError, match="session"):
        normalize_dashboard_market_gauge(payload(), "2026-08-13")
