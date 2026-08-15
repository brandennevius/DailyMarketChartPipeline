import json

from market_chart_pipeline.delivery_state import write_processed_state


def test_processed_state_records_terminal_failed_delivery_without_success_claim(tmp_path):
    receipt = tmp_path / "processed" / "request.json"
    write_processed_state(
        receipt,
        manifest="requests/2026-08-14.json",
        session_date="2026-08-14",
        delivery={"status": "FAILED", "error": "SMTP rejected message"},
    )

    data = json.loads(receipt.read_text(encoding="utf-8"))
    assert data["terminal"] is True
    assert data["delivery"]["status"] == "FAILED"
    assert "success" not in json.dumps(data).lower()
