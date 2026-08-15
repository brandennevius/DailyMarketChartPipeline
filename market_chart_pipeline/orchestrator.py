from __future__ import annotations

import argparse
import json
from pathlib import Path

from .audit import audit_packet
from .packet import build_review_packet, freeze_packet
from .policy import load_policy
from .render import render_markdown, render_pdf
from .utils import atomic_write_text, load_json


def _optional_json(path: str | None, default):
    return load_json(Path(path)) if path else default


def run_daily_review(
    *,
    requested_date: str,
    session_date: str | None,
    mode: str,
    output_dir: Path,
    policy_path: Path | None = None,
    market_data_path: str | None = None,
    portfolio_path: str | None = None,
    candidates_path: str | None = None,
    shakeouts_path: str | None = None,
    chart_packet_dir: str | None = None,
) -> dict:
    if mode != "read-only":
        raise ValueError("Only read-only mode is currently supported")
    resolved_session = session_date or requested_date
    policy = load_policy(policy_path)
    packet = build_review_packet(
        requested_date=requested_date,
        session_date=resolved_session,
        policy=policy,
        market_data=_optional_json(market_data_path, {}),
        portfolio=_optional_json(portfolio_path, []),
        candidates=_optional_json(candidates_path, []),
        shakeouts=_optional_json(shakeouts_path, []),
        chart_packet_dir=Path(chart_packet_dir) if chart_packet_dir else None,
    )
    evidence = audit_packet(packet)
    packet["validation_evidence"] = evidence
    packet = freeze_packet(packet)
    audit_packet(packet)

    session_dir = output_dir / resolved_session
    json_path = session_dir / f"{resolved_session}-market-review.json"
    md_path = session_dir / f"{resolved_session}-market-review.md"
    pdf_path = session_dir / f"{resolved_session}-market-review.pdf"
    atomic_write_text(json_path, json.dumps(packet, indent=2, sort_keys=True) + "\n")
    markdown = render_markdown(packet)
    atomic_write_text(md_path, markdown)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(render_pdf(markdown))
    return {"packet": packet, "json_path": str(json_path), "markdown_path": str(md_path), "pdf_path": str(pdf_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic daily market and portfolio review")
    parser.add_argument("--date", required=True, dest="requested_date")
    parser.add_argument("--session-date")
    parser.add_argument("--mode", default="read-only", choices=["read-only"])
    parser.add_argument("--output-dir", default="reports/market")
    parser.add_argument("--policy")
    parser.add_argument("--market-data")
    parser.add_argument("--portfolio")
    parser.add_argument("--candidates")
    parser.add_argument("--shakeouts")
    parser.add_argument("--chart-packet-dir")
    args = parser.parse_args()
    result = run_daily_review(
        requested_date=args.requested_date,
        session_date=args.session_date,
        mode=args.mode,
        output_dir=Path(args.output_dir),
        policy_path=Path(args.policy) if args.policy else None,
        market_data_path=args.market_data,
        portfolio_path=args.portfolio,
        candidates_path=args.candidates,
        shakeouts_path=args.shakeouts,
        chart_packet_dir=args.chart_packet_dir,
    )
    print(json.dumps({key: value for key, value in result.items() if key != "packet"}, indent=2))


if __name__ == "__main__":
    main()
