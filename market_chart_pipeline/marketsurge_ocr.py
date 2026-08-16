from __future__ import annotations

import csv
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from .core import ValidationError


SECTION_LABELS = {
    "breaking out today": "Breaking Out Today",
    "recent breakouts": "Recent Breakouts",
    "tight areas": "Tight Areas",
    "near pivot": "Near Pivot",
    "power from pivot": "Power from Pivot",
    "top rated stocks": "Top Rated Stocks",
    "brandens watchlist": "BRANDENS WATCHLIST",
    "branden's watchlist": "BRANDENS WATCHLIST",
}
TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
ROW_RE = re.compile(r"^\s*(?:\d+\s+)?([A-Z][A-Z0-9.-]{0,9})\b")
COUNT_RE = re.compile(r"\b(\d{1,3})\s+(?:items?|stocks?|results?)\b", re.IGNORECASE)
EXCLUDED_TOKENS = {
    "ACC", "ADR", "AVG", "BUY", "CANSLIM", "COMP", "EPS", "ETF", "GROUP",
    "IBD", "INDUSTRY", "MARKET", "NAME", "PRICE", "RANK", "RATING", "RS",
    "SECTOR", "SELL", "SMR", "STOCK", "SYMBOL", "TODAY", "VOL", "VOLUME",
}


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float


def _section_label(text: str, page: int) -> str | None:
    for raw_line in text.splitlines():
        normalized = " ".join(raw_line.lower().split())
        matches = [label for needle, label in SECTION_LABELS.items() if needle in normalized]
        if len(set(matches)) == 1:
            return matches[0]
    return None


def parse_ocr_pages(
    pages: list[dict[str, Any]], *, session_date: str, source_sha256: str, minimum_confidence: float = 72.0
) -> dict[str, Any]:
    records: dict[str, dict[str, Any]] = {}
    page_results: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    needs_review = False

    for page in pages:
        page_number = int(page["page"])
        lines = [
            line if isinstance(line, OcrLine) else OcrLine(str(line["text"]), float(line["confidence"]))
            for line in page.get("lines", [])
        ]
        text = "\n".join(line.text for line in lines)
        label = _section_label(text, page_number)
        declared = [int(value) for value in COUNT_RE.findall(text)]
        tickers: list[str] = []
        low_confidence: list[str] = []
        if label is None:
            needs_review = True
            warnings.append({"code": "UNRECOGNIZED_SECTION", "pdf_page": page_number})
        else:
            for line in lines:
                if _section_label(line.text, page_number) is not None or COUNT_RE.search(line.text):
                    continue
                match = ROW_RE.match(line.text.upper())
                if not match:
                    continue
                ticker = match.group(1).strip(".-")
                if not TOKEN_RE.fullmatch(ticker) or ticker in EXCLUDED_TOKENS:
                    continue
                if line.confidence < minimum_confidence:
                    low_confidence.append(ticker)
                    continue
                if ticker not in tickers:
                    tickers.append(ticker)

        if low_confidence:
            needs_review = True
            warnings.append(
                {"code": "LOW_CONFIDENCE_TICKERS", "pdf_page": page_number, "tickers": low_confidence}
            )
        if declared and max(declared) != len(tickers):
            needs_review = True
            warnings.append(
                {
                    "code": "VISIBLE_ROW_COUNT_MISMATCH",
                    "pdf_page": page_number,
                    "declared_count": max(declared),
                    "extracted_count": len(tickers),
                }
            )
        if not tickers:
            needs_review = True
            warnings.append({"code": "NO_VERIFIED_TICKERS", "pdf_page": page_number})

        page_results.append(
            {
                "pdf_page": page_number,
                "label": label,
                "declared_count": max(declared) if declared else None,
                "tickers": tickers,
                "low_confidence_tickers": low_confidence,
            }
        )
        if label:
            source_type = "BRANDENS_WATCHLIST" if label == "BRANDENS WATCHLIST" else "STANDARD_MARKETSURGE"
            for ticker in tickers:
                record = records.setdefault(ticker, {"ticker": ticker, "chart_required": True, "sources": []})
                source = {"source_type": source_type, "label": label, "pdf_page": page_number}
                if source not in record["sources"]:
                    record["sources"].append(source)

    status = "NEEDS_REVIEW" if needs_review else "COMPLETE"
    return {
        "schema_version": "marketsurge_ocr_v1",
        "status": status,
        "session_date": session_date,
        "feed": "iex",
        "marketsurge_pdf_sha256": source_sha256,
        "unique_ticker_count": len(records),
        "records": [records[ticker] for ticker in sorted(records)],
        "pages": page_results,
        "warnings": warnings,
    }


def _tsv_lines(raw: str) -> list[OcrLine]:
    grouped: dict[tuple[str, str, str, str], list[tuple[str, float]]] = {}
    for row in csv.DictReader(raw.splitlines(), delimiter="\t"):
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(row.get("conf") or -1)
        except ValueError:
            confidence = -1
        key = tuple(row.get(name, "") for name in ("block_num", "par_num", "line_num", "page_num"))
        grouped.setdefault(key, []).append((text, confidence))
    result = []
    for words in grouped.values():
        confidences = [confidence for _, confidence in words if confidence >= 0]
        result.append(OcrLine(" ".join(word for word, _ in words), mean(confidences) if confidences else 0.0))
    return result


def extract_pdf_manifest(
    pdf_path: Path, *, session_date: str, source_sha256: str, work_dir: Path
) -> dict[str, Any]:
    for command in ("pdftoppm", "tesseract"):
        if shutil.which(command) is None:
            raise ValidationError(f"{command} is required for MarketSurge OCR")
    image_dir = work_dir / "ocr-pages"
    image_dir.mkdir(parents=True, exist_ok=True)
    prefix = image_dir / "page"
    subprocess.run(
        ["pdftoppm", "-png", "-r", "220", str(pdf_path), str(prefix)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    images = sorted(image_dir.glob("page-*.png"))
    if not images:
        raise ValidationError("MarketSurge PDF rendered no pages")
    pages = []
    for page_number, image in enumerate(images, start=1):
        completed = subprocess.run(
            ["tesseract", str(image), "stdout", "--psm", "6", "tsv"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        pages.append({"page": page_number, "lines": _tsv_lines(completed.stdout)})
    return parse_ocr_pages(pages, session_date=session_date, source_sha256=source_sha256)
