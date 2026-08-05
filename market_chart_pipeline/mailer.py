from __future__ import annotations

import argparse
import json
import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from .core import ValidationError

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 465
MAX_MESSAGE_BYTES = 24 * 1024 * 1024


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValidationError(f"Missing required email secret: {name}")
    return value


def _attach(message: EmailMessage, path: Path) -> None:
    if not path.exists() or not path.is_file():
        raise ValidationError(f"Email attachment does not exist: {path}")
    mime, _ = mimetypes.guess_type(path.name)
    maintype, subtype = (mime or "application/octet-stream").split("/", 1)
    message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)


def send_chart_packet(session_date: str, output_dir: Path) -> None:
    sender = _required_env("GMAIL_ADDRESS")
    app_password = _required_env("GMAIL_APP_PASSWORD").replace(" ", "")
    recipient = os.getenv("CHART_PACKET_RECIPIENT", "").strip() or sender

    json_path = output_dir / f"Market_Chart_Data_{session_date}.json"
    pdf_path = output_dir / f"Market_Chart_Packet_{session_date}.pdf"
    if not json_path.exists() or not pdf_path.exists():
        raise ValidationError("Expected current-session chart packet PDF and JSON were not generated")

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    if payload.get("session_date") != session_date:
        raise ValidationError("Chart data session date does not match email session date")
    if payload.get("status") not in {"COMPLETE", "COMPLETE_WITH_WARNINGS"}:
        raise ValidationError(f"Chart packet status is not deliverable: {payload.get('status')}")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = f"Market Chart Packet — {session_date}"
    msg.set_content(
        "\n".join(
            [
                f"Current-session market chart packet for {session_date}.",
                f"Status: {payload.get('status')}",
                f"Requested tickers: {len(payload.get('requested_tickers', []))}",
                f"Verified charts: {payload.get('verified_count', 0)}",
                f"Ticker errors: {payload.get('error_count', 0)}",
                f"PDF SHA-256: {(payload.get('artifacts') or {}).get('pdf_sha256', 'UNAVAILABLE')}",
                "The attached PDF and JSON belong to this chart-generation run.",
            ]
        )
    )
    _attach(msg, pdf_path)
    _attach(msg, json_path)

    message_size = len(msg.as_bytes())
    if message_size > MAX_MESSAGE_BYTES:
        raise ValidationError(
            f"Email would be {message_size / 1024 / 1024:.1f} MB, above the configured 24 MB safety limit"
        )

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, context=context, timeout=45) as smtp:
        smtp.login(sender, app_password)
        smtp.send_message(msg)


def main() -> None:
    parser = argparse.ArgumentParser(description="Email the generated market chart packet")
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    send_chart_packet(args.session_date, Path(args.output_dir))
    print(f"emailed chart packet for {args.session_date}")


if __name__ == "__main__":
    main()
