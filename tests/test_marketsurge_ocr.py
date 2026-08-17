from market_chart_pipeline.marketsurge_ocr import parse_ocr_pages


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


def test_count_mismatch_is_needs_review():
    result = parse_ocr_pages(
        [{"page": 1, "lines": [line("Recent Breakouts 3 items"), line("1 NVDA NVIDIA")]}],
        session_date="2026-08-14",
        source_sha256="b" * 64,
    )
    assert result["status"] == "NEEDS_REVIEW"
    assert result["warnings"][0]["code"] == "VISIBLE_ROW_COUNT_MISMATCH"


def test_low_confidence_ticker_is_not_silently_admitted():
    result = parse_ocr_pages(
        [{"page": 1, "lines": [line("Tight Areas"), line("1 LLY Lilly", 55)]}],
        session_date="2026-08-14",
        source_sha256="c" * 64,
    )
    assert result["status"] == "NEEDS_REVIEW"
    assert result["records"] == []
    assert any(item["code"] == "LOW_CONFIDENCE_TICKERS" for item in result["warnings"])
