from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import ValidationError, build_packet
from .manifest import load_manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", help="Verified current-session manifest JSON")
    source.add_argument("--tickers", help="Comma-separated tickers for manual testing only")
    parser.add_argument("--session-date")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--feed", choices=["iex", "sip"])
    args = parser.parse_args()

    provenance = None
    if args.manifest:
        request = load_manifest(args.manifest)
        session_date = request.session_date
        feed = args.feed or request.feed
        tickers = request.tickers
        provenance = {ticker: record["sources"] for ticker, record in request.records_by_ticker.items()}
    else:
        if not args.session_date:
            raise ValidationError("--session-date is required with --tickers")
        session_date = args.session_date
        feed = args.feed or "iex"
        tickers = [x.strip().upper() for x in args.tickers.split(",") if x.strip()]

    output_dir = Path(args.output_dir) / session_date
    result = build_packet(tickers, session_date, output_dir, feed, provenance)
    if args.manifest:
        result["source_manifest"] = {
            "path": str(args.manifest),
            "records": request.records_by_ticker,
        }
        for record in result["records"]:
            ticker = record["metrics"]["ticker"]
            record["chart_required"] = request.records_by_ticker[ticker]["chart_required"]
        json_path = Path(result["artifacts"]["json"])
        json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    summary_path = output_dir / "run_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "session_date": session_date,
                "status": result["status"],
                "verified_count": result["verified_count"],
                "error_count": result["error_count"],
                "enrichment_error_count": len(result.get("enrichment_errors", {})),
                "pdf_sha256": result["artifacts"]["pdf_sha256"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"session_date={session_date} status={result['status']} "
        f"verified={result['verified_count']} errors={result['error_count']} "
        f"enrichment_errors={len(result.get('enrichment_errors', {}))}"
    )


if __name__ == "__main__":
    main()
