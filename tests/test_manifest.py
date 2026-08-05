import json
from pathlib import Path

import pytest

from market_chart_pipeline.core import ValidationError
from market_chart_pipeline.manifest import load_manifest


def write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def valid_payload():
    return {
        "status": "COMPLETE",
        "session_date": "2026-08-04",
        "feed": "iex",
        "unique_ticker_count": 1,
        "records": [
            {
                "ticker": "HPE",
                "chart_required": True,
                "sources": [
                    {
                        "source_type": "BRANDENS_WATCHLIST",
                        "label": "BRANDENS WATCHLIST",
                        "pdf_page": 11,
                    }
                ],
            }
        ],
    }


def test_valid_manifest_loads(tmp_path):
    request = load_manifest(write(tmp_path, valid_payload()))
    assert request.session_date == "2026-08-04"
    assert request.tickers == ["HPE"]
    assert request.records_by_ticker["HPE"]["chart_required"] is True


def test_count_mismatch_fails(tmp_path):
    payload = valid_payload()
    payload["unique_ticker_count"] = 2
    with pytest.raises(ValidationError):
        load_manifest(write(tmp_path, payload))


def test_source_page_required_for_marketsurge(tmp_path):
    payload = valid_payload()
    payload["records"][0]["sources"][0]["pdf_page"] = None
    with pytest.raises(ValidationError):
        load_manifest(write(tmp_path, payload))


def test_duplicate_ticker_memberships_are_preserved(tmp_path):
    payload = valid_payload()
    payload["records"].append(
        {
            "ticker": "HPE",
            "sources": [
                {
                    "source_type": "STANDARD_MARKETSURGE",
                    "label": "Near Pivot",
                    "pdf_page": 6,
                }
            ],
        }
    )
    request = load_manifest(write(tmp_path, payload))
    assert len(request.records_by_ticker["HPE"]["sources"]) == 2
