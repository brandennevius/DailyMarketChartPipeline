import json
from pathlib import Path

from market_chart_pipeline import mailer


def write_packet(tmp_path: Path, session_date: str = "2026-08-14") -> Path:
    output_dir = tmp_path / session_date
    output_dir.mkdir()
    (output_dir / f"Market_Chart_Data_{session_date}.json").write_text(
        json.dumps(
            {
                "session_date": session_date,
                "status": "COMPLETE",
                "requested_tickers": ["AAPL"],
                "verified_count": 1,
                "error_count": 0,
                "artifacts": {"pdf_sha256": "abc"},
            }
        ),
        encoding="utf-8",
    )
    (output_dir / f"Market_Chart_Packet_{session_date}.pdf").write_bytes(b"pdf")
    return output_dir


def test_oversized_packet_sends_link_message(monkeypatch, tmp_path):
    output_dir = write_packet(tmp_path)
    sent = []

    monkeypatch.setenv("GMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "password")
    monkeypatch.setattr(mailer, "MAX_MESSAGE_BYTES", 1)
    monkeypatch.setattr(mailer, "_send", lambda sender, app_password, msg: sent.append(msg))

    result = mailer.send_chart_packet("2026-08-14", output_dir, artifact_url="https://example.com/artifact")

    assert result.status == mailer.DELIVERY_LINK_SENT
    assert result.artifact_url == "https://example.com/artifact"
    assert len(sent) == 1
    assert "Artifact URL: https://example.com/artifact" in sent[0].get_content()


def test_normal_packet_sends_attachment_message(monkeypatch, tmp_path):
    output_dir = write_packet(tmp_path)
    sent = []

    monkeypatch.setenv("GMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "password")
    monkeypatch.setattr(mailer, "_send", lambda sender, app_password, msg: sent.append(msg))

    result = mailer.send_chart_packet("2026-08-14", output_dir)

    assert result.status == mailer.DELIVERY_SUCCESS
    assert len(sent) == 1
    assert len(list(sent[0].iter_attachments())) == 2
