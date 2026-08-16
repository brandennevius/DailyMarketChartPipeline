from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd

from .core import ValidationError
from .utils import sha256_file


def _history_frame(position: dict[str, Any]) -> pd.DataFrame:
    rows = position.get("price_history") or []
    required = {"date", "open", "high", "low", "close", "volume"}
    if not rows or any(not required.issubset(row) for row in rows):
        raise ValidationError(f"{position.get('ticker')}: complete OHLCV price history is required for the sell sandbox")
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.set_index("date").sort_index()
    frame = frame.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    return frame[["Open", "High", "Low", "Close", "Volume"]].astype(float)


def _entry_index(frame: pd.DataFrame, entry_date: str) -> int:
    eligible = frame.index >= pd.Timestamp(entry_date)
    indexes = [index for index, match in enumerate(eligible) if match]
    if not indexes:
        raise ValidationError("Entry date is outside available chart history")
    return indexes[0]


def build_sell_sandbox_chart(
    position: dict[str, Any],
    policy: dict[str, Any],
    session_date: str,
    output_path: Path,
) -> dict[str, Any]:
    ticker = str(position.get("ticker") or "UNKNOWN").upper()
    entry = position.get("entry_price")
    entry_date = position.get("entry_date")
    if entry is None or not entry_date:
        raise ValidationError(f"{ticker}: entry price and date are required for the sell sandbox")
    entry = float(entry)
    full_frame = _history_frame(position)
    full_entry_index = _entry_index(full_frame, str(entry_date))
    context_start = max(0, full_entry_index - 80)
    frame = full_frame.iloc[context_start:].copy()
    entry_index = full_entry_index - context_start
    current_index = len(frame) - 1
    future_end = max(current_index + 4, entry_index + 65)

    hard = policy["hard_rules"]
    trailing = policy["trailing"]
    zone = policy["profit_zone"]
    patience = policy["patience"]
    pivot = float(position["pivot_price"]) if position.get("pivot_price") is not None else None
    structural_stop = float(position["stop_price"]) if position.get("stop_price") is not None else None
    atr = float(position["atr"]) if position.get("atr") is not None else None
    loss_limit = entry * (1.0 - float(hard["max_initial_loss_pct"]) / 100.0)
    protected_floor = entry * (1.0 - float(trailing["protected_loss_floor_pct"]) / 100.0)
    atr_stop = entry - atr * float(hard["atr_stop_multiple"]) if atr is not None else None
    eight_week_index = entry_index + round(float(zone["minimum_hold_weeks"]) * 5)
    thirteen_week_index = entry_index + round(float(patience["slow_leader_patience_weeks"]) * 5)

    post_entry = frame["Close"].iloc[entry_index:]
    cumulative_high = post_entry.cummax()
    highest_close = float(cumulative_high.iloc[-1])
    activation_price = entry * (1.0 + float(trailing["activation_gain_pct"]) / 100.0)
    activated = cumulative_high >= activation_price
    trail = cumulative_high * (1.0 - float(trailing["peak_drawdown_exit_pct"]) / 100.0)
    trail = trail.where(activated)
    initial_stops = [loss_limit]
    if structural_stop is not None:
        initial_stops.append(structural_stop)
    if atr_stop is not None:
        initial_stops.append(atr_stop)
    initial_stop = max(initial_stops)
    initial_risk = entry - initial_stop
    hard_stops = list(initial_stops)
    if bool(activated.iloc[-1]):
        hard_stops.append(protected_floor)
    if initial_risk > 0 and highest_close >= entry + initial_risk * float(hard["break_even_after_r"]):
        hard_stops.append(entry)
    effective_stop = max(hard_stops)

    market_colors = mpf.make_marketcolors(up="#197A50", down="#C33E45", edge="inherit", wick="inherit", volume="inherit")
    style = mpf.make_mpf_style(base_mpf_style="yahoo", marketcolors=market_colors, gridcolor="#DCE3E8", gridstyle="--", facecolor="#FCFDFE")
    figure, axes = mpf.plot(
        frame,
        type="candle",
        volume=True,
        mav=(21, 50),
        style=style,
        figsize=(13, 7.8),
        panel_ratios=(5, 1.2),
        returnfig=True,
        tight_layout=True,
        xrotation=20,
    )
    price_ax = axes[0]
    price_ax.set_xlim(-2, future_end + 2)

    price_ax.axhspan(loss_limit, protected_floor, xmin=max(0.0, entry_index / (future_end + 2)), xmax=1.0, color="#E45D68", alpha=0.16, label="5%-8% loss zone")
    price_ax.axhline(entry, color="#244F73", linewidth=1.4, linestyle="--", label=f"Entry {entry:.2f}")
    price_ax.axhline(loss_limit, color="#B33A3A", linewidth=1.2, label=f"{hard['max_initial_loss_pct']:.0f}% loss cap {loss_limit:.2f}")
    if structural_stop is not None:
        price_ax.axhline(structural_stop, color="#7A1E48", linewidth=1.7, label=f"Working stop {structural_stop:.2f}")
    if atr_stop is not None:
        price_ax.axhline(atr_stop, color="#6F42A1", linewidth=1.2, linestyle=":", label=f"{hard['atr_stop_multiple']:.0f} ATR initial {atr_stop:.2f}")
    price_ax.axhline(effective_stop, color="#17202A", linewidth=2.2, linestyle="-.", label=f"Effective policy stop {effective_stop:.2f}")

    activation_positions = [entry_index + offset for offset, value in enumerate(activated.tolist()) if value]
    if activation_positions:
        activation_index = activation_positions[0]
        price_ax.hlines(protected_floor, activation_index, future_end, color="#C17A00", linewidth=1.4, linestyle="--", label=f"+7% protection floor {protected_floor:.2f}")
        trail_x = list(range(entry_index, current_index + 1))
        trail_values = trail.tolist()
        price_ax.step(trail_x, trail_values, where="post", color="#218C5B", linewidth=2.0, label=f"{trailing['peak_drawdown_exit_pct']:.0f}% highest-close trail")
        valid_x = [x for x, value in zip(trail_x, trail_values) if pd.notna(value)]
        valid_y = [float(value) for value in trail_values if pd.notna(value)]
        if valid_x:
            upper = max(float(frame["High"].max()), max(valid_y)) * 1.05
            price_ax.fill_between(valid_x, valid_y, upper, step="post", color="#66B982", alpha=0.07)

    if pivot is not None:
        lower_target = pivot * (1.0 + float(zone["lower_pct_from_pivot"]) / 100.0)
        upper_target = pivot * (1.0 + float(zone["upper_pct_from_pivot"]) / 100.0)
        price_ax.axhline(pivot, color="#2E6F9E", linewidth=1.2, linestyle="-.", label=f"Verified pivot {pivot:.2f}")
        price_ax.fill_between([eight_week_index, future_end], lower_target, upper_target, color="#5EBB78", alpha=0.18, label="20%-25% profit zone after 8 weeks")
        price_ax.fill_between([entry_index, min(eight_week_index, future_end)], lower_target, upper_target, color="#E8C75A", alpha=0.13, label="Profit zone / minimum hold")
    else:
        lower_target = None
        upper_target = None

    price_ax.axvline(eight_week_index, color="#3B7C52", linewidth=1.0, linestyle="--")
    price_ax.text(eight_week_index + 0.8, price_ax.get_ylim()[1] * 0.985, "8 weeks", color="#2F6542", fontsize=8, va="top")
    price_ax.axvline(thirteen_week_index, color="#7A6C34", linewidth=1.0, linestyle="--")
    price_ax.text(thirteen_week_index + 0.8, price_ax.get_ylim()[1] * 0.985, "13 weeks", color="#6A5C2B", fontsize=8, va="top")

    rapid_days = position.get("trading_days_to_rapid_advance")
    if rapid_days is not None and int(rapid_days) <= int(policy["rapid_advance"]["max_trading_days"]):
        rapid_start = entry_index + int(rapid_days)
        price_ax.axvspan(rapid_start, eight_week_index, color="#E8C75A", alpha=0.10, label="Rapid-advance hold")

    current = float(frame["Close"].iloc[-1])
    price_ax.scatter([current_index], [current], s=42, color="#17202A", zorder=5)
    price_ax.annotate(f"Close {current:.2f}", (current_index, current), xytext=(6, 8), textcoords="offset points", fontsize=8, color="#17202A")
    price_ax.set_title(f"{ticker} Sell Rule Sandbox - {session_date}", fontsize=14, fontweight="bold", color="#17324D", pad=10)
    price_ax.set_ylabel("Price")
    handles, labels = price_ax.get_legend_handles_labels()
    price_ax.legend(handles, labels, loc="upper left", fontsize=7.3, framealpha=0.92, ncol=2)
    figure.text(
        0.5,
        0.01,
        "Fixed CANSLIM boundaries remain policy rules; ATR is the volatility-aware initial-stop input. Unverified pivots are never drawn.",
        ha="center",
        fontsize=8,
        color="#5D6D7E",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(figure)
    if not output_path.exists() or output_path.stat().st_size < 10_000:
        raise ValidationError(f"{ticker}: sell sandbox chart render failed")

    return {
        "status": "verified",
        "file": f"assets/{output_path.name}",
        "sha256": sha256_file(output_path),
        "levels": {
            "entry": round(entry, 4),
            "loss_limit": round(loss_limit, 4),
            "protected_loss_floor": round(protected_floor, 4),
            "atr_stop": round(atr_stop, 4) if atr_stop is not None else None,
            "working_stop": round(structural_stop, 4) if structural_stop is not None else None,
            "effective_stop": round(effective_stop, 4),
            "pivot": round(pivot, 4) if pivot is not None else None,
            "profit_zone_lower": round(lower_target, 4) if lower_target is not None else None,
            "profit_zone_upper": round(upper_target, 4) if upper_target is not None else None,
        },
        "time_boundaries": {"eight_week_trading_day": 40, "thirteen_week_trading_day": 65},
    }
