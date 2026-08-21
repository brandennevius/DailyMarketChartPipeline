from __future__ import annotations

from typing import Any

import pandas as pd

from .patterns import analyze_ohlcv_patterns


def _pct_distance(value: float, reference: float) -> float | None:
    if reference is None or reference == 0:
        return None
    return float((value / reference - 1) * 100)


def _slope_pct(series: pd.Series, periods: int = 4) -> float | None:
    clean = series.dropna()
    if len(clean) <= periods or clean.iloc[-periods - 1] == 0:
        return None
    return float((clean.iloc[-1] / clean.iloc[-periods - 1] - 1) * 100)


def _trend_label(value: float | None, rising: float = 1.0, falling: float = -1.0) -> str:
    if value is None:
        return "INSUFFICIENT_EVIDENCE"
    if value >= rising:
        return "RISING"
    if value <= falling:
        return "FALLING"
    return "FLAT"


def _rs_metrics(rs: pd.Series) -> dict[str, Any]:
    clean = rs.dropna()
    if len(clean) < 20:
        return {
            "status": "INSUFFICIENT_EVIDENCE",
            "trend_21d": "INSUFFICIENT_EVIDENCE",
            "change_21d_pct": None,
            "new_high_52w": None,
            "distance_from_52w_high_pct": None,
        }
    recent = clean.tail(252)
    change_21 = _slope_pct(clean, 21)
    high_52 = float(recent.max())
    latest = float(clean.iloc[-1])
    return {
        "status": "VERIFIED",
        "trend_21d": _trend_label(change_21, rising=2.0, falling=-2.0),
        "change_21d_pct": change_21,
        "new_high_52w": bool(latest >= high_52 * 0.995),
        "distance_from_52w_high_pct": _pct_distance(latest, high_52),
    }


def _volume_metrics(df: pd.DataFrame) -> dict[str, Any]:
    if "Volume" not in df or df["Volume"].isna().any():
        return {
            "status": "INSUFFICIENT_EVIDENCE",
            "reason": "FMP historical volume is unavailable for this instrument.",
            "up_volume_20": None,
            "down_volume_20": None,
            "up_down_volume_ratio_20": None,
            "accumulation_days_20": None,
            "distribution_days_20": None,
            "accumulation_distribution_estimate": "INSUFFICIENT_EVIDENCE",
        }
    recent = df.tail(21).copy()
    changes = recent["Close"].pct_change()
    up_volume = float(recent.loc[changes > 0, "Volume"].sum())
    down_volume = float(recent.loc[changes < 0, "Volume"].sum())
    ratio = None if down_volume <= 0 else up_volume / down_volume
    prev_volume = recent["Volume"].shift(1)
    accumulation = int(((changes >= 0.002) & (recent["Volume"] > prev_volume)).sum())
    distribution = int(((changes <= -0.002) & (recent["Volume"] > prev_volume)).sum())
    return {
        "status": "VERIFIED",
        "up_volume_20": up_volume,
        "down_volume_20": down_volume,
        "up_down_volume_ratio_20": ratio,
        "accumulation_days_20": accumulation,
        "distribution_days_20": distribution,
        "accumulation_distribution_estimate": (
            "POSITIVE" if accumulation >= distribution + 2 else
            "NEGATIVE" if distribution >= accumulation + 2 else
            "NEUTRAL"
        ),
    }


def calculate_technical_context(
    df: pd.DataFrame,
    rs: pd.Series,
    pattern_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    close = df["Close"]
    price = float(close.iloc[-1])
    sma21 = close.rolling(21).mean()
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()

    weekly = df.resample("W-FRI").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last"
    }).dropna()
    ma10w = weekly["Close"].rolling(10).mean()
    ma40w = weekly["Close"].rolling(40).mean()
    slope10 = _slope_pct(ma10w, 4)
    slope40 = _slope_pct(ma40w, 4)

    return {
        "distance_from_21d_pct": _pct_distance(price, float(sma21.iloc[-1])),
        "distance_from_50d_pct": _pct_distance(price, float(sma50.iloc[-1])),
        "distance_from_200d_pct": _pct_distance(price, float(sma200.iloc[-1])),
        "ma_10w": None if pd.isna(ma10w.iloc[-1]) else float(ma10w.iloc[-1]),
        "ma_40w": None if pd.isna(ma40w.iloc[-1]) else float(ma40w.iloc[-1]),
        "ma_10w_slope_4w_pct": slope10,
        "ma_40w_slope_4w_pct": slope40,
        "ma_10w_trend": _trend_label(slope10),
        "ma_40w_trend": _trend_label(slope40, rising=0.5, falling=-0.5),
        "relative_strength": _rs_metrics(rs),
        "volume": _volume_metrics(df),
        "base_analysis": analyze_ohlcv_patterns(df, policy=pattern_policy),
    }
