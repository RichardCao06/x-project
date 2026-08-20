"""Read models and narrowly-scoped control actions for the dashboard.

The dashboard deliberately builds projections from persisted control-plane
facts.  It never infers success from files in a workspace and never updates
SQLite directly for control actions.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from lca_project.control import ControlPlane
from lca_project.kernel.orchestrator import PersistentOrchestrator
from lca_project.kernel.registry import CapabilityRegistry
from lca_project.kernel.skills import SkillInvoker, SkillRegistry
from lca_project.kernel.workflow import WorkflowSpec, compile_workflow
from lca_project.kernel.worker import WorkerLoop
from lca_project.contracts import load_json


def _json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return {} if default is None else default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {} if default is None else default


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in tuple(result):
        if key in {"metadata", "dependencies", "inputs", "input_hashes", "output_hashes", "summary"} or key.endswith("payload"):
            result[key] = _json(result[key], [] if key.endswith("hashes") or key == "dependencies" else {})
    return result


class DashboardService:
    """Query the platform through stable JSON-friendly dashboard projections."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.control = ControlPlane(self.root)
        self.state = self.control.state
        self._workers: dict[str, threading.Thread] = {}
        self._worker_lock = threading.Lock()
        self._autonomy_threads: dict[str, threading.Thread] = {}
        self._autonomy_lock = threading.Lock()
        self._goal_reconciler_thread: threading.Thread | None = None
        self._goal_reconciler_stop = threading.Event()

    @property
    def conn(self) -> sqlite3.Connection:
        return self.state._connection()

    def _has_table(self, name: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [_row(item) or {} for item in self.conn.execute(sql, params)]

    def _count(self, table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
        if not self._has_table(table):
            return 0
        suffix = f" WHERE {where}" if where else ""
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}{suffix}", params).fetchone()[0])

    def overview(self) -> dict[str, Any]:
        job_states = {row["status"]: row["n"] for row in self.conn.execute(
            "SELECT status,COUNT(*) AS n FROM jobs GROUP BY status ORDER BY status"
        )}
        task_states: dict[str, int] = {}
        if self._has_table("orchestrator_tasks"):
            task_states = {row["status"]: row["n"] for row in self.conn.execute(
                "SELECT status,COUNT(*) AS n FROM orchestrator_tasks GROUP BY status ORDER BY status"
            )}
        gates: dict[str, int] = {}
        if self._has_table("gate_results"):
            gates = {row["verdict"]: row["n"] for row in self.conn.execute(
                "SELECT verdict,COUNT(*) AS n FROM gate_results GROUP BY verdict ORDER BY verdict"
            )}
        recent_events = self.events(limit=12)["items"]
        recent_jobs = self.jobs(limit=8)["items"]
        active_leases = self._count("leases", "expires_at > ?", (datetime.now(timezone.utc).isoformat(),))
        total_budget = {"limit": 0, "reserved": 0, "consumed": 0}
        if self._has_table("budgets"):
            budget = self.conn.execute(
                "SELECT COALESCE(SUM(limit_units),0),COALESCE(SUM(reserved_units),0),"
                "COALESCE(SUM(consumed_units),0) FROM budgets"
            ).fetchone()
            total_budget = {"limit": budget[0], "reserved": budget[1], "consumed": budget[2]}
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "root": str(self.root),
            "counts": {
                "jobs": self._count("jobs"),
                "workflow_runs": self._count("orchestrator_runs"),
                "tasks": self._count("orchestrator_tasks"),
                "artifacts": self._count("artifacts"),
                "events": self._count("events"),
                "open_exceptions": self._count("exceptions", "status NOT IN ('resolved','closed')"),
                "releases": self._count("releases"),
                "active_leases": active_leases,
                "workers": self._count("worker_instances"),
                "open_deviations": self._count("deviation_reports", "status='open'"),
                "change_candidates": self._count("system_change_candidates"),
                "autonomous_campaigns": self._count("autonomous_campaigns"),
            },
            "job_states": job_states,
            "task_states": task_states,
            "gate_verdicts": gates,
            "budget": total_budget,
            "recent_jobs": recent_jobs,
            "recent_events": recent_events,
            "goal_alignment": self.goal_alignment(limit=8),
            "autonomy": self.autonomy(limit=8),
        }

    def skill_catalog(self) -> dict[str, Any]:
        """Expose registered Skill routes and their authoritative input schemas."""
        items: list[dict[str, Any]] = []
        for skill in SkillRegistry(self.root).all():
            schema = load_json(self.root / "contracts" / f"{skill.input_schema}.schema.json")
            items.append({
                "name": skill.name,
                "description": skill.description,
                "version": skill.version,
                "workflow": skill.workflow_ref,
                "policy": skill.policy_version,
                "input_schema": skill.input_schema,
                "schema": schema,
            })
        return {"items": items, "total": len(items)}

    def workers(self) -> dict[str, Any]:
        if not self._has_table("worker_instances"):
            return {"items": [], "total": 0}
        items = self._rows("SELECT * FROM worker_instances ORDER BY heartbeat_at DESC")
        return {"items": items, "total": len(items)}

    def create_job(self, skill_name: str, request: dict[str, Any], *,
                   idempotency_key: str | None = None, materialize: bool = False) -> dict[str, Any]:
        """Submit only through the registered Skill route and optionally materialise it."""
        if not isinstance(skill_name, str) or not skill_name.strip():
            raise ValueError("skill is required")
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key.strip()):
            raise ValueError("idempotency_key must be a non-empty string when provided")
        if not isinstance(materialize, bool):
            raise ValueError("materialize must be a boolean")
        accepted = SkillInvoker(self.root).invoke(
            skill_name.strip(), request, idempotency_key=idempotency_key.strip() if idempotency_key else None
        )
        result: dict[str, Any] = {**accepted, "materialized": False}
        if materialize:
            expanded = self.materialize(str(accepted["job_id"]))
            result.update({"materialized": True, "run_id": expanded["run_id"],
                           "tasks": len(expanded["tasks"])})
        return result

    def jobs(self, *, status: str = "", query: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("j.status=?")
            params.append(status)
        if query:
            clauses.append("(j.id LIKE ? OR j.workflow_id LIKE ? OR j.payload LIKE ?)")
            needle = f"%{query}%"
            params.extend((needle, needle, needle))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = int(self.conn.execute(f"SELECT COUNT(*) FROM jobs j{where}", tuple(params)).fetchone()[0])
        has_runs = self._has_table("orchestrator_runs")
        join = "LEFT JOIN orchestrator_runs o ON o.job_id=j.id" if has_runs else ""
        run_columns = "o.run_id,o.status AS run_status" if has_runs else "NULL AS run_id,NULL AS run_status"
        rows = self._rows(
            f"SELECT j.*,{run_columns} FROM jobs j {join}{where} "
            "ORDER BY j.updated_at DESC LIMIT ? OFFSET ?", (*params, limit, offset)
        )
        for item in rows:
            payload = item.get("payload") or {}
            scope = payload.get("scope") or {}
            item["target"] = payload.get("target") or scope.get("node_id") or scope.get("target") or "—"
            item["policy_version"] = payload.get("policy_version") or "—"
            item["risk"] = payload.get("risk") or "standard"
        states = [dict(row) for row in self.conn.execute(
            "SELECT status,COUNT(*) AS count FROM jobs GROUP BY status ORDER BY status"
        )]
        return {"items": rows, "total": total, "limit": limit, "offset": offset, "states": states}

    def job(self, job_id: str) -> dict[str, Any]:
        job = _row(self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        if job is None:
            raise KeyError(job_id)
        run = None
        tasks: list[dict[str, Any]] = []
        attempts: list[dict[str, Any]] = []
        if self._has_table("orchestrator_runs"):
            run = _row(self.conn.execute(
                "SELECT * FROM orchestrator_runs WHERE job_id=?", (job_id,)
            ).fetchone())
        if run and self._has_table("orchestrator_tasks"):
            tasks = self._rows(
                "SELECT * FROM orchestrator_tasks WHERE run_id=? ORDER BY rowid", (run["run_id"],)
            )
            attempts = self._rows(
                "SELECT * FROM orchestrator_attempts WHERE run_id=? ORDER BY started_at DESC", (run["run_id"],)
            )
        events = self._rows(
            "SELECT * FROM events WHERE (aggregate_type='job' AND aggregate_id=?) "
            "OR (aggregate_type='workflow_run' AND aggregate_id=?) ORDER BY sequence DESC LIMIT 100",
            (job_id, run["run_id"] if run else ""),
        )
        run_id = run["run_id"] if run else ""
        gates = self._rows("SELECT * FROM gate_results WHERE run_id=? ORDER BY created_at", (run_id,))
        decisions = self._rows("SELECT * FROM decisions WHERE run_id=? ORDER BY created_at", (run_id,))
        exceptions = self._rows("SELECT * FROM exceptions WHERE run_id=? ORDER BY opened_at DESC", (run_id,))
        hashes: set[str] = set((job.get("payload") or {}).get("input_hashes") or [])
        for task in tasks:
            if task.get("output_hash"):
                hashes.add(str(task["output_hash"]))
        artifacts = []
        for digest in hashes:
            artifact = _row(self.conn.execute("SELECT * FROM artifacts WHERE digest=?", (digest,)).fetchone())
            if artifact:
                artifacts.append(artifact)
        workflow = self._workflow_detail(str(job.get("workflow_id") or ""))
        preview = self._preview_projection(job_id, run=run, tasks=tasks)
        return {"job": job, "run": run, "tasks": tasks, "attempts": attempts,
                "events": events, "gates": gates, "decisions": decisions,
                "exceptions": exceptions, "artifacts": artifacts, "workflow": workflow,
                "goal_alignment": self.goal_alignment(job_id=job_id), "preview": preview}

    def _preview_projection(
        self,
        job_id: str,
        *,
        run: dict[str, Any] | None = None,
        tasks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Return a verified, workspace-confined preview projection for a Job."""
        if not self._has_table("orchestrator_runs") or not self._has_table("orchestrator_tasks"):
            return None
        if run is None:
            run = _row(self.conn.execute(
                "SELECT * FROM orchestrator_runs WHERE job_id=?", (job_id,)
            ).fetchone())
        if not run:
            return None
        if tasks is None:
            tasks = self._rows(
                "SELECT * FROM orchestrator_tasks WHERE run_id=? ORDER BY rowid",
                (run["run_id"],),
            )
        preview_task = next((item for item in tasks if item.get("task_id") == "preview"), None)
        if not preview_task or preview_task.get("status") != "succeeded":
            return None
        output_hash = str(preview_task.get("output_hash") or "")
        if not output_hash:
            return None
        try:
            manifest = self.control.artifacts.verify_task_output_manifest(output_hash)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            return None
        report_entry = next(
            (item for item in manifest.get("files") or []
             if str(item.get("path") or "").endswith("/preview-report.json")),
            None,
        )
        if not report_entry:
            return None
        workspace = (self.root / "var" / "workspaces" / "jobs" / job_id).resolve()
        report_path = (workspace / str(report_entry["path"])).resolve()
        if (not report_path.is_relative_to(workspace) or not report_path.is_file()
                or report_path.is_symlink()):
            return None
        report_bytes = report_path.read_bytes()
        if hashlib.sha256(report_bytes).hexdigest() != str(report_entry.get("sha256") or ""):
            return None
        try:
            report = json.loads(report_bytes)
        except json.JSONDecodeError:
            return None
        docs = (workspace / "docs").resolve()
        assets: dict[str, dict[str, str]] = {}
        for role, value in (report.get("artifacts") or {}).items():
            if not isinstance(value, dict):
                continue
            path = Path(str(value.get("path") or "")).resolve()
            digest = str(value.get("sha256") or "")
            if (not path.is_relative_to(docs) or not path.is_file() or path.is_symlink()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
                return None
            assets[str(role)] = {"filename": path.name, "sha256": digest}
        viewer_value = assets.get("viewer") or {}
        viewer_name = str(viewer_value.get("filename") or "")
        if not viewer_name:
            return None
        return {
            "url": f"/preview/{job_id}/{viewer_name}",
            "viewer": viewer_name,
            "assets": assets,
            "maturity": report.get("maturity"),
            "mode": report.get("mode"),
            "start_node": report.get("start_node"),
            "all_passed": report.get("all_passed") is True,
            "candidate_eligible": report.get("candidate_eligible") is True,
            "report_sha256": str(report_entry.get("sha256") or ""),
        }

    def preview_asset(self, job_id: str, filename: str) -> Path:
        """Resolve one generated preview asset without exposing the Job workspace."""
        if not filename or Path(filename).name != filename:
            raise KeyError(job_id)
        projection = self._preview_projection(job_id)
        if not projection:
            raise KeyError(job_id)
        expected = next(
            (str(value.get("sha256") or "") for value in projection.get("assets", {}).values()
             if value.get("filename") == filename),
            "",
        )
        if not expected:
            raise KeyError(filename)
        docs = (self.root / "var" / "workspaces" / "jobs" / job_id / "docs").resolve()
        path = (docs / filename).resolve()
        if (not path.is_relative_to(docs) or not path.is_file() or path.is_symlink()):
            raise KeyError(filename)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise KeyError(filename)
        return path

    def goal_alignment(self, *, job_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        from lca_project.kernel.goal_alignment import GoalAlignmentController
        try:
            status = GoalAlignmentController(self.root, self.control).status(job_id=job_id)
        except OSError:
            status = {"goal_contracts": [], "quality_observations": [], "deviations": [],
                      "repair_plans": [], "change_candidates": [],
                      "failure_triage_runs": [],
                      "system_repair_runs": [],
                      "validation_certificates": [], "promotion_receipts": []}
        if limit:
            for key, value in status.items():
                if isinstance(value, list):
                    status[key] = value[:limit]
        return status

    def audit_goal(self, job_id: str, *, auto_repair: bool = False) -> dict[str, Any]:
        from lca_project.kernel.goal_alignment import GoalAlignmentController
        return GoalAlignmentController(self.root, self.control).audit_job(
            job_id, auto_repair=auto_repair, trigger="dashboard"
        )

    def goal_feedback(self, job_id: str, message: str, category: str) -> dict[str, Any]:
        from lca_project.kernel.goal_alignment import GoalAlignmentController
        return GoalAlignmentController(self.root, self.control).report_user_feedback(
            job_id, message, category=category
        )

    def autonomy(self, *, campaign_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        from lca_project.kernel.goal_alignment.autonomous_supervisor import AutonomousJobSupervisor
        supervisor = AutonomousJobSupervisor(self.root, control=self.control)
        return supervisor.campaign(campaign_id) if campaign_id else supervisor.campaigns(limit=limit)

    def create_autonomy(self, spec: dict[str, Any], *, start: bool = True) -> dict[str, Any]:
        from lca_project.kernel.goal_alignment.autonomous_supervisor import AutonomousJobSupervisor
        supervisor = AutonomousJobSupervisor(self.root, control=self.control)
        created = supervisor.create_campaign(spec)
        campaign_id = str(created["campaign"]["campaign_id"])
        first_tick = supervisor.tick(campaign_id, execute_task=False)
        started = self.start_autonomy(campaign_id) if start else {"status": "not_started"}
        return {**supervisor.campaign(campaign_id), "first_tick": first_tick,
                "background": started}

    def start_autonomy(self, campaign_id: str) -> dict[str, Any]:
        from lca_project.kernel.goal_alignment.autonomous_supervisor import AutonomousJobSupervisor
        # Resolve before spawning so a bad ID is returned synchronously.
        AutonomousJobSupervisor(self.root, control=self.control).campaign(campaign_id)
        with self._autonomy_lock:
            current = self._autonomy_threads.get(campaign_id)
            if current and current.is_alive():
                return {"status": "already_running", "campaign_id": campaign_id,
                        "supervisor_id": current.name}
            supervisor_id = f"dashboard-autonomy:{campaign_id[-10:]}"
            thread = threading.Thread(
                target=AutonomousJobSupervisor(
                    self.root, supervisor_id=supervisor_id
                ).run,
                args=(campaign_id,), name=supervisor_id, daemon=True,
            )
            self._autonomy_threads[campaign_id] = thread
            thread.start()
        return {"status": "started", "campaign_id": campaign_id,
                "supervisor_id": supervisor_id}

    def reconcile_goal_wakeups_once(self) -> dict[str, Any]:
        """Restart campaign supervisors that have durable, unconsumed goal work."""
        if not self._has_table("goal_supervisor_wakeups"):
            return {"status": "idle", "campaigns": []}
        rows = self.conn.execute(
            "SELECT DISTINCT c.campaign_id FROM goal_supervisor_wakeups w "
            "JOIN autonomous_job_items i ON i.job_id=w.job_id "
            "JOIN autonomous_campaigns c ON c.campaign_id=i.campaign_id "
            "WHERE w.status='pending' AND c.status IN ('running','completed') "
            "ORDER BY c.created_at"
        )
        started: list[dict[str, Any]] = []
        for row in rows:
            started.append(self.start_autonomy(str(row["campaign_id"])))
        return {"status": "started" if started else "idle", "campaigns": started}

    def reconcile_system_meta_once(self) -> dict[str, Any]:
        """Advance the outer repair loop before ordinary Campaign projection."""
        from lca_project.kernel.goal_alignment.meta_supervisor import SystemMetaSupervisor
        return SystemMetaSupervisor(
            self.root, control=self.control, supervisor_id="dashboard-system-meta"
        ).reconcile()

    def start_goal_reconciler(self, *, poll_seconds: float = 2.0) -> None:
        if self._goal_reconciler_thread and self._goal_reconciler_thread.is_alive():
            return
        self._goal_reconciler_stop.clear()

        def loop() -> None:
            while not self._goal_reconciler_stop.is_set():
                try:
                    self.reconcile_system_meta_once()
                    self.reconcile_goal_wakeups_once()
                except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error):
                    # Individual campaign/audit events retain the detailed
                    # failure.  The service loop remains alive for later work.
                    pass
                self._goal_reconciler_stop.wait(max(0.1, poll_seconds))

        self._goal_reconciler_thread = threading.Thread(
            target=loop, name="dashboard-goal-reconciler", daemon=True,
        )
        self._goal_reconciler_thread.start()

    def stop_goal_reconciler(self) -> None:
        self._goal_reconciler_stop.set()
        if self._goal_reconciler_thread:
            self._goal_reconciler_thread.join(timeout=3)

    def pause_autonomy(self, campaign_id: str) -> dict[str, Any]:
        from lca_project.kernel.goal_alignment.autonomous_supervisor import AutonomousJobSupervisor
        return AutonomousJobSupervisor(self.root, control=self.control).pause(campaign_id)

    def resume_autonomy(self, campaign_id: str, *, start: bool = True) -> dict[str, Any]:
        from lca_project.kernel.goal_alignment.autonomous_supervisor import AutonomousJobSupervisor
        result = AutonomousJobSupervisor(self.root, control=self.control).resume(campaign_id)
        return {**result, "background": self.start_autonomy(campaign_id) if start else None}

    def workflow_runs(self, *, status: str = "", limit: int = 100) -> dict[str, Any]:
        if not self._has_table("orchestrator_runs"):
            return {"items": [], "total": 0}
        params: list[Any] = []
        where = ""
        if status:
            where, params = "WHERE o.status=?", [status]
        rows = self._rows(
            "SELECT o.*,j.status AS job_status,j.payload AS job_payload,"
            "COUNT(t.task_id) AS task_count,"
            "SUM(CASE WHEN t.status='succeeded' THEN 1 ELSE 0 END) AS succeeded_count,"
            "SUM(CASE WHEN t.status='ready' THEN 1 ELSE 0 END) AS ready_count "
            "FROM orchestrator_runs o JOIN jobs j ON j.id=o.job_id "
            "LEFT JOIN orchestrator_tasks t ON t.run_id=o.run_id "
            f"{where} GROUP BY o.run_id ORDER BY o.updated_at DESC LIMIT ?", (*params, min(max(limit, 1), 200))
        )
        for item in rows:
            payload = item.pop("job_payload", {}) or {}
            item["target"] = payload.get("target", "—")
        return {"items": rows, "total": self._count("orchestrator_runs")}

    def artifacts(self, *, query: str = "", media_type: str = "", limit: int = 60,
                  offset: int = 0) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if query:
            clauses.append("(digest LIKE ? OR metadata LIKE ?)")
            params.extend((f"%{query}%", f"%{query}%"))
        if media_type:
            clauses.append("media_type=?")
            params.append(media_type)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = int(self.conn.execute(f"SELECT COUNT(*) FROM artifacts{where}", tuple(params)).fetchone()[0])
        rows = self._rows(
            f"SELECT * FROM artifacts{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, min(max(limit, 1), 200), max(offset, 0)),
        )
        types = [dict(row) for row in self.conn.execute(
            "SELECT media_type,COUNT(*) AS count FROM artifacts GROUP BY media_type ORDER BY count DESC"
        )]
        return {"items": rows, "total": total, "types": types}

    def artifact(self, digest: str) -> dict[str, Any]:
        artifact = _row(self.conn.execute("SELECT * FROM artifacts WHERE digest=?", (digest,)).fetchone())
        if artifact is None:
            raise KeyError(digest)
        edges = self._rows(
            "SELECT * FROM artifact_edges WHERE parent_digest=? OR child_digest=? ORDER BY created_at",
            (digest, digest),
        )
        preview: Any = None
        preview_type = "none"
        path = Path(str(artifact["uri"]))
        if path.is_file() and path.stat().st_size <= 512_000:
            raw = path.read_bytes()
            if artifact["media_type"] == "application/json":
                preview, preview_type = _json(raw.decode("utf-8", "replace")), "json"
            elif artifact["media_type"].startswith("text/") or artifact["media_type"] in {
                "application/xml", "application/javascript"
            }:
                preview, preview_type = raw.decode("utf-8", "replace"), "text"
        return {"artifact": artifact, "edges": edges, "preview": preview, "preview_type": preview_type}

    def events(self, *, query: str = "", event_type: str = "", limit: int = 100,
               after: int = 0) -> dict[str, Any]:
        clauses = ["sequence>?"]
        params: list[Any] = [max(after, 0)]
        if query:
            clauses.append("(aggregate_id LIKE ? OR event_type LIKE ? OR actor LIKE ? OR payload LIKE ?)")
            needle = f"%{query}%"
            params.extend((needle, needle, needle, needle))
        if event_type:
            clauses.append("event_type=?")
            params.append(event_type)
        rows = self._rows(
            "SELECT * FROM events WHERE " + " AND ".join(clauses) +
            " ORDER BY sequence DESC LIMIT ?", (*params, min(max(limit, 1), 500))
        )
        types = [dict(row) for row in self.conn.execute(
            "SELECT event_type,COUNT(*) AS count FROM events GROUP BY event_type ORDER BY count DESC,event_type"
        )]
        return {"items": rows, "total": self._count("events"), "types": types}

    def exceptions(self, *, status: str = "", limit: int = 100) -> dict[str, Any]:
        where, params = (" WHERE status=?", (status,)) if status else ("", ())
        items = self._rows(
            f"SELECT * FROM exceptions{where} ORDER BY opened_at DESC LIMIT ?",
            (*params, min(max(limit, 1), 200)),
        )
        faults = self._rows(
            "SELECT * FROM wiki_runtime_faults ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 200),)
        ) if self._has_table("wiki_runtime_faults") else []
        return {"items": items, "faults": faults, "total": self._count("exceptions") + len(faults)}

    def system(self) -> dict[str, Any]:
        capabilities = []
        try:
            registry = CapabilityRegistry.load_directory(self.root / "capabilities")
            raw_by_id = {}
            for path in (self.root / "capabilities").glob("*.json"):
                raw = load_json(path)
                raw_by_id[str(raw.get("capability_id", raw.get("id", "")))] = raw
            capabilities = [{"id": item.id, "version": item.version,
                             "executor": raw_by_id.get(item.id, {}).get("executor", "unknown"),
                             "side_effects": item.side_effects,
                             "production_ready": raw_by_id.get(item.id, {}).get("production_ready", False),
                             "tags": raw_by_id.get(item.id, {}).get("tags", [])}
                            for item in registry.all()]
        except (OSError, ValueError):
            pass
        skills = []
        try:
            skills = [{"name": item.name, "version": item.version, "workflow": item.workflow_ref,
                       "policy": item.policy_version} for item in SkillRegistry(self.root).all()]
        except (OSError, ValueError):
            pass
        workflows = []
        known = {item["id"] for item in capabilities}
        for path in sorted((self.root / "workflows").glob("*.json")):
            try:
                spec = WorkflowSpec.from_mapping(load_json(path))
                compiled = compile_workflow(spec, known)
                workflows.append({"ref": path.stem, "id": spec.id, "version": spec.version,
                                  "tasks": len(spec.tasks), "status": "valid", "order": list(compiled.order)})
            except (OSError, ValueError) as exc:
                workflows.append({"ref": path.stem, "status": "invalid", "error": str(exc)})
        budgets = self._rows("SELECT * FROM budgets ORDER BY scope") if self._has_table("budgets") else []
        leases = self._rows("SELECT * FROM leases ORDER BY updated_at DESC") if self._has_table("leases") else []
        stages = self._rows(
            "SELECT * FROM stage_executions ORDER BY updated_at DESC LIMIT 50"
        ) if self._has_table("stage_executions") else []
        proofs = self._rows(
            "SELECT proof_id,kind,subject,producer,artifact_digest,created_at FROM proof_receipts "
            "ORDER BY created_at DESC LIMIT 50"
        ) if self._has_table("proof_receipts") else []
        return {"root": str(self.root), "database": str(self.state.path), "capabilities": capabilities,
                "skills": skills, "workflows": workflows, "budgets": budgets, "leases": leases,
                "stages": stages, "proofs": proofs}

    def materialize(self, job_id: str) -> dict[str, Any]:
        orchestrator = PersistentOrchestrator(self.root)
        run_id = orchestrator.materialize(job_id)
        return {"status": "ok", "run_id": run_id,
                "tasks": [item.__dict__ for item in orchestrator.tasks(run_id)]}

    def recover(self, run_id: str, task_id: str) -> dict[str, Any]:
        orchestrator = PersistentOrchestrator(self.root)
        orchestrator.recover(run_id, task_id)
        return {"status": "ok", "run_id": run_id, "task_id": task_id}

    def pause_job(self, job_id: str) -> dict[str, Any]:
        return self.control.pause_job(job_id, reason="Dashboard operator requested pause")

    def resume_job(self, job_id: str) -> dict[str, Any]:
        return self.control.resume_job(job_id, reason="Dashboard operator resumed Job")

    def start_worker(self, job_id: str) -> dict[str, Any]:
        """Start one Job-scoped background loop without blocking the HTTP request."""
        if self.state.get("jobs", job_id) is None:
            raise KeyError(job_id)
        if self.state.get("jobs", job_id)["status"] == "paused":
            raise ValueError("Job is paused; resume it before starting a worker")
        with self._worker_lock:
            current = self._workers.get(job_id)
            if current and current.is_alive():
                return {"status": "already_running", "job_id": job_id,
                        "worker_id": current.name}
            worker_id = f"dashboard:{job_id[-12:]}"
            thread = threading.Thread(
                target=WorkerLoop(self.root, worker_id=worker_id).run,
                kwargs={"job_id": job_id, "poll_seconds": 1.0},
                name=worker_id, daemon=True,
            )
            self._workers[job_id] = thread
            thread.start()
        return {"status": "started", "job_id": job_id, "worker_id": worker_id}

    def _workflow_detail(self, workflow_ref: str) -> dict[str, Any] | None:
        if not workflow_ref:
            return None
        path = self.root / "workflows" / f"{workflow_ref}.json"
        if not path.is_file():
            return None
        raw = load_json(path)
        spec = WorkflowSpec.from_mapping(raw)
        return {"ref": workflow_ref, "description": raw.get("description", ""),
                "policy_version": raw.get("policy_version"), "steps": raw.get("steps", raw.get("tasks", [])),
                "task_count": len(spec.tasks)}
