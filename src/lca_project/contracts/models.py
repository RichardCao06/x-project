"""Small dependency-free contracts shared by the control and execution planes."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import json
from pathlib import Path
from typing import Any
from uuid import uuid4


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be an object")
    return value


class JobState(StrEnum):
    PLANNED = "planned"; READY = "ready"; LEASED = "leased"; RUNNING = "running"
    CANDIDATE = "candidate"; GATED = "gated"; APPLIED = "applied"; PUBLISHED = "published"
    RETRYABLE = "retryable"; REPAIRABLE = "repairable"; QUARANTINED = "quarantined"
    BLOCKED_BUDGET = "blocked_budget"; FAILED = "failed"; SUPERSEDED = "superseded"


class RunStatus(StrEnum):
    STARTED = "started"; SUCCEEDED = "succeeded"; FAILED = "failed"; CANCELLED = "cancelled"; TIMED_OUT = "timed_out"


class GateStatus(StrEnum):
    PASS = "pass"; FAIL = "fail"; BLOCKED = "blocked"; NOT_RUN = "not_run"


class ArtifactKind(StrEnum):
    INPUT = "input"; DOSSIER = "dossier"; PROPOSAL = "proposal"; CANDIDATE = "candidate"
    GATE_REPORT = "gate_report"; DECISION = "decision"; RELEASE = "release"; LOG = "log"


@dataclass(frozen=True)
class Job:
    target: str
    workflow: str
    scope: dict[str, Any]
    policy_version: str
    input_hashes: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    budget: dict[str, int] = field(default_factory=dict)
    risk: str = "standard"
    job_id: str = field(default_factory=lambda: f"job_{uuid4().hex}")
    schema_version: str = "job-v1"
    state: JobState = JobState.PLANNED
    created_at: str = field(default_factory=utcnow)

    def asdict(self) -> dict[str, Any]: return asdict(self)


@dataclass(frozen=True)
class Run:
    job_id: str; executor: str; attempt: int
    run_id: str = field(default_factory=lambda: f"run_{uuid4().hex}")
    status: RunStatus = RunStatus.STARTED
    started_at: str = field(default_factory=utcnow)
    ended_at: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    attestation: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "run-v1"


@dataclass(frozen=True)
class Artifact:
    kind: ArtifactKind; schema: str; sha256: str; uri: str; producer: str
    lineage: tuple[str, ...] = ()
    artifact_id: str = field(default_factory=lambda: f"art_{uuid4().hex}")
    created_at: str = field(default_factory=utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "artifact-v1"


@dataclass(frozen=True)
class Event:
    event_type: str; producer: str; target: str; payload: dict[str, Any]
    event_id: str = field(default_factory=lambda: f"evt_{uuid4().hex}")
    causation_id: str | None = None; correlation_id: str | None = None
    occurred_at: str = field(default_factory=utcnow)
    schema_version: str = "event-v1"


@dataclass(frozen=True)
class GateResult:
    gate_id: str; scope: str; status: GateStatus; input_hashes: tuple[str, ...]; policy_version: str
    findings: tuple[dict[str, Any], ...] = ()
    result_id: str = field(default_factory=lambda: f"gate_{uuid4().hex}")
    schema_version: str = "gate-result-v1"


@dataclass(frozen=True)
class Decision:
    scope: str; decision: str; rationale: str; evidence: tuple[str, ...]
    decision_id: str = field(default_factory=lambda: f"dec_{uuid4().hex}")
    supersedes: str | None = None; policy_version: str = "governance-v1"
    schema_version: str = "decision-v1"


@dataclass(frozen=True)
class CapabilityManifest:
    capability_id: str; version: str; executor: str; entrypoint: str
    input_schema: str; output_schema: str
    side_effects: str = "none"; idempotent: bool = True
    timeout_seconds: int = 600; permissions: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilityManifest":
        required = {"capability_id", "version", "executor", "entrypoint", "input_schema", "output_schema"}
        missing = required - value.keys()
        if missing: raise ValueError(f"capability missing {sorted(missing)}")
        return cls(**{k: value[k] for k in cls.__dataclass_fields__ if k in value})


@dataclass(frozen=True)
class WorkflowSpec:
    workflow_id: str; version: str; description: str; steps: tuple[dict[str, Any], ...]
    policy_version: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkflowSpec":
        required = {"workflow_id", "version", "description", "steps", "policy_version"}
        missing = required - value.keys()
        if missing: raise ValueError(f"workflow missing {sorted(missing)}")
        result = cls(**{**value, "steps": tuple(value["steps"])})
        ids = [step.get("id") for step in result.steps]
        if not all(ids) or len(ids) != len(set(ids)): raise ValueError("workflow step ids must be unique")
        known = set(ids)
        if any(set(step.get("needs", [])) - known for step in result.steps): raise ValueError("workflow references unknown dependency")
        return result
