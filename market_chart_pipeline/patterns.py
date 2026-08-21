from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd


PATTERN_ALGORITHM_VERSION = "oneil_style_ohlcv_patterns_v1"
PATTERN_POLICY_VERSION = "oneil_style_pattern_policy_v1"

DEFAULT_PATTERN_POLICY: dict[str, Any] = {
    "policy_version": PATTERN_POLICY_VERSION,
    "normal_depth_min_pct": 8.0,
    "normal_depth_max_pct": 35.0,
    "deep_depth_max_pct": 50.0,
    "allow_deep_bases": False,
    "cup_min_weeks": 7,
    "cup_max_weeks": 35,
    "cup_min_depth_pct": 12.0,
    "handle_min_weeks": 1,
    "handle_max_weeks": 5,
    "handle_max_depth_pct": 15.0,
    "flat_base_min_weeks": 5,
    "flat_base_max_weeks": 8,
    "flat_base_max_depth_pct": 15.0,
    "flat_base_max_net_change_pct": 5.0,
    "flat_base_min_down_weeks": 1,
    "double_bottom_min_weeks": 7,
    "double_bottom_max_weeks": 30,
    "prior_uptrend_min_pct": 25.0,
    "right_side_recovery_min_ratio": 0.90,
    "volume_contraction_max_ratio": 0.90,
    "breakout_relative_volume_min": 1.50,
    "buy_zone_pct": 5.0,
    "pivot_increment": 0.10,
    "scan_recent_weeks": 10,
}


def _policy(overrides: dict[str, Any] | None) -> dict[str, Any]:
    return {**DEFAULT_PATTERN_POLICY, **(overrides or {})}


def _gate(status: str, reason: str, **evidence: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, "evidence": evidence}


def _weekly_bars(df: pd.DataFrame) -> pd.DataFrame:
    clean = df.sort_index().copy()
    clean["_actual_date"] = clean.index
    aggregation: dict[str, Any] = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "_actual_date": "max",
    }
    if "Volume" in clean:
        aggregation["Volume"] = "sum"
    weekly = clean.resample("W-FRI").agg(aggregation).dropna(subset=["Open", "High", "Low", "Close"])
    weekly = weekly.set_index("_actual_date")
    weekly.index = pd.DatetimeIndex(weekly.index).normalize()
    return weekly


def _date(value: Any) -> str:
    return pd.Timestamp(value).date().isoformat()


def _depth(high: float, low: float) -> float:
    return (high - low) / high * 100.0 if high > 0 else 100.0


def _depth_gate(depth_pct: float, policy: dict[str, Any], *, flat: bool = False) -> dict[str, Any]:
    minimum = 3.0 if flat else float(policy["normal_depth_min_pct"])
    maximum = float(policy["flat_base_max_depth_pct"] if flat else policy["normal_depth_max_pct"])
    if minimum <= depth_pct <= maximum:
        return _gate("PASS", "Correction depth is within the configured normal range.", depth_pct=round(depth_pct, 4), minimum_pct=minimum, maximum_pct=maximum)
    if not flat and maximum < depth_pct <= float(policy["deep_depth_max_pct"]):
        status = "PASS" if policy.get("allow_deep_bases") else "FAIL"
        return _gate(status, "Correction is in the configured deep-base range." if status == "PASS" else "Deep bases are disabled by policy.", depth_pct=round(depth_pct, 4), maximum_normal_pct=maximum, maximum_deep_pct=float(policy["deep_depth_max_pct"]))
    return _gate("FAIL", "Correction depth is outside configured bounds.", depth_pct=round(depth_pct, 4), minimum_pct=minimum, maximum_pct=maximum)


def _prior_uptrend(weekly: pd.DataFrame, base_start: pd.Timestamp, left_high: float, policy: dict[str, Any]) -> dict[str, Any]:
    prior = weekly.loc[weekly.index < base_start].tail(26)
    if len(prior) < 10:
        return _gate("UNKNOWN", "Fewer than ten prior weekly bars are available.", prior_weeks=len(prior))
    prior_low = float(prior["Low"].min())
    gain = (left_high / prior_low - 1.0) * 100.0 if prior_low > 0 else None
    if gain is None:
        return _gate("UNKNOWN", "Prior-uptrend denominator is invalid.")
    status = "PASS" if gain >= float(policy["prior_uptrend_min_pct"]) else "FAIL"
    return _gate(status, "A material advance preceded the base." if status == "PASS" else "The required prior advance was not present.", prior_low=round(prior_low, 6), left_high=round(left_high, 6), advance_pct=round(gain, 4), minimum_pct=float(policy["prior_uptrend_min_pct"]), evidence_start=_date(prior.index[0]), evidence_end=_date(prior.index[-1]))


def _weekly_structure(weekly: pd.DataFrame, formation_end: pd.Timestamp) -> dict[str, Any]:
    history = weekly.loc[weekly.index <= formation_end]
    if len(history) < 42:
        return _gate("UNKNOWN", "Forty-two weekly bars are required for moving-average structure.", weeks=len(history))
    close = history["Close"]
    ma10 = close.rolling(10).mean()
    ma40 = close.rolling(40).mean()
    latest = float(close.iloc[-1])
    ten = float(ma10.iloc[-1])
    forty = float(ma40.iloc[-1])
    ten_slope = (ten / float(ma10.iloc[-5]) - 1.0) * 100.0 if pd.notna(ma10.iloc[-5]) else None
    forty_slope = (forty / float(ma40.iloc[-5]) - 1.0) * 100.0 if pd.notna(ma40.iloc[-5]) else None
    passed = latest >= ten >= forty and ten_slope is not None and forty_slope is not None and ten_slope > 0 and forty_slope >= 0
    return _gate("PASS" if passed else "FAIL", "Weekly close and moving averages are aligned and non-declining." if passed else "Weekly moving-average alignment is not fully constructive.", close=round(latest, 6), ma10w=round(ten, 6), ma40w=round(forty, 6), ma10w_slope_4w_pct=None if ten_slope is None else round(ten_slope, 4), ma40w_slope_4w_pct=None if forty_slope is None else round(forty_slope, 4))


def _stage_proxy(weekly: pd.DataFrame, base_start: pd.Timestamp, left_high: float) -> tuple[dict[str, Any], int | None, str]:
    history = weekly.loc[weekly.index < base_start].tail(52)
    if len(history) < 26:
        return _gate("UNKNOWN", "Insufficient history for the algorithmic stage proxy.", prior_weeks=len(history)), None, "UNKNOWN"
    prior_high = float(history["High"].max())
    near_high = left_high >= prior_high * 0.95
    status = "PASS" if near_high else "FAIL"
    return (
        _gate(status, "Base began near the prior 52-week high; classified as algorithmic early-stage proxy." if near_high else "Base did not begin near the prior 52-week high.", left_high=round(left_high, 6), prior_52w_high=round(prior_high, 6), threshold_ratio=0.95),
        1 if near_high else None,
        "ALGORITHMIC_STAGE_1_OR_2" if near_high else "UNVERIFIED",
    )


def _volume_contraction(window: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    if "Volume" not in window or window["Volume"].isna().any() or (window["Volume"] <= 0).any():
        return _gate("UNKNOWN", "Volume contraction cannot be evaluated because weekly volume is unavailable.")
    size = max(2, min(4, len(window) // 3))
    early = float(window["Volume"].head(size).mean())
    late = float(window["Volume"].tail(size).mean())
    ratio = late / early if early > 0 else None
    if ratio is None:
        return _gate("UNKNOWN", "Volume contraction denominator is invalid.")
    passed = ratio <= float(policy["volume_contraction_max_ratio"])
    return _gate("PASS" if passed else "FAIL", "Late-base weekly volume contracted versus early-base volume." if passed else "Late-base volume did not contract enough.", early_average=round(early, 4), late_average=round(late, 4), ratio=round(ratio, 4), maximum_ratio=float(policy["volume_contraction_max_ratio"]))


def _breakout(df: pd.DataFrame, formation_end: pd.Timestamp, pivot: float, policy: dict[str, Any]) -> dict[str, Any]:
    future = df.loc[(df.index > formation_end) & (df["Close"] > pivot)]
    current = float(df["Close"].iloc[-1])
    buy_upper = pivot * (1.0 + float(policy["buy_zone_pct"]) / 100.0)
    extension = (current / pivot - 1.0) * 100.0
    if future.empty:
        return {
            "status": "NOT_BROKEN_OUT",
            "date": None,
            "close": None,
            "volume_ratio_50d": None,
            "volume_confirmation": False,
            "current_price": round(current, 6),
            "distance_from_pivot_pct": round(extension, 4),
            "inside_buy_zone": False,
            "extended": False,
        }
    breakout_date = future.index[0]
    row = df.loc[breakout_date]
    prior_volume = df.loc[df.index < breakout_date, "Volume"].tail(50) if "Volume" in df else pd.Series(dtype=float)
    ratio = None
    if len(prior_volume) >= 20 and prior_volume.notna().all() and float(prior_volume.mean()) > 0 and pd.notna(row.get("Volume")):
        ratio = float(row["Volume"]) / float(prior_volume.mean())
    confirmed = ratio is not None and ratio >= float(policy["breakout_relative_volume_min"])
    status = "CONFIRMED" if confirmed else "PRICE_ONLY"
    if current < pivot:
        status = "FAILED"
    elif current > buy_upper:
        status = "EXTENDED"
    return {
        "status": status,
        "date": _date(breakout_date),
        "close": round(float(row["Close"]), 6),
        "volume_ratio_50d": None if ratio is None else round(ratio, 4),
        "volume_confirmation": confirmed,
        "minimum_volume_ratio": float(policy["breakout_relative_volume_min"]),
        "current_price": round(current, 6),
        "distance_from_pivot_pct": round(extension, 4),
        "inside_buy_zone": pivot <= current <= buy_upper,
        "extended": current > buy_upper,
    }


def _candidate(
    *,
    pattern_type: str,
    window: pd.DataFrame,
    weekly: pd.DataFrame,
    df: pd.DataFrame,
    policy: dict[str, Any],
    pivot: float,
    pivot_basis: str,
    left_high_date: pd.Timestamp,
    left_high: float,
    low_date: pd.Timestamp,
    low: float,
    pattern_gates: dict[str, dict[str, Any]],
    markers: dict[str, Any],
    handle: dict[str, Any] | None = None,
    containment_start: pd.Timestamp | None = None,
) -> dict[str, Any]:
    formation_end = pd.Timestamp(window.index[-1])
    depth_pct = _depth(left_high, low)
    stage_gate, stage_number, stage_status = _stage_proxy(weekly, pd.Timestamp(window.index[0]), left_high)
    containment_boundary = containment_start if containment_start is not None else pd.Timestamp(window.index[0])
    containment_window = window.loc[window.index >= containment_boundary]
    highest_formation_close = float(containment_window["Close"].max())
    contained = highest_formation_close <= pivot
    gates = {
        "prior_uptrend": _prior_uptrend(weekly, pd.Timestamp(window.index[0]), left_high, policy),
        "conventional_base_type": _gate("PASS", "Pattern type is one of the published deterministic base types.", pattern_type=pattern_type),
        "base_duration": _gate("PASS", "Base duration satisfies this pattern's configured range.", weeks=len(window)),
        "base_depth": _depth_gate(depth_pct, policy, flat=pattern_type == "FLAT_BASE"),
        "base_stage": stage_gate,
        "weekly_structure": _weekly_structure(weekly, formation_end),
        "volume_contraction": _volume_contraction(window, policy),
        "exact_pivot_price": _gate("PASS", "Pivot is calculated from the pattern-specific conventional reference plus the configured increment.", pivot_price=round(pivot, 6), basis=pivot_basis, increment=float(policy["pivot_increment"])),
        "prebreakout_containment": _gate(
            "PASS" if contained else "FAIL",
            "Formation closes remained below the calculated pivot before the breakout."
            if contained else "A formation close exceeded the calculated pivot before the selected base ended.",
            highest_formation_close=round(highest_formation_close, 6),
            pivot_price=round(pivot, 6),
        ),
        **pattern_gates,
    }
    hard_gate_names = [
        "prior_uptrend", "conventional_base_type", "base_duration", "base_depth", "base_stage",
        "weekly_structure", "volume_contraction", "exact_pivot_price", "prebreakout_containment", *pattern_gates.keys(),
    ]
    hard_statuses = [gates[name]["status"] for name in hard_gate_names]
    verified = bool(hard_statuses) and all(status == "PASS" for status in hard_statuses)
    pass_count = sum(status == "PASS" for status in hard_statuses)
    fail_count = sum(status == "FAIL" for status in hard_statuses)
    unknown_count = sum(status == "UNKNOWN" for status in hard_statuses)
    confidence_score = max(0.0, min(100.0, 100.0 * pass_count / len(hard_statuses) - fail_count * 8.0 - unknown_count * 3.0))
    breakout = _breakout(df, formation_end, pivot, policy)
    missing = sorted(name for name in hard_gate_names if gates[name]["status"] != "PASS")
    status = "VERIFIED_ALGORITHMIC_PIVOT" if verified else "BUILDING" if pass_count >= max(4, len(hard_statuses) - 3) else "INSUFFICIENT_EVIDENCE"
    return {
        "status": status,
        "pattern_type": pattern_type,
        "confidence": "HIGH" if verified else "MEDIUM" if confidence_score >= 65 else "LOW",
        "confidence_score": round(confidence_score, 2),
        "algorithm_version": PATTERN_ALGORITHM_VERSION,
        "policy_version": str(policy["policy_version"]),
        "base_start": _date(window.index[0]),
        "base_end": _date(formation_end),
        "base_length_weeks": len(window),
        "base_depth_pct": round(depth_pct, 4),
        "left_side_high": {"date": _date(left_high_date), "price": round(left_high, 6)},
        "base_low": {"date": _date(low_date), "price": round(low, 6)},
        "pivot_price": round(pivot, 6) if verified else None,
        "candidate_pivot_price": round(pivot, 6),
        "pivot_basis": pivot_basis,
        "pivot_status": "VERIFIED_ALGORITHMIC_PIVOT" if verified else "UNVERIFIED_ALGORITHMIC_CANDIDATE",
        "buy_zone_upper_bound": round(pivot * (1.0 + float(policy["buy_zone_pct"]) / 100.0), 6) if verified else None,
        "early_entry_price": None,
        "early_entry_verification_status": "unverified",
        "breakout_status": breakout["status"],
        "breakout_date": breakout["date"],
        "breakout_volume_ratio_50d": breakout["volume_ratio_50d"],
        "breakout_volume_confirmation": breakout["volume_confirmation"],
        "inside_buy_zone": bool(verified and breakout["inside_buy_zone"]),
        "extended": bool(verified and breakout["extended"]),
        "distance_from_pivot_pct": breakout["distance_from_pivot_pct"] if verified else None,
        "candidate_resistance": round(pivot, 6),
        "candidate_resistance_distance_pct": breakout["distance_from_pivot_pct"],
        "stage_number": stage_number,
        "stage_status": stage_status,
        "handle": handle,
        "invalidation_support": {
            "primary": round(float((handle or {}).get("low_price") or low), 6),
            "basis": "handle low" if handle else "base low",
        },
        "gates": gates,
        "hard_gate_names": hard_gate_names,
        "missing_evidence": missing,
        "evidence_bars": {
            "session_end": _date(df.index[-1]),
            "base_start": _date(window.index[0]),
            "base_end": _date(formation_end),
            "weekly_bar_count": len(window),
            "markers": markers,
        },
        "reason": "Every deterministic structural pivot gate passed." if verified else "One or more deterministic structural pivot gates failed or remain unknown.",
        "breakout_evidence": breakout,
    }


def _cup_candidates(df: pd.DataFrame, weekly: pd.DataFrame, policy: dict[str, Any]) -> Iterable[dict[str, Any]]:
    max_end = len(weekly) - 1
    min_end = max(0, max_end - int(policy["scan_recent_weeks"]))
    for end in range(max_end, min_end - 1, -1):
        for duration in range(int(policy["cup_min_weeks"]), min(int(policy["cup_max_weeks"]), end + 1) + 1):
            window = weekly.iloc[end - duration + 1:end + 1]
            for handle_weeks in range(0, min(int(policy["handle_max_weeks"]), duration - int(policy["cup_min_weeks"])) + 1):
                if handle_weeks and handle_weeks < int(policy["handle_min_weeks"]):
                    continue
                cup = window.iloc[:-handle_weeks] if handle_weeks else window
                if len(cup) < int(policy["cup_min_weeks"]):
                    continue
                first_count = max(2, int(len(cup) * 0.35))
                left_segment = cup.iloc[:first_count]
                left_date = pd.Timestamp(left_segment["High"].idxmax())
                left_high = float(left_segment.loc[left_date, "High"])
                left_pos = cup.index.get_loc(left_date)
                low_segment = cup.iloc[left_pos + 1:max(left_pos + 2, len(cup) - 1)]
                if low_segment.empty:
                    continue
                low_date = pd.Timestamp(low_segment["Low"].idxmin())
                low = float(low_segment.loc[low_date, "Low"])
                low_pos = cup.index.get_loc(low_date)
                cup_depth = _depth(left_high, low)
                low_position_ratio = low_pos / max(1, len(cup) - 1)
                low_position_gate = _gate(
                    "PASS" if 0.20 <= low_position_ratio <= 0.80 else "FAIL",
                    "Cup low is positioned within the central 60% of the formation."
                    if 0.20 <= low_position_ratio <= 0.80
                    else "Cup low is too close to a formation edge for reliable U-shaped geometry.",
                    position_ratio=round(low_position_ratio, 4),
                    minimum_ratio=0.20,
                    maximum_ratio=0.80,
                )
                right = cup.iloc[low_pos + 1:]
                if len(right) < 2:
                    continue
                right_high_date = pd.Timestamp(right["High"].idxmax())
                right_high = float(right.loc[right_high_date, "High"])
                recovery_ratio = right_high / left_high if left_high > 0 else 0
                recovery_gate = _gate("PASS" if recovery_ratio >= float(policy["right_side_recovery_min_ratio"]) else "FAIL", "Right side recovered near the left-side high." if recovery_ratio >= float(policy["right_side_recovery_min_ratio"]) else "Right-side recovery is incomplete.", recovery_ratio=round(recovery_ratio, 4), minimum_ratio=float(policy["right_side_recovery_min_ratio"]))
                if handle_weeks:
                    handle_frame = window.tail(handle_weeks)
                    handle_high_date = pd.Timestamp(handle_frame["High"].idxmax())
                    handle_low_date = pd.Timestamp(handle_frame["Low"].idxmin())
                    handle_high = float(handle_frame.loc[handle_high_date, "High"])
                    handle_low = float(handle_frame.loc[handle_low_date, "Low"])
                    handle_depth = _depth(handle_high, handle_low)
                    midpoint = low + (left_high - low) / 2.0
                    upper_half = handle_low >= midpoint
                    drift_pct = (float(handle_frame["Close"].iloc[-1]) / float(handle_frame["Close"].iloc[0]) - 1.0) * 100.0
                    pattern_gates = {
                        "cup_correction_depth": _gate(
                            "PASS" if cup_depth >= float(policy["cup_min_depth_pct"]) else "FAIL",
                            "Cup correction is deep enough to distinguish it from a flat consolidation."
                            if cup_depth >= float(policy["cup_min_depth_pct"]) else "Cup correction is too shallow for the configured cup geometry.",
                            depth_pct=round(cup_depth, 4),
                            minimum_pct=float(policy["cup_min_depth_pct"]),
                        ),
                        "right_side_recovery": recovery_gate,
                        "cup_low_position": low_position_gate,
                        "handle_duration": _gate("PASS", "Handle duration is within configured bounds.", weeks=handle_weeks),
                        "handle_depth": _gate("PASS" if handle_depth <= float(policy["handle_max_depth_pct"]) else "FAIL", "Handle depth is acceptable." if handle_depth <= float(policy["handle_max_depth_pct"]) else "Handle is too deep.", depth_pct=round(handle_depth, 4), maximum_pct=float(policy["handle_max_depth_pct"])),
                        "handle_upper_half": _gate("PASS" if upper_half else "FAIL", "Handle low stayed in the upper half of the cup." if upper_half else "Handle fell into the lower half of the cup.", handle_low=round(handle_low, 6), cup_midpoint=round(midpoint, 6)),
                        "handle_downward_drift": _gate("PASS" if drift_pct <= 2.0 else "FAIL", "Handle was flat-to-down rather than advancing materially." if drift_pct <= 2.0 else "Handle advanced too sharply before the pivot.", close_change_pct=round(drift_pct, 4), maximum_pct=2.0),
                        "handle_pullback": _gate(
                            "PASS" if float(handle_frame["Close"].iloc[-1]) <= float(cup["Close"].iloc[-1]) * 1.005 else "FAIL",
                            "Handle ended flat-to-down versus the preceding cup recovery."
                            if float(handle_frame["Close"].iloc[-1]) <= float(cup["Close"].iloc[-1]) * 1.005
                            else "The proposed handle continued the right-side advance instead of pausing or pulling back.",
                            pre_handle_close=round(float(cup["Close"].iloc[-1]), 6),
                            handle_end_close=round(float(handle_frame["Close"].iloc[-1]), 6),
                            maximum_ratio=1.005,
                        ),
                    }
                    pivot_reference = handle_high
                    pattern_type = "CUP_WITH_HANDLE"
                    handle_payload = {"start": _date(handle_frame.index[0]), "end": _date(handle_frame.index[-1]), "weeks": handle_weeks, "high_date": _date(handle_high_date), "high_price": round(handle_high, 6), "low_date": _date(handle_low_date), "low_price": round(handle_low, 6), "depth_pct": round(handle_depth, 4), "drift_pct": round(drift_pct, 4)}
                    basis = "handle high plus configured pivot increment"
                else:
                    pattern_gates = {
                        "cup_correction_depth": _gate(
                            "PASS" if cup_depth >= float(policy["cup_min_depth_pct"]) else "FAIL",
                            "Cup correction is deep enough to distinguish it from a flat consolidation."
                            if cup_depth >= float(policy["cup_min_depth_pct"]) else "Cup correction is too shallow for the configured cup geometry.",
                            depth_pct=round(cup_depth, 4),
                            minimum_pct=float(policy["cup_min_depth_pct"]),
                        ),
                        "right_side_recovery": recovery_gate,
                        "cup_low_position": low_position_gate,
                        "handle_quality": _gate("PASS", "A handle is not required for the cup-without-handle pattern."),
                    }
                    pivot_reference = left_high
                    pattern_type = "CUP_WITHOUT_HANDLE"
                    handle_payload = None
                    basis = "left-side cup high plus configured pivot increment"
                pivot = pivot_reference + float(policy["pivot_increment"])
                yield _candidate(pattern_type=pattern_type, window=window, weekly=weekly, df=df, policy=policy, pivot=pivot, pivot_basis=basis, left_high_date=left_date, left_high=left_high, low_date=low_date, low=low, pattern_gates=pattern_gates, markers={"right_side_high": {"date": _date(right_high_date), "price": round(right_high, 6)}}, handle=handle_payload, containment_start=pd.Timestamp(handle_frame.index[0]) if handle_weeks else None)


def _flat_candidates(df: pd.DataFrame, weekly: pd.DataFrame, policy: dict[str, Any]) -> Iterable[dict[str, Any]]:
    max_end = len(weekly) - 1
    min_end = max(0, max_end - int(policy["scan_recent_weeks"]))
    for end in range(max_end, min_end - 1, -1):
        for duration in range(int(policy["flat_base_min_weeks"]), int(policy["flat_base_max_weeks"]) + 1):
            if duration > end + 1:
                continue
            window = weekly.iloc[end - duration + 1:end + 1]
            left_date = pd.Timestamp(window["High"].idxmax())
            low_date = pd.Timestamp(window["Low"].idxmin())
            high = float(window.loc[left_date, "High"])
            low = float(window.loc[low_date, "Low"])
            net_change = (float(window["Close"].iloc[-1]) / float(window["Close"].iloc[0]) - 1.0) * 100.0
            down_weeks = int((window["Close"].diff() < 0).sum())
            consolidation_pass = (
                abs(net_change) <= float(policy["flat_base_max_net_change_pct"])
                and down_weeks >= int(policy["flat_base_min_down_weeks"])
            )
            yield _candidate(pattern_type="FLAT_BASE", window=window, weekly=weekly, df=df, policy=policy, pivot=high + float(policy["pivot_increment"]), pivot_basis="flat-base high plus configured pivot increment", left_high_date=left_date, left_high=high, low_date=low_date, low=low, pattern_gates={
                "flat_base_geometry": _gate("PASS", "Flat-base high/low geometry is directly measured from the configured window."),
                "flat_base_consolidation": _gate(
                    "PASS" if consolidation_pass else "FAIL",
                    "Flat-base closes remained sideways and included a pullback week."
                    if consolidation_pass else "The window is a directional advance rather than a sideways flat base.",
                    net_close_change_pct=round(net_change, 4),
                    maximum_absolute_pct=float(policy["flat_base_max_net_change_pct"]),
                    down_weeks=down_weeks,
                    minimum_down_weeks=int(policy["flat_base_min_down_weeks"]),
                ),
            }, markers={}, handle=None)


def _double_bottom_candidates(df: pd.DataFrame, weekly: pd.DataFrame, policy: dict[str, Any]) -> Iterable[dict[str, Any]]:
    max_end = len(weekly) - 1
    min_end = max(0, max_end - int(policy["scan_recent_weeks"]))
    for end in range(max_end, min_end - 1, -1):
        for duration in range(int(policy["double_bottom_min_weeks"]), min(int(policy["double_bottom_max_weeks"]), end + 1) + 1):
            window = weekly.iloc[end - duration + 1:end + 1]
            left_count = max(2, int(duration * 0.25))
            left_date = pd.Timestamp(window.iloc[:left_count]["High"].idxmax())
            left_high = float(window.loc[left_date, "High"])
            first_zone = window.iloc[left_count:max(left_count + 1, int(duration * 0.55))]
            if first_zone.empty:
                continue
            first_low_date = pd.Timestamp(first_zone["Low"].idxmin())
            first_pos = window.index.get_loc(first_low_date)
            midpoint_zone = window.iloc[first_pos + 1:max(first_pos + 2, int(duration * 0.80))]
            if midpoint_zone.empty:
                continue
            midpoint_date = pd.Timestamp(midpoint_zone["High"].idxmax())
            midpoint = float(midpoint_zone.loc[midpoint_date, "High"])
            midpoint_pos = window.index.get_loc(midpoint_date)
            second_zone = window.iloc[midpoint_pos + 1:]
            if second_zone.empty:
                continue
            second_low_date = pd.Timestamp(second_zone["Low"].idxmin())
            first_low = float(window.loc[first_low_date, "Low"])
            second_low = float(window.loc[second_low_date, "Low"])
            relation = (second_low / first_low - 1.0) * 100.0 if first_low > 0 else 100.0
            midpoint_gain = (midpoint / first_low - 1.0) * 100.0 if first_low > 0 else 0.0
            relation_pass = -3.0 <= relation <= 5.0
            midpoint_pass = midpoint_gain >= 10.0
            yield _candidate(pattern_type="DOUBLE_BOTTOM", window=window, weekly=weekly, df=df, policy=policy, pivot=midpoint + float(policy["pivot_increment"]), pivot_basis="middle-W peak plus configured pivot increment", left_high_date=left_date, left_high=left_high, low_date=min(first_low_date, second_low_date, key=lambda d: float(window.loc[d, "Low"])), low=min(first_low, second_low), pattern_gates={
                "double_bottom_low_relation": _gate("PASS" if relation_pass else "FAIL", "Second low is within the configured relation to the first low." if relation_pass else "Second low is too far from the first low.", first_low=round(first_low, 6), second_low=round(second_low, 6), relation_pct=round(relation, 4), minimum_pct=-3.0, maximum_pct=5.0),
                "double_bottom_midpoint": _gate("PASS" if midpoint_pass else "FAIL", "The W midpoint recovered at least 10% from the first low." if midpoint_pass else "The W midpoint recovery is insufficient.", midpoint=round(midpoint, 6), recovery_pct=round(midpoint_gain, 4), minimum_pct=10.0),
            }, markers={"first_low": {"date": _date(first_low_date), "price": round(first_low, 6)}, "midpoint": {"date": _date(midpoint_date), "price": round(midpoint, 6)}, "second_low": {"date": _date(second_low_date), "price": round(second_low, 6)}}, handle=None, containment_start=midpoint_date)


def analyze_ohlcv_patterns(df: pd.DataFrame, *, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    selected = _policy(policy)
    if df.empty or len(df) < 200:
        return {
            "status": "INSUFFICIENT_EVIDENCE",
            "pattern_type": "UNKNOWN",
            "algorithm_version": PATTERN_ALGORITHM_VERSION,
            "policy_version": str(selected["policy_version"]),
            "reason": "At least 200 exact-session daily bars are required.",
            "pivot_price": None,
            "candidate_pivot_price": None,
            "pivot_status": "UNVERIFIED_ALGORITHMIC_CANDIDATE",
            "gates": {},
            "missing_evidence": ["daily_history_200_bars"],
        }
    weekly = _weekly_bars(df)
    candidates = [*_flat_candidates(df, weekly, selected), *_double_bottom_candidates(df, weekly, selected), *_cup_candidates(df, weekly, selected)]
    if not candidates:
        return {
            "status": "NO_PATTERN_CANDIDATE",
            "pattern_type": "UNKNOWN",
            "confidence": "LOW",
            "confidence_score": 0.0,
            "algorithm_version": PATTERN_ALGORITHM_VERSION,
            "policy_version": str(selected["policy_version"]),
            "reason": "No supported deterministic pattern candidate was found.",
            "pivot_price": None,
            "candidate_pivot_price": None,
            "pivot_status": "UNVERIFIED_ALGORITHMIC_CANDIDATE",
            "candidate_resistance": None,
            "candidate_resistance_distance_pct": None,
            "stage_number": None,
            "stage_status": "UNVERIFIED",
            "gates": {},
            "missing_evidence": ["supported_pattern_geometry"],
            "evidence_bars": {"session_end": _date(df.index[-1])},
        }
    # Prefer the narrowest published geometry when multiple broader detectors
    # describe the same shallow, short consolidation.
    priority = {"DOUBLE_BOTTOM": 0, "CUP_WITH_HANDLE": 1, "FLAT_BASE": 2, "CUP_WITHOUT_HANDLE": 3}
    breakout_priority = {"CONFIRMED": 0, "PRICE_ONLY": 1, "NOT_BROKEN_OUT": 2, "EXTENDED": 3, "FAILED": 4}
    candidates.sort(key=lambda item: (
        0 if item["status"] == "VERIFIED_ALGORITHMIC_PIVOT" else 1,
        breakout_priority.get(item.get("breakout_status"), 9),
        -float(item["confidence_score"]),
        -pd.Timestamp(item["base_end"]).value,
        priority.get(item["pattern_type"], 99),
        item["base_start"],
    ))
    best = candidates[0]
    best["alternative_candidates"] = [
        {key: item.get(key) for key in ("pattern_type", "status", "confidence", "confidence_score", "base_start", "base_end", "candidate_pivot_price")}
        for item in candidates[1:6]
    ]
    return best
