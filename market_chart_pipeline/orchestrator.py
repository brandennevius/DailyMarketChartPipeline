from __future__ import annotations

import argparse
import json
from pathlib import Path

from .adapters import (
    derive_candidates_from_chart,
    derive_market_breadth,
    enrich_positions_from_charts,
    normalize_portfolio_snapshot,
)
from .audit import audit_packet
from .packet import build_review_packet, freeze_packet
from .policy import load_policy
from .render import audit_rendered_pdf, render_markdown, render_pdf
from .sell_charts import build_sell_sandbox_chart
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
    source_manifest_path: str | None = None,
    audit_profile: str = "standard",
) -> dict:
    if mode != "read-only":
        raise ValueError("Only read-only mode is currently supported")
    resolved_session = session_date or requested_date
    policy = load_policy(policy_path)
    session_dir = output_dir / resolved_session
    raw_portfolio = _optional_json(portfolio_path, [])
    portfolio_risk_details = None
    if isinstance(raw_portfolio, dict) and raw_portfolio.get("metadata"):
        portfolio, portfolio_risk_details = normalize_portfolio_snapshot(raw_portfolio, resolved_session)
    elif isinstance(raw_portfolio, list):
        portfolio = raw_portfolio
    else:
        raise ValueError("Portfolio input must be a normalized list or a daily portfolio snapshot object")

    chart_dir = Path(chart_packet_dir) if chart_packet_dir else None
    chart_payload = {}
    if chart_dir:
        chart_json = chart_dir / f"Market_Chart_Data_{resolved_session}.json"
        if chart_json.exists():
            chart_payload = load_json(chart_json)
    candidates = _optional_json(candidates_path, None)
    if candidates is None and chart_payload:
        candidates = derive_candidates_from_chart(chart_payload, chart_dir)
    candidates = candidates or []
    market_breadth = derive_market_breadth(chart_payload)
    if chart_payload and chart_dir:
        portfolio = enrich_positions_from_charts(portfolio, chart_payload, chart_dir)
    for position in portfolio:
        ticker = str(position.get("ticker") or "UNKNOWN").upper()
        try:
            position["sell_sandbox_asset"] = build_sell_sandbox_chart(
                position,
                policy,
                resolved_session,
                session_dir / "assets" / f"{ticker}_sell_sandbox.png",
            )
            position["sell_sandbox_status"] = "verified"
        except Exception as exc:
            position["sell_sandbox_status"] = "insufficient_evidence"
            position["sell_sandbox_error"] = str(exc)
    source_manifest = _optional_json(source_manifest_path, {})

    packet = build_review_packet(
        requested_date=requested_date,
        session_date=resolved_session,
        policy=policy,
        market_data=_optional_json(market_data_path, {}),
        portfolio=portfolio,
        candidates=candidates,
        shakeouts=_optional_json(shakeouts_path, []),
        chart_packet_dir=chart_dir,
        source_manifest=source_manifest,
        portfolio_risk_details=portfolio_risk_details,
        market_breadth=market_breadth,
        audit_profile=audit_profile,
    )
    evidence = audit_packet(packet)
    packet["validation_evidence"] = evidence
    packet = freeze_packet(packet)
    audit_packet(packet)

    json_path = session_dir / f"{resolved_session}-market-review.json"
    md_path = session_dir / f"{resolved_session}-market-review.md"
    pdf_path = session_dir / f"{resolved_session}-market-review.pdf"
    atomic_write_text(json_path, json.dumps(packet, indent=2, sort_keys=True) + "\n")
    markdown = render_markdown(packet)
    atomic_write_text(md_path, markdown)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(render_pdf(packet, chart_dir, session_dir))
    rendered_evidence = audit_rendered_pdf(pdf_path, packet)
    packet["validation_evidence"] = [*packet.get("validation_evidence", []), *rendered_evidence]
    packet = freeze_packet(packet)
    audit_packet(packet)
    pdf_path.write_bytes(render_pdf(packet, chart_dir, session_dir))
    audit_rendered_pdf(pdf_path, packet)
    atomic_write_text(json_path, json.dumps(packet, indent=2, sort_keys=True) + "\n")
    atomic_write_text(md_path, render_markdown(packet))
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
    parser.add_argument("--source-manifest")
    parser.add_argument("--audit-profile", choices=["standard", "strict-core"], default="standard")
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
        source_manifest_path=args.source_manifest,
        audit_profile=args.audit_profile,
    )
    print(json.dumps({key: value for key, value in result.items() if key != "packet"}, indent=2))


if __name__ == "__main__":
    main()
