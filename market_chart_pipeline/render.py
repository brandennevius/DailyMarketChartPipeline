from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .core import ValidationError
from .utils import sha256_file


INK = colors.HexColor("#17202A")
MUTED = colors.HexColor("#5D6D7E")
NAVY = colors.HexColor("#17324D")
BLUE = colors.HexColor("#2E6F9E")
GREEN = colors.HexColor("#1E7A4D")
GREEN_BG = colors.HexColor("#E9F5EE")
RED = colors.HexColor("#B33A3A")
RED_BG = colors.HexColor("#FBECEC")
AMBER = colors.HexColor("#A86300")
AMBER_BG = colors.HexColor("#FFF4DB")
LINE = colors.HexColor("#D7DEE5")
PANEL = colors.HexColor("#F4F7F9")
WHITE = colors.white


def _money(value: Any, decimals: int = 0) -> str:
    if value is None:
        return "-"
    return f"${float(value):,.{decimals}f}"


def _pct(value: Any, decimals: int = 1) -> str:
    if value is None:
        return "-"
    return f"{float(value):+,.{decimals}f}%"


def _number(value: Any, decimals: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.{decimals}f}"


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("ReviewTitle", parent=base["Title"], fontName="Helvetica-Bold", fontSize=22, leading=26, textColor=NAVY, alignment=TA_LEFT, spaceAfter=3),
        "subtitle": ParagraphStyle("ReviewSubtitle", parent=base["BodyText"], fontName="Helvetica", fontSize=9.5, leading=13, textColor=MUTED, spaceAfter=12),
        "h1": ParagraphStyle("ReviewH1", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=15, leading=18, textColor=NAVY, spaceBefore=10, spaceAfter=7),
        "h2": ParagraphStyle("ReviewH2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=12, leading=15, textColor=INK, spaceBefore=7, spaceAfter=5),
        "body": ParagraphStyle("ReviewBody", parent=base["BodyText"], fontName="Helvetica", fontSize=8.8, leading=12.5, textColor=INK, spaceAfter=5),
        "small": ParagraphStyle("ReviewSmall", parent=base["BodyText"], fontName="Helvetica", fontSize=7.4, leading=10, textColor=MUTED),
        "candidate": ParagraphStyle("ReviewCandidate", parent=base["BodyText"], fontName="Helvetica", fontSize=6.6, leading=8.1, textColor=INK),
        "table": ParagraphStyle("ReviewTable", parent=base["BodyText"], fontName="Helvetica", fontSize=7.2, leading=9, textColor=INK),
        "table_right": ParagraphStyle("ReviewTableRight", parent=base["BodyText"], fontName="Helvetica", fontSize=7.2, leading=9, textColor=INK, alignment=TA_RIGHT),
        "table_head": ParagraphStyle("ReviewTableHead", parent=base["BodyText"], fontName="Helvetica-Bold", fontSize=7, leading=8.5, textColor=WHITE),
        "badge": ParagraphStyle("ReviewBadge", parent=base["BodyText"], fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=INK, alignment=TA_CENTER),
    }


def _p(text: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(str(text)), style)


def _linked_p(prefix: str, title: str, url: str, suffix: str, style: ParagraphStyle) -> Paragraph:
    safe_url = escape(str(url), {'"': "&quot;"})
    return Paragraph(
        f"{escape(prefix)}<link href=\"{safe_url}\" color=\"#2E6F9E\">{escape(title)}</link>{escape(suffix)}",
        style,
    )


def _event(result: dict[str, Any], rule: str) -> dict[str, Any]:
    return next((item for item in result.get("events", []) if item.get("rule") == rule), {})


def _headline(packet: dict[str, Any]) -> str:
    results = packet.get("sell_rule_results", [])
    exits = [item["ticker"] for item in results if item.get("action") == "EXIT"]
    repairs = [item["ticker"] for item in results if item.get("action") == "REPAIR"]
    reduces = [item["ticker"] for item in results if item.get("action") == "REDUCE"]
    if exits:
        return f"Capital-protection exits: {', '.join(exits)}."
    if reduces:
        return f"Risk reduction required: {', '.join(reduces)}."
    if repairs:
        return f"Protect and reassess {', '.join(repairs)}; no hard stop is breached."
    return "No portfolio position requires a deterministic sell action."


def _top_setups(packet: dict[str, Any]) -> list[dict[str, Any]]:
    return list(packet.get("top_canslim_setups") or [])


def _source_provenance(item: dict[str, Any], limit: int | None = None) -> str:
    sources = list((item.get("snapshot") or {}).get("source_evidence") or [])
    labels = []
    for source in sources:
        label = str(source.get("label") or source.get("source_type") or "MarketSurge")
        details = []
        if source.get("pdf_page") is not None:
            details.append(f"p.{source['pdf_page']}")
        if source.get("rank") is not None:
            details.append(f"rank {source['rank']}")
        labels.append(f"{label} ({', '.join(details)})" if details else label)
    if limit is not None and len(labels) > limit:
        return "; ".join(labels[:limit]) + f" (+{len(labels) - limit} more sources)"
    return "; ".join(labels) or "MarketSurge provenance unavailable"


def _candidate_context(item: dict[str, Any]) -> str:
    snap = item.get("snapshot") or {}
    earnings = snap.get("earnings_date") or snap.get("earnings_status") or "unavailable"
    return (
        f"RS {snap.get('rs_trend') or 'unavailable'}"
        f" ({_pct(snap.get('rs_change_21d_pct'))}); rel vol {_number(snap.get('relative_volume'))}x; "
        f"quarter EPS {_pct(snap.get('quarterly_eps_growth_pct'))}; sales {_pct(snap.get('quarterly_sales_growth_pct'))}; "
        f"earnings {earnings}"
    )


def _pivot_gap_text(item: dict[str, Any], limit: int | None = None) -> str:
    missing = list(item.get("snapshot", {}).get("pivot_missing_evidence") or [])
    labels = {
        "current_session_chart_history": "current-session chart history",
        "prior_uptrend": "prior uptrend",
        "conventional_base_type": "base type",
        "base_stage": "base stage",
        "handle_quality_where_applicable": "handle quality",
        "weekly_structure": "weekly structure",
        "volume_contraction": "volume contraction",
        "exact_pivot_price": "exact pivot",
        "breakout_volume_confirmation": "breakout volume",
    }
    missing = [labels.get(value, str(value).replace("_", " ")) for value in missing]
    if limit is not None and len(missing) > limit:
        return ", ".join(missing[:limit]) + f" (+{len(missing) - limit} more)"
    return ", ".join(missing) or "none"


def _markdown_link(title: str, url: str) -> str:
    return f"[{title.replace('[', '(').replace(']', ')')}]({url.replace(')', '%29')})"


def render_markdown(packet: dict[str, Any]) -> str:
    risk = packet.get("portfolio_risk", {})
    breadth = packet.get("market_breadth", {})
    regime = packet.get("market_regime", {})
    cross_market = packet.get("cross_market_context", {})
    synthesis = cross_market.get("llm_synthesis") or {}
    lines = [
        f"# Daily Market & Portfolio Review - {packet['session_date']}",
        "",
        "## Decision Summary",
        f"- {_headline(packet)}",
        "- BUY NOW and EARLY ENTRY require fully verified setup, volume, leadership, earnings-risk, and market-permission gates; algorithmic resistance alone is never actionable.",
        f"- Account value: {_money(risk.get('account_value'))}; gross exposure: {_pct(risk.get('gross_exposure_pct'))}; open P&L: {_money(risk.get('total_open_pnl'))}.",
        f"- Remaining risk to stops: {_money(risk.get('total_remaining_risk_to_stops'))} ({_pct(risk.get('total_remaining_risk_pct'))} of equity).",
        "",
        "## Market And Leadership Breadth",
        f"- Dashboard Gauge posture: {regime.get('dashboard_market_gauge_posture') or 'unavailable'} (score {_number(regime.get('dashboard_market_gauge_score'))}; generated {regime.get('dashboard_market_gauge_generated_at') or 'unavailable'}).",
        "- Dashboard Gauge scope: exact-session trend and extension context. It is not an O'Neil market-regime classification.",
        f"- Dashboard Gauge providers: {', '.join(regime.get('dashboard_market_gauge_providers') or []) or 'unavailable'}.",
        f"- Historical price evidence: {breadth.get('price_history_provider') or 'unavailable'} via {breadth.get('price_history_endpoint') or 'unavailable'}; live/current quote substitution: {'prohibited' if breadth.get('live_quote_substitution') is False else 'not verified'}.",
        f"- Review universe: {breadth.get('verified_symbols', 0)} verified symbols; above 21d {breadth.get('above_21d_pct', '-')}%; above 50d {breadth.get('above_50d_pct', '-')}%; above 200d {breadth.get('above_200d_pct', '-')}%.",
    ]
    for index in regime.get("dashboard_market_gauge_indexes") or []:
        lines.append(
            f"- {index.get('symbol')}: close {_number(index.get('close'))}; "
            f"21EMA {_number(index.get('ema21'))} ({_pct(index.get('distance_above_21d_pct'))}); "
            f"50SMA {_number(index.get('sma50'))} ({_pct(index.get('distance_above_50d_pct'))}); "
            f"200SMA {_number(index.get('sma200'))}; trends {index.get('short_term_trend')}/{index.get('medium_term_trend')}/{index.get('long_term_trend')}; "
            f"extension {index.get('extension')}; as of {index.get('price_session')} (source generated {index.get('source_generated_at') or 'unavailable'})."
        )
    for component in regime.get("dashboard_market_gauge_components") or []:
        lines.append(
            f"- Gauge component {component.get('label') or 'unnamed'}: {component.get('state') or 'unavailable'}; "
            f"{component.get('detail') or 'detail unavailable'}."
        )
    lines.extend(
        [
            f"- O'Neil regime evidence: {regime.get('classification')}; follow-through-day and distribution-day inputs are unavailable.",
            f"- Exposure guidance: {packet.get('exposure_guidance', {}).get('statement') or 'Exact exposure is indeterminate because market-permission evidence is incomplete.'}",
            "",
            "## Cross-Market Context",
            f"- Status: {cross_market.get('status') or 'INSUFFICIENT_EVIDENCE'}; provider FMP; window {(cross_market.get('lookback_window') or {}).get('start_date') or '-'} through {(cross_market.get('lookback_window') or {}).get('end_date') or '-'} (America/New_York calendar dates).",
            "- Interpretation only: this section cannot override regime, exposure, portfolio, candidate, or sell-rule actions.",
        ]
    )
    if synthesis.get("status") == "AVAILABLE":
        lines.extend(
            [
                "",
                "### LLM Synthesis of Frozen Sources",
                f"_OpenAI {synthesis.get('model')}; prompt {synthesis.get('prompt_version')}; generated {synthesis.get('generated_at')}. No web or tool access._",
            ]
        )
        llm_output = synthesis.get("validated_output") or {}
        for paragraph in llm_output.get("summary_paragraphs") or []:
            citations = ", ".join(paragraph.get("citation_ids") or [])
            lines.append(f"{paragraph.get('text')} **[{citations}]**")
        if llm_output.get("key_themes"):
            lines.append("")
            lines.append("Key themes:")
            for theme in llm_output["key_themes"]:
                citations = ", ".join(theme.get("citation_ids") or [])
                lines.append(f"- {theme.get('theme')} **[{citations}]**")
        for note in llm_output.get("uncertainty_notes") or []:
            citations = ", ".join(note.get("citation_ids") or [])
            lines.append(f"- Uncertainty: {note.get('note')} **[{citations}]**")
    else:
        lines.extend(
            [
                "",
                "### LLM Synthesis of Frozen Sources",
                f"- **LLM synthesis unavailable / INSUFFICIENT_EVIDENCE.** {synthesis.get('reason') or 'No validated frozen synthesis was supplied.'}",
            ]
        )
    lines.extend(["", "### Frozen Evidence and Provenance"])
    for article in cross_market.get("cited_context") or []:
        themes = ", ".join(article.get("themes") or []) or "unclassified"
        lines.append(
            f"- [{str(article.get('category') or '').upper()}] {_markdown_link(str(article.get('title') or 'Untitled'), str(article.get('url') or ''))} "
            f"— {article.get('publisher') or 'publisher unavailable'}, {article.get('published_at') or 'time unavailable'}; themes: {themes}."
        )
    treasury = cross_market.get("treasury_context") or {}
    if treasury:
        rates = treasury.get("maturities_pct") or {}
        lines.append(f"- Treasury context {treasury.get('date')}: 2Y {_number(rates.get('year2'))}%; 10Y {_number(rates.get('year10'))}%; 30Y {_number(rates.get('year30'))}%.")
    for event in (cross_market.get("economic_calendar") or [])[:5]:
        lines.append(
            f"- Economic calendar: {event.get('date')} {event.get('country') or '-'} {event.get('event')}; "
            f"actual {event.get('actual') if event.get('actual') is not None else '-'}, estimate {event.get('estimate') if event.get('estimate') is not None else '-'}, impact {event.get('impact') or '-'}."
        )
    if cross_market.get("evidence_gaps"):
        lines.append(f"- Insufficient evidence: {'; '.join(cross_market['evidence_gaps'])}.")
    lines.extend(["", "## Portfolio Actions"])
    for result in packet.get("sell_rule_results", []):
        snap = result.get("position_snapshot", {})
        lines.append(
            f"- **{result['ticker']} - {result['action']}**: return {_pct(result.get('gain_pct'))}, "
            f"open R {_number(snap.get('open_r_multiple'))}, stop {_money(snap.get('stop_price'), 2)}, "
            f"target {_money(snap.get('take_profit'), 2)}. {result['rationale']}"
        )
    top_setups = _top_setups(packet)
    universe = packet.get("candidate_universe_audit") or {}
    lines.extend(
        [
            "",
            f"## Top 10 CANSLIM Setups ({len(top_setups)})",
            "- Global ranking across every distinct valid equity ticker in the frozen MarketSurge PDF. Current open positions and non-equities are excluded; list labels do not confer priority.",
            "- Scores use only frozen C/A, RS/group proxy, technical setup, supply/demand, and new/proximity evidence. The frozen market posture gates actions but does not add score points.",
        ]
    )
    if len(top_setups) < 10:
        lines.append(
            f"- Fewer than 10 are shown because only {universe.get('adequately_evidenced_count', 0)} distinct non-portfolio equities met the minimum evidence and eligibility gates."
        )
    for item in top_setups:
        snap = item.get("snapshot") or {}
        resistance_status = (
            f"verified pivot {_money(snap.get('exact_pivot_price'), 2)}"
            if snap.get("pivot_verification_status") == "verified"
            else f"algorithmic resistance {_money(snap.get('candidate_resistance'), 2)} (not a verified pivot)"
        )
        lines.extend(
            [
                "",
                f"### #{item['rank']} {item['ticker']} — {item.get('action')} | Score {_number(item.get('internal_canslim_score'))} | {item.get('confidence')} confidence",
                f"- **Origin:** {_source_provenance(item)}.",
                f"- **Setup:** {snap.get('setup_pattern_state') or 'INSUFFICIENT EVIDENCE'}; {resistance_status}; distance {_pct(snap.get('candidate_resistance_distance_pct'))}.",
                f"- **Context:** {_candidate_context(item)}.",
                f"- **Why ranked:** {item.get('why_ranked')}",
                f"- **Missing evidence:** {', '.join(item.get('missing_evidence') or []) or 'none'}.",
                f"- **Action:** {item.get('action')}. {item.get('rationale')}",
                f"- **Trigger:** {item.get('trigger')}",
                f"- **Risk / invalidates:** {item.get('risk_invalidates')}",
            ]
        )
    lines.extend(
        [
            "",
            "## Evidence Limits",
            "- A proper O'Neil market regime requires index follow-through and distribution-day evidence, which was not present in this packet.",
            "- Candidate resistance is a visual reference only. It is not treated as a verified pivot or permission to buy.",
            f"- MarketSurge universe audit: {universe.get('distinct_manifest_ticker_count', 0)} distinct tickers; {universe.get('valid_manifest_equity_count', 0)} valid equities; {universe.get('open_position_exclusion_count', 0)} open-position exclusions; {universe.get('non_equity_exclusion_count', 0)} non-equity exclusions; {len(universe.get('rejected') or [])} rejected after evidence/eligibility gates.",
            "- Complete classifications, rejection reasons, provenance, and scoring components remain in canonical JSON under candidate_results and candidate_universe_audit.",
            "- News and LLM synthesis cannot create, remove, score, or reorder candidates.",
            f"- Policy {packet['policy_version']}; packet {packet['packet_sha256']}.",
        ]
    )
    return "\n".join(lines) + "\n"


def _table(data: list[list[Any]], widths: list[float], styles: dict[str, ParagraphStyle], numeric: set[int] | None = None) -> Table:
    numeric = numeric or set()
    cooked = []
    for row_index, row in enumerate(data):
        cooked.append(
            [
                _p(cell, styles["table_head"] if row_index == 0 else styles["table_right"] if col in numeric else styles["table"])
                for col, cell in enumerate(row)
            ]
        )
    table = Table(cooked, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.35, LINE),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, PANEL]),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _action_color(action: str) -> tuple[colors.Color, colors.Color]:
    if action == "EXIT":
        return RED_BG, RED
    if action in {"REPAIR", "REDUCE"}:
        return AMBER_BG, AMBER
    return GREEN_BG, GREEN


def _position_narrative(result: dict[str, Any]) -> str:
    snap = result.get("position_snapshot", {})
    notes = []
    if result.get("action") == "REPAIR":
        notes.append("Do not add while below entry; preserve the working stop and reassess relative strength.")
    elif result.get("action") == "HOLD":
        notes.append("Hold while the working stop remains intact; no deterministic sell trigger fired.")
    elif result.get("action") == "REDUCE":
        notes.append("Reduce exposure according to the triggered trailing or patience rule.")
    elif result.get("action") == "EXIT":
        notes.append("Exit: hard capital protection overrides all other evidence.")
    if snap.get("relative_strength_trend") == "FALLING":
        notes.append("The 21-day relative-strength trend is falling.")
    if snap.get("chart_gate_reasons"):
        notes.append("Chart flag: " + "; ".join(snap["chart_gate_reasons"]) + ".")
    gain_protection = _event(result, "seven_percent_gain_protection")
    if gain_protection.get("status") == "ACTIVE":
        floor = gain_protection.get("values", {}).get("protected_loss_floor")
        notes.append(f"The +7% gain-protection rule is active with a {_money(floor, 2)} loss floor.")
    peak_trail = _event(result, "peak_drawdown_trail")
    if peak_trail.get("status") in {"PASS", "TRIGGERED"}:
        trail = peak_trail.get("values", {}).get("trail_stop_price")
        notes.append(f"The 11% highest-close trail is {'triggered' if peak_trail.get('status') == 'TRIGGERED' else 'at'} {_money(trail, 2)}.")
    zone_hold = _event(result, "profit_zone_minimum_hold")
    if zone_hold:
        notes.append(
            "The pivot profit zone is reached; "
            + ("the eight-week minimum hold remains active." if zone_hold.get("status") == "ACTIVE" else "the minimum hold is satisfied.")
        )
    earnings = snap.get("earnings_date")
    if earnings:
        notes.append(f"Next verified earnings date: {earnings}.")
    return " ".join(notes)


def render_pdf(packet: dict[str, Any], chart_dir: Path | None = None, report_dir: Path | None = None) -> bytes:
    buffer = BytesIO()
    styles = _styles()
    document = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        title="Daily Market & Portfolio Review",
        author="DailyMarketChartPipeline",
        leftMargin=0.55 * inch,
        rightMargin=0.55 * inch,
        topMargin=0.55 * inch,
        bottomMargin=0.55 * inch,
    )
    story: list[Any] = []
    session = packet["session_date"]
    risk = packet.get("portfolio_risk", {})
    breadth = packet.get("market_breadth", {})
    regime = packet.get("market_regime", {})
    cross_market = packet.get("cross_market_context", {})
    synthesis = cross_market.get("llm_synthesis") or {}
    results = packet.get("sell_rule_results", [])

    story.append(_p("Daily Market & Portfolio Review", styles["title"]))
    story.append(_p(f"Post-close decision brief | {session} | Read-only analysis", styles["subtitle"]))
    bg, fg = _action_color("REPAIR" if any(item.get("action") in {"REPAIR", "REDUCE"} for item in results) else "HOLD")
    summary = Table(
        [[_p("TODAY'S PORTFOLIO DECISION", styles["badge"]), _p(_headline(packet), styles["body"]) ]],
        colWidths=[1.65 * inch, 5.15 * inch],
    )
    summary.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("TEXTCOLOR", (0, 0), (0, 0), fg), ("BOX", (0, 0), (-1, -1), 0.8, fg), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story.extend([summary, Spacer(1, 10), _p("Portfolio at a glance", styles["h1"])])
    metrics = [
        ["Account value", "Gross exposure", "Open P&L", "Risk to stops", "Long positions"],
        [_money(risk.get("account_value")), _pct(risk.get("gross_exposure_pct")), _money(risk.get("total_open_pnl")), f"{_money(risk.get('total_remaining_risk_to_stops'))} / {_pct(risk.get('total_remaining_risk_pct'))}", str(risk.get("normalized_long_position_count", len(results)))],
    ]
    story.append(_table(metrics, [1.35 * inch] * 5, styles, {0, 1, 2, 3, 4}))
    story.extend([Spacer(1, 10), _p("Action board", styles["h1"])])
    action_rows = [["Ticker", "Action", "Return", "Open R", "Stop", "Target", "Why now"]]
    for result in results:
        snap = result.get("position_snapshot", {})
        action_rows.append([result["ticker"], result["action"], _pct(result.get("gain_pct")), _number(snap.get("open_r_multiple")), _money(snap.get("stop_price"), 2), _money(snap.get("take_profit"), 2), result.get("rationale", "")])
    story.append(_table(action_rows, [0.55 * inch, 0.65 * inch, 0.58 * inch, 0.48 * inch, 0.65 * inch, 0.7 * inch, 2.95 * inch], styles, {2, 3, 4, 5}))
    story.extend([Spacer(1, 10), _p("Market and leadership evidence", styles["h1"])])
    posture = regime.get("dashboard_market_gauge_posture") or "unavailable"
    story.append(_p(f"Dashboard Gauge posture: {posture} (score {_number(regime.get('dashboard_market_gauge_score'))}; generated {regime.get('dashboard_market_gauge_generated_at') or 'unavailable'}). This is exact-session trend and extension evidence, not an O'Neil regime classification.", styles["body"]))
    story.append(_p(f"Dashboard Gauge providers: {', '.join(regime.get('dashboard_market_gauge_providers') or []) or 'unavailable'}.", styles["small"]))
    gauge_rows = [["Index", "Close", "21EMA / dist.", "50SMA / dist.", "200SMA", "Trend S/M/L", "Extension", "As of"]]
    for index in regime.get("dashboard_market_gauge_indexes") or []:
        gauge_rows.append([
            index.get("symbol") or "-",
            _number(index.get("close")),
            f"{_number(index.get('ema21'))} / {_pct(index.get('distance_above_21d_pct'))}",
            f"{_number(index.get('sma50'))} / {_pct(index.get('distance_above_50d_pct'))}",
            _number(index.get("sma200")),
            f"{index.get('short_term_trend') or '-'}/{index.get('medium_term_trend') or '-'}/{index.get('long_term_trend') or '-'}",
            index.get("extension") or "-",
            index.get("price_session") or "-",
        ])
    if len(gauge_rows) > 1:
        story.append(_table(gauge_rows, [0.48 * inch, 0.62 * inch, 1.0 * inch, 1.0 * inch, 0.72 * inch, 0.85 * inch, 0.72 * inch, 0.72 * inch], styles, {1, 2, 3, 4}))
    component_rows = [["Gauge component", "State", "Frozen detail"]]
    for component in regime.get("dashboard_market_gauge_components") or []:
        component_rows.append([
            component.get("label") or "-",
            component.get("state") or "-",
            component.get("detail") or "detail unavailable",
        ])
    if len(component_rows) > 1:
        story.append(Spacer(1, 4))
        story.append(_table(component_rows, [1.2 * inch, 0.75 * inch, 4.85 * inch], styles))
    story.append(_p("O'Neil regime evidence: INSUFFICIENT_EVIDENCE. Follow-through-day and distribution-day series were not supplied; those missing inputs are not inferred from the Dashboard Gauge.", styles["body"]))
    story.append(_p(f"Historical price evidence: {breadth.get('price_history_provider') or 'unavailable'} via {breadth.get('price_history_endpoint') or 'unavailable'}. Live/current quote substitution is prohibited.", styles["body"]))
    story.append(_p(f"Exposure guidance (separate): {packet.get('exposure_guidance', {}).get('statement') or 'Exact exposure is indeterminate because market-permission evidence is incomplete.'}", styles["body"]))
    breadth_rows = [
        ["Verified", "Above 21d", "Above 50d", "Above 200d", "RS rising", "Positive A/D", "Chart priority"],
        [str(breadth.get("verified_symbols", 0)), f"{breadth.get('above_21d_pct', '-')}%", f"{breadth.get('above_50d_pct', '-')}%", f"{breadth.get('above_200d_pct', '-')}%", f"{breadth.get('rs_rising_pct', '-')}%", f"{breadth.get('positive_accumulation_pct', '-')}%", str(breadth.get("chart_review_priority_count", 0))],
    ]
    story.append(_table(breadth_rows, [0.78 * inch, 0.9 * inch, 0.9 * inch, 0.9 * inch, 0.88 * inch, 0.95 * inch, 0.95 * inch], styles, set(range(7))))
    story.append(Spacer(1, 8))
    story.append(_p("Entry posture: BUY NOW and EARLY ENTRY require verified entry, volume, leadership, earnings-risk, and market-permission gates. Algorithmic resistance alone is never actionable.", styles["body"]))

    story.append(_p("Cross-Market Context", styles["h1"]))
    window = cross_market.get("lookback_window") or {}
    story.append(_p(
        f"Status {cross_market.get('status') or 'INSUFFICIENT_EVIDENCE'} | FMP | {window.get('start_date') or '-'} through {window.get('end_date') or '-'} New York calendar dates. Interpretation only: this context cannot override regime, exposure, portfolio, candidate, or sell-rule actions.",
        styles["body"],
    ))
    story.append(_p("LLM synthesis of frozen sources", styles["h2"]))
    if synthesis.get("status") == "AVAILABLE":
        story.append(_p(
            f"OpenAI {synthesis.get('model')} | prompt {synthesis.get('prompt_version')} | generated {synthesis.get('generated_at')}. This synthesis used no web or tool access.",
            styles["small"],
        ))
        llm_output = synthesis.get("validated_output") or {}
        for paragraph in llm_output.get("summary_paragraphs") or []:
            citations = ", ".join(paragraph.get("citation_ids") or [])
            story.append(_p(f"{paragraph.get('text')} [{citations}]", styles["body"]))
        for theme in llm_output.get("key_themes") or []:
            citations = ", ".join(theme.get("citation_ids") or [])
            story.append(_p(f"• {theme.get('theme')} [{citations}]", styles["small"]))
        for note in llm_output.get("uncertainty_notes") or []:
            citations = ", ".join(note.get("citation_ids") or [])
            story.append(_p(f"Uncertainty: {note.get('note')} [{citations}]", styles["small"]))
    else:
        story.append(_p(
            f"LLM synthesis unavailable / INSUFFICIENT_EVIDENCE. {synthesis.get('reason') or 'No validated frozen synthesis was supplied.'}",
            styles["body"],
        ))
    story.append(_p("Frozen evidence and provenance", styles["h2"]))
    for article in (cross_market.get("cited_context") or [])[:8]:
        suffix = f" - {article.get('publisher') or 'publisher unavailable'}, {article.get('published_at') or 'time unavailable'} [{str(article.get('category') or '').upper()}]"
        _article_title = str(article.get("title") or "Untitled")
        _article_url = str(article.get("url") or "")
        if _article_url:
            story.append(_linked_p("• ", _article_title, _article_url, suffix, styles["small"]))
        else:
            story.append(_p(f"• {_article_title}{suffix}", styles["small"]))
    treasury = cross_market.get("treasury_context") or {}
    if treasury:
        rates = treasury.get("maturities_pct") or {}
        story.append(_p(f"Treasury {treasury.get('date')}: 2Y {_number(rates.get('year2'))}% | 10Y {_number(rates.get('year10'))}% | 30Y {_number(rates.get('year30'))}%.", styles["small"]))
    for event in (cross_market.get("economic_calendar") or [])[:5]:
        story.append(_p(f"Economic calendar: {event.get('date')} {event.get('country') or '-'} {event.get('event')} | actual {event.get('actual') if event.get('actual') is not None else '-'} | estimate {event.get('estimate') if event.get('estimate') is not None else '-'} | impact {event.get('impact') or '-' }.", styles["small"]))
    if cross_market.get("evidence_gaps"):
        story.append(_p(f"INSUFFICIENT_EVIDENCE: {'; '.join(cross_market['evidence_gaps'])}.", styles["small"]))

    story.append(PageBreak())
    story.append(_p("Position Review", styles["title"]))
    story.append(_p("Working stops come from the portfolio snapshot. Rule-engine stops may be tighter when the 8% or 2-ATR policy requires it.", styles["subtitle"]))
    for index, result in enumerate(results):
        snap = result.get("position_snapshot", {})
        bg, fg = _action_color(result.get("action", "HOLD"))
        header = Table(
            [[_p(f"{result['ticker']}  |  {result['action']}", styles["h2"]), _p(f"{_pct(result.get('gain_pct'))}  |  {_number(snap.get('open_r_multiple'))}R", styles["badge"]) ]],
            colWidths=[5.35 * inch, 1.45 * inch],
        )
        header.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("TEXTCOLOR", (1, 0), (1, 0), fg), ("BOX", (0, 0), (-1, -1), 0.6, fg), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7)]))
        story.extend([header, Spacer(1, 5)])
        detail_rows = [
            ["Entry", "Close", "Shares", "Value", "Weight", "Stop", "Risk $", "Target", "Grade"],
            [_money(snap.get("entry_price"), 2), _money(snap.get("current_price"), 2), _number(snap.get("shares"), 0), _money(snap.get("market_value")), _pct(snap.get("position_weight_pct")), _money(snap.get("stop_price"), 2), _money(snap.get("remaining_risk_to_stop_dollars")), _money(snap.get("take_profit"), 2), snap.get("grade") or "-"],
        ]
        story.append(_table(detail_rows, [0.73 * inch, 0.73 * inch, 0.55 * inch, 0.75 * inch, 0.65 * inch, 0.7 * inch, 0.65 * inch, 0.73 * inch, 0.55 * inch], styles, set(range(8))))
        story.extend([Spacer(1, 5), _p(_position_narrative(result), styles["body"])])
        hard = _event(result, "hard_capital_protection")
        if hard:
            values = hard.get("values", {})
            story.append(_p(f"Capital protection: {hard.get('status')} | effective policy stop {_money(values.get('effective_stop'), 2)} | working stop {_money(snap.get('stop_price'), 2)}", styles["small"]))
        sandbox = snap.get("sell_sandbox_asset") or {}
        sandbox_path = report_dir / sandbox.get("file", "") if report_dir and sandbox.get("file") else None
        if sandbox_path and sandbox_path.exists():
            expected_hash = sandbox.get("sha256")
            if not expected_hash or sha256_file(sandbox_path) != expected_hash:
                raise ValidationError(f"{result['ticker']}: report chart asset hash mismatch")
            image = Image(str(sandbox_path), width=6.75 * inch, height=3.75 * inch, kind="proportional")
            story.extend([
                Spacer(1, 4),
                _p(f"VERIFIED SELL-RULE SANDBOX - {result['ticker']}", styles["h2"]),
                image,
                _p(f"{result['ticker']} sell-rule sandbox through {session}. Chart asset is hash-locked in the review packet.", styles["small"]),
            ])
        else:
            error = snap.get("sell_sandbox_error") or "The deterministic sell-rule sandbox could not be produced from the frozen inputs."
            failure = Table(
                [[_p(f"SELL-RULE SANDBOX UNAVAILABLE - {result['ticker']}", styles["h2"])], [_p(error, styles["body"])], [_p("No ordinary daily chart was substituted. Position advice remains insufficient where this evidence is required.", styles["body"]) ]],
                colWidths=[6.8 * inch],
            )
            failure.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), RED_BG), ("BOX", (0, 0), (-1, -1), 0.8, RED), ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
            story.extend([Spacer(1, 6), failure])
        if index < len(results) - 1:
            story.append(PageBreak())

    top_setups = _top_setups(packet)
    universe = packet.get("candidate_universe_audit") or {}
    story.append(PageBreak())
    story.append(_p(f"Top 10 CANSLIM Setups ({len(top_setups)})", styles["title"]))
    story.append(_p(
        "Global deterministic ranking across every distinct valid equity in the frozen MarketSurge PDF. Current open positions and non-equities are excluded; MarketSurge list labels do not confer priority. Market posture gates actions but does not add score points.",
        styles["subtitle"],
    ))
    if len(top_setups) < 10:
        story.append(_p(
            f"Only {len(top_setups)} names are printed because {universe.get('adequately_evidenced_count', 0)} distinct non-portfolio equities met the minimum evidence and eligibility gates.",
            styles["body"],
        ))
    for index, item in enumerate(top_setups):
        if index and index % 5 == 0:
            story.append(PageBreak())
            story.append(Spacer(1, 5))
            story.append(_p("Top 10 CANSLIM Setups (continued)", styles["title"]))
        snap = item.get("snapshot") or {}
        pivot_text = (
            f"VERIFIED pivot {_money(snap.get('exact_pivot_price'), 2)}"
            if snap.get("pivot_verification_status") == "verified"
            else f"candidate resistance {_money(snap.get('candidate_resistance'), 2)}; NOT A VERIFIED PIVOT"
        )
        missing_items = list(item.get("missing_evidence") or [])
        missing = ", ".join(missing_items[:6]) or "none"
        if len(missing_items) > 6:
            missing += f" (+{len(missing_items) - 6} more in canonical JSON)"
        body = Paragraph(
            "<b>Origin:</b> " + escape(_source_provenance(item, 2)) + "<br/>"
            + "<b>Setup/pivot:</b> " + escape(f"{snap.get('setup_pattern_state') or 'INSUFFICIENT EVIDENCE'} | {pivot_text} | distance {_pct(snap.get('candidate_resistance_distance_pct'))}") + "<br/>"
            + "<b>Context:</b> " + escape(_candidate_context(item)) + "<br/>"
            + "<b>Why ranked:</b> " + escape(item.get("why_ranked") or "insufficient evidence") + " <b>Missing:</b> " + escape(missing) + ".<br/>"
            + "<b>Action:</b> " + escape(f"{item.get('action')}. {item.get('rationale')}") + " <b>Trigger:</b> " + escape(item.get("trigger") or "No verified trigger") + "<br/>"
            + "<b>Risk/invalidates:</b> " + escape(item.get("risk_invalidates") or "INSUFFICIENT EVIDENCE"),
            styles["candidate"],
        )
        card_rows = [
            [_p(f"#{item['rank']} {item['ticker']}", styles["h2"]), _p(f"{item.get('action')} | score {_number(item.get('internal_canslim_score'))} | {item.get('confidence')}", styles["badge"])],
            [body, ""],
        ]
        card = Table(card_rows, colWidths=[1.25 * inch, 5.55 * inch], hAlign="LEFT")
        bg, fg = _action_color("REPAIR" if item.get("action") in {"WAIT FOR CONFIRMATION", "AVOID", "INSUFFICIENT EVIDENCE"} else "HOLD")
        card.setStyle(TableStyle([
            ("SPAN", (0, 0), (0, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), bg),
            ("TEXTCOLOR", (1, 0), (1, 0), fg),
            ("SPAN", (0, 1), (1, 1)),
            ("GRID", (0, 0), (-1, -1), 0.35, LINE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        story.append(KeepTogether([card, Spacer(1, 5)]))

    story.append(PageBreak())
    story.append(_p("Evidence, Controls, and Limits", styles["title"]))
    story.append(_p("This section supports auditability without crowding the decision pages.", styles["subtitle"]))
    story.append(_p("What was verified", styles["h1"]))
    verified_count = len(packet.get("chart_verification", {}).get("verified_tickers", []))
    requested_count = len(packet.get("chart_verification", {}).get("requested_tickers", []))
    evidence_lines = [
        f"Portfolio snapshot matched the {session} session and contained current closing prices and broker working stops.",
        f"{verified_count} of {requested_count} requested symbols had current-session daily and weekly chart records.",
        f"{sum(1 for item in results if item.get('position_snapshot', {}).get('sell_sandbox_status') == 'verified')} of {len(results)} open long positions had hash-verified sell-rule sandbox charts.",
        f"MarketSurge audit retained {universe.get('distinct_manifest_ticker_count', 0)} distinct manifest tickers and {universe.get('valid_manifest_equity_count', 0)} valid equities; excluded {universe.get('open_position_exclusion_count', 0)} open positions and {universe.get('non_equity_exclusion_count', 0)} non-equities from new-entry ranking.",
        f"{universe.get('adequately_evidenced_count', 0)} candidates met minimum ranking evidence; {len(top_setups)} were printed. Complete classifications and rejections remain in canonical JSON.",
        "Hard capital-protection rules were evaluated before trailing, patience, profit-zone, and candidate signals.",
        "News and LLM synthesis did not create, score, remove, or reorder candidates.",
        "The canonical packet was audited and hash-frozen before Markdown and PDF rendering.",
    ]
    for line in evidence_lines:
        story.append(_p(f"• {line}", styles["body"]))
    story.append(_p("Known evidence gaps", styles["h1"]))
    gaps = [
        "No index follow-through-day or distribution-day series was supplied, so the O'Neil market regime remains unclassified.",
        "Shakeout/re-entry logic is implemented, but no active shakeout records were supplied for this session.",
    ]
    if any(_event(item, "profit_zone").get("status") == "INSUFFICIENT_EVIDENCE" for item in results):
        gaps.append("One or more portfolio positions lack a verified numeric pivot; profit-zone and rapid-advance rules remain unavailable for those positions.")
    if any(_event(item, "peak_drawdown_trail").get("status") == "INSUFFICIENT_EVIDENCE" for item in results):
        gaps.append("One or more positions lack verified highest-close history, so their gain-protection trail remains unavailable.")
    gaps.append("The chart engine identifies candidate resistance for visual review but deliberately does not promote it to a verified pivot.")
    for line in gaps:
        story.append(_p(f"• {line}", styles["body"]))
    story.append(_p("Audit identity", styles["h1"]))
    identity_rows = [
        ["Policy", packet.get("policy_version")],
        ["Calculation", packet.get("calculation_version")],
        ["Packet SHA-256", packet.get("packet_sha256")],
        ["Audit profile", packet.get("audit_profile")],
        ["Price as of", risk.get("price_as_of") or "-"],
        ["Price source", risk.get("price_source") or "-"],
    ]
    identity = Table([[_p(a, styles["table_head"]), _p(b, styles["table"])] for a, b in identity_rows], colWidths=[1.25 * inch, 5.55 * inch])
    identity.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), NAVY), ("GRID", (0, 0), (-1, -1), 0.35, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story.append(identity)

    def footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.line(doc.leftMargin, 0.38 * inch, letter[0] - doc.rightMargin, 0.38 * inch)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(doc.leftMargin, 0.22 * inch, f"Daily Market & Portfolio Review | {session}")
        canvas.drawRightString(letter[0] - doc.rightMargin, 0.22 * inch, f"Page {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def audit_rendered_pdf(pdf_path: Path, packet: dict[str, Any]) -> list[dict[str, Any]]:
    """Verify position safeguards and the decision-facing Top 10 survived PDF composition."""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    page_text = [(page.extract_text() or "") for page in reader.pages]
    evidence: list[dict[str, Any]] = []
    for result in packet.get("sell_rule_results", []):
        ticker = str(result.get("ticker") or "UNKNOWN").upper()
        matching_pages = [
            index + 1
            for index, text in enumerate(page_text)
            if f"VERIFIED SELL-RULE SANDBOX - {ticker}" in text
            or f"SELL-RULE SANDBOX UNAVAILABLE - {ticker}" in text
        ]
        if len(matching_pages) != 1:
            raise ValidationError(f"{ticker}: rendered PDF must contain exactly one dedicated sell-sandbox status page")
        evidence.append({"gate": "position_sandbox_page", "status": "pass", "ticker": ticker, "pdf_page": matching_pages[0]})
    full_text = "\n".join(page_text)
    forbidden_labels = ["Brandens Watchlist - Complete Results", "Visual Review Queue", "First Charts to Review"]
    present_forbidden = [label for label in forbidden_labels if label in full_text]
    if present_forbidden:
        raise ValidationError(f"Rendered PDF retained superseded candidate sections: {present_forbidden}")
    top_setups = _top_setups(packet)
    for item in top_setups:
        anchor = f"#{item.get('rank')} {item.get('ticker')}"
        matches = [index + 1 for index, text in enumerate(page_text) if anchor in text]
        if len(matches) != 1:
            raise ValidationError(f"Rendered PDF must contain exactly one ranked setup card for {item.get('ticker')}")
    evidence.append({
        "gate": "top_canslim_render",
        "status": "pass",
        "ticker_count": len(top_setups),
        "tickers": [item.get("ticker") for item in top_setups],
    })
    universe = packet.get("candidate_universe_audit") or {}
    if "Complete classifications and rejections remain in canonical JSON" not in full_text:
        raise ValidationError("Rendered PDF omitted compact full-universe audit reference")
    evidence.append({
        "gate": "candidate_universe_appendix",
        "status": "pass",
        "distinct_manifest_ticker_count": universe.get("distinct_manifest_ticker_count", 0),
    })
    return evidence
