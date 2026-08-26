"""SQLite-backed workflow materialisation and recovery.

The orchestrator is intentionally deterministic: it persists task state and
attempts, while capability and agent executors only return immutable outputs.
No conversation owns the next-step decision.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import uuid
from typing import Any

from lca_project.contracts import JobState, load_json
from lca_project.control import ControlPlane
from .consistency import ConsistencyLedger, canonical as canonical_json, digest as consistency_digest
from .registry import CapabilityRegistry
from .state import utcnow
from .workflow import WorkflowSpec, compile_workflow


class OrchestratorError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskRecord:
    run_id: str
    task_id: str
    capability_id: str
    status: str
    attempt: int
    output_hash: str | None
    inputs: dict[str, Any]


class PersistentOrchestrator:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.control = ControlPlane(self.root)
        self.state = self.control.state
        self.consistency = ConsistencyLedger(self.state)
        self.registry = CapabilityRegistry.load_directory(self.root / "capabilities")
        self._initialize()

    def _initialize(self) -> None:
        self.state._connection().executescript("""
        CREATE TABLE IF NOT EXISTS orchestrator_runs(
          run_id TEXT PRIMARY KEY, job_id TEXT UNIQUE NOT NULL, workflow_ref TEXT NOT NULL,
          status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(job_id) REFERENCES jobs(id));
        CREATE TABLE IF NOT EXISTS orchestrator_tasks(
          run_id TEXT NOT NULL, task_id TEXT NOT NULL, capability_id TEXT NOT NULL,
          dependencies TEXT NOT NULL, status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
          output_hash TEXT, failure_code TEXT, failure_payload TEXT,
          updated_at TEXT NOT NULL, inputs TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY(run_id,task_id), FOREIGN KEY(run_id) REFERENCES orchestrator_runs(run_id));
        CREATE TABLE IF NOT EXISTS orchestrator_attempts(
          attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
          attempt INTEGER NOT NULL, status TEXT NOT NULL, input_hashes TEXT NOT NULL,
          output_hash TEXT, failure_code TEXT, failure_payload TEXT,
          started_at TEXT NOT NULL, finished_at TEXT,
          worker_id TEXT, lease_resource TEXT, fencing_token INTEGER, output_manifest_hash TEXT,
          input_artifact_manifest_hash TEXT, capability_version_hash TEXT,
          workflow_task_binding_hash TEXT, workspace_manifest_hash TEXT,
          policy_hash TEXT, profile_hash TEXT, effective_input_hash TEXT,
          UNIQUE(run_id,task_id,attempt));
        CREATE TABLE IF NOT EXISTS task_reuse_receipts(
          receipt_hash TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
          reused_attempt_id TEXT NOT NULL, source_attempt_id TEXT NOT NULL,
          effective_input_hash TEXT NOT NULL, output_manifest_hash TEXT NOT NULL,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS task_binding_generations(
          run_id TEXT NOT NULL, task_id TEXT NOT NULL, generation INTEGER NOT NULL,
          status TEXT NOT NULL, effective_input_hash TEXT, prior_output_hash TEXT,
          reason TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(run_id,task_id,generation));
        CREATE TABLE IF NOT EXISTS task_repair_epochs(
          run_id TEXT NOT NULL, task_id TEXT NOT NULL, epoch INTEGER NOT NULL,
          base_attempt INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(run_id,task_id,epoch));
        """)
        columns = {row["name"] for row in self.state._connection().execute("PRAGMA table_info(orchestrator_tasks)")}
        if "inputs" not in columns:
            self.state._connection().execute(
                "ALTER TABLE orchestrator_tasks ADD COLUMN inputs TEXT NOT NULL DEFAULT '{}'"
            )
        if "failure_payload" not in columns:
            self.state._connection().execute(
                "ALTER TABLE orchestrator_tasks ADD COLUMN failure_payload TEXT"
            )
        attempt_columns = {
            row["name"] for row in self.state._connection().execute(
                "PRAGMA table_info(orchestrator_attempts)"
            )
        }
        if "failure_payload" not in attempt_columns:
            self.state._connection().execute(
                "ALTER TABLE orchestrator_attempts ADD COLUMN failure_payload TEXT"
            )
        for declaration in (
            "input_artifact_manifest_hash TEXT", "capability_version_hash TEXT",
            "workflow_task_binding_hash TEXT", "workspace_manifest_hash TEXT",
            "policy_hash TEXT", "profile_hash TEXT", "effective_input_hash TEXT",
        ):
            name = declaration.split()[0]
            if name not in attempt_columns:
                self.state._connection().execute(
                    f"ALTER TABLE orchestrator_attempts ADD COLUMN {declaration}"
                )
        # Backfill executable bindings for runs created before task inputs were
        # persisted.  The workflow file is authoritative and version-pinned by
        # orchestrator_runs.workflow_ref.
        for run in self.state._connection().execute("SELECT run_id,workflow_ref FROM orchestrator_runs"):
            workflow_path = self.root / "workflows" / f"{run['workflow_ref']}.json"
            if not workflow_path.is_file():
                continue
            spec = WorkflowSpec.from_mapping(load_json(workflow_path))
            with self.state.transaction() as conn:
                for task in spec.tasks:
                    conn.execute(
                        """UPDATE orchestrator_tasks SET inputs=?
                           WHERE run_id=? AND task_id=? AND inputs='{}'""",
                        (json.dumps(task.inputs, sort_keys=True), run["run_id"], task.id),
                    )

    def materialize(self, job_id: str) -> str:
        job = self.state.get("jobs", job_id)
        if job is None:
            raise KeyError(job_id)
        existing = self.state._connection().execute(
            "SELECT run_id FROM orchestrator_runs WHERE job_id=?", (job_id,)).fetchone()
        if existing:
            return str(existing["run_id"])
        workflow_ref = str(job["workflow_id"])
        path = self.root / "workflows" / f"{workflow_ref}.json"
        if not path.is_file():
            raise OrchestratorError(f"workflow not found: {workflow_ref}")
        spec = WorkflowSpec.from_mapping(load_json(path))
        compiled = compile_workflow(spec, {item.id for item in self.registry.all()})
        run_id, now = f"run_{uuid.uuid4().hex}", utcnow()
        with self.state.transaction() as conn:
            conn.execute("INSERT INTO orchestrator_runs VALUES(?,?,?,?,?,?)",
                         (run_id, job_id, workflow_ref, "ready", now, now))
            for task in spec.tasks:
                conn.execute("""INSERT INTO orchestrator_tasks
                    (run_id,task_id,capability_id,dependencies,status,attempt,output_hash,
                     failure_code,failure_payload,updated_at,inputs)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                    run_id, task.id, task.capability, json.dumps(list(task.depends_on)),
                    "pending", 0, None, None, None, now, json.dumps(task.inputs, sort_keys=True)))
                conn.execute("INSERT INTO task_binding_generations VALUES(?,?,?,?,?,?,?,?)", (
                    run_id, task.id, 1, "active", None, None, "workflow materialized", now))
        if job["status"] == str(JobState.PLANNED):
            self.control.transition_job(job_id, JobState.READY, reason="workflow materialized")
        self._refresh_ready(run_id)
        self.control.events.append("workflow_run", run_id, "workflow.materialized", {
            "job_id": job_id, "workflow": workflow_ref, "tasks": list(compiled.order),
        }, actor="orchestrator")
        return run_id

    def _refresh_ready(self, run_id: str) -> None:
        with self.state.transaction() as conn:
            rows = list(conn.execute("SELECT * FROM orchestrator_tasks WHERE run_id=?", (run_id,)))
            statuses = {row["task_id"]: row["status"] for row in rows}
            now = utcnow()
            for row in rows:
                dependencies = json.loads(row["dependencies"])
                if row["status"] == "pending" and all(
                    statuses.get(dep) in {"succeeded", "skipped"} for dep in dependencies
                ):
                    conn.execute("UPDATE orchestrator_tasks SET status='ready',updated_at=? WHERE run_id=? AND task_id=?",
                                 (now, run_id, row["task_id"]))

    def tasks(self, run_id: str) -> tuple[TaskRecord, ...]:
        rows = self.state._connection().execute(
            "SELECT * FROM orchestrator_tasks WHERE run_id=? ORDER BY rowid", (run_id,))
        return tuple(TaskRecord(run_id, row["task_id"], row["capability_id"], row["status"],
                                row["attempt"], row["output_hash"], json.loads(row["inputs"])) for row in rows)

    def ready(self, run_id: str) -> tuple[TaskRecord, ...]:
        self._refresh_ready(run_id)
        return tuple(item for item in self.tasks(run_id) if item.status == "ready")

    @staticmethod
    def _canonical_digest(value: Any) -> str:
        return hashlib.sha256(json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()

    def _file_digest(self, path: Path) -> str:
        if not path.is_file():
            return self._canonical_digest({"missing": path.name})
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _input_hashes(self, conn: Any, run_id: str, row: Any) -> tuple[str, ...]:
        dependencies = json.loads(row["dependencies"])
        hashes = tuple(conn.execute(
            "SELECT output_hash FROM orchestrator_tasks WHERE run_id=? AND task_id=?",
            (run_id, dep)).fetchone()[0] for dep in dependencies)
        if not dependencies:
            run_job = conn.execute("""SELECT j.payload FROM orchestrator_runs r
                JOIN jobs j ON j.id=r.job_id WHERE r.run_id=?""", (run_id,)).fetchone()
            if run_job is not None:
                hashes = tuple(json.loads(run_job["payload"]).get("input_hashes") or ())
        if int(row["attempt"]) > 0 and row["output_hash"] and row["status"] != "ready":
            hashes = (*hashes, str(row["output_hash"]))
        return hashes

    def binding(self, run_id: str, task_id: str, input_hashes: tuple[str, ...]) -> dict[str, str]:
        conn = self.state._connection()
        run = conn.execute("""SELECT r.workflow_ref,r.job_id,j.payload FROM orchestrator_runs r
            JOIN jobs j ON j.id=r.job_id WHERE r.run_id=?""", (run_id,)).fetchone()
        if run is None:
            raise OrchestratorError(f"run not found: {run_id}")
        workflow_path = self.root / "workflows" / f"{run['workflow_ref']}.json"
        workflow = load_json(workflow_path)
        steps = workflow.get("steps", workflow.get("tasks", []))
        step = next((item for item in steps if item.get("id") == task_id), None)
        if step is None:
            raise OrchestratorError(f"workflow binding not found: {task_id}")
        capability_id = str(step.get("capability"))
        capability_path = next((path for path in sorted((self.root / "capabilities").glob("*.json"))
                                if load_json(path).get("capability_id", load_json(path).get("id"))
                                == capability_id), None)
        if capability_path is None:
            raise OrchestratorError(f"capability manifest not found: {capability_id}")
        generation_row = conn.execute(
            "SELECT generation FROM task_binding_generations "
            "WHERE run_id=? AND task_id=? AND status='active' "
            "ORDER BY generation DESC LIMIT 1",
            (run_id, task_id),
        ).fetchone()
        binding_generation = int(generation_row["generation"]) if generation_row else 0
        job_payload = json.loads(run["payload"])
        workspace_manifest = self.root / "var/workspaces/jobs" / str(run["job_id"]) / "workspace-manifest.json"
        policy = self.root / "policies" / f"{job_payload['policy_version']}.json"
        profile = (self.root / "skills/industry-graph/production-profile-v1.json"
                   if str(run["workflow_ref"]).startswith("graph-industry-production@")
                   else self.root / "vendor/lca_cornerstone/profiles/wiki-node-production-profile-v1.json")
        components = {
            "input_artifact_manifest_hash": self._canonical_digest(list(input_hashes)),
            "capability_version_hash": self._file_digest(capability_path),
            "workflow_task_binding_hash": self._canonical_digest({
                "step": step,
                "binding_generation": binding_generation,
            }),
            "workspace_manifest_hash": self._file_digest(workspace_manifest),
            "policy_hash": self._file_digest(policy),
            "profile_hash": self._file_digest(profile),
        }
        components["effective_input_hash"] = self._canonical_digest(components)
        return components

    def claim(self, run_id: str, task_id: str, *, worker_id: str | None = None,
              lease_resource: str | None = None, fencing_token: int | None = None) -> tuple[str, tuple[str, ...]]:
        with self.state.transaction() as conn:
            row = conn.execute("SELECT * FROM orchestrator_tasks WHERE run_id=? AND task_id=?",
                               (run_id, task_id)).fetchone()
            if row is None or row["status"] != "ready":
                raise OrchestratorError(f"task is not ready: {task_id}")
            attempt = int(row["attempt"]) + 1
            # Preserve deterministic dependency order rather than SQL row order.
            input_hashes = self._input_hashes(conn, run_id, row)
            # A repaired attempt is cryptographically bound to the prior
            # failure envelope, so a worker cannot silently rerun unchanged
            # context and call it a repair.
            if int(row["attempt"]) > 0 and row["output_hash"]:
                if not input_hashes or input_hashes[-1] != str(row["output_hash"]):
                    input_hashes = (*input_hashes, str(row["output_hash"]))
            attempt_id, now = f"attempt_{uuid.uuid4().hex}", utcnow()
            binding = self.binding(run_id, task_id, input_hashes)
            if any(value is not None for value in (worker_id, lease_resource, fencing_token)):
                if not worker_id or not lease_resource or fencing_token is None:
                    raise OrchestratorError("worker claim requires worker_id, lease_resource and fencing_token")
                lease = conn.execute(
                    "SELECT * FROM leases WHERE resource=? AND holder=? AND fencing_token=? AND expires_at>?",
                    (lease_resource, worker_id, fencing_token, now),
                ).fetchone()
                if lease is None:
                    raise OrchestratorError("worker claim has no valid fenced lease")
            conn.execute("UPDATE orchestrator_tasks SET status='running',attempt=?,updated_at=? WHERE run_id=? AND task_id=?",
                         (attempt, now, run_id, task_id))
            conn.execute("""INSERT INTO orchestrator_attempts
                (attempt_id,run_id,task_id,attempt,status,input_hashes,output_hash,failure_code,
                 failure_payload,started_at,finished_at,worker_id,lease_resource,fencing_token,
                 output_manifest_hash,input_artifact_manifest_hash,capability_version_hash,
                 workflow_task_binding_hash,workspace_manifest_hash,policy_hash,profile_hash,
                 effective_input_hash)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                attempt_id, run_id, task_id, attempt, "running", json.dumps(input_hashes),
                None, None, None, now, None, worker_id, lease_resource, fencing_token, None,
                binding["input_artifact_manifest_hash"], binding["capability_version_hash"],
                binding["workflow_task_binding_hash"], binding["workspace_manifest_hash"],
                binding["policy_hash"], binding["profile_hash"], binding["effective_input_hash"]))
        return attempt_id, input_hashes

    def refresh_attempt_binding(self, attempt_id: str) -> dict[str, str]:
        row = self.state._connection().execute(
            "SELECT run_id,task_id,input_hashes,status FROM orchestrator_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None or row["status"] != "running":
            raise OrchestratorError("attempt is not active")
        binding = self.binding(row["run_id"], row["task_id"], tuple(json.loads(row["input_hashes"])))
        with self.state.transaction() as conn:
            conn.execute("""UPDATE orchestrator_attempts SET input_artifact_manifest_hash=?,
                capability_version_hash=?,workflow_task_binding_hash=?,workspace_manifest_hash=?,
                policy_hash=?,profile_hash=?,effective_input_hash=?
                WHERE attempt_id=? AND status='running'""", (*binding.values(), attempt_id))
        return binding

    def try_reuse(self, run_id: str, task_id: str) -> tuple[str, str, str] | None:
        conn = self.state._connection()
        task = conn.execute("SELECT * FROM orchestrator_tasks WHERE run_id=? AND task_id=?",
                            (run_id, task_id)).fetchone()
        if task is None or task["status"] != "ready":
            return None
        inputs = self._input_hashes(conn, run_id, task)
        binding = self.binding(run_id, task_id, inputs)
        source = conn.execute("""SELECT * FROM orchestrator_attempts
            WHERE run_id=? AND task_id=? AND status='succeeded' AND effective_input_hash=?
              AND output_manifest_hash IS NOT NULL ORDER BY attempt DESC LIMIT 1""",
            (run_id, task_id, binding["effective_input_hash"])).fetchone()
        if source is None:
            return None
        source_manifest = self.control.artifacts.verify_task_output_manifest(
            source["output_manifest_hash"]
        )
        reused_attempt = f"attempt_{uuid.uuid4().hex}"
        receipt_value = {"protocol": "task-reuse-receipt-v1", "run_id": run_id,
                         "task_id": task_id, "source_attempt_id": source["attempt_id"],
                         "reused_attempt_id": reused_attempt,
                         "effective_input_hash": binding["effective_input_hash"],
                         "output_manifest_hash": source["output_manifest_hash"]}
        receipt = self.control.artifacts.put_json(
            receipt_value, metadata={"schema": "task-reuse-receipt-v1"}
        )
        with self.state.transaction() as tx:
            current = tx.execute("SELECT * FROM orchestrator_tasks WHERE run_id=? AND task_id=?",
                                 (run_id, task_id)).fetchone()
            if current is None or current["status"] != "ready":
                return None
            attempt_number, now = int(current["attempt"]) + 1, utcnow()
            tx.execute("UPDATE orchestrator_tasks SET status='succeeded',attempt=?,output_hash=?,failure_code=NULL,failure_payload=NULL,updated_at=? WHERE run_id=? AND task_id=?",
                       (attempt_number, source["output_manifest_hash"], now, run_id, task_id))
            tx.execute("""INSERT INTO orchestrator_attempts(
                attempt_id,run_id,task_id,attempt,status,input_hashes,output_hash,failure_code,
                failure_payload,started_at,finished_at,output_manifest_hash,
                input_artifact_manifest_hash,capability_version_hash,workflow_task_binding_hash,
                workspace_manifest_hash,policy_hash,profile_hash,effective_input_hash)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                reused_attempt, run_id, task_id, attempt_number, "reused", json.dumps(inputs),
                source["output_manifest_hash"], None, None, now, now, source["output_manifest_hash"],
                *binding.values()))
            tx.execute("INSERT INTO task_reuse_receipts VALUES(?,?,?,?,?,?,?,?)", (
                receipt.digest, run_id, task_id, reused_attempt, source["attempt_id"],
                binding["effective_input_hash"], source["output_manifest_hash"], now))
            self.consistency.record_artifact_manifest(
                tx, run_id=run_id, task_id=task_id, attempt_id=reused_attempt,
                manifest_digest=str(source["output_manifest_hash"]),
                manifest=source_manifest,
            )
            self.consistency.record_stage_outcome(
                tx, run_id=run_id, task_id=task_id, attempt_id=reused_attempt,
                execution_status="reused", gate_decision="NOT_APPLICABLE",
                goal_effect="progress", payload={"reuse_receipt": receipt.digest},
            )
        self.control.artifacts.link(source["output_manifest_hash"], receipt.digest, "reused_output")
        self._refresh_ready(run_id); self._finish_if_terminal(run_id)
        return reused_attempt, str(source["output_manifest_hash"]), receipt.digest

    def repair_dry_run(self, run_id: str, task_id: str, *,
                       policy_invalidates: tuple[str, ...] = (),
                       policy_preserves: tuple[str, ...] = ()) -> dict[str, Any]:
        rows = list(self.state._connection().execute(
            "SELECT task_id,dependencies,status,output_hash FROM orchestrator_tasks WHERE run_id=?",
            (run_id,),
        ))
        if not any(row["task_id"] == task_id for row in rows):
            raise OrchestratorError(f"task not found: {task_id}")
        descendants: set[str] = set(); changed = True
        while changed:
            changed = False
            for row in rows:
                dependencies = set(json.loads(row["dependencies"]))
                if row["task_id"] != task_id and dependencies & ({task_id} | descendants) \
                        and row["task_id"] not in descendants:
                    descendants.add(str(row["task_id"])); changed = True
        allowed = {task_id, *descendants}
        invalidates = ({task_id, *policy_invalidates} & allowed
                       if policy_invalidates else allowed)
        invalidates -= set(policy_preserves)
        preserves = {str(row["task_id"]) for row in rows} - invalidates
        new_queries = 0
        if {"table_collect", "table_search_execution_gate"} & invalidates:
            matrix = next((self.root / "var/workspaces/jobs").glob(
                f"*/runs/wiki-batches/*/*/table-data/search-matrix.json"
            ), None)
            if matrix and matrix.is_file():
                new_queries = len(load_json(matrix).get("queries", []))
        return {"protocol": "wiki-repair-dry-run-v1", "run_id": run_id,
                "task_id": task_id, "will_invalidate": sorted(invalidates),
                "will_preserve": sorted(preserves), "new_queries": new_queries,
                "reused_queries": 0, "estimated_external_calls": new_queries,
                "estimated_runtime_seconds": new_queries * 3,
                "created_at": utcnow()}

    def create_binding_generation(self, run_id: str, task_id: str, *, reason: str) -> int:
        with self.state.transaction() as conn:
            current = conn.execute("SELECT output_hash FROM orchestrator_tasks WHERE run_id=? AND task_id=?",
                                   (run_id, task_id)).fetchone()
            if current is None:
                raise OrchestratorError(f"task not found: {task_id}")
            generation = int(conn.execute(
                "SELECT COALESCE(MAX(generation),0)+1 FROM task_binding_generations WHERE run_id=? AND task_id=?",
                (run_id, task_id)).fetchone()[0])
            conn.execute("UPDATE task_binding_generations SET status='superseded' WHERE run_id=? AND task_id=? AND status='active'",
                         (run_id, task_id))
            conn.execute("INSERT INTO task_binding_generations VALUES(?,?,?,?,?,?,?,?)", (
                run_id, task_id, generation, "active", None, current["output_hash"], reason, utcnow()))
        return generation

    def materialized_output_lineage(
        self, run_id: str, task_ids: set[str] | None = None
    ) -> dict[str, Any]:
        """Verify and return the latest task-owned mutable-output lineage."""
        run = self.state._connection().execute(
            "SELECT job_id FROM orchestrator_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise OrchestratorError(f"run not found: {run_id}")
        workspace = (self.root / "var/workspaces/jobs" / str(run["job_id"])).resolve()
        rows = list(self.state._connection().execute(
            "SELECT rowid,task_id,output_hash FROM orchestrator_tasks "
            "WHERE run_id=? AND task_id IN ('draft_apply','table_apply') ORDER BY rowid",
            (run_id,),
        ))
        manifests: list[dict[str, str]] = []
        targets: dict[str, dict[str, str]] = {}
        for row in rows:
            task_id = str(row["task_id"])
            if task_ids is not None and task_id not in task_ids:
                continue
            candidates = [str(item["prior_output_hash"]) for item in
                          self.state._connection().execute(
                "SELECT prior_output_hash FROM task_binding_generations "
                "WHERE run_id=? AND task_id=? AND prior_output_hash IS NOT NULL "
                "ORDER BY generation",
                (run_id, task_id),
            )]
            if row["output_hash"]:
                candidates.append(str(row["output_hash"]))
            for digest in dict.fromkeys(candidates):
                try:
                    manifest = self.control.artifacts.verify_task_output_manifest(digest)
                except (KeyError, RuntimeError, TypeError, ValueError) as exc:
                    raise OrchestratorError(
                        f"materialized output manifest is not verifiable: {digest}"
                    ) from exc
                owned = [item for item in manifest.get("files") or []
                         if item.get("role") == "materialized_output"]
                if not owned:
                    continue
                manifests.append({"task_id": task_id, "sha256": digest})
                for item in owned:
                    logical = str(item.get("path") or "")
                    expected = str(item.get("sha256") or "")
                    physical = (workspace / logical).resolve()
                    actual = (hashlib.sha256(physical.read_bytes()).hexdigest()
                              if physical.is_relative_to(workspace)
                              and physical.is_file() and not physical.is_symlink() else "")
                    targets[logical] = {
                        "sha256": expected,
                        "actual_sha256": actual,
                        "source_task": task_id,
                        "classification": (
                            "matching_plan_output" if actual == expected
                            and task_id == "draft_apply" else
                            "legitimate_descendant_output" if actual == expected else
                            "unknown_drift"
                        ),
                    }
        return {"manifests": manifests, "targets": targets}

    @staticmethod
    def _assert_attempt_ownership(conn: Any, attempt: Any, *, worker_id: str | None,
                                  lease_resource: str | None, fencing_token: int | None) -> None:
        owned = attempt["worker_id"] is not None
        if not owned:
            return
        if (worker_id, lease_resource, fencing_token) != (
            attempt["worker_id"], attempt["lease_resource"], attempt["fencing_token"]
        ):
            raise OrchestratorError("attempt ownership does not match its fenced claim")
        valid = conn.execute(
            "SELECT 1 FROM leases WHERE resource=? AND holder=? AND fencing_token=? AND expires_at>?",
            (lease_resource, worker_id, fencing_token, utcnow()),
        ).fetchone()
        if valid is None:
            raise OrchestratorError("stale worker cannot commit after lease takeover")

    def complete(self, attempt_id: str, payload: dict[str, Any], *,
                 output_manifest_hash: str | None = None, worker_id: str | None = None,
                 lease_resource: str | None = None, fencing_token: int | None = None) -> str:
        artifact = (self.control.artifacts.put_json(payload, metadata={"schema": "capability-output-v1"})
                    if output_manifest_hash is None else None)
        digest = output_manifest_hash or str(artifact.digest)
        # Verify the referenced CAS object before the state transaction. The
        # fencing check is repeated inside the commit transaction below.
        self.control.artifacts.get_bytes(digest)
        output_manifest = (
            self.control.artifacts.verify_task_output_manifest(digest)
            if output_manifest_hash else None
        )
        input_hashes: tuple[str, ...] = ()
        with self.state.transaction() as conn:
            attempt = conn.execute("SELECT * FROM orchestrator_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if attempt is None or attempt["status"] != "running":
                raise OrchestratorError("attempt is not active")
            self._assert_attempt_ownership(
                conn, attempt, worker_id=worker_id, lease_resource=lease_resource,
                fencing_token=fencing_token,
            )
            now = utcnow()
            conn.execute("""UPDATE orchestrator_attempts
                         SET status='succeeded',output_hash=?,output_manifest_hash=?,finished_at=?
                         WHERE attempt_id=?""", (digest, output_manifest_hash, now, attempt_id))
            conn.execute("UPDATE orchestrator_tasks SET status='succeeded',output_hash=?,failure_code=NULL,failure_payload=NULL,updated_at=? WHERE run_id=? AND task_id=?",
                         (digest, now, attempt["run_id"], attempt["task_id"]))
            if output_manifest is not None:
                self.consistency.record_artifact_manifest(
                    conn, run_id=str(attempt["run_id"]), task_id=str(attempt["task_id"]),
                    attempt_id=attempt_id, manifest_digest=digest,
                    manifest=output_manifest,
                )
            gate = payload.get("gate_result") if isinstance(payload, dict) else None
            gate = gate if isinstance(gate, dict) else {}
            raw_decision = str(gate.get("decision") or gate.get("verdict") or "NOT_APPLICABLE")
            goal_effect = (
                "progress_with_debt" if raw_decision.upper() == "PASS_WITH_DEBT"
                else "blocked" if raw_decision.upper() in {
                    "RESEARCH_MORE", "BLOCKED_INTEGRITY", "BLOCKED",
                } else "progress"
            )
            self.consistency.record_stage_outcome(
                conn, run_id=str(attempt["run_id"]), task_id=str(attempt["task_id"]),
                attempt_id=attempt_id, execution_status="completed",
                gate_decision=raw_decision, goal_effect=goal_effect,
                payload={
                    "output_manifest_hash": output_manifest_hash,
                    "failed_requirement_ids": gate.get("failed_requirement_ids") or [],
                },
            )
            input_hashes = tuple(json.loads(attempt["input_hashes"]))
        self._refresh_ready(attempt["run_id"])
        self._finish_if_terminal(attempt["run_id"])
        for parent in input_hashes:
            if parent:
                self.control.artifacts.link(str(parent), digest, "task_input")
        return digest

    def skip(self, run_id: str, task_id: str, reason: str) -> str:
        """Persist an explicit non-applicable branch decision with an artifact hash."""
        artifact = self.control.artifacts.put_json(
            {"protocol": "workflow-skip-v1", "run_id": run_id, "task_id": task_id,
             "reason": reason}, metadata={"schema": "workflow-skip-v1"})
        with self.state.transaction() as conn:
            row = conn.execute("SELECT status FROM orchestrator_tasks WHERE run_id=? AND task_id=?",
                               (run_id, task_id)).fetchone()
            if row is None or row["status"] not in {"pending", "ready"}:
                raise OrchestratorError(f"task cannot be skipped: {task_id}")
            conn.execute("UPDATE orchestrator_tasks SET status='skipped',output_hash=?,failure_code=NULL,"
                         "updated_at=? WHERE run_id=? AND task_id=?",
                         (artifact.digest, utcnow(), run_id, task_id))
            self.consistency.record_stage_outcome(
                conn, run_id=run_id, task_id=task_id, attempt_id=None,
                execution_status="skipped", gate_decision="NOT_APPLICABLE",
                goal_effect="no_effect", payload={"reason": reason},
            )
        self._refresh_ready(run_id)
        self._finish_if_terminal(run_id)
        return artifact.digest

    def fail(self, attempt_id: str, code: str, detail: dict[str, Any], *, repairable: bool,
             worker_id: str | None = None, lease_resource: str | None = None,
             fencing_token: int | None = None, status_override: str | None = None) -> None:
        encoded_failure = json.dumps(detail, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"))
        failure = self.control.artifacts.put_json(
            detail, metadata={"schema": "failure-envelope-v1", "code": code}
        )
        with self.state.transaction() as conn:
            attempt = conn.execute("SELECT * FROM orchestrator_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if attempt is None or attempt["status"] != "running":
                raise OrchestratorError("attempt is not active")
            self._assert_attempt_ownership(
                conn, attempt, worker_id=worker_id, lease_resource=lease_resource,
                fencing_token=fencing_token,
            )
            status, now = (status_override or ("repairable" if repairable else "quarantined")), utcnow()
            if status not in {"repairable", "quarantined", "manual_review"}:
                raise OrchestratorError(f"invalid failure status: {status}")
            conn.execute("""UPDATE orchestrator_attempts SET status=?,failure_code=?,
                failure_payload=?,output_hash=?,finished_at=? WHERE attempt_id=?""",
                         (status, code, encoded_failure, failure.digest, now, attempt_id))
            conn.execute("""UPDATE orchestrator_tasks SET status=?,failure_code=?,
                failure_payload=?,output_hash=?,updated_at=? WHERE run_id=? AND task_id=?""",
                         (status, code, encoded_failure, failure.digest, now,
                          attempt["run_id"], attempt["task_id"]))
            conn.execute("UPDATE orchestrator_runs SET status=?,updated_at=? WHERE run_id=?",
                         (status, now, attempt["run_id"]))
            failure_fingerprint = str(
                detail.get("failure_fingerprint")
                or consistency_digest({"code": code, "detail": detail})
            )
            self.consistency.record_stage_outcome(
                conn, run_id=str(attempt["run_id"]), task_id=str(attempt["task_id"]),
                attempt_id=attempt_id, execution_status="completed_with_block",
                gate_decision=str(detail.get("gate_decision") or "BLOCKED"),
                goal_effect="blocked", failure_fingerprint=failure_fingerprint,
                payload={"failure_code": code, "failure": detail},
            )

    def recover(self, run_id: str, task_id: str) -> None:
        """Schedule a bounded retry from a persisted repairable failure."""
        run = self.state._connection().execute(
            "SELECT workflow_ref FROM orchestrator_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        row = self.state._connection().execute(
            "SELECT status,attempt FROM orchestrator_tasks WHERE run_id=? AND task_id=?", (run_id, task_id)
        ).fetchone()
        if run is None or row is None or row["status"] != "repairable":
            raise OrchestratorError(f"task is not repairable: {task_id}")
        spec = WorkflowSpec.from_mapping(load_json(self.root / "workflows" / f"{run['workflow_ref']}.json"))
        task = next(item for item in spec.tasks if item.id == task_id)
        epoch_attempt = self.repair_epoch_attempt(
            run_id, task_id, int(row["attempt"])
        )
        if epoch_attempt >= task.max_attempts:
            with self.state.transaction() as conn:
                now = utcnow()
                conn.execute("UPDATE orchestrator_tasks SET status='quarantined',updated_at=? WHERE run_id=? AND task_id=?",
                             (now, run_id, task_id))
                conn.execute("UPDATE orchestrator_runs SET status='quarantined',updated_at=? WHERE run_id=?", (now, run_id))
            raise OrchestratorError(f"repair budget exhausted: {task_id}")
        with self.state.transaction() as conn:
            now = utcnow()
            conn.execute("UPDATE orchestrator_tasks SET status='ready',updated_at=? WHERE run_id=? AND task_id=?",
                         (now, run_id, task_id))
            conn.execute("UPDATE orchestrator_runs SET status='ready',updated_at=? WHERE run_id=?", (now, run_id))
        run_job = self.state._connection().execute(
            "SELECT job_id FROM orchestrator_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run_job:
            job = self.control.state.get("jobs", str(run_job["job_id"]))
            if job and JobState(job["status"]) in {JobState.REPAIRABLE, JobState.RETRYABLE}:
                self.control.transition_job(str(run_job["job_id"]), JobState.READY,
                                            reason=f"repair scheduled for {task_id}")
        self.control.events.append("workflow_run", run_id, "task.repair_scheduled", {
            "task_id": task_id, "next_attempt": epoch_attempt + 1,
            "lifetime_next_attempt": int(row["attempt"]) + 1,
            "max_attempts": task.max_attempts,
        }, actor="orchestrator")

    def repair_epoch_attempt(self, run_id: str, task_id: str,
                             lifetime_attempt: int) -> int:
        """Return attempts consumed since the latest approved causal repair."""
        row = self.state._connection().execute(
            "SELECT base_attempt FROM task_repair_epochs WHERE run_id=? AND task_id=? "
            "ORDER BY epoch DESC LIMIT 1", (run_id, task_id),
        ).fetchone()
        base = int(row["base_attempt"]) if row else 0
        return max(0, int(lifetime_attempt) - base)

    def rewind_from(self, run_id: str, task_id: str, *, reason: str,
                    actor: str = "operator", reset_attempts: bool = False) -> tuple[str, ...]:
        """Atomically reopen a causal branch and every aggregate projection.

        Binding generations, artifact generations, task/run/job state, campaign
        state, repair epoch, obsolete wakeups, and the recovery event commit as
        one SQLite transaction.  A process crash therefore cannot leave a
        ready task behind a terminal Job or a blocked campaign.
        """
        run = self.state._connection().execute(
            "SELECT job_id FROM orchestrator_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        rows = list(self.state._connection().execute(
            "SELECT task_id,dependencies,status,attempt,output_hash FROM orchestrator_tasks "
            "WHERE run_id=? ORDER BY rowid", (run_id,)
        ))
        if run is None or not any(row["task_id"] == task_id for row in rows):
            raise OrchestratorError(f"task not found: {task_id}")
        invalidated = {task_id}
        changed = True
        while changed:
            changed = False
            for row in rows:
                dependencies = set(json.loads(row["dependencies"]))
                if row["task_id"] not in invalidated and dependencies & invalidated:
                    invalidated.add(str(row["task_id"]))
                    changed = True
        active = [str(row["task_id"]) for row in rows
                  if row["task_id"] in invalidated and row["status"] == "running"]
        if active:
            raise OrchestratorError(f"cannot rewind running tasks: {active}")
        materialization_lineage = self.materialized_output_lineage(run_id, invalidated)
        ordered = tuple(str(row["task_id"]) for row in rows if row["task_id"] in invalidated)
        job_id = str(run["job_id"])
        now = utcnow()
        with self.state.transaction() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise OrchestratorError(f"job not found: {job_id}")
            prior_job_status = str(job["status"])
            if prior_job_status in {"published", "superseded", "quarantined"}:
                raise OrchestratorError(
                    f"terminal Job requires an explicit new Job revision: {prior_job_status}"
                )
            generations: dict[str, int] = {}
            for source in rows:
                invalidated_task = str(source["task_id"])
                if invalidated_task not in invalidated:
                    continue
                current_generation = int(conn.execute(
                    "SELECT COALESCE(MAX(generation),0) FROM task_binding_generations "
                    "WHERE run_id=? AND task_id=?", (run_id, invalidated_task),
                ).fetchone()[0])
                if int(source["attempt"]) or source["output_hash"]:
                    generation = current_generation + 1
                    conn.execute(
                        "UPDATE task_binding_generations SET status='superseded' "
                        "WHERE run_id=? AND task_id=? AND status='active'",
                        (run_id, invalidated_task),
                    )
                    conn.execute(
                        "INSERT INTO task_binding_generations VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, invalidated_task, generation, "active", None,
                         source["output_hash"], reason, now),
                    )
                else:
                    generation = max(current_generation, 1)
                generations[invalidated_task] = generation
            for invalidated_task in sorted(invalidated):
                status = "ready" if invalidated_task == task_id else "pending"
                conn.execute(
                    "UPDATE orchestrator_tasks SET status=?,output_hash=NULL,failure_code=NULL,"
                    "failure_payload=NULL,updated_at=? "
                    "WHERE run_id=? AND task_id=?",
                    (status, now, run_id, invalidated_task),
                )
                if reset_attempts:
                    source = next(row for row in rows
                                  if row["task_id"] == invalidated_task)
                    epoch = int(conn.execute(
                        "SELECT COALESCE(MAX(epoch),0)+1 FROM task_repair_epochs "
                        "WHERE run_id=? AND task_id=?", (run_id, invalidated_task),
                    ).fetchone()[0])
                    conn.execute(
                        "INSERT INTO task_repair_epochs VALUES(?,?,?,?,?,?)",
                        (run_id, invalidated_task, epoch, int(source["attempt"]),
                         reason, now),
                    )
            stale_artifacts = self.consistency.stale_task_artifacts(
                conn, run_id, invalidated
            )
            conn.execute("UPDATE orchestrator_runs SET status='ready',updated_at=? WHERE run_id=?",
                         (now, run_id))
            job_payload = json.loads(job["payload"])
            job_payload.update({"state": "ready", "transition_reason": reason})
            conn.execute(
                "UPDATE jobs SET status='ready',payload=?,updated_at=? WHERE id=?",
                (canonical_json(job_payload), now, job_id),
            )
            item_rows = list(conn.execute(
                "SELECT item_id,campaign_id FROM autonomous_job_items WHERE job_id=?",
                (job_id,),
            ))
            for item in item_rows:
                conn.execute(
                    "UPDATE autonomous_job_items SET status='running',last_error=NULL,"
                    "updated_at=? WHERE item_id=?",
                    (now, item["item_id"]),
                )
                conn.execute(
                    "UPDATE autonomous_campaigns SET status='running',updated_at=? "
                    "WHERE campaign_id=? AND status!='paused'",
                    (now, item["campaign_id"]),
                )
            # The recovery transaction supersedes the wakeup that requested
            # it.  A later deviation receives a different observation hash and
            # therefore a fresh durable wakeup.
            conn.execute(
                "UPDATE goal_supervisor_wakeups SET status='obsolete',updated_at=? "
                "WHERE job_id=? AND status='pending'",
                (now, job_id),
            )
            causal_generation = generations[task_id]
            recovery_value = {
                "schema_version": "atomic-recovery-transaction-v1",
                "job_id": job_id,
                "run_id": run_id,
                "from_task": task_id,
                "causal_generation": causal_generation,
                "invalidated_tasks": list(ordered),
                "binding_generations": generations,
                "stale_artifact_generations": stale_artifacts,
                "repair_epoch_reset": reset_attempts,
                "materialization_lineage": materialization_lineage,
                "reason": reason,
            }
            recovery_id = "rcv_" + consistency_digest(recovery_value)[:32]
            conn.execute(
                "INSERT INTO recovery_transactions VALUES(?,?,?,?,?,?,?,?,?)",
                (recovery_id, job_id, run_id, task_id, causal_generation,
                 "committed", reason, canonical_json(recovery_value), now),
            )
            self.consistency.append_event(
                conn, "workflow_run", run_id, "workflow.rewound",
                {**recovery_value, "recovery_id": recovery_id}, actor=actor,
            )
            if prior_job_status != "ready":
                self.consistency.append_event(
                    conn, "job", job_id, "job.transitioned",
                    {"from": prior_job_status, "to": "ready", "reason": reason,
                     "recovery_id": recovery_id}, actor="control-plane",
                )
        return ordered

    def reopen_skipped_table_branch(self, run_id: str) -> None:
        """Reopen only the terminal branch produced by the old preview skip defect."""
        ordered = ("table_collect", "table_verify", "table_population_gate", "table_apply",
                   "preview", "release_gate", "reviewed_apply", "publish")
        rows = {row.task_id: row for row in self.tasks(run_id)}
        if any(task not in rows for task in ordered):
            raise OrchestratorError("run has no complete v6 table branch")
        historical = (all(rows[task].status == "skipped" for task in ordered[:4])
                      and rows["preview"].status == "succeeded"
                      and all(rows[task].status == "skipped" for task in ordered[5:]))
        applied_repair = (all(rows[task].status == "succeeded" for task in ordered[:4])
                          and rows["preview"].status in {"ready", "repairable"}
                          and all(rows[task].status == "pending" for task in ordered[5:]))
        if not historical and not applied_repair:
            raise OrchestratorError("table/preview branch is not in an approved repair shape")
        run = self.state._connection().execute(
            "SELECT job_id,status FROM orchestrator_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None or run["status"] not in {"succeeded", "ready", "repairable"}:
            raise OrchestratorError("run is not in a repairable table branch state")
        job_id = str(run["job_id"])
        job = self.state.get("jobs", job_id)
        if job is None or JobState(job["status"]) not in {JobState.CANDIDATE, JobState.READY}:
            raise OrchestratorError("preview Job is not in an approved table repair state")
        if JobState(job["status"]) == JobState.CANDIDATE:
            self.control.transition_job(job_id, JobState.REPAIRABLE,
                                        reason="historical preview skipped executable table branch")
            self.control.transition_job(job_id, JobState.READY,
                                        reason="table branch repair scheduled")
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute("UPDATE orchestrator_runs SET status='ready',updated_at=? WHERE run_id=?",
                         (now, run_id))
            for index, task in enumerate(ordered):
                conn.execute(
                    "UPDATE orchestrator_tasks SET status=?,failure_code=NULL,updated_at=? "
                    "WHERE run_id=? AND task_id=?",
                    ("ready" if index == 0 else "pending", now, run_id, task),
                )
        self.control.events.append("workflow_run", run_id, "table_branch.reopened", {
            "tasks": list(ordered), "reason": "remove historical preview table skip",
        }, actor="orchestrator")

    def _finish_if_terminal(self, run_id: str) -> None:
        rows = self.tasks(run_id)
        if rows and all(row.status in {"succeeded", "skipped"} for row in rows):
            now = utcnow()
            with self.state.transaction() as conn:
                conn.execute("UPDATE orchestrator_runs SET status='succeeded',updated_at=? WHERE run_id=?", (now, run_id))
