"""SQLite-backed workflow materialisation and recovery.

The orchestrator is intentionally deterministic: it persists task state and
attempts, while capability and agent executors only return immutable outputs.
No conversation owns the next-step decision.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import uuid
from typing import Any

from lca_project.contracts import JobState, load_json
from lca_project.control import ControlPlane
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
          output_hash TEXT, failure_code TEXT, updated_at TEXT NOT NULL, inputs TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY(run_id,task_id), FOREIGN KEY(run_id) REFERENCES orchestrator_runs(run_id));
        CREATE TABLE IF NOT EXISTS orchestrator_attempts(
          attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
          attempt INTEGER NOT NULL, status TEXT NOT NULL, input_hashes TEXT NOT NULL,
          output_hash TEXT, failure_code TEXT, started_at TEXT NOT NULL, finished_at TEXT,
          UNIQUE(run_id,task_id,attempt));
        """)
        columns = {row["name"] for row in self.state._connection().execute("PRAGMA table_info(orchestrator_tasks)")}
        if "inputs" not in columns:
            self.state._connection().execute(
                "ALTER TABLE orchestrator_tasks ADD COLUMN inputs TEXT NOT NULL DEFAULT '{}'"
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
                    (run_id,task_id,capability_id,dependencies,status,attempt,output_hash,failure_code,updated_at,inputs)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                    run_id, task.id, task.capability, json.dumps(list(task.depends_on)),
                    "pending", 0, None, None, now, json.dumps(task.inputs, sort_keys=True)))
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
                if row["status"] == "pending" and all(statuses.get(dep) == "succeeded" for dep in dependencies):
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

    def claim(self, run_id: str, task_id: str) -> tuple[str, tuple[str, ...]]:
        with self.state.transaction() as conn:
            row = conn.execute("SELECT * FROM orchestrator_tasks WHERE run_id=? AND task_id=?",
                               (run_id, task_id)).fetchone()
            if row is None or row["status"] != "ready":
                raise OrchestratorError(f"task is not ready: {task_id}")
            attempt = int(row["attempt"]) + 1
            dependencies = json.loads(row["dependencies"])
            # Preserve deterministic dependency order rather than SQL row order.
            input_hashes = tuple(conn.execute(
                "SELECT output_hash FROM orchestrator_tasks WHERE run_id=? AND task_id=?",
                (run_id, dep)).fetchone()[0] for dep in dependencies)
            # A repaired attempt is cryptographically bound to the prior
            # failure envelope, so a worker cannot silently rerun unchanged
            # context and call it a repair.
            if int(row["attempt"]) > 0 and row["output_hash"]:
                input_hashes = (*input_hashes, str(row["output_hash"]))
            attempt_id, now = f"attempt_{uuid.uuid4().hex}", utcnow()
            conn.execute("UPDATE orchestrator_tasks SET status='running',attempt=?,updated_at=? WHERE run_id=? AND task_id=?",
                         (attempt, now, run_id, task_id))
            conn.execute("INSERT INTO orchestrator_attempts VALUES(?,?,?,?,?,?,?,?,?,?)", (
                attempt_id, run_id, task_id, attempt, "running", json.dumps(input_hashes),
                None, None, now, None))
        return attempt_id, input_hashes

    def complete(self, attempt_id: str, payload: dict[str, Any]) -> str:
        artifact = self.control.artifacts.put_json(payload, metadata={"schema": "capability-output-v1"})
        with self.state.transaction() as conn:
            attempt = conn.execute("SELECT * FROM orchestrator_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if attempt is None or attempt["status"] != "running":
                raise OrchestratorError("attempt is not active")
            now = utcnow()
            conn.execute("UPDATE orchestrator_attempts SET status='succeeded',output_hash=?,finished_at=? WHERE attempt_id=?",
                         (artifact.digest, now, attempt_id))
            conn.execute("UPDATE orchestrator_tasks SET status='succeeded',output_hash=?,updated_at=? WHERE run_id=? AND task_id=?",
                         (artifact.digest, now, attempt["run_id"], attempt["task_id"]))
        self._refresh_ready(attempt["run_id"])
        self._finish_if_terminal(attempt["run_id"])
        return artifact.digest

    def fail(self, attempt_id: str, code: str, detail: dict[str, Any], *, repairable: bool) -> None:
        failure = self.control.artifacts.put_json(detail, metadata={"schema": "failure-envelope-v1", "code": code})
        with self.state.transaction() as conn:
            attempt = conn.execute("SELECT * FROM orchestrator_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if attempt is None or attempt["status"] != "running":
                raise OrchestratorError("attempt is not active")
            status, now = ("repairable" if repairable else "quarantined"), utcnow()
            conn.execute("UPDATE orchestrator_attempts SET status=?,failure_code=?,output_hash=?,finished_at=? WHERE attempt_id=?",
                         (status, code, failure.digest, now, attempt_id))
            conn.execute("UPDATE orchestrator_tasks SET status=?,failure_code=?,output_hash=?,updated_at=? WHERE run_id=? AND task_id=?",
                         (status, code, failure.digest, now, attempt["run_id"], attempt["task_id"]))
            conn.execute("UPDATE orchestrator_runs SET status=?,updated_at=? WHERE run_id=?",
                         (status, now, attempt["run_id"]))

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
        if int(row["attempt"]) >= task.max_attempts:
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
        self.control.events.append("workflow_run", run_id, "task.repair_scheduled", {
            "task_id": task_id, "next_attempt": int(row["attempt"]) + 1,
            "max_attempts": task.max_attempts,
        }, actor="orchestrator")

    def _finish_if_terminal(self, run_id: str) -> None:
        rows = self.tasks(run_id)
        if rows and all(row.status == "succeeded" for row in rows):
            now = utcnow()
            with self.state.transaction() as conn:
                conn.execute("UPDATE orchestrator_runs SET status='succeeded',updated_at=? WHERE run_id=?", (now, run_id))
