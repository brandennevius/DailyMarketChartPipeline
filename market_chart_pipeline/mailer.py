from __future__ import annotations

import argparse
import json
import mimetypes
import os
import smtplib
import ssl
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from pathlib import Path

from .core import ValidationError

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 465
MAX_MESSAGE_BYTES = 24 * 1024 * 1024

DELIVERY_SUCCESS = "SUCCESS"
DELIVERY_LINK_SENT = "LINK_SENT"
DELIVERY_FAILED = "FAILED"


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    message_size_bytes: int | None = None
    fallback_reason: str | None = None
    artifact_url: str | None = None
    error: str | None = None


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


def _send(sender: str, app_password: str, message: EmailMessage) -> None:
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, context=context, timeout=45) as smtp:
        smtp.login(sender, app_password)
        smtp.send_message(message)


def _load_payload(session_date: str, output_dir: Path) -> tuple[dict, Path, Path]:
    json_path = output_dir / f"Market_Chart_Data_{session_date}.json"
    pdf_path = output_dir / f"Market_Chart_Packet_{session_date}.pdf"
    if not json_path.exists() or not pdf_path.exists():
        raise ValidationError("Expected current-session chart packet PDF and JSON were not generated")

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    if payload.get("session_date") != session_date:
        raise ValidationError("Chart data session date does not match email session date")
    if payload.get("status") not in {"COMPLETE", "COMPLETE_WITH_WARNINGS"}:
        raise ValidationError(f"Chart packet status is not deliverable: {payload.get('status')}")
    return payload, json_path, pdf_path


def _base_body(session_date: str, payload: dict) -> list[str]:
    return [
        f"Current-session market chart packet for {session_date}.",
        f"Status: {payload.get('status')}",
        f"Requested tickers: {len(payload.get('requested_tickers', []))}",
        f"Verified charts: {payload.get('verified_count', 0)}",
        f"Ticker errors: {payload.get('error_count', 0)}",
        f"PDF SHA-256: {(payload.get('artifacts') or {}).get('pdf_sha256', 'UNAVAILABLE')}",
    ]


def _full_packet_message(
    sender: str, recipient: str, session_date: str, payload: dict, pdf_path: Path, json_path: Path
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = f"Market Chart Packet - {session_date}"
    msg.set_content("\n".join([*_base_body(session_date, payload), "The attached PDF and JSON belong to this chart-generation run."]))
    _attach(msg, pdf_path)
    _attach(msg, json_path)
    return msg


def _link_message(sender: str, recipient: str, session_date: str, payload: dict, artifact_url: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = f"Market Chart Packet link - {session_date}"
    msg.set_content(
        "\n".join(
            [
                *_base_body(session_date, payload),
                "The chart packet was generated and uploaded, but the PDF is too large for Gmail attachment delivery.",
                f"Artifact URL: {artifact_url}",
            ]
        )
    )
    return msg


def send_chart_packet(session_date: str, output_dir: Path, artifact_url: str | None = None) -> DeliveryResult:
    sender = _required_env("GMAIL_ADDRESS")
    app_password = _required_env("GMAIL_APP_PASSWORD").replace(" ", "")
    recipient = os.getenv("CHART_PACKET_RECIPIENT", "").strip() or sender

    payload, json_path, pdf_path = _load_payload(session_date, output_dir)
    full_msg = _full_packet_message(sender, recipient, session_date, payload, pdf_path, json_path)
    message_size = len(full_msg.as_bytes())
    if message_size <= MAX_MESSAGE_BYTES:
        _send(sender, app_password, full_msg)
        return DeliveryResult(status=DELIVERY_SUCCESS, message_size_bytes=message_size)

    fallback_reason = (
        f"Email would be {message_size / 1024 / 1024:.1f} MB, above the configured 24 MB safety limit"
    )
    if not artifact_url:
        raise ValidationError(f"{fallback_reason}; no artifact URL provided for link delivery")

    link_msg = _link_message(sender, recipient, session_date, payload, artifact_url)
    _send(sender, app_password, link_msg)
    return DeliveryResult(
        status=DELIVERY_LINK_SENT,
        message_size_bytes=message_size,
        fallback_reason=fallback_reason,
        artifact_url=artifact_url,
    )


def _write_status(path: Path, result: DeliveryResult) -> None:
    path.write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Email the generated market chart packet")
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--artifact-url")
    parser.add_argument("--status-json")
    args = parser.parse_args()

    status_path = Path(args.status_json) if args.status_json else None
    try:
        result = send_chart_packet(args.session_date, Path(args.output_dir), artifact_url=args.artifact_url)
    except Exception as exc:
        result = DeliveryResult(status=DELIVERY_FAILED, error=str(exc))
        if status_path:
            _write_status(status_path, result)
        raise

    if status_path:
        _write_status(status_path, result)
    print(f"email delivery status for {args.session_date}: {result.status}")


if __name__ == "__main__":
    main()
