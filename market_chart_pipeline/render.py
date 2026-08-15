from __future__ import annotations

from io import BytesIO
from typing import Any

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


def render_markdown(packet: dict[str, Any]) -> str:
    lines = [
        f"# Daily Market & Portfolio Review - {packet['session_date']}",
        "",
        "## Executive Decision",
        f"- Market regime: {packet['market_regime'].get('classification')}",
        f"- Policy version: {packet['policy_version']}",
        f"- Packet SHA-256: `{packet['packet_sha256']}`",
        "",
        "## Current Portfolio Implications",
    ]
    sell_results = packet.get("sell_rule_results", [])
    if not sell_results:
        lines.append("- INSUFFICIENT_EVIDENCE: No portfolio positions were supplied.")
    for result in sell_results:
        lines.append(f"- {result['ticker']}: {result['action']} - {result['rationale']}")
        for event in result.get("events", []):
            lines.append(f"  - {event['rule']}: {event['status']} ({event['rationale']})")

    lines.extend(["", "## Candidate Results"])
    candidates = packet.get("candidate_results", [])
    if not any(item.get("classification") in {"BUY_NOW", "EARLY_ENTRY"} for item in candidates):
        lines.append("NO VERIFIED ACTIONABLE CANDIDATES.")
    for item in candidates[:20]:
        lines.append(
            f"- {item['ticker']}: {item['classification']} / {item['action']} "
            f"(score {item['internal_canslim_score']}) - {item['rationale']}"
        )

    lines.extend(["", "## Shakeout / Re-entry State"])
    if not packet.get("shakeout_results"):
        lines.append("- No shakeout records supplied.")
    for item in packet.get("shakeout_results", []):
        lines.append(f"- {item['ticker']}: {item['state']} / {item['action']}")

    lines.extend(["", "## Sources And Data Timestamps"])
    for source in packet.get("sources", []):
        lines.append(f"- {source['label']}: {source['status']} {source.get('sha256') or ''}".rstrip())
    if packet.get("source_timestamps"):
        for key, value in packet["source_timestamps"].items():
            lines.append(f"- {key}: {value}")
    else:
        lines.append("- No external market-data timestamps supplied.")

    lines.extend(["", "## Validation Evidence"])
    for item in packet.get("validation_evidence", []):
        lines.append(f"- {item['gate']}: {item['status']}")
    return "\n".join(lines) + "\n"


def render_pdf(markdown: str) -> bytes:
    buffer = BytesIO()
    styles = getSampleStyleSheet()
    story = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 8))
            continue
        if line.startswith("# "):
            style = styles["Title"]
            line = line[2:]
        elif line.startswith("## "):
            style = styles["Heading2"]
            line = line[3:]
        else:
            style = styles["BodyText"]
        safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        story.append(Paragraph(safe, style))
    document = SimpleDocTemplate(buffer, pagesize=letter, title="Daily Market & Portfolio Review")
    document.build(story)
    return buffer.getvalue()
