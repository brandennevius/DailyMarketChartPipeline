from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .core import ValidationError, build_packet
from .manifest import TICKER_RE, load_manifest
from .marketsurge_ocr import SECTION_LABELS, extract_pdf_manifest
from .orchestrator import run_daily_review
from .review_mailer import send_review
from .utils import atomic_write_text, sha256_file


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
CALLBACK_SCHEMA = "campus-fund-market-review-callback-v1"
WORKER_INPUT_SCHEMA = "campus-fund-market-review-worker-input-v1"


class ResultRegistrationError(RuntimeError):
    def __init__(self, result: dict[str, Any], cause: Exception) -> None:
        super().__init__(f"Dashboard result registration failed: {cause}")
        self.result = result


class DashboardClient:
    def __init__(
        self,
        *,
        worker_input_url: str,
        worker_callback_url: str,
        allowed_base_url: str,
        secret: str,
        run_id: str,
        session_date: str,
        attempt: int,
        source_hashes: dict[str, str],
    ) -> None:
        if not RUN_ID_RE.fullmatch(run_id):
            raise ValidationError("Dashboard run_id contains unsupported characters")
        if attempt < 1:
            raise ValidationError("Dashboard attempt must be positive")
        if set(source_hashes) != {
            "marketsurge_pdf_sha256",
            "snapshot_json_sha256",
            "snapshot_markdown_sha256",
        } or any(not SHA256_RE.fullmatch(value) for value in source_hashes.values()):
            raise ValidationError("Dashboard dispatch must contain all three source SHA-256 values")
        allowed = urlparse(self._absolute_https(allowed_base_url))
        self.origin = (allowed.scheme, allowed.netloc)
        self.worker_input_url = self._same_origin_url(worker_input_url)
        self.expected_callback_url = self._absolute_https(worker_callback_url)
        callback_parsed = urlparse(self.expected_callback_url)
        if (callback_parsed.scheme, callback_parsed.netloc) != self.origin:
            raise ValidationError("Dashboard worker endpoints must use the same origin")
        self.secret = secret
        self.run_id = run_id
        self.session_date = session_date
        self.attempt = attempt
        self.source_hashes = source_hashes
        self.callback_url: str | None = None
        self.callback_token: str | None = None

    @staticmethod
    def _absolute_https(value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValidationError("Dashboard endpoint must be an absolute HTTPS URL")
        return value

    def _same_origin_url(self, value: str) -> str:
        parsed = urlparse(self._absolute_https(value))
        if (parsed.scheme, parsed.netloc) != self.origin:
            raise ValidationError("Dashboard supplied a cross-origin URL")
        return value

    def input(self) -> dict[str, Any]:
        response = requests.get(
            self.worker_input_url,
            headers={
                "Authorization": f"Bearer {self.secret}",
                "Accept": "application/json",
                "X-Review-Attempt": str(self.attempt),
                "X-MarketSurge-PDF-SHA256": self.source_hashes["marketsurge_pdf_sha256"],
                "X-Snapshot-JSON-SHA256": self.source_hashes["snapshot_json_sha256"],
                "X-Snapshot-Markdown-SHA256": self.source_hashes["snapshot_markdown_sha256"],
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("schema_version") != WORKER_INPUT_SCHEMA:
            raise ValidationError("Dashboard worker input schema is unsupported")
        if (
            str(payload.get("review_run_id")) != self.run_id
            or str(payload.get("session_date")) != self.session_date
            or int(payload.get("attempt", 0)) != self.attempt
            or payload.get("source_hashes") != self.source_hashes
        ):
            raise ValidationError("Dashboard worker input correlation mismatch")
        callback = payload.get("callback") or {}
        callback_url = self._same_origin_url(str(callback.get("url") or ""))
        if callback_url != self.expected_callback_url or callback.get("schema_version") != CALLBACK_SCHEMA:
            raise ValidationError("Dashboard callback endpoint does not match the dispatch")
        token = str(callback.get("token") or "")
        if not token:
            raise ValidationError("Dashboard worker input omitted the callback token")
        self.callback_url = callback_url
        self.callback_token = token
        return payload

    def download(
        self, url: str, output: Path, expected_sha256: str, *, max_bytes: int = 50_000_000
    ) -> None:
        response = requests.get(self._same_origin_url(url), timeout=120)
        response.raise_for_status()
        length = int(response.headers.get("Content-Length") or 0)
        if length > max_bytes or len(response.content) > max_bytes:
            raise ValidationError(f"Dashboard source exceeds the {max_bytes}-byte limit")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(response.content)
        if sha256_file(output) != expected_sha256:
            raise ValidationError(f"Dashboard source hash mismatch for {output.name}")

    def _envelope(self, event_type: str, event_id: str) -> dict[str, Any]:
        repository = os.getenv("GITHUB_REPOSITORY", "brandennevius/DailyMarketChartPipeline")
        workflow_run_id = os.getenv("GITHUB_RUN_ID", "local")
        server_url = os.getenv("GITHUB_SERVER_URL", "https://github.com")
        return {
            "schema_version": CALLBACK_SCHEMA,
            "event_id": event_id[:160],
            "event_type": event_type,
            "review_run_id": self.run_id,
            "session_date": self.session_date,
            "attempt": self.attempt,
            "source_hashes": self.source_hashes,
            "github": {
                "repository": repository,
                "workflow_run_id": workflow_run_id,
                "workflow_run_attempt": int(os.getenv("GITHUB_RUN_ATTEMPT", "1")),
                "workflow_url": f"{server_url}/{repository}/actions/runs/{workflow_run_id}",
            },
        }

    def callback(
        self,
        event_type: str,
        event_id: str,
        details: dict[str, Any],
        files: dict[str, tuple[Path, str]] | None = None,
    ) -> None:
        if not self.callback_url or not self.callback_token:
            raise ValidationError("Dashboard callback credentials are unavailable")
        metadata = {**self._envelope(event_type, event_id), **details}
        headers = {"Authorization": f"Bearer {self.callback_token}"}
        if files:
            handles = {
                name: (path.name, path.open("rb"), media_type)
                for name, (path, media_type) in files.items()
            }
            try:
                response = requests.post(
                    self.callback_url,
                    headers=headers,
                    data={"metadata": json.dumps(metadata, separators=(",", ":"))},
                    files=handles,
                    timeout=180,
                )
            finally:
                for _, handle, _ in handles.values():
                    handle.close()
        else:
            response = requests.post(
                self.callback_url,
                headers={**headers, "Content-Type": "application/json"},
                json=metadata,
                timeout=60,
            )
        response.raise_for_status()


def _validate_pdf(path: Path, expected_pages: int | None) -> int:
    if path.read_bytes()[:5] != b"%PDF-":
        raise ValidationError("MarketSurge upload is not a PDF")
    completed = subprocess.run(
        ["pdfinfo", str(path)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    pages = next(
        (int(line.split(":", 1)[1].strip()) for line in completed.stdout.splitlines() if line.startswith("Pages:")),
        None,
    )
    if not pages or not 1 <= pages <= 75:
        raise ValidationError("MarketSurge PDF must contain 1-75 readable pages")
    if expected_pages is not None and pages != int(expected_pages):
        raise ValidationError(f"MarketSurge PDF page count mismatch: expected {expected_pages}, got {pages}")
    return pages


def _merge_portfolio(manifest: dict[str, Any], portfolio: dict[str, Any]) -> dict[str, Any]:
    by_ticker = {record["ticker"]: record for record in manifest.get("records", [])}
    positions = portfolio.get("open_positions", []) if isinstance(portfolio, dict) else []
    for position in positions:
        ticker = str(position.get("ticker") or position.get("symbol") or "").strip().upper()
        if not ticker:
            continue
        record = by_ticker.setdefault(ticker, {"ticker": ticker, "chart_required": True, "sources": []})
        source = {"source_type": "PORTFOLIO", "label": "Current Portfolio", "pdf_page": None}
        if source not in record["sources"]:
            record["sources"].append(source)
    manifest["records"] = [by_ticker[ticker] for ticker in sorted(by_ticker)]
    manifest["unique_ticker_count"] = len(by_ticker)
    return manifest


def _manifest_from_corrections(
    correction_payload: dict[str, Any], *, session_date: str, marketsurge_sha256: str
) -> dict[str, Any]:
    corrections = correction_payload.get("corrections")
    if not isinstance(corrections, list) or not corrections:
        raise ValidationError("OCR correction source must contain a non-empty corrections array")
    labels = set(SECTION_LABELS.values())
    records: dict[str, dict[str, Any]] = {}
    pages = []
    for item in corrections:
        if not isinstance(item, dict):
            raise ValidationError("Every OCR correction item must be an object")
        page = item.get("pdf_page")
        label = item.get("label")
        tickers = item.get("tickers")
        if not isinstance(page, int) or page < 1 or label not in labels or not isinstance(tickers, list):
            raise ValidationError("OCR correction items require pdf_page, a known label, and tickers")
        cleaned = []
        for raw_ticker in tickers:
            ticker = str(raw_ticker).strip().upper()
            if not TICKER_RE.fullmatch(ticker):
                raise ValidationError(f"OCR correction contains an invalid ticker: {raw_ticker}")
            if ticker not in cleaned:
                cleaned.append(ticker)
            source_type = "BRANDENS_WATCHLIST" if label == "BRANDENS WATCHLIST" else "STANDARD_MARKETSURGE"
            record = records.setdefault(ticker, {"ticker": ticker, "chart_required": True, "sources": []})
            source = {"source_type": source_type, "label": label, "pdf_page": page}
            if source not in record["sources"]:
                record["sources"].append(source)
        pages.append({"pdf_page": page, "label": label, "tickers": cleaned})
    return {
        "schema_version": "marketsurge_ocr_v1",
        "status": "COMPLETE_WITH_WARNINGS",
        "session_date": session_date,
        "feed": "iex",
        "marketsurge_pdf_sha256": marketsurge_sha256,
        "unique_ticker_count": len(records),
        "records": [records[ticker] for ticker in sorted(records)],
        "pages": pages,
        "warnings": [{"code": "USER_APPROVED_OCR_CORRECTIONS"}],
    }


def _artifact_metadata(kind: str, path: Path, media_type: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "filename": path.name,
        "media_type": media_type,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def run_dashboard_review(client: DashboardClient, work_dir: Path, output_dir: Path) -> dict[str, Any]:
    request = client.input()
    event_prefix = f"{client.run_id}:{client.attempt}"
    client.callback("RUNNING", f"{event_prefix}:acquiring", {"stage": "ACQUIRING_INPUTS"})
    portfolio_spec = request.get("portfolio_snapshot") or {}
    pdf_spec = request.get("marketsurge_pdf") or {}
    portfolio_path = work_dir / "portfolio-snapshot.json"
    snapshot_markdown_path = work_dir / "portfolio-snapshot.md"
    pdf_path = work_dir / "marketsurge-scan.pdf"
    client.download(str(portfolio_spec["download_url"]), portfolio_path, str(portfolio_spec["sha256"]), max_bytes=10_000_000)
    client.download(
        str(portfolio_spec["markdown_download_url"]),
        snapshot_markdown_path,
        str(portfolio_spec["markdown_sha256"]),
        max_bytes=10_000_000,
    )
    client.download(str(pdf_spec["download_url"]), pdf_path, str(pdf_spec["sha256"]), max_bytes=20 * 1024 * 1024)
    page_count = _validate_pdf(pdf_path, pdf_spec.get("page_count"))
    portfolio = json.loads(portfolio_path.read_text(encoding="utf-8"))

    corrections = request.get("ocr_corrections")
    if corrections:
        corrections_path = work_dir / "ocr-corrections.json"
        client.download(
            str(corrections["download_url"]),
            corrections_path,
            str(corrections["sha256"]),
            max_bytes=512 * 1024,
        )
        manifest = _manifest_from_corrections(
            json.loads(corrections_path.read_text(encoding="utf-8")),
            session_date=client.session_date,
            marketsurge_sha256=sha256_file(pdf_path),
        )
    else:
        client.callback("RUNNING", f"{event_prefix}:ocr", {"stage": "OCR"})
        manifest = extract_pdf_manifest(
            pdf_path,
            session_date=client.session_date,
            source_sha256=sha256_file(pdf_path),
            work_dir=work_dir,
        )
    manifest = _merge_portfolio(manifest, portfolio)
    manifest_path = work_dir / "chart-request-manifest.json"
    atomic_write_text(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    if manifest.get("status") == "NEEDS_REVIEW":
        ocr = {
            "status": "NEEDS_REVIEW",
            "version": 0,
            "items": manifest.get("pages", []),
            "message": "Review page labels, visible ticker rows, and OCR warnings before retrying.",
        }
        client.callback("OCR_REVIEW_REQUIRED", f"{event_prefix}:ocr-review", {"ocr": ocr})
        return {
            "review_run_id": client.run_id,
            "session_date": client.session_date,
            "attempt": client.attempt,
            "status": "NEEDS_REVIEW",
            "page_count": page_count,
            "ocr": ocr,
        }

    chart_request = load_manifest(manifest_path)
    client.callback(
        "RUNNING",
        f"{event_prefix}:charts",
        {"stage": "BUILDING_CHARTS", "ticker_count": manifest["unique_ticker_count"]},
    )
    provenance = {ticker: record["sources"] for ticker, record in chart_request.records_by_ticker.items()}
    chart_dir = work_dir / "chart-packet" / client.session_date
    chart_payload = build_packet(
        sorted(provenance), client.session_date, chart_dir, manifest.get("feed", "iex"), provenance
    )
    chart_payload["source_manifest"] = {"path": str(manifest_path), "records": chart_request.records_by_ticker}
    chart_json = chart_dir / f"Market_Chart_Data_{client.session_date}.json"
    atomic_write_text(chart_json, json.dumps(chart_payload, indent=2, sort_keys=True) + "\n")
    chart_archive = Path(shutil.make_archive(str(work_dir / "chart-packet-artifact"), "zip", chart_dir))
    source_manifest = {
        "schema_version": "daily_review_source_manifest_v2",
        "review_run_id": client.run_id,
        "session_date": client.session_date,
        "sources": [
            {"label": "portfolio_snapshot", "path": str(portfolio_path), "sha256": sha256_file(portfolio_path), "status": "verified"},
            {"label": "portfolio_snapshot_markdown", "path": str(snapshot_markdown_path), "sha256": sha256_file(snapshot_markdown_path), "status": "verified"},
            {"label": "marketsurge_scan", "path": str(pdf_path), "sha256": sha256_file(pdf_path), "status": "verified", "page_count": page_count},
            {"label": "chart_packet_artifact", "path": str(chart_archive), "sha256": sha256_file(chart_archive), "status": "verified"},
        ],
    }
    source_manifest_path = work_dir / "source-manifest.json"
    atomic_write_text(source_manifest_path, json.dumps(source_manifest, indent=2, sort_keys=True) + "\n")
    client.callback("RUNNING", f"{event_prefix}:render", {"stage": "RENDERING_REVIEW"})
    review = run_daily_review(
        requested_date=client.session_date,
        session_date=client.session_date,
        mode="read-only",
        output_dir=output_dir,
        portfolio_path=str(portfolio_path),
        chart_packet_dir=str(chart_dir),
        source_manifest_path=str(source_manifest_path),
        audit_profile="strict-core",
    )
    packet = review["packet"]
    paths = {
        "pdf": Path(review["pdf_path"]),
        "markdown": Path(review["markdown_path"]),
        "json": Path(review["json_path"]),
    }
    artifacts = [
        _artifact_metadata("pdf", paths["pdf"], "application/pdf"),
        _artifact_metadata("markdown", paths["markdown"], "text/markdown; charset=utf-8"),
        _artifact_metadata("json", paths["json"], "application/json"),
    ]
    completed = {
        "review_run_id": client.run_id,
        "session_date": client.session_date,
        "attempt": client.attempt,
        "status": "COMPLETED",
        "packet_sha256": packet["packet_sha256"],
        "packet_artifact_sha256": sha256_file(paths["json"]),
        "audit_status": "pass",
        "delivery": {"status": "PENDING", "error": None},
    }
    try:
        client.callback(
            "RESULTS_REGISTERED",
            f"{event_prefix}:results",
            {
                "audit": {
                    "status": "PASS",
                    "packet_sha256": sha256_file(paths["json"]),
                    "evidence": {
                        "canonical_packet_sha256": packet["packet_sha256"],
                        "validation_evidence": packet.get("validation_evidence", []),
                    },
                },
                "delivery": {"status": "PENDING", "error": None},
                "artifacts": artifacts,
            },
            files={
                "pdf": (paths["pdf"], "application/pdf"),
                "markdown": (paths["markdown"], "text/markdown; charset=utf-8"),
                "packet": (paths["json"], "application/json"),
            },
        )
    except Exception as exc:
        failed = {
            **completed,
            "status": "FAILED",
            "stage": "RESULT_REGISTRATION",
            "delivery": {"status": "FAILED", "error": str(exc)},
        }
        raise ResultRegistrationError(failed, exc) from exc

    try:
        delivery_result = send_review(client.session_date, output_dir)
        delivery = {"status": "SENT", "error": None, "message_size_bytes": delivery_result.message_size_bytes}
    except Exception as exc:
        delivery = {"status": "FAILED", "error": str(exc)}
    try:
        client.callback(
            "DELIVERY_STATUS",
            f"{event_prefix}:delivery:{delivery['status'].lower()}",
            {"delivery": {"status": delivery["status"], "error": delivery.get("error")}},
        )
    except Exception as exc:
        delivery = {"status": "FAILED", "error": f"Delivery callback failed: {exc}"}
    return {**completed, "delivery": delivery}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a dashboard-triggered deterministic daily review")
    parser.add_argument("--review-run-id", required=True)
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--marketsurge-pdf-sha256", required=True)
    parser.add_argument("--snapshot-json-sha256", required=True)
    parser.add_argument("--snapshot-markdown-sha256", required=True)
    parser.add_argument("--worker-input-url", required=True)
    parser.add_argument("--worker-callback-url", required=True)
    parser.add_argument("--work-dir", default="dashboard-input")
    parser.add_argument("--output-dir", default="reports/market")
    parser.add_argument("--status-json", default="dashboard-status.json")
    args = parser.parse_args()
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    status_path = Path(args.status_json)
    client = DashboardClient(
        worker_input_url=args.worker_input_url,
        worker_callback_url=args.worker_callback_url,
        allowed_base_url=os.environ["DASHBOARD_BASE_URL"],
        secret=os.environ["DASHBOARD_WORKER_SECRET"],
        run_id=args.review_run_id,
        session_date=args.session_date,
        attempt=args.attempt,
        source_hashes={
            "marketsurge_pdf_sha256": args.marketsurge_pdf_sha256,
            "snapshot_json_sha256": args.snapshot_json_sha256,
            "snapshot_markdown_sha256": args.snapshot_markdown_sha256,
        },
    )
    try:
        result = run_dashboard_review(client, work_dir, Path(args.output_dir))
    except Exception as exc:
        result = (
            {**exc.result, "error": str(exc)}
            if isinstance(exc, ResultRegistrationError)
            else {
                "review_run_id": args.review_run_id,
                "session_date": args.session_date,
                "attempt": args.attempt,
                "status": "FAILED",
                "error": str(exc),
            }
        )
        if client.callback_token:
            try:
                client.callback(
                    "FAILED",
                    f"{args.review_run_id}:{args.attempt}:failed",
                    {"error": {"code": "PIPELINE_FAILED", "message": str(exc)}},
                )
            except Exception as callback_exc:
                result["callback_error"] = str(callback_exc)
        atomic_write_text(status_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
        raise
    atomic_write_text(status_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "ocr"}, indent=2))


if __name__ == "__main__":
    main()
