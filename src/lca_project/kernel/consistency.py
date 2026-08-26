"""Durable cross-stage outcome, artifact-generation, and recovery ledgers.

The workflow DAG remains the execution authority.  This module supplies the
orthogonal consistency facts needed to answer three different questions:

* did a stage execute successfully;
* did its Gate authorize progress; and
* did the result move the declared Job goal forward.

It also prevents a mutable workspace path from silently changing owners across
stages.  Every materialized file is generation-addressed and a replacement is
accepted only from the same producer or one of its DAG descendants.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .state import StateStore, utcnow


EXECUTION_STATUSES = {
    "completed", "completed_with_block", "failed", "skipped", "reused",
}
GATE_DECISIONS = {
    "PASS", "PASS_WITH_DEBT", "RESEARCH_MORE", "EVIDENCE_LIMITED",
    "BLOCKED_INTEGRITY", "BLOCKED", "NOT_APPLICABLE",
}
GOAL_EFFECTS = {
    "progress", "progress_with_debt", "no_effect", "blocked", "regression",
}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class ConsistencyError(RuntimeError):
    """A transition would make the cross-stage state internally inconsistent."""


class ConsistencyLedger:
    """Write consistency projections inside their caller's SQLite transaction."""

    def __init__(self, state: StateStore) -> None:
        self.state = state

    @staticmethod
    def _binding_generation(conn: Any, run_id: str, task_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(generation),0) AS generation "
            "FROM task_binding_generations WHERE run_id=? AND task_id=?",
            (run_id, task_id),
        ).fetchone()
        return int(row["generation"] if row else 0)

    @staticmethod
    def _job_id(conn: Any, run_id: str) -> str:
        row = conn.execute(
            "SELECT job_id FROM orchestrator_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ConsistencyError(f"workflow run not found: {run_id}")
        return str(row["job_id"])

    def record_stage_outcome(
        self,
        conn: Any,
        *,
        run_id: str,
        task_id: str,
        attempt_id: str | None,
        execution_status: str,
        gate_decision: str,
        goal_effect: str,
        failure_fingerprint: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        decision = str(gate_decision or "NOT_APPLICABLE").upper()
        if execution_status not in EXECUTION_STATUSES:
            raise ConsistencyError(f"invalid execution status: {execution_status}")
        if decision not in GATE_DECISIONS:
            # Gate adapters sometimes expose domain verbs such as GO/ACCEPT.
            # Preserve their raw value in payload while normalising the state
            # axis to the stable cross-stage vocabulary.
            decision = "PASS" if decision in {
                "GO", "APPROVED", "ACCEPT", "ACCEPT_WITH_ADVISORIES",
            } else "BLOCKED"
        if goal_effect not in GOAL_EFFECTS:
            raise ConsistencyError(f"invalid goal effect: {goal_effect}")
        job_id = self._job_id(conn, run_id)
        generation = self._binding_generation(conn, run_id, task_id)
        envelope = {
            "schema_version": "stage-outcome-v1",
            "job_id": job_id,
            "run_id": run_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "binding_generation": generation,
            "causal_generation": generation,
            "execution": execution_status,
            "decision": decision,
            "goal_effect": goal_effect,
            "failure_fingerprint": failure_fingerprint,
            "detail": payload or {},
        }
        outcome_id = "sto_" + digest(envelope)[:32]
        conn.execute(
            "INSERT INTO stage_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id,task_id,attempt_id) DO UPDATE SET "
            "execution_status=excluded.execution_status,"
            "gate_decision=excluded.gate_decision,goal_effect=excluded.goal_effect,"
            "failure_fingerprint=excluded.failure_fingerprint,payload=excluded.payload",
            (
                outcome_id, job_id, run_id, task_id, attempt_id, generation,
                execution_status, decision, goal_effect, failure_fingerprint,
                generation, canonical(envelope), utcnow(),
            ),
        )
        return outcome_id

    @staticmethod
    def _descends_from(conn: Any, run_id: str, task_id: str, ancestor: str) -> bool:
        if task_id == ancestor:
            return True
        rows = {
            str(row["task_id"]): set(json.loads(row["dependencies"]))
            for row in conn.execute(
                "SELECT task_id,dependencies FROM orchestrator_tasks WHERE run_id=?",
                (run_id,),
            )
        }
        frontier = list(rows.get(task_id, set()))
        seen: set[str] = set()
        while frontier:
            current = frontier.pop()
            if current == ancestor:
                return True
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(rows.get(current, set()))
        return False

    @staticmethod
    def _successors(conn: Any, run_id: str, task_id: str) -> list[str]:
        return sorted(
            str(row["task_id"])
            for row in conn.execute(
                "SELECT task_id,dependencies FROM orchestrator_tasks WHERE run_id=?",
                (run_id,),
            )
            if task_id in set(json.loads(row["dependencies"]))
        )

    def record_artifact_manifest(
        self,
        conn: Any,
        *,
        run_id: str,
        task_id: str,
        attempt_id: str,
        manifest_digest: str,
        manifest: dict[str, Any],
    ) -> list[str]:
        """Record every logical output and reject unauthorised path takeover."""
        job_id = self._job_id(conn, run_id)
        recorded: list[str] = []
        successors = self._successors(conn, run_id, task_id)
        for item in manifest.get("files") or []:
            logical_path = str(item.get("path") or "").strip()
            output_sha = str(item.get("sha256") or "").strip()
            if not logical_path or len(output_sha) != 64:
                raise ConsistencyError("task output manifest has an invalid logical file")
            prior = conn.execute(
                "SELECT * FROM artifact_generations "
                "WHERE job_id=? AND logical_path=? AND status='current'",
                (job_id, logical_path),
            ).fetchone()
            if prior is not None and not self._descends_from(
                conn, run_id, task_id, str(prior["task_id"])
            ):
                raise ConsistencyError(
                    "logical artifact path takeover is not authorised by the DAG: "
                    f"{logical_path} ({prior['task_id']} -> {task_id})"
                )
            generation = int(prior["generation"]) + 1 if prior else 1
            if prior is not None:
                conn.execute(
                    "UPDATE artifact_generations SET status='superseded' "
                    "WHERE generation_id=? AND status='current'",
                    (prior["generation_id"],),
                )
            semantic_identity = digest({"job_id": job_id, "logical_path": logical_path})
            value = {
                "schema_version": "artifact-generation-v1",
                "job_id": job_id,
                "run_id": run_id,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "logical_path": logical_path,
                "generation": generation,
                "base_generation": int(prior["generation"]) if prior else None,
                "base_sha256": str(prior["output_sha256"]) if prior else None,
                "output_sha256": output_sha,
                "semantic_identity": semantic_identity,
                "authorized_successors": successors,
                "proof_digest": manifest_digest,
                "role": str(item.get("role") or "protocol_artifact"),
            }
            generation_id = "arg_" + digest(value)[:32]
            conn.execute(
                "INSERT INTO artifact_generations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    generation_id, job_id, run_id, task_id, attempt_id,
                    logical_path, generation, "current",
                    int(prior["generation"]) if prior else None,
                    str(prior["output_sha256"]) if prior else None,
                    output_sha, semantic_identity, canonical(successors),
                    manifest_digest, canonical(value), utcnow(),
                ),
            )
            recorded.append(generation_id)
        return recorded

    @staticmethod
    def stale_task_artifacts(conn: Any, run_id: str, task_ids: set[str]) -> int:
        if not task_ids:
            return 0
        placeholders = ",".join("?" for _ in task_ids)
        cursor = conn.execute(
            "UPDATE artifact_generations SET status='stale' "
            f"WHERE run_id=? AND task_id IN ({placeholders}) AND status='current'",
            (run_id, *sorted(task_ids)),
        )
        return int(cursor.rowcount)

    @staticmethod
    def append_event(
        conn: Any,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor: str,
    ) -> None:
        conn.execute(
            "INSERT INTO events(event_id,aggregate_type,aggregate_id,event_type,payload,"
            "trace_id,actor,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), aggregate_type, aggregate_id, event_type,
                canonical(payload), None, actor, utcnow(),
            ),
        )

    def assert_run_invariants(self, run_id: str) -> list[str]:
        """Return actionable invariant violations without mutating runtime state."""
        conn = self.state._connection()
        run = conn.execute(
            "SELECT * FROM orchestrator_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            return ["run_missing"]
        job = conn.execute("SELECT status FROM jobs WHERE id=?", (run["job_id"],)).fetchone()
        violations: list[str] = []
        ready = conn.execute(
            "SELECT COUNT(*) FROM orchestrator_tasks WHERE run_id=? AND status='ready'",
            (run_id,),
        ).fetchone()[0]
        if ready and job and str(job["status"]) in {
            "published", "failed", "quarantined", "superseded",
        }:
            violations.append("ready_task_with_terminal_job")
        running_without_lease = conn.execute(
            "SELECT COUNT(*) FROM orchestrator_attempts a "
            "LEFT JOIN leases l ON l.resource=a.lease_resource "
            "AND l.holder=a.worker_id AND l.fencing_token=a.fencing_token "
            "AND l.expires_at>? WHERE a.run_id=? AND a.status='running' "
            "AND (a.worker_id IS NULL OR l.resource IS NULL)",
            (utcnow(), run_id),
        ).fetchone()[0]
        if running_without_lease:
            violations.append("running_attempt_without_live_fenced_lease")
        if job and str(job["status"]) == "published":
            publish = conn.execute(
                "SELECT status,output_hash FROM orchestrator_tasks "
                "WHERE run_id=? AND task_id='publish'", (run_id,),
            ).fetchone()
            if not publish or publish["status"] != "succeeded" or not publish["output_hash"]:
                violations.append("published_without_publish_proof")
        return violations


__all__ = ["ConsistencyError", "ConsistencyLedger"]
