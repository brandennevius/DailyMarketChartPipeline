from __future__ import annotations

from pathlib import Path
from typing import Any

from .core import ValidationError
from .utils import load_json

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "trading_policy.json"


def load_policy(path: Path | None = None) -> dict[str, Any]:
    policy_path = path or DEFAULT_POLICY_PATH
    policy = load_json(policy_path)
    required = [
        "policy_version",
        "calculation_version",
        "actions",
        "candidate_actions",
        "hard_rules",
        "rapid_advance",
        "profit_zone",
        "trailing",
        "patience",
        "shakeout",
        "portfolio",
        "candidate_scoring",
        "candidate_ranking",
        "pattern_engine",
    ]
    missing = [key for key in required if key not in policy]
    if missing:
        raise ValidationError(f"Trading policy missing required keys: {missing}")
    if abs(sum(policy["candidate_scoring"].values()) - 1.0) > 0.00001:
        raise ValidationError("Candidate scoring weights must sum to 1.0")
    expected_components = {
        "fundamental_quality",
        "relative_strength_group",
        "technical_setup",
        "accumulation_supply",
        "new_catalyst",
    }
    if set(policy["candidate_scoring"]) != expected_components:
        raise ValidationError("Candidate scoring must define the five versioned CANSLIM/setup components")
    ranking = policy["candidate_ranking"]
    for field in [
        "schema_version",
        "maximum_ranked_setups",
        "minimum_available_dimensions",
        "minimum_average_dollar_volume",
        "minimum_breakout_relative_volume",
        "maximum_industry_group_rank_for_action",
        "minimum_days_to_earnings_for_action",
    ]:
        if field not in ranking:
            raise ValidationError(f"Candidate ranking policy missing required field: {field}")
    if not 1 <= int(ranking["maximum_ranked_setups"]) <= 10:
        raise ValidationError("Candidate ranking limit must be between 1 and 10")
    pattern = policy["pattern_engine"]
    required_pattern_fields = [
        "algorithm_version", "policy_version", "normal_depth_min_pct", "normal_depth_max_pct", "deep_depth_max_pct",
        "allow_deep_bases", "cup_min_weeks", "cup_max_weeks", "cup_min_depth_pct", "handle_min_weeks", "handle_max_weeks",
        "handle_max_depth_pct", "flat_base_min_weeks", "flat_base_max_weeks", "flat_base_max_depth_pct",
        "flat_base_max_net_change_pct", "flat_base_min_down_weeks",
        "double_bottom_min_weeks", "double_bottom_max_weeks", "prior_uptrend_min_pct",
        "right_side_recovery_min_ratio", "volume_contraction_max_ratio", "breakout_relative_volume_min",
        "buy_zone_pct", "pivot_increment", "scan_recent_weeks",
    ]
    missing_pattern = [field for field in required_pattern_fields if field not in pattern]
    if missing_pattern:
        raise ValidationError(f"Pattern-engine policy missing required fields: {missing_pattern}")
    if pattern["algorithm_version"] != "oneil_style_ohlcv_patterns_v1":
        raise ValidationError("Pattern-engine algorithm version is unsupported")
    if pattern["policy_version"] != "oneil_style_pattern_policy_v1":
        raise ValidationError("Pattern-engine policy version is unsupported")
    if not 0 < float(pattern["normal_depth_min_pct"]) < float(pattern["normal_depth_max_pct"]) <= float(pattern["deep_depth_max_pct"]):
        raise ValidationError("Pattern-engine correction-depth bounds are invalid")
    if not 0 < int(pattern["cup_min_weeks"]) <= int(pattern["cup_max_weeks"]):
        raise ValidationError("Pattern-engine cup-duration bounds are invalid")
    if not 0 < int(pattern["flat_base_min_weeks"]) <= int(pattern["flat_base_max_weeks"]):
        raise ValidationError("Pattern-engine flat-base duration bounds are invalid")
    trailing = policy["trailing"]
    for field in ["activation_gain_pct", "protected_loss_floor_pct", "peak_drawdown_exit_pct", "peak_basis", "action"]:
        if field not in trailing:
            raise ValidationError(f"Trading policy trailing rule missing required field: {field}")
    if trailing["peak_basis"] != "highest_close":
        raise ValidationError("Only highest_close peak trailing is currently supported")
    if trailing["action"] not in policy["actions"]:
        raise ValidationError("Trailing action must be in the configured action set")
    zone = policy["profit_zone"]
    for field in ["minimum_hold_weeks", "action"]:
        if field not in zone:
            raise ValidationError(f"Trading policy profit-zone rule missing required field: {field}")
    if zone["action"] not in policy["actions"]:
        raise ValidationError("Profit-zone action must be in the configured action set")
    return policy
