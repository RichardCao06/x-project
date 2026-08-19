"""Public control-plane facade.

All interactive surfaces call this class instead of editing domain files.  It
keeps protocol validation, state transitions and audit events in one boundary.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .contracts import Job, JobState
from .contracts.models import utcnow
from .kernel import ArtifactStore, BudgetLedger, EventLedger, LeaseManager, StateStore


class ProtocolError(ValueError):
    pass


LEGAL_JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.PLANNED: frozenset({JobState.READY, JobState.SUPERSEDED, JobState.PAUSED}),
    JobState.READY: frozenset({JobState.LEASED, JobState.STALLED, JobState.QUARANTINED, JobState.BLOCKED_BUDGET, JobState.PAUSED}),
    JobState.LEASED: frozenset({JobState.RUNNING, JobState.READY, JobState.STALLED, JobState.PAUSED}),
    JobState.RUNNING: frozenset({JobState.DIAGNOSTIC_PREVIEW, JobState.EVIDENCE_LIMITED,
                                 JobState.CANDIDATE, JobState.STALLED, JobState.RETRYABLE,
                                 JobState.REPAIRABLE, JobState.MANUAL_REVIEW, JobState.FAILED, JobState.PAUSED}),
    JobState.DIAGNOSTIC_PREVIEW: frozenset({JobState.REPAIRABLE, JobState.PAUSED,
                                            JobState.SUPERSEDED}),
    JobState.EVIDENCE_LIMITED: frozenset({JobState.REPAIRABLE, JobState.PAUSED,
                                          JobState.SUPERSEDED}),
    JobState.STALLED: frozenset({JobState.READY, JobState.LEASED, JobState.REPAIRABLE,
                                 JobState.QUARANTINED, JobState.FAILED, JobState.PAUSED}),
    JobState.CANDIDATE: frozenset({JobState.GATED, JobState.REPAIRABLE, JobState.FAILED}),
    JobState.GATED: frozenset({JobState.APPLIED, JobState.REPAIRABLE, JobState.FAILED}),
    JobState.APPLIED: frozenset({JobState.PUBLISHED, JobState.REPAIRABLE, JobState.FAILED}),
    JobState.RETRYABLE: frozenset({JobState.READY, JobState.QUARANTINED, JobState.PAUSED}),
    JobState.REPAIRABLE: frozenset({JobState.READY, JobState.QUARANTINED, JobState.PAUSED}),
    JobState.BLOCKED_BUDGET: frozenset({JobState.READY, JobState.QUARANTINED, JobState.PAUSED}),
    JobState.MANUAL_REVIEW: frozenset({JobState.REPAIRABLE, JobState.READY, JobState.QUARANTINED, JobState.PAUSED}),
    JobState.PAUSED: frozenset({JobState.READY, JobState.REPAIRABLE, JobState.MANUAL_REVIEW,
                                JobState.RETRYABLE, JobState.BLOCKED_BUDGET,
                                JobState.DIAGNOSTIC_PREVIEW, JobState.EVIDENCE_LIMITED,
                                JobState.CANDIDATE}),
    # A terminal failure may be reopened only by an explicit operator repair;
    # ordinary worker execution never takes this edge automatically.
    JobState.FAILED: frozenset({JobState.REPAIRABLE}),
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
        from .kernel.governance_runtime import GovernanceRuntime
        self.governance = GovernanceRuntime(
            self.root, self.state, self.events, self.artifacts
        )

    def submit_job(self, job: Job, *, idempotency_key: str | None = None) -> tuple[str, bool]:
        if not job.target or not job.workflow or not job.policy_version or not job.input_hashes:
            raise ProtocolError("Job.v1 requires target, workflow, policy_version and input_hashes")
        if idempotency_key:
            row = self.state._connection().execute(
                "SELECT id FROM jobs WHERE json_extract(payload, '$.idempotency_key')=?", (idempotency_key,)
            ).fetchone()
            if row:
                stored = self.state.get("jobs", str(row["id"]))
                if stored is not None and self.governance.controller.binding(str(row["id"])) is None:
                    self.governance.bind_job(str(row["id"]), stored["payload"])
                return str(row["id"]), True
        try:
            self.governance.require_submission_mapping(job.workflow)
        except RuntimeError as exc:
            raise ProtocolError(str(exc)) from exc
        payload = asdict(job)
        payload["state"] = str(job.state)
        payload["idempotency_key"] = idempotency_key
        self.state.upsert_entity("jobs", job.job_id, str(job.state), payload, workflow_id=job.workflow)
        try:
            binding = self.governance.bind_job(job.job_id, payload)
        except RuntimeError as exc:
            raise ProtocolError(str(exc)) from exc
        if binding is not None:
            payload["governance"] = {
                "schema_version": "job-governance-binding-ref-v1",
                "binding_hash": binding["binding_hash"],
                "goal_ref": binding["goal_ref"],
                "autonomy_ref": binding["autonomy_ref"],
                "assurance_ref": binding["assurance_ref"],
                "capability_ref": binding["capability_ref"],
            }
            self.state.upsert_entity(
                "jobs", job.job_id, str(job.state), payload, workflow_id=job.workflow
            )
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

    def pause_job(self, job_id: str, *, reason: str = "operator requested pause") -> dict[str, Any]:
        row = self.state.get("jobs", job_id)
        if row is None:
            raise KeyError(job_id)
        current = JobState(row["status"])
        if current == JobState.PAUSED:
            return {"status": "already_paused", "job_id": job_id}
        if JobState.PAUSED not in LEGAL_JOB_TRANSITIONS.get(current, frozenset()):
            raise ProtocolError(f"Job cannot be paused from {current}")
        payload = dict(row["payload"])
        payload.update({"state": str(JobState.PAUSED), "paused_from": str(current),
                        "pause_reason": reason, "transition_reason": reason})
        run = None
        connection = self.state._connection()
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='orchestrator_runs'").fetchone():
            run = connection.execute(
                "SELECT run_id,status FROM orchestrator_runs WHERE job_id=? ORDER BY created_at DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if run:
                payload["paused_run_status"] = str(run["status"])
        self.state.upsert_entity("jobs", job_id, str(JobState.PAUSED), payload,
                                 program_id=row.get("program_id"), industry_id=row.get("industry_id"),
                                 workflow_id=row.get("workflow_id"))
        if run and run["status"] != "succeeded":
            with self.state.transaction() as conn:
                conn.execute("UPDATE orchestrator_runs SET status='paused',updated_at=? WHERE run_id=?",
                             (utcnow(), run["run_id"]))
        self.events.append("job", job_id, "job.paused", {
            "from": str(current), "reason": reason,
            "run_id": str(run["run_id"]) if run else None,
        }, actor="control-plane")
        return {"status": "paused", "job_id": job_id,
                "run_id": str(run["run_id"]) if run else None}

    def resume_job(self, job_id: str, *, reason: str = "operator resumed paused Job") -> dict[str, Any]:
        row = self.state.get("jobs", job_id)
        if row is None:
            raise KeyError(job_id)
        if JobState(row["status"]) != JobState.PAUSED:
            raise ProtocolError("only a paused Job can be resumed")
        payload = dict(row["payload"])
        previous = JobState(str(payload.get("paused_from") or JobState.READY))
        target = (JobState.READY if previous in {
            JobState.PLANNED, JobState.READY, JobState.LEASED, JobState.RUNNING, JobState.STALLED,
        } else previous)
        connection = self.state._connection()
        run = connection.execute(
            "SELECT run_id,status FROM orchestrator_runs WHERE job_id=? ORDER BY created_at DESC LIMIT 1",
            (job_id,),
        ).fetchone() if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orchestrator_runs'"
        ).fetchone() else None
        if run and connection.execute(
            "SELECT 1 FROM orchestrator_tasks WHERE run_id=? AND status='ready' LIMIT 1",
            (run["run_id"],),
        ).fetchone():
            target = JobState.READY
        if (run and run["status"] == "succeeded" and previous == JobState.CANDIDATE
                and ((payload.get("scope") or {}).get("request") or {}).get("publication_mode") == "preview"):
            target = JobState.CANDIDATE
        payload.update({"state": str(target), "resumed_from": str(previous),
                        "transition_reason": reason})
        self.state.upsert_entity("jobs", job_id, str(target), payload,
                                 program_id=row.get("program_id"), industry_id=row.get("industry_id"),
                                 workflow_id=row.get("workflow_id"))
        if run and run["status"] == "paused":
            with self.state.transaction() as conn:
                conn.execute("UPDATE orchestrator_runs SET status=?,updated_at=? WHERE run_id=?",
                             ("ready" if target == JobState.READY else str(payload.get("paused_run_status") or "ready"),
                              utcnow(), run["run_id"]))
        self.events.append("job", job_id, "job.resumed", {
            "to": str(target), "reason": reason,
            "run_id": str(run["run_id"]) if run else None,
        }, actor="control-plane")
        return {"status": "resumed", "job_id": job_id, "job_status": str(target),
                "run_id": str(run["run_id"]) if run else None}

    def status(self) -> dict[str, Any]:
        conn = self.state._connection()
        tables = ("programs", "industries", "jobs", "runs", "artifacts", "events", "exceptions", "releases")
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        job_states = {row["status"]: row["n"] for row in conn.execute("SELECT status,COUNT(*) AS n FROM jobs GROUP BY status")}
        return {"root": str(self.root), "counts": counts, "job_states": job_states}
