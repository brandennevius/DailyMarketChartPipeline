from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import shutil
import zipfile
from datetime import date, datetime, timezone
from email.header import decode_header, make_header
from pathlib import Path
from typing import Any

import requests

from .core import ValidationError
from .utils import atomic_write_text, sha256_file


def _subject(message: email.message.Message) -> str:
    return str(make_header(decode_header(message.get("Subject", ""))))


def _message_date(message: email.message.Message) -> str | None:
    value = message.get("Date")
    if not value:
        return None
    parsed = email.utils.parsedate_to_datetime(value)
    return parsed.astimezone(timezone.utc).isoformat() if parsed else None


def _safe_attachment_name(name: str) -> str:
    cleaned = Path(name).name
    if not cleaned or cleaned in {".", ".."}:
        raise ValidationError("Email attachment has an unsafe filename")
    return cleaned


def _find_exact_message(client: imaplib.IMAP4_SSL, subject: str, session_date: str) -> tuple[bytes, email.message.Message]:
    since = date.fromisoformat(session_date).strftime("%d-%b-%Y")
    status, data = client.search(None, "SINCE", since)
    if status != "OK":
        raise ValidationError(f"Gmail search failed for {subject}")
    matches: list[tuple[bytes, email.message.Message]] = []
    for message_id in data[0].split():
        status, payload = client.fetch(message_id, "(RFC822)")
        if status != "OK" or not payload or not isinstance(payload[0], tuple):
            continue
        message = email.message_from_bytes(payload[0][1])
        if _subject(message) == subject:
            matches.append((message_id, message))
    if len(matches) != 1:
        raise ValidationError(f"Expected exactly one Gmail message with subject {subject!r}; found {len(matches)}")
    return matches[0]


def _write_matching_attachment(message: email.message.Message, output_dir: Path, suffix: str) -> Path:
    matches = []
    for part in message.walk():
        filename = part.get_filename()
        if filename and filename.lower().endswith(suffix.lower()):
            matches.append(part)
    if len(matches) != 1:
        raise ValidationError(f"Expected exactly one {suffix} attachment; found {len(matches)}")
    filename = _safe_attachment_name(str(make_header(decode_header(matches[0].get_filename()))))
    path = output_dir / filename
    path.write_bytes(matches[0].get_payload(decode=True) or b"")
    if not path.stat().st_size:
        raise ValidationError(f"Downloaded attachment is empty: {filename}")
    return path


def acquire_gmail_sources(session_date: str, output_dir: Path) -> list[dict[str, Any]]:
    address = os.getenv("GMAIL_ADDRESS", "").strip()
    password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not address or not password:
        raise ValidationError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD are required for source acquisition")
    output_dir.mkdir(parents=True, exist_ok=True)
    sources = []
    with imaplib.IMAP4_SSL("imap.gmail.com", 993) as client:
        client.login(address, password)
        # Both required self-sent source messages are retained in INBOX. Using
        # the portable mailbox avoids provider-specific quoting rules for
        # names such as "[Gmail]/All Mail".
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise ValidationError("Could not open Gmail source mailbox")

        specs = [
            (f"Trading Dashboard Snapshot — {session_date}", ".json", "portfolio_snapshot"),
            (f"MarketSurge Scan — {session_date}", ".pdf", "marketsurge_scan"),
        ]
        for subject, suffix, label in specs:
            message_id, message = _find_exact_message(client, subject, session_date)
            path = _write_matching_attachment(message, output_dir, suffix)
            sources.append(
                {
                    "label": label,
                    "path": str(path),
                    "sha256": sha256_file(path),
                    "status": "verified",
                    "subject": subject,
                    "message_uid": message_id.decode("ascii"),
                    "source_timestamp": _message_date(message),
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                }
            )
    return sources


def _safe_extract(archive: Path, output_dir: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (output_dir / member.filename).resolve()
            if output_dir.resolve() not in target.parents and target != output_dir.resolve():
                raise ValidationError("Chart artifact contains an unsafe path")
        bundle.extractall(output_dir)


def acquire_chart_artifact(session_date: str, output_dir: Path, repository: str) -> list[dict[str, Any]]:
    token = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    if not token:
        raise ValidationError("GITHUB_TOKEN is required for chart artifact acquisition")
    name = f"market-chart-packet-{session_date}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    response = requests.get(
        f"https://api.github.com/repos/{repository}/actions/artifacts",
        params={"name": name, "per_page": 100},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    artifacts = [item for item in response.json().get("artifacts", []) if not item.get("expired")]
    if not artifacts:
        raise ValidationError(f"No unexpired GitHub artifact named {name}")
    artifact = max(artifacts, key=lambda item: item["created_at"])
    download = requests.get(artifact["archive_download_url"], headers=headers, timeout=60)
    download.raise_for_status()
    archive = output_dir / f"{name}.zip"
    archive.write_bytes(download.content)
    chart_dir = output_dir / "chart_packet"
    chart_dir.mkdir(parents=True, exist_ok=True)
    _safe_extract(archive, chart_dir)
    required = [
        chart_dir / f"Market_Chart_Data_{session_date}.json",
        chart_dir / f"Market_Chart_Packet_{session_date}.pdf",
    ]
    for path in required:
        if not path.exists():
            found = list(chart_dir.rglob(path.name))
            if len(found) != 1:
                raise ValidationError(f"Chart artifact is missing {path.name}")
            shutil.copy2(found[0], path)
    return [
        {
            "label": "chart_packet_artifact",
            "path": str(archive),
            "sha256": sha256_file(archive),
            "status": "verified",
            "artifact_id": artifact["id"],
            "workflow_run_id": artifact.get("workflow_run", {}).get("id"),
            "source_timestamp": artifact["created_at"],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }
    ]


def acquire_sources(session_date: str, output_dir: Path, repository: str) -> dict[str, Any]:
    sources = acquire_gmail_sources(session_date, output_dir)
    sources.extend(acquire_chart_artifact(session_date, output_dir, repository))
    manifest = {
        "schema_version": "daily_review_source_manifest_v1",
        "session_date": session_date,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
    }
    manifest_path = output_dir / "source-manifest.json"
    atomic_write_text(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return {
        "manifest": str(manifest_path),
        "portfolio": next(item["path"] for item in sources if item["label"] == "portfolio_snapshot"),
        "marketsurge": next(item["path"] for item in sources if item["label"] == "marketsurge_scan"),
        "chart_packet_dir": str(output_dir / "chart_packet"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Acquire exact-session daily-review source artifacts")
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", "brandennevius/DailyMarketChartPipeline"))
    parser.add_argument("--outputs-json", required=True)
    args = parser.parse_args()
    try:
        result = acquire_sources(args.session_date, Path(args.output_dir), args.repository)
    except Exception as exc:
        atomic_write_text(Path(args.outputs_json), json.dumps({"error": str(exc)}, indent=2) + "\n")
        raise
    atomic_write_text(Path(args.outputs_json), json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
