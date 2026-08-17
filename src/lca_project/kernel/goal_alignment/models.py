"""Versioned records shared by the alignment plane."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class QualityObservation:
    job_id: str
    run_id: str | None
    goal_id: str
    dimensions: dict[str, float]
    score: float
    evidence: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "quality-observation-v1"

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Deviation:
    deviation_type: str
    severity: str
    evidence: dict[str, Any]
    summary: str


@dataclass(frozen=True)
class Diagnosis:
    cause_code: str
    confidence: float
    evidence: dict[str, Any]
    explanation: str


@dataclass(frozen=True)
class RepairProposal:
    level: str
    action: str
    authority: str
    invalidates: tuple[str, ...]
    preserves: tuple[str, ...]
    validation: tuple[str, ...]
    automatic: bool
