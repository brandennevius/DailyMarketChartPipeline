import json
from pathlib import Path

from market_chart_pipeline.marketsurge_ocr import parse_ocr_pages, parse_ocr_word_pages


def line(text, confidence=95):
    return {"text": text, "confidence": confidence}


def test_high_confidence_page_builds_manifest():
    result = parse_ocr_pages(
        [
            {
                "page": 1,
                "lines": [
                    line("Near Pivot 2 stocks"),
                    line("1 NVDA NVIDIA Corp"),
                    line("2 MSFT Microsoft Corp"),
                ],
            }
        ],
        session_date="2026-08-14",
        source_sha256="a" * 64,
    )
    assert result["status"] == "COMPLETE"
    assert [record["ticker"] for record in result["records"]] == ["MSFT", "NVDA"]
    assert result["records"][0]["sources"][0]["pdf_page"] == 1


def test_screen_result_count_is_informational_not_visible_row_equality():
    result = parse_ocr_pages(
        [{"page": 1, "lines": [line("Recent Breakouts 3 items"), line("1 NVDA NVIDIA")]}],
        session_date="2026-08-14",
        source_sha256="b" * 64,
    )
    assert result["status"] == "COMPLETE"
    assert result["pages"][0]["screen_result_count"] == 3
    assert result["pages"][0]["visible_row_count"] == 1


def test_low_confidence_ticker_is_not_silently_admitted():
    result = parse_ocr_pages(
        [{"page": 1, "lines": [line("Tight Areas"), line("1 LLY Lilly", 55)]}],
        session_date="2026-08-14",
        source_sha256="c" * 64,
    )
    assert result["status"] == "NEEDS_REVIEW"
    assert result["records"] == []
    assert any(item["code"] == "SYMBOL_ROWS_NEED_REVIEW" for item in result["warnings"])


def word(text, confidence, left, top, width=70, height=28):
    return {
        "text": text,
        "confidence": confidence,
        "left": left,
        "top": top,
        "width": width,
        "height": height,
    }


def test_geometry_uses_rank_and_symbol_column_and_excludes_navigation():
    result = parse_ocr_word_pages(
        [{
            "page": 1,
            "image_width": 1800,
            "image_height": 1200,
            "words": [
                word("Recent", 99, 450, 160), word("Breakouts", 99, 525, 160), word("33", 99, 700, 160), word("items", 99, 745, 160),
                word("FAVORITES", 99, 25, 220), word("Symbol", 99, 440, 260), word("Name", 99, 690, 260),
                word("1", 99, 300, 360, 20), word("FET", 96, 440, 360), word("Forum", 96, 690, 360),
                word("2", 99, 300, 455, 20), word("P", 98, 440, 455, 25), word("Pandora", 96, 690, 455),
            ],
        }],
        session_date="2026-08-14",
        source_sha256="d" * 64,
    )
    assert result["status"] == "COMPLETE"
    assert result["pages"][0]["screen_result_count"] == 33
    assert [record["ticker"] for record in result["records"]] == ["FET", "P"]
    assert "FAVORITES" not in str(result["records"])


def test_continuation_pages_dedupe_ranks_and_keep_distinct_rows():
    base = [word("Power", 99, 450, 160), word("from", 99, 525, 160), word("Pivot", 99, 600, 160), word("58", 99, 690, 160), word("items", 99, 735, 160), word("Symbol", 99, 440, 260), word("Name", 99, 690, 260)]
    result = parse_ocr_word_pages(
        [
            {"page": 1, "image_width": 1800, "image_height": 1200, "words": base + [word("1", 99, 300, 360, 20), word("NVDA", 99, 440, 360), word("Nvidia", 99, 690, 360)]},
            {"page": 2, "image_width": 1800, "image_height": 1200, "words": base + [word("1", 99, 300, 360, 20), word("NVDA", 99, 440, 360), word("Nvidia", 99, 690, 360), word("2", 99, 300, 455, 20), word("MSFT", 99, 440, 455), word("Microsoft", 99, 690, 455)]},
        ],
        session_date="2026-08-14",
        source_sha256="e" * 64,
    )
    assert [record["ticker"] for record in result["records"]] == ["MSFT", "NVDA"]
    assert any(warning["code"] == "DUPLICATE_CONTINUATION_ROWS_EXCLUDED" for warning in result["warnings"])


def test_sanitized_exact_pdf_word_boxes_preserve_geometry_and_confidence_evidence():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "marketsurge_exact_pdf_word_boxes.json").read_text()
    )
    result = parse_ocr_word_pages(
        fixture["pages"], session_date="2026-08-14", source_sha256="f" * 64
    )
    recent = result["pages"][0]
    assert recent["label"] == "Recent Breakouts"
    assert recent["screen_result_count"] == 33
    assert recent["tickers"] == ["LNVGY", "NESR"]
    assert [row["reason"] for row in recent["review_rows"]] == [
        "SYMBOL_CASE_AMBIGUOUS",
        "SYMBOL_TOKEN_INVALID",
    ]
    assert recent["review_rows"][0]["confidence"] == 46.35
    assert "FAVORITES" not in str(result["records"])
    continuation = result["pages"][1]
    assert continuation["label"] == "Top Rated Stocks"
    assert continuation["screen_result_count"] == 48
    assert continuation["tickers"] == ["ATLC", "LTH"]
