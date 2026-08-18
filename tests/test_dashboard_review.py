import json
from pathlib import Path

import pytest

from market_chart_pipeline.core import ValidationError
from market_chart_pipeline.dashboard_review import (
    CALLBACK_SCHEMA,
    WORKER_INPUT_SCHEMA,
    DashboardClient,
    _artifact_metadata,
    _manifest_from_corrections,
    _merge_portfolio,
)


HASHES = {
    "marketsurge_pdf_sha256": "a" * 64,
    "snapshot_json_sha256": "b" * 64,
    "snapshot_markdown_sha256": "c" * 64,
    "market_gauge_json_sha256": "d" * 64,
}


def client(**overrides):
    values = {
        "worker_input_url": "https://dashboard.example/api/runs/run-1/worker-input",
        "worker_callback_url": "https://dashboard.example/api/runs/run-1/worker-callback",
        "allowed_base_url": "https://dashboard.example",
        "secret": "secret",
        "run_id": "run-1",
        "session_date": "2026-08-14",
        "attempt": 1,
        "source_hashes": HASHES,
    }
    values.update(overrides)
    return DashboardClient(**values)


class Response:
    def __init__(self, payload=None, content=b"", headers=None):
        self.payload = payload
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def worker_payload():
    return {
        "schema_version": WORKER_INPUT_SCHEMA,
        "review_run_id": "run-1",
        "session_date": "2026-08-14",
        "attempt": 1,
        "source_hashes": HASHES,
        "callback": {
            "url": "https://dashboard.example/api/runs/run-1/worker-callback",
            "schema_version": CALLBACK_SCHEMA,
            "token": "short-lived-token",
        },
    }


def test_dashboard_client_rejects_cross_origin_source_url():
    with pytest.raises(ValidationError, match="same origin"):
        client(worker_callback_url="https://attacker.example/callback")
    with pytest.raises(ValidationError, match="cross-origin"):
        client()._same_origin_url("https://attacker.example/source.pdf")


def test_dashboard_client_rejects_unsafe_run_id_and_missing_hash():
    with pytest.raises(ValidationError, match="run_id"):
        client(run_id="../../receipt")
    with pytest.raises(ValidationError, match="four source"):
        client(source_hashes={"marketsurge_pdf_sha256": "a" * 64})
    with pytest.raises(ValidationError, match="YYYY-MM-DD"):
        client(session_date="Fri Aug 14")


def test_worker_input_repeats_dispatch_correlation_and_adopts_callback_token(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response(worker_payload())

    monkeypatch.setattr("market_chart_pipeline.dashboard_review.requests.get", fake_get)
    dashboard = client()
    result = dashboard.input()

    assert result["review_run_id"] == "run-1"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["headers"]["X-Review-Attempt"] == "1"
    assert captured["headers"]["X-Snapshot-Markdown-SHA256"] == "c" * 64
    assert captured["headers"]["X-Market-Gauge-JSON-SHA256"] == "d" * 64
    assert dashboard.callback_token == "short-lived-token"


def test_worker_input_rejects_delayed_attempt(monkeypatch):
    payload = worker_payload()
    payload["attempt"] = 2
    monkeypatch.setattr(
        "market_chart_pipeline.dashboard_review.requests.get", lambda *args, **kwargs: Response(payload)
    )
    with pytest.raises(ValidationError, match="correlation"):
        client().input()


def test_callback_uses_short_lived_token_and_exact_envelope(monkeypatch):
    dashboard = client()
    dashboard.callback_url = "https://dashboard.example/api/runs/run-1/worker-callback"
    dashboard.callback_token = "short-lived-token"
    captured = {}

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response({"duplicate": False})

    monkeypatch.setattr("market_chart_pipeline.dashboard_review.requests.post", fake_post)
    dashboard.callback("RUNNING", "run-1:1:ocr", {"stage": "OCR"})
    assert captured["headers"]["Authorization"] == "Bearer short-lived-token"
    assert captured["json"]["event_type"] == "RUNNING"
    assert captured["json"]["source_hashes"] == HASHES
    assert captured["json"]["attempt"] == 1


def test_signed_source_download_is_hash_checked_without_worker_secret(monkeypatch, tmp_path):
    body = b"exact bytes"
    import hashlib

    captured = {}

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        return Response(content=body, headers={"Content-Length": str(len(body))})

    monkeypatch.setattr("market_chart_pipeline.dashboard_review.requests.get", fake_get)
    path = tmp_path / "source.bin"
    client().download(
        "https://dashboard.example/api/source?token=signed", path, hashlib.sha256(body).hexdigest()
    )
    assert path.read_bytes() == body
    assert "headers" not in captured


def test_ocr_corrections_form_a_hash_bound_manifest():
    manifest = _manifest_from_corrections(
        {
            "schema_version": "marketsurge_ocr_corrections_v2",
            "expected_version": 0,
            "corrections": [
                {"pdf_page": 2, "label": "Near Pivot", "tickers": ["NVDA", "MSFT"], "reviewed": True},
                {"pdf_page": 3, "label": "BRANDENS WATCHLIST", "tickers": ["MSFT"], "reviewed": True},
            ],
        },
        session_date="2026-08-14",
        marketsurge_sha256="a" * 64,
    )
    assert manifest["status"] == "COMPLETE_WITH_WARNINGS"
    assert manifest["marketsurge_pdf_sha256"] == "a" * 64
    assert [record["ticker"] for record in manifest["records"]] == ["MSFT", "NVDA"]
    assert len(manifest["records"][0]["sources"]) == 2


@pytest.mark.parametrize("payload", [
    {"schema_version": "marketsurge_ocr_v1", "corrections": [{"pdf_page": 1, "label": "Near Pivot", "tickers": ["NVDA"], "reviewed": True}]},
    {"schema_version": "marketsurge_ocr_corrections_v2", "corrections": [{"pdf_page": 1, "label": "Near Pivot", "tickers": ["FAVORITES"], "reviewed": True}]},
    {"schema_version": "marketsurge_ocr_corrections_v2", "corrections": [{"pdf_page": 1, "label": "Near Pivot", "tickers": ["NVDA"]}]},
])
def test_ocr_corrections_fail_closed_without_v2_reviewed_valid_symbols(payload):
    with pytest.raises(ValidationError):
        _manifest_from_corrections(payload, session_date="2026-08-14", marketsurge_sha256="a" * 64)


def test_portfolio_tickers_are_merged_without_losing_scan_provenance():
    manifest = {
        "records": [
            {
                "ticker": "MSFT",
                "chart_required": True,
                "sources": [{"source_type": "STANDARD_MARKETSURGE", "label": "Near Pivot", "pdf_page": 2}],
            }
        ],
        "unique_ticker_count": 1,
    }
    merged = _merge_portfolio(manifest, {"open_positions": [{"ticker": "MSFT"}, {"ticker": "LLY"}]})
    assert [record["ticker"] for record in merged["records"]] == ["LLY", "MSFT"]
    msft = next(record for record in merged["records"] if record["ticker"] == "MSFT")
    assert {source["source_type"] for source in msft["sources"]} == {"STANDARD_MARKETSURGE", "PORTFOLIO"}


def test_artifact_metadata_hashes_exact_registered_bytes(tmp_path):
    path = tmp_path / "packet.json"
    path.write_text(json.dumps({"packet": True}) + "\n", encoding="utf-8")
    metadata = _artifact_metadata("json", path, "application/json")
    assert metadata["size_bytes"] == path.stat().st_size
    assert len(metadata["sha256"]) == 64


def test_daily_review_workflow_exposes_exact_non_secret_dashboard_contract():
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "daily-review.yml").read_text()
    for name in (
        "review_run_id",
        "attempt",
        "marketsurge_pdf_sha256",
        "snapshot_json_sha256",
        "snapshot_markdown_sha256",
        "market_gauge_json_sha256",
        "worker_input_url",
        "worker_callback_url",
    ):
        assert f"{name}:" in workflow
    assert "DASHBOARD_WORKER_SECRET: ${{ secrets.DASHBOARD_WORKER_SECRET }}" in workflow
    assert "DASHBOARD_BASE_URL: ${{ vars.DASHBOARD_BASE_URL }}" in workflow
    assert "daily-review-{run_id}-attempt-{attempt}.json" in workflow
