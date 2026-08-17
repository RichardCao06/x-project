"""Executable baseline and reliability verification reports for Wiki optimization."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
from typing import Any

from .orchestrator import PersistentOrchestrator
from .state import utcnow


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class OptimizationVerifier:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.orchestrator = PersistentOrchestrator(self.root)
        self.state = self.orchestrator.control.state
        self.artifacts = self.orchestrator.control.artifacts

    def freeze_baseline(self, *, job_id: str | None = None) -> tuple[dict[str, Any], str]:
        conn = self.state._connection()
        job = self.state.get("jobs", job_id) if job_id else None
        if job_id and job is None:
            raise KeyError(job_id)
        runs = [dict(row) for row in conn.execute(
            "SELECT run_id,job_id,workflow_ref,status,created_at,updated_at "
            "FROM orchestrator_runs" + (" WHERE job_id=?" if job_id else "") + " ORDER BY created_at",
            ((job_id,) if job_id else ()),
        )]
        workspace_manifests: list[dict[str, str]] = []
        workspace_root = self.root / "var/workspaces/jobs"
        candidates = ([workspace_root / job_id] if job_id else
                      list(workspace_root.iterdir()) if workspace_root.is_dir() else [])
        for workspace in candidates:
            manifest = workspace / "workspace-manifest.json"
            if manifest.is_file():
                workspace_manifests.append({
                    "job_id": workspace.name, "path": str(manifest.relative_to(self.root)),
                    "sha256": _digest(manifest),
                })
        policy_hashes = {
            path.name: _digest(path) for path in sorted((self.root / "policies").glob("*.json"))
        }
        schema_version = conn.execute(
            "SELECT COALESCE(MAX(version),0) AS version FROM schema_migrations"
        ).fetchone()["version"]
        report = {
            "protocol": "wiki-optimization-baseline-v1", "created_at": utcnow(),
            "environment": {"python": platform.python_version(), "platform": platform.platform()},
            "scope": {"job_id": job_id}, "job": job, "runs": runs,
            "schema_version": schema_version, "policy_hashes": policy_hashes,
            "workspace_manifests": workspace_manifests,
            "counts": {
                table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                for table in ("jobs", "orchestrator_runs", "orchestrator_tasks",
                              "orchestrator_attempts", "events", "artifacts", "worker_instances")
            },
        }
        artifact = self.artifacts.put_json(
            report, metadata={"schema": "wiki-optimization-baseline-v1", "job_id": job_id}
        )
        return report, artifact.digest

    def verify(self, *, job_id: str | None = None) -> tuple[dict[str, Any], str]:
        conn = self.state._connection()
        params: tuple[Any, ...] = ()
        where = ""
        if job_id:
            if self.state.get("jobs", job_id) is None:
                raise KeyError(job_id)
            where = " AND r.job_id=?"
            params = (job_id,)
        attempts = [dict(row) for row in conn.execute("""
            SELECT a.*,r.job_id FROM orchestrator_attempts a
            JOIN orchestrator_runs r ON r.run_id=a.run_id WHERE 1=1
        """ + where, params)]
        owned_successes = [row for row in attempts
                           if row["worker_id"] is not None and row["status"] == "succeeded"]
        manifest_errors: list[dict[str, str]] = []
        lineage_errors: list[dict[str, Any]] = []
        replayable = 0
        for row in owned_successes:
            digest = str(row.get("output_manifest_hash") or "")
            try:
                if not digest:
                    raise ValueError("missing output manifest")
                manifest = self.artifacts.verify_task_output_manifest(digest)
                relations = {item.get("relation") for item in manifest.get("lineage", [])}
                required = {"task_input", "capability_manifest", "workflow_binding",
                            "production_policy", "repair_policy", "node_profile",
                            "workspace_manifest"}
                missing = sorted(required - relations)
                if missing:
                    lineage_errors.append({"attempt_id": row["attempt_id"], "missing": missing})
                replayable += 1
            except (KeyError, ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
                manifest_errors.append({"attempt_id": row["attempt_id"], "error": str(exc)})
        failures = [row for row in attempts if row["status"] in {
            "repairable", "quarantined", "failed", "abandoned"
        }]
        unstructured: list[str] = []
        noninfra_process_exit: list[str] = []
        for row in failures:
            try:
                payload = json.loads(row.get("failure_payload") or "")
            except json.JSONDecodeError:
                payload = {}
            if not row.get("failure_code") or payload.get("code") != row.get("failure_code"):
                unstructured.append(str(row["attempt_id"]))
            if row.get("failure_code") == "PROCESS_EXIT" and payload.get("category") != "infrastructure":
                noninfra_process_exit.append(str(row["attempt_id"]))
        contradictions = [dict(row) for row in conn.execute("""
            SELECT a.attempt_id,a.status AS attempt_status,t.status AS task_status
            FROM orchestrator_attempts a JOIN orchestrator_tasks t
              ON t.run_id=a.run_id AND t.task_id=a.task_id
            WHERE (a.status='running' AND t.status!='running')
               OR (a.status!='running' AND t.status='running' AND NOT EXISTS(
                 SELECT 1 FROM orchestrator_attempts active
                 WHERE active.run_id=t.run_id AND active.task_id=t.task_id AND active.status='running'))
        """)]
        duplicate_requeues = [dict(row) for row in conn.execute("""
            SELECT json_extract(payload,'$.attempt_id') AS attempt_id,COUNT(*) AS n
            FROM events WHERE event_type='task.requeued'
            GROUP BY json_extract(payload,'$.attempt_id') HAVING COUNT(*)>1
        """)]
        reused_attempts = [row for row in attempts if row["status"] == "reused"]
        reuse_receipts = {row["reused_attempt_id"]: dict(row) for row in conn.execute(
            "SELECT * FROM task_reuse_receipts"
        )}
        missing_reuse_receipts: list[str] = []
        for row in reused_attempts:
            receipt = reuse_receipts.get(row["attempt_id"])
            try:
                if receipt is None:
                    raise ValueError("receipt row is missing")
                document = json.loads(self.artifacts.get_bytes(receipt["receipt_hash"]))
                if (document.get("effective_input_hash") != row["effective_input_hash"]
                        or document.get("output_manifest_hash") != row["output_manifest_hash"]):
                    raise ValueError("receipt does not bind effective input and output")
            except (KeyError, ValueError, OSError, json.JSONDecodeError):
                missing_reuse_receipts.append(str(row["attempt_id"]))
        frozen_rate = replayable / len(owned_successes) if owned_successes else 1.0
        lineage_rate = ((len(owned_successes) - len(lineage_errors)) / len(owned_successes)
                        if owned_successes else 1.0)
        structured_rate = ((len(failures) - len(unstructured)) / len(failures)
                           if failures else 1.0)
        violations = {
            "manifest_errors": manifest_errors,
            "lineage_errors": lineage_errors,
            "unstructured_failures": unstructured,
            "non_infrastructure_process_exit": noninfra_process_exit,
            "state_contradictions": contradictions,
            "duplicate_watchdog_requeues": duplicate_requeues,
            "missing_reuse_receipts": missing_reuse_receipts,
        }
        passed = all(not value for value in violations.values())
        has_samples = bool(attempts)
        status = "pass" if passed and has_samples else "insufficient_data" if passed else "fail"
        report = {
            "protocol": "wiki-optimization-verification-v1", "created_at": utcnow(),
            "scope": {"job_id": job_id}, "status": status,
            "sample_coverage": {"attempts": len(attempts),
                                "formal_acceptance_eligible": has_samples},
            "metrics": {
                "A1_task_output_cas_freeze_rate": frozen_rate,
                "A2_offline_replay_rate": frozen_rate,
                "A3_manifest_hash_validation_rate": frozen_rate,
                "A6_artifact_lineage_complete_rate": lineage_rate,
                "A5_reuse_receipt_complete_rate": (
                    (len(reused_attempts) - len(missing_reuse_receipts)) / len(reused_attempts)
                    if reused_attempts else 1.0
                ),
                "F1_structured_failure_coverage_rate": structured_rate,
                "F2_non_infrastructure_process_exit_count": len(noninfra_process_exit),
                "R7_state_contradiction_count": len(contradictions),
                "R8_duplicate_watchdog_requeue_count": len(duplicate_requeues),
                "owned_succeeded_attempts": len(owned_successes),
                "classified_failure_attempts": len(failures) - len(unstructured),
            },
            "violations": violations,
        }
        artifact = self.artifacts.put_json(
            report, metadata={"schema": "wiki-optimization-verification-v1", "job_id": job_id}
        )
        return report, artifact.digest

    def diagnose_job(self, job_id: str) -> dict[str, Any]:
        job = self.state.get("jobs", job_id)
        if job is None:
            raise KeyError(job_id)
        conn = self.state._connection()
        run = conn.execute("SELECT * FROM orchestrator_runs WHERE job_id=?", (job_id,)).fetchone()
        tasks: list[dict[str, Any]] = []
        attempts: list[dict[str, Any]] = []
        worker = lease = failure = dry_run = None
        if run is not None:
            tasks = [dict(row) for row in conn.execute(
                "SELECT * FROM orchestrator_tasks WHERE run_id=? ORDER BY rowid", (run["run_id"],)
            )]
            attempts = [dict(row) for row in conn.execute(
                "SELECT * FROM orchestrator_attempts WHERE run_id=? ORDER BY started_at DESC LIMIT 20",
                (run["run_id"],),
            )]
            active = next((row for row in attempts if row["status"] == "running"), None)
            latest = attempts[0] if attempts else None
            owner = active or latest
            if owner and owner.get("worker_id"):
                row = conn.execute("SELECT * FROM worker_instances WHERE worker_id=?",
                                   (owner["worker_id"],)).fetchone()
                worker = dict(row) if row else None
            if active and active.get("lease_resource"):
                row = conn.execute("SELECT * FROM leases WHERE resource=?",
                                   (active["lease_resource"],)).fetchone()
                lease = dict(row) if row else None
            if latest and latest.get("failure_payload"):
                failure = json.loads(latest["failure_payload"])
                decision = (failure.get("policy_decision") or {})
                dry_run = self.orchestrator.repair_dry_run(
                    str(run["run_id"]), str(latest["task_id"]),
                    policy_invalidates=tuple(decision.get("invalidates", [])),
                    policy_preserves=tuple(decision.get("preserves", [])),
                )
        return {"protocol": "wiki-job-diagnosis-v1", "generated_at": utcnow(),
                "job": job, "run": dict(run) if run else None, "tasks": tasks,
                "attempts": attempts, "worker": worker, "lease": lease,
                "latest_failure": failure, "repair_dry_run": dry_run}
