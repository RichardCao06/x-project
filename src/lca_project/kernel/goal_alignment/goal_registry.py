"""Immutable Goal Contract registry."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .store import AlignmentStore
from ..state import StateStore


class GoalRegistry:
    REQUIRED = {"schema_version", "goal_id", "version", "scope", "quality_dimensions",
                "maturity_policy", "repair_authority", "promotion_policy"}

    def __init__(self, root: str | Path, state: StateStore) -> None:
        self.root = Path(root)
        self.store = AlignmentStore(state)

    def load(self, path: str | Path | None = None) -> dict[str, Any]:
        source = Path(path) if path else self.root / "policies/wiki-goal-contract-v1.json"
        value = json.loads(source.read_text(encoding="utf-8"))
        missing = self.REQUIRED - value.keys()
        if missing or value.get("schema_version") != "goal-contract-v1":
            raise ValueError(f"invalid Goal Contract; missing={sorted(missing)}")
        dimensions = value.get("quality_dimensions")
        if not isinstance(dimensions, dict) or not dimensions:
            raise ValueError("Goal Contract requires quality dimensions")
        if abs(sum(float(v.get("weight", 0)) for v in dimensions.values()) - 1.0) > 1e-6:
            raise ValueError("Goal Contract quality weights must total 1")
        return self.store.upsert_goal(value)
