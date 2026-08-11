"""Public control-plane facade.

All interactive surfaces call this class instead of editing domain files.  It
keeps protocol validation, state transitions and audit events in one boundary.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .contracts import Job, JobState
from .kernel import ArtifactStore, BudgetLedger, EventLedger, LeaseManager, StateStore


class ProtocolError(ValueError):
    pass


LEGAL_JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.PLANNED: frozenset({JobState.READY, JobState.SUPERSEDED}),
    JobState.READY: frozenset({JobState.LEASED, JobState.QUARANTINED, JobState.BLOCKED_BUDGET}),
    JobState.LEASED: frozenset({JobState.RUNNING, JobState.READY}),
    JobState.RUNNING: frozenset({JobState.CANDIDATE, JobState.RETRYABLE, JobState.REPAIRABLE, JobState.FAILED}),
    JobState.CANDIDATE: frozenset({JobState.GATED, JobState.REPAIRABLE, JobState.FAILED}),
    JobState.GATED: frozenset({JobState.APPLIED, JobState.REPAIRABLE, JobState.FAILED}),
    JobState.APPLIED: frozenset({JobState.PUBLISHED, JobState.REPAIRABLE, JobState.FAILED}),
    JobState.RETRYABLE: frozenset({JobState.READY, JobState.QUARANTINED}),
    JobState.REPAIRABLE: frozenset({JobState.READY, JobState.QUARANTINED}),
    JobState.BLOCKED_BUDGET: frozenset({JobState.READY, JobState.QUARANTINED}),
}


class ControlPlane:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        runtime = self.root / "var"
        runtime.mkdir(parents=True, exist_ok=True)
        self.state = StateStore(runtime / "state.db")
        self.artifacts = ArtifactStore(runtime / "artifacts", self.state)
        self.events = EventLedger(self.state)
        self.leases = LeaseManager(self.state)
        self.budgets = BudgetLedger(self.state)

    def submit_job(self, job: Job, *, idempotency_key: str | None = None) -> tuple[str, bool]:
        if not job.target or not job.workflow or not job.policy_version or not job.input_hashes:
            raise ProtocolError("Job.v1 requires target, workflow, policy_version and input_hashes")
        if idempotency_key:
            row = self.state._connection().execute(
                "SELECT id FROM jobs WHERE json_extract(payload, '$.idempotency_key')=?", (idempotency_key,)
            ).fetchone()
            if row:
                return str(row["id"]), True
        payload = asdict(job)
        payload["state"] = str(job.state)
        payload["idempotency_key"] = idempotency_key
        self.state.upsert_entity("jobs", job.job_id, str(job.state), payload, workflow_id=job.workflow)
        self.events.append("job", job.job_id, "job.submitted", payload, actor="control-plane")
        return job.job_id, False

    def transition_job(self, job_id: str, target: JobState, *, reason: str, fencing_token: int | None = None) -> None:
        row = self.state.get("jobs", job_id)
        if row is None:
            raise KeyError(job_id)
        current = JobState(row["status"])
        if target not in LEGAL_JOB_TRANSITIONS.get(current, frozenset()):
            raise ProtocolError(f"illegal Job transition: {current} -> {target}")
        payload = dict(row["payload"])
        payload.update({"state": str(target), "transition_reason": reason})
        if fencing_token is not None:
            payload["fencing_token"] = fencing_token
        self.state.upsert_entity(
            "jobs", job_id, str(target), payload,
            program_id=row.get("program_id"), industry_id=row.get("industry_id"), workflow_id=row.get("workflow_id"),
        )
        self.events.append("job", job_id, "job.transitioned", {"from": str(current), "to": str(target), "reason": reason}, actor="control-plane")

    def status(self) -> dict[str, Any]:
        conn = self.state._connection()
        tables = ("programs", "industries", "jobs", "runs", "artifacts", "events", "exceptions", "releases")
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        job_states = {row["status"]: row["n"] for row in conn.execute("SELECT status,COUNT(*) AS n FROM jobs GROUP BY status")}
        return {"root": str(self.root), "counts": counts, "job_states": job_states}
