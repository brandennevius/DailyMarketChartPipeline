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
