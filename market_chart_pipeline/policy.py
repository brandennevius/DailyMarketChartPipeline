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
        "hard_rules",
        "rapid_advance",
        "profit_zone",
        "trailing",
        "patience",
        "shakeout",
        "portfolio",
        "candidate_scoring",
    ]
    missing = [key for key in required if key not in policy]
    if missing:
        raise ValidationError(f"Trading policy missing required keys: {missing}")
    if abs(sum(policy["candidate_scoring"].values()) - 1.0) > 0.00001:
        raise ValidationError("Candidate scoring weights must sum to 1.0")
    return policy
