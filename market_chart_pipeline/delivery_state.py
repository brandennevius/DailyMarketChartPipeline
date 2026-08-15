from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import atomic_write_text


@dataclass(frozen=True)
class ProcessedState:
    manifest: str
    session_date: str
    processed_at: str
    terminal: bool
    delivery: dict[str, Any]


def write_processed_state(path: Path, *, manifest: str, session_date: str, delivery: dict[str, Any]) -> ProcessedState:
    state = ProcessedState(
        manifest=manifest,
        session_date=session_date,
        processed_at=datetime.now(timezone.utc).isoformat(),
        terminal=True,
        delivery=delivery,
    )
    atomic_write_text(path, json.dumps(asdict(state), indent=2, sort_keys=True) + "\n")
    return state
