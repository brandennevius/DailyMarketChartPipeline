from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from email.message import EmailMessage
from pathlib import Path

from .core import ValidationError
from .mailer import DELIVERY_FAILED, DELIVERY_SUCCESS, DeliveryResult, MAX_MESSAGE_BYTES, _attach, _required_env, _send, _write_status

DELIVERY_FAILURE_NOTICE_SENT = "FAILURE_NOTICE_SENT"


def send_review(session_date: str, output_dir: Path) -> DeliveryResult:
    sender = _required_env("GMAIL_ADDRESS")
    password = _required_env("GMAIL_APP_PASSWORD").replace(" ", "")
    recipient = os.getenv("DAILY_REVIEW_RECIPIENT", "").strip() or os.getenv("CHART_PACKET_RECIPIENT", "").strip() or sender
    session_dir = output_dir / session_date
    json_path = session_dir / f"{session_date}-market-review.json"
    md_path = session_dir / f"{session_date}-market-review.md"
    pdf_path = session_dir / f"{session_date}-market-review.pdf"
    if not all(path.exists() for path in (json_path, md_path, pdf_path)):
        raise ValidationError("Validated daily-review JSON, Markdown, and PDF are required for delivery")
    packet = json.loads(json_path.read_text(encoding="utf-8"))
    if packet.get("session_date") != session_date or not packet.get("packet_sha256"):
        raise ValidationError("Daily-review packet identity is invalid")
    if any(item.get("status") != "pass" for item in packet.get("validation_evidence", [])):
        raise ValidationError("Daily-review packet has a failed validation gate")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = f"Daily Market & Portfolio Review - {session_date}"
    message.set_content(
        "\n".join(
            [
                f"Validated deterministic daily review for {session_date}.",
                f"Packet SHA-256: {packet['packet_sha256']}",
                f"Policy version: {packet['policy_version']}",
                "The attached PDF, Markdown, and canonical JSON were generated from the same frozen packet.",
            ]
        )
    )
    for path in (pdf_path, md_path, json_path):
        _attach(message, path)
    size = len(message.as_bytes())
    if size > MAX_MESSAGE_BYTES:
        raise ValidationError(f"Daily review email is {size} bytes and exceeds the attachment safety limit")
    _send(sender, password, message)
    return DeliveryResult(status=DELIVERY_SUCCESS, message_size_bytes=size)


def send_failure(session_date: str, error: str) -> DeliveryResult:
    sender = _required_env("GMAIL_ADDRESS")
    password = _required_env("GMAIL_APP_PASSWORD").replace(" ", "")
    recipient = os.getenv("DAILY_REVIEW_RECIPIENT", "").strip() or os.getenv("CHART_PACKET_RECIPIENT", "").strip() or sender
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = f"Daily Market Review FAILED VALIDATION - {session_date}"
    message.set_content(
        "\n".join(
            [
                f"The deterministic daily review failed closed for {session_date}.",
                f"Failed gate: {error}",
                "No report attachment or trading recommendation was produced.",
            ]
        )
    )
    _send(sender, password, message)
    return DeliveryResult(
        status=DELIVERY_FAILURE_NOTICE_SENT,
        message_size_bytes=len(message.as_bytes()),
        fallback_reason="failure_notice_only_no_report_delivered",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Email a validated deterministic daily review")
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--status-json", required=True)
    parser.add_argument("--failure-error")
    args = parser.parse_args()
    try:
        result = send_failure(args.session_date, args.failure_error) if args.failure_error else send_review(
            args.session_date, Path(args.output_dir)
        )
    except Exception as exc:
        result = DeliveryResult(status=DELIVERY_FAILED, error=str(exc))
        _write_status(Path(args.status_json), result)
        raise
    _write_status(Path(args.status_json), result)
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
