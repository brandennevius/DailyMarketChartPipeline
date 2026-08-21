from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd


def render_pattern_chart(
    symbol: str,
    df: pd.DataFrame,
    session_date: str,
    pattern: dict[str, Any],
    path: Path,
) -> None:
    """Render an auditable OHLCV pattern overlay; never infer from pixels."""
    session_end = pd.Timestamp(session_date).normalize()
    frame = df.loc[df.index <= session_end].tail(180).copy()
    if frame.empty or frame.index[-1] != session_end:
        raise ValueError(f"{symbol}: pattern chart lacks the exact-session bar")
    volume_available = bool("Volume" in frame and frame["Volume"].notna().all())
    path.parent.mkdir(parents=True, exist_ok=True)
    figure, axes = mpf.plot(
        frame,
        type="candle",
        volume=volume_available,
        mav=(21, 50),
        style="yahoo",
        title=f"{symbol} - Algorithmic base/pivot evidence through {session_date}",
        figsize=(11.5, 7.2),
        panel_ratios=(5, 1.3) if volume_available else None,
        returnfig=True,
    )
    price_ax = axes[0]
    def xpos(value: Any) -> int:
        target = pd.Timestamp(value).normalize()
        return int(frame.index.get_indexer([target], method="nearest")[0])

    base_start = pattern.get("base_start")
    base_end = pattern.get("base_end")
    if base_start and base_end:
        price_ax.axvspan(xpos(base_start), xpos(base_end), color="#457b9d", alpha=0.10, label="Detected base window")
    handle = pattern.get("handle") or {}
    if handle.get("start") and handle.get("end"):
        price_ax.axvspan(xpos(handle["start"]), xpos(handle["end"]), color="#f4a261", alpha=0.18, label="Detected handle")
        if handle.get("low_price") is not None:
            price_ax.scatter(
                xpos(handle.get("low_date") or handle["end"]),
                float(handle["low_price"]),
                color="#e76f51",
                marker="v",
                s=42,
                zorder=5,
                label="Handle low",
            )
    candidate_pivot = pattern.get("candidate_pivot_price")
    verified_pivot = pattern.get("pivot_price")
    if candidate_pivot is not None:
        label = "Verified algorithmic pivot" if verified_pivot is not None else "Unverified candidate pivot"
        price_ax.axhline(float(candidate_pivot), color="#1d3557" if verified_pivot is not None else "#8d6e63", linewidth=1.3, linestyle="--", label=f"{label} {float(candidate_pivot):.2f}")
    buy_upper = pattern.get("buy_zone_upper_bound")
    if verified_pivot is not None and buy_upper is not None:
        price_ax.axhspan(float(verified_pivot), float(buy_upper), color="#2a9d8f", alpha=0.10, label=f"Buy zone to {float(buy_upper):.2f}")
    for field, color, marker in (("left_side_high", "#264653", "o"), ("base_low", "#e76f51", "v")):
        evidence = pattern.get(field) or {}
        if evidence.get("date") and evidence.get("price") is not None:
            price_ax.scatter(xpos(evidence["date"]), float(evidence["price"]), color=color, marker=marker, s=45, zorder=5, label=field.replace("_", " ").title())
    markers = ((pattern.get("evidence_bars") or {}).get("markers") or {})
    for label, value in markers.items():
        if isinstance(value, dict) and value.get("date") and value.get("price") is not None:
            price_ax.scatter(xpos(value["date"]), float(value["price"]), marker="x", s=45, zorder=5, label=label.replace("_", " ").title())
    breakout_date = pattern.get("breakout_date")
    breakout_evidence = pattern.get("breakout_evidence") or {}
    if breakout_date and breakout_evidence.get("close") is not None:
        price_ax.scatter(xpos(breakout_date), float(breakout_evidence["close"]), color="#2a9d8f", marker="^", s=60, zorder=6, label=f"Breakout {pattern.get('breakout_status')}")
    handles, labels = price_ax.get_legend_handles_labels()
    deduped = dict(zip(labels, handles))
    if deduped:
        price_ax.legend(deduped.values(), deduped.keys(), loc="upper left", fontsize=7, framealpha=0.88)
    figure.text(
        0.01,
        0.01,
        f"{pattern.get('pattern_type') or 'UNKNOWN'} | {pattern.get('pivot_status') or 'UNVERIFIED'} | action evidence only - not proprietary MarketSurge/IBD recognition",
        fontsize=7,
        color="#455a64",
    )
    figure.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(figure)
    if not path.exists() or path.stat().st_size < 5_000:
        raise ValueError(f"{symbol}: pattern overlay chart render failed")
