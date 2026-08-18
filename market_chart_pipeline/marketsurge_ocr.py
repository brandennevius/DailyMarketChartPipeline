from __future__ import annotations

import csv
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from PIL import Image

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
RANKED_ROW_RE = re.compile(r"^\s*(\d{1,3})\s+([A-Z][A-Z0-9.-]{0,9})\b")
COUNT_RE = re.compile(r"\b(\d{1,3})\s*(?:items?|stocks?|results?)\b", re.IGNORECASE)
EXCLUDED_TOKENS = {
    "ACC", "ADR", "AVG", "BUY", "CANSLIM", "COMP", "EPS", "ETF", "GROUP",
    "FAVORITES", "IBD", "INDUSTRY", "MARKET", "MARKETS", "NAME", "PRICE",
    "RANK", "RATING", "RS", "SCREENS", "SECTOR", "SELL", "SMR", "STOCK",
    "STOCKS", "SYMBOL", "TICKER", "TODAY", "VOL", "VOLUME",
}


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float


@dataclass(frozen=True)
class OcrWord:
    text: str
    confidence: float
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def center_y(self) -> float:
        return self.top + self.height / 2


def _section_label(text: str, page: int) -> str | None:
    del page
    normalized = " ".join(text.lower().split())
    matches = [label for needle, label in SECTION_LABELS.items() if needle in normalized]
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def _simple_line_manifest(
    pages: list[dict[str, Any]], *, session_date: str, source_sha256: str, minimum_confidence: float
) -> dict[str, Any]:
    """Compatibility parser for unit fixtures; production uses TSV geometry below."""
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
        review_rows: list[dict[str, Any]] = []
        if label is None:
            needs_review = True
            warnings.append({"code": "UNRECOGNIZED_SECTION", "pdf_page": page_number})
        else:
            for line in lines:
                match = RANKED_ROW_RE.match(line.text.upper())
                if not match:
                    continue
                rank, ticker = int(match.group(1)), match.group(2).strip(".-")
                if not TOKEN_RE.fullmatch(ticker) or ticker in EXCLUDED_TOKENS:
                    continue
                if line.confidence < minimum_confidence:
                    review_rows.append(
                        {"rank": rank, "raw_text": ticker, "confidence": line.confidence, "reason": "LOW_CONFIDENCE_SYMBOL"}
                    )
                    continue
                if ticker not in tickers:
                    tickers.append(ticker)

        if review_rows:
            needs_review = True
            warnings.append({"code": "SYMBOL_ROWS_NEED_REVIEW", "pdf_page": page_number, "rows": review_rows})
        if not tickers:
            needs_review = True
            warnings.append({"code": "NO_VERIFIED_TICKERS", "pdf_page": page_number})

        page_results.append(
            {
                "pdf_page": page_number,
                "label": label,
                "declared_count": max(declared) if declared else None,
                "screen_result_count": max(declared) if declared else None,
                "visible_row_count": len(tickers) + len(review_rows),
                "tickers": tickers,
                "low_confidence_tickers": [row["raw_text"] for row in review_rows],
                "review_rows": review_rows,
            }
        )
        if label:
            source_type = "BRANDENS_WATCHLIST" if label == "BRANDENS WATCHLIST" else "STANDARD_MARKETSURGE"
            for ticker in tickers:
                record = records.setdefault(ticker, {"ticker": ticker, "chart_required": True, "sources": []})
                source = {"source_type": source_type, "label": label, "pdf_page": page_number}
                if source not in record["sources"]:
                    record["sources"].append(source)

    return {
        "schema_version": "marketsurge_ocr_v2",
        "status": "NEEDS_REVIEW" if needs_review else "COMPLETE",
        "session_date": session_date,
        "marketsurge_pdf_sha256": source_sha256,
        "unique_ticker_count": len(records),
        "records": [records[ticker] for ticker in sorted(records)],
        "pages": page_results,
        "warnings": warnings,
    }


def parse_ocr_pages(
    pages: list[dict[str, Any]], *, session_date: str, source_sha256: str, minimum_confidence: float = 72.0
) -> dict[str, Any]:
    return _simple_line_manifest(
        pages, session_date=session_date, source_sha256=source_sha256, minimum_confidence=minimum_confidence
    )


def _words_from_tsv(raw: str) -> list[OcrWord]:
    words: list[OcrWord] = []
    for row in csv.DictReader(raw.splitlines(), delimiter="\t"):
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(row.get("conf") or -1)
            left = int(row.get("left") or 0)
            top = int(row.get("top") or 0)
            width = int(row.get("width") or 0)
            height = int(row.get("height") or 0)
        except ValueError:
            continue
        words.append(OcrWord(text, confidence, left, top, width, height))
    return words


def _title_text(words: list[OcrWord], image_width: int, image_height: int) -> str:
    candidates = [
        word
        for word in words
        if word.left >= image_width * 0.20 and image_height * 0.12 <= word.top <= image_height * 0.19
    ]
    candidates.sort(key=lambda word: (round(word.center_y / 18), word.left))
    return " ".join(word.text for word in candidates)


def _word_box(word: OcrWord) -> dict[str, Any]:
    return {
        "text": word.text,
        "confidence": round(word.confidence, 2),
        "left": word.left,
        "top": word.top,
        "width": word.width,
        "height": word.height,
    }


def _candidate_ticker(raw: str) -> str:
    return raw.strip().strip(",;:").upper().strip(".-")


def _parse_table_page(
    page: dict[str, Any], *, minimum_confidence: float, evidence_dir: Path | None
) -> dict[str, Any]:
    page_number = int(page["page"])
    image_width = int(page["image_width"])
    image_height = int(page["image_height"])
    words = [word if isinstance(word, OcrWord) else OcrWord(**word) for word in page.get("words", [])]
    title_text = _title_text(words, image_width, image_height)
    label = _section_label(title_text, page_number)
    declared = [int(value) for value in COUNT_RE.findall(title_text)]
    result: dict[str, Any] = {
        "pdf_page": page_number,
        "label": label,
        "declared_count": max(declared) if declared else None,
        "screen_result_count": max(declared) if declared else None,
        "visible_row_count": 0,
        "tickers": [],
        "low_confidence_tickers": [],
        "review_rows": [],
        "accepted_rows": [],
        "duplicate_rows": [],
        "clipped_rows": [],
        "evidence": {"title_text": title_text, "image_width": image_width, "image_height": image_height},
        "blocking_codes": [],
    }
    if label is None:
        result["blocking_codes"].append("UNRECOGNIZED_SECTION")

    symbol_headers = [
        word
        for word in words
        if word.text.strip().lower() in {"symbol", "ticker"}
        and word.confidence >= 50
        and word.left >= image_width * 0.20
        and image_height * 0.12 <= word.top <= image_height * 0.30
    ]
    if not symbol_headers:
        result["blocking_codes"].append("SYMBOL_COLUMN_NOT_FOUND")
        return result
    symbol_header = max(symbol_headers, key=lambda word: word.confidence)
    name_headers = [
        word
        for word in words
        if word.text.strip().lower() == "name"
        and word.left > symbol_header.right
        and abs(word.center_y - symbol_header.center_y) <= max(40, symbol_header.height)
    ]
    name_header = min(name_headers, key=lambda word: word.left) if name_headers else None
    symbol_right = name_header.left - 15 if name_header else symbol_header.right + max(220, symbol_header.width * 3)
    body_top = symbol_header.bottom + 55
    rank_left = symbol_header.left - 300
    rank_right = symbol_header.left - 30
    rank_words = [
        word
        for word in words
        if body_top <= word.top < image_height - 80
        and rank_left <= word.left <= rank_right
        and word.confidence >= 50
        and word.text.isdigit()
        and 1 <= int(word.text) <= 999
    ]
    rank_words.sort(key=lambda word: (word.top, word.left))
    result["evidence"].update(
        {
            "symbol_header": _word_box(symbol_header),
            "name_header": _word_box(name_header) if name_header else None,
            "content_bounds": {
                "left": rank_left,
                "top": body_top,
                "right": symbol_right,
                "bottom": image_height - 80,
            },
        }
    )

    symbol_words = [
        word
        for word in words
        if body_top <= word.top < image_height - 80
        and symbol_header.left - 55 <= word.left < symbol_right
    ]
    row_seeds = sorted(rank_words + symbol_words, key=lambda word: word.center_y)
    row_clusters: list[list[OcrWord]] = []
    for word in row_seeds:
        if not row_clusters or abs(word.center_y - median(item.center_y for item in row_clusters[-1])) > 30:
            row_clusters.append([word])
        else:
            row_clusters[-1].append(word)
    centers = [median(word.center_y for word in cluster) for cluster in row_clusters]
    spacings = [later - earlier for earlier, later in zip(centers, centers[1:]) if 55 <= later - earlier <= 150]
    row_spacing = median(spacings) if spacings else 98.0
    detected_ranks = [
        (median(word.center_y for word in cluster), int(rank.text))
        for cluster in row_clusters
        for rank in cluster
        if rank in rank_words
    ]

    for ordinal, cluster in enumerate(row_clusters, start=1):
        row_center = median(word.center_y for word in cluster)
        detected = next((word for word in cluster if word in rank_words), None)
        if detected:
            rank = int(detected.text)
            rank_source = "detected"
            rank_word = detected
        elif detected_ranks:
            anchor_y, anchor_rank = min(detected_ranks, key=lambda item: abs(item[0] - row_center))
            rank = anchor_rank + round((row_center - anchor_y) / row_spacing)
            rank_source = "inferred_from_row_grid"
            rank_word = None
        else:
            rank = ordinal
            rank_source = "inferred_visible_ordinal"
            rank_word = None
        same_row = [word for word in cluster if word in symbol_words]
        same_row.sort(key=lambda word: (abs(word.left - symbol_header.left), -word.confidence))
        candidate = same_row[0] if same_row else None
        row_has_data = any(
            word.left >= symbol_right
            and abs(word.center_y - row_center) <= 28
            and word.top >= body_top
            for word in words
        )
        if candidate is None and not row_has_data:
            continue
        row_evidence = {
            "rank": rank,
            "rank_source": rank_source,
            "rank_box": _word_box(rank_word) if rank_word else None,
            "raw_text": candidate.text if candidate else None,
            "confidence": round(candidate.confidence, 2) if candidate else None,
            "symbol_box": _word_box(candidate) if candidate else None,
        }
        if candidate is None:
            result["review_rows"].append({**row_evidence, "reason": "SYMBOL_CELL_NOT_DETECTED"})
            continue
        ticker = _candidate_ticker(candidate.text)
        row_evidence["candidate_ticker"] = ticker
        raw_has_lowercase = any(character.islower() for character in candidate.text)
        if (
            rank_source != "detected"
            and row_center > image_height * 0.88
            and (raw_has_lowercase or not TOKEN_RE.fullmatch(ticker))
        ):
            result["clipped_rows"].append(
                {**row_evidence, "reason": "CLIPPED_PARTIAL_ROW_EXCLUDED"}
            )
            continue
        if not TOKEN_RE.fullmatch(ticker) or ticker in EXCLUDED_TOKENS:
            result["review_rows"].append({**row_evidence, "reason": "SYMBOL_TOKEN_INVALID"})
        elif raw_has_lowercase:
            result["review_rows"].append({**row_evidence, "reason": "SYMBOL_CASE_AMBIGUOUS"})
            result["low_confidence_tickers"].append(ticker)
        elif candidate.confidence < minimum_confidence:
            result["review_rows"].append({**row_evidence, "reason": "LOW_CONFIDENCE_SYMBOL"})
            result["low_confidence_tickers"].append(ticker)
        else:
            result["accepted_rows"].append({**row_evidence, "ticker": ticker})
            if ticker not in result["tickers"]:
                result["tickers"].append(ticker)

    result["visible_row_count"] = len(result["accepted_rows"]) + len(result["review_rows"])
    if result["review_rows"]:
        result["blocking_codes"].append("SYMBOL_ROWS_NEED_REVIEW")
    if not result["tickers"]:
        result["blocking_codes"].append("NO_VERIFIED_TICKERS")

    if evidence_dir is not None and page.get("image_path"):
        evidence_dir.mkdir(parents=True, exist_ok=True)
        output = evidence_dir / f"page-{page_number}-table.png"
        image = Image.open(page["image_path"])
        last_bottom = max((word.bottom for word in rank_words), default=body_top + 200)
        crop_left = max(0, rank_left - 40)
        crop_top = max(0, min((word.top for word in words if word in symbol_headers), default=body_top) - 170)
        crop_right = min(image_width, (name_header.right + 850) if name_header else symbol_right + 850)
        crop_bottom = min(image_height, last_bottom + 45)
        image.crop((crop_left, crop_top, crop_right, crop_bottom)).save(output)
        result["evidence"]["crop_path"] = output.relative_to(evidence_dir.parent).as_posix()
        result["evidence"]["crop_bounds"] = {
            "left": crop_left,
            "top": crop_top,
            "right": crop_right,
            "bottom": crop_bottom,
        }
    result["evidence"]["ocr_text"] = "\n".join(
        [title_text]
        + [
            f"{row['rank']} {row['raw_text'] or '[missing]'} confidence={row['confidence']}"
            for row in result["accepted_rows"] + result["review_rows"]
        ]
    )
    return result


def parse_ocr_word_pages(
    pages: list[dict[str, Any]],
    *,
    session_date: str,
    source_sha256: str,
    minimum_confidence: float = 72.0,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    records: dict[str, dict[str, Any]] = {}
    page_results: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen_ranks: dict[str, set[int]] = {}
    needs_review = False

    for page in pages:
        parsed = _parse_table_page(page, minimum_confidence=minimum_confidence, evidence_dir=evidence_dir)
        label = parsed["label"]
        if label:
            label_ranks = seen_ranks.setdefault(label, set())
            kept_rows = []
            kept_tickers = []
            for row in parsed["accepted_rows"]:
                if row["rank"] in label_ranks:
                    parsed["duplicate_rows"].append({**row, "reason": "DUPLICATE_CONTINUATION_ROW"})
                    continue
                label_ranks.add(row["rank"])
                kept_rows.append(row)
                if row["ticker"] not in kept_tickers:
                    kept_tickers.append(row["ticker"])
            for row in parsed["review_rows"]:
                label_ranks.add(row["rank"])
            parsed["accepted_rows"] = kept_rows
            parsed["tickers"] = kept_tickers

        for code in parsed.pop("blocking_codes"):
            needs_review = True
            warning: dict[str, Any] = {"code": code, "pdf_page": parsed["pdf_page"]}
            if code == "SYMBOL_ROWS_NEED_REVIEW":
                warning["rows"] = parsed["review_rows"]
            warnings.append(warning)
        if parsed["duplicate_rows"]:
            warnings.append(
                {
                    "code": "DUPLICATE_CONTINUATION_ROWS_EXCLUDED",
                    "pdf_page": parsed["pdf_page"],
                    "ranks": [row["rank"] for row in parsed["duplicate_rows"]],
                    "blocking": False,
                }
            )
        if parsed["clipped_rows"]:
            warnings.append(
                {
                    "code": "CLIPPED_PARTIAL_ROWS_EXCLUDED",
                    "pdf_page": parsed["pdf_page"],
                    "rows": parsed["clipped_rows"],
                    "blocking": False,
                }
            )

        page_results.append(parsed)
        if label:
            source_type = "BRANDENS_WATCHLIST" if label == "BRANDENS WATCHLIST" else "STANDARD_MARKETSURGE"
            for ticker in parsed["tickers"]:
                record = records.setdefault(ticker, {"ticker": ticker, "chart_required": True, "sources": []})
                source = {"source_type": source_type, "label": label, "pdf_page": parsed["pdf_page"]}
                if source not in record["sources"]:
                    record["sources"].append(source)

    return {
        "schema_version": "marketsurge_ocr_v2",
        "status": "NEEDS_REVIEW" if needs_review else "COMPLETE",
        "session_date": session_date,
        "marketsurge_pdf_sha256": source_sha256,
        "unique_ticker_count": len(records),
        "records": [records[ticker] for ticker in sorted(records)],
        "pages": page_results,
        "warnings": warnings,
    }


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
            ["tesseract", str(image), "stdout", "--psm", "11", "tsv"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with Image.open(image) as rendered:
            width, height = rendered.size
        pages.append(
            {
                "page": page_number,
                "image_width": width,
                "image_height": height,
                "image_path": image,
                "words": _words_from_tsv(completed.stdout),
            }
        )
    return parse_ocr_word_pages(
        pages,
        session_date=session_date,
        source_sha256=source_sha256,
        evidence_dir=work_dir / "ocr-evidence",
    )
