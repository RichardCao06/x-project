"""Finite, policy-bounded autonomous Job creation and execution campaigns."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any
import uuid

from ...control import ControlPlane
from ..leases import LeaseLost
from ..orchestrator import PersistentOrchestrator
from ..skills import SkillInvoker
from ..state import utcnow
from ..worker import WorkerLoop
from .controller import GoalAlignmentController
from .execution_ownership import ExecutionOwnership
from .system_repair_agent import SystemRepairAgent
from .work_dispatcher import (
    dispatch_failure_triage,
    dispatch_scm_publication,
    dispatch_system_repair,
)
from .store import AlignmentStore, canonical, digest


ACTIVE_JOB_STATES = {"planned", "ready", "leased", "running", "stalled", "retryable",
                     "repairable", "manual_review", "blocked_budget"}
GOAL_READY_JOB_STATES = {"candidate", "gated", "applied", "published"}
LIMITED_JOB_STATES = {"diagnostic_preview", "evidence_limited"}
FAILURE_JOB_STATES = {"failed", "quarantined", "superseded"}
ITEM_TERMINAL = {"succeeded", "evidence_limited", "failed", "blocked",
                 "awaiting_approval", "superseded"}
COMPLETION_GOALS = {"lca_modeling_ready", "reviewed_publication", "workflow_delivery"}
SCM_PUBLICATION_RETRY_SECONDS = 300
_DEFAULT_SYSTEM_REPAIR_AGENT = SystemRepairAgent


def _dispatch_repair(root: Path, repair_run_id: str) -> dict[str, Any]:
    # Keep the Agent dependency injectable for deterministic unit tests while
    # production execution remains outside the Supervisor thread.
    if SystemRepairAgent is not _DEFAULT_SYSTEM_REPAIR_AGENT:
        return SystemRepairAgent(root).execute(repair_run_id)
    dispatch_system_repair(root, repair_run_id)
    return {"repair_run_id": repair_run_id, "status": "dispatched"}


def _dispatch_scm(root: Path, repair_run_id: str) -> dict[str, Any]:
    if SystemRepairAgent is not _DEFAULT_SYSTEM_REPAIR_AGENT:
        return SystemRepairAgent(root).publish_scm(repair_run_id)
    dispatch_scm_publication(root, repair_run_id)
    return {"repair_run_id": repair_run_id, "status": "scm_dispatched"}


def _scm_publication_retry_due(updated_at: str) -> bool:
    try:
        updated = datetime.fromisoformat(updated_at)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - updated).total_seconds() >= (
            SCM_PUBLICATION_RETRY_SECONDS
        )
    except (TypeError, ValueError):
        return True


def verify_reviewed_publication(
    control: ControlPlane, job_id: str, run_id: str | None,
) -> tuple[bool, str | None]:
    """Verify the immutable publish manifest and its Job-bound release record."""
    if not run_id:
        return False, "reviewed publication has no Workflow Run"
    task = control.state._connection().execute(
        "SELECT status,output_hash FROM orchestrator_tasks "
        "WHERE run_id=? AND task_id='publish'",
        (run_id,),
    ).fetchone()
    if not task or task["status"] != "succeeded" or not task["output_hash"]:
        return False, "publish task has no successful immutable output manifest"
    try:
        manifest = control.artifacts.verify_task_output_manifest(str(task["output_hash"]))
        release_file = next(
            item for item in manifest.get("files") or []
            if str(item.get("path") or "").endswith("/release-record.json")
            or str(item.get("path") or "") == "release-record.json"
        )
        record = json.loads(control.artifacts.get_bytes(str(release_file["sha256"])))
    except (KeyError, StopIteration, ValueError, RuntimeError, OSError, json.JSONDecodeError):
        return False, "publish output does not contain a valid immutable release-record"
    required_hashes = (
        "gate_report_sha256", "reviewed_apply_sha256", "publish_report_sha256",
    )
    valid = bool(
        record.get("protocol") == "release-record-v1"
        and record.get("publication_status") == "published"
        and record.get("job_id") == job_id
        and record.get("release_id")
        and record.get("candidate_hashes")
        and all(len(str(record.get(name) or "")) == 64 for name in required_hashes)
    )
    return (True, None) if valid else (
        False, "release-record is not bound to this Job and reviewed publication proofs",
    )


class AutonomousJobSupervisor:
    """Create Jobs only through Skills, then supervise them to an honest terminal state."""

    MAX_CONSECUTIVE_CYCLE_FAILURES = 3

    def __init__(self, root: str | Path, *, supervisor_id: str | None = None,
                 control: ControlPlane | None = None) -> None:
        self.root = Path(root).resolve()
        self.control = control or ControlPlane(self.root)
        self.state = self.control.state
        self.supervisor_id = supervisor_id or f"autonomy:{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _validate_spec(spec: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(spec, dict) or spec.get("schema_version") != "autonomous-job-campaign-v1":
            raise ValueError("campaign requires schema_version=autonomous-job-campaign-v1")
        allowed = {"schema_version", "name", "skill", "requests", "max_concurrency",
                   "max_auto_repairs_per_job", "completion_goal", "poll_seconds",
                   "stop_on_failure"}
        extras = set(spec) - allowed
        if extras:
            raise ValueError(f"unknown autonomous campaign fields: {sorted(extras)}")
        name, skill, requests = str(spec.get("name") or "").strip(), str(spec.get("skill") or "").strip(), spec.get("requests")
        if not name or len(name) > 120 or not skill:
            raise ValueError("campaign name and skill are required")
        if not isinstance(requests, list) or not 1 <= len(requests) <= 100:
            raise ValueError("campaign requests must contain 1..100 objects")
        if not all(isinstance(item, dict) and item for item in requests):
            raise ValueError("each autonomous request must be a non-empty object")
        request_hashes = [digest(item) for item in requests]
        if len(request_hashes) != len(set(request_hashes)):
            raise ValueError("campaign requests must be unique")
        max_concurrency = int(spec.get("max_concurrency", 1))
        max_repairs = int(spec.get("max_auto_repairs_per_job", 3))
        poll_seconds = float(spec.get("poll_seconds", 2))
        if not 1 <= max_concurrency <= 8:
            raise ValueError("max_concurrency must be 1..8")
        if not 0 <= max_repairs <= 20:
            raise ValueError("max_auto_repairs_per_job must be 0..20")
        if not 0 < poll_seconds <= 300:
            raise ValueError("poll_seconds must be >0 and <=300")
        request_modes = {
            str(item.get("publication_mode") or "preview") for item in requests
        } if skill == "generate-node-wiki" else set()
        if len(request_modes) > 1:
            raise ValueError("one Wiki campaign cannot mix preview and reviewed publication modes")
        completion_goal = str(spec.get("completion_goal") or (
            "reviewed_publication"
            if skill == "generate-node-wiki" and request_modes == {"reviewed"}
            else "lca_modeling_ready"
        ))
        if completion_goal not in COMPLETION_GOALS:
            raise ValueError(
                "completion_goal must be lca_modeling_ready, reviewed_publication, "
                "or workflow_delivery"
            )
        if completion_goal == "reviewed_publication":
            if skill != "generate-node-wiki":
                raise ValueError("reviewed_publication is supported only for generate-node-wiki")
            if request_modes != {"reviewed"}:
                raise ValueError(
                    "completion_goal=reviewed_publication requires every request to set "
                    "publication_mode=reviewed"
                )
        if (skill == "generate-node-wiki" and request_modes == {"reviewed"}
                and completion_goal == "lca_modeling_ready"):
            raise ValueError(
                "reviewed Wiki requests must use completion_goal=reviewed_publication; "
                "lca_modeling_ready would terminate before governed publication"
            )
        return {"schema_version": "autonomous-job-campaign-v1", "name": name,
                "skill": skill, "requests": requests, "max_concurrency": max_concurrency,
                "max_auto_repairs_per_job": max_repairs, "poll_seconds": poll_seconds,
                "completion_goal": completion_goal,
                "stop_on_failure": bool(spec.get("stop_on_failure", False))}

    def create_campaign(self, spec: dict[str, Any]) -> dict[str, Any]:
        value = self._validate_spec(spec)
        # Validate and normalize every request before writing campaign facts.
        # The same public Skill boundary is used again during Job creation.
        invoker = SkillInvoker(self.root)
        value["requests"] = [
            invoker.validate_request(value["skill"], request)
            for request in value["requests"]
        ]
        if value["completion_goal"] == "reviewed_publication" and any(
            request.get("publication_mode") != "reviewed" for request in value["requests"]
        ):
            raise ValueError(
                "completion_goal=reviewed_publication requires normalized reviewed requests"
            )
        normalized_hashes = [digest(request) for request in value["requests"]]
        if len(normalized_hashes) != len(set(normalized_hashes)):
            raise ValueError("campaign requests must remain unique after Skill normalization")
        spec_hash = digest(value)
        campaign_id = "aut_" + spec_hash[:32]
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO autonomous_campaigns VALUES(?,?,?,?,?,?,?,?,?,?)",
                (campaign_id, value["name"], value["skill"], "running", spec_hash,
                 value["max_concurrency"], value["max_auto_repairs_per_job"],
                 canonical(value), now, now),
            )
            for ordinal, request in enumerate(value["requests"]):
                request_hash = digest(request)
                item_id = "aji_" + digest({"campaign": campaign_id,
                                            "request": request_hash})[:32]
                conn.execute(
                    "INSERT OR IGNORE INTO autonomous_job_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (item_id, campaign_id, ordinal, request_hash, "pending", None, None,
                     0, None, None, canonical({"request": request}), now, now),
                )
        self.control.events.append("autonomous_campaign", campaign_id,
                                   "autonomy.campaign_created", {
                                       "name": value["name"], "skill": value["skill"],
                                       "requests": len(value["requests"]),
                                       "max_concurrency": value["max_concurrency"],
                                   }, actor="autonomous-supervisor")
        return self.campaign(campaign_id)

    def campaign(self, campaign_id: str) -> dict[str, Any]:
        row = self.state._connection().execute(
            "SELECT * FROM autonomous_campaigns WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        if row is None:
            raise KeyError(campaign_id)
        campaign = dict(row)
        campaign["payload"] = json.loads(campaign["payload"])
        items = []
        for item_row in self.state._connection().execute(
            "SELECT * FROM autonomous_job_items WHERE campaign_id=? ORDER BY ordinal",
            (campaign_id,),
        ):
            item = dict(item_row); item["payload"] = json.loads(item["payload"])
            items.append(item)
        heartbeat = self.state._connection().execute(
            "SELECT * FROM autonomous_supervisor_heartbeats WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        return {"campaign": campaign, "items": items,
                "supervisor": dict(heartbeat) if heartbeat else None}

    def campaigns(self, *, limit: int = 100) -> dict[str, Any]:
        rows = list(self.state._connection().execute(
            "SELECT campaign_id FROM autonomous_campaigns ORDER BY created_at DESC LIMIT ?",
            (min(max(int(limit), 1), 200),),
        ))
        return {"items": [self.campaign(str(row["campaign_id"])) for row in rows],
                "total": int(self.state._connection().execute(
                    "SELECT COUNT(*) FROM autonomous_campaigns").fetchone()[0])}

    def _heartbeat(self, campaign_id: str, status: str, *, item_id: str | None = None,
                   error: str | None = None) -> None:
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT INTO autonomous_supervisor_heartbeats VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(campaign_id) DO UPDATE SET supervisor_id=excluded.supervisor_id,"
                "status=excluded.status,current_item_id=excluded.current_item_id,"
                "cycle=autonomous_supervisor_heartbeats.cycle+1,last_error=excluded.last_error,"
                "heartbeat_at=excluded.heartbeat_at",
                (campaign_id, self.supervisor_id, status, item_id, 1, error, now, now),
            )

    def _sync_item(self, item: dict[str, Any]) -> dict[str, Any]:
        job_id = item.get("job_id")
        if not job_id:
            return item
        # A durable wakeup is runnable supervision work even when the previous
        # workflow projection already reached an evidence-limited terminal.
        pending_supervision = bool(
            AlignmentStore(self.state).pending_wakeups(job_id=str(job_id))
        )
        job = self.state.get("jobs", str(job_id))
        run = self.state._connection().execute(
            "SELECT run_id,status FROM orchestrator_runs WHERE job_id=?", (job_id,)
        ).fetchone()
        status = item["status"]
        last_error = item.get("last_error")
        completion_goal = self._completion_goal_for_item(item)
        if job is None:
            status = "failed"
        elif pending_supervision:
            # A terminal workflow state does not cancel newly durable
            # supervision work.  Keep the Item runnable long enough to consume
            # the wakeup and queue Triage; WorkerLoop will still refuse to
            # execute a quarantined DAG task.
            status = "running"
            last_error = None
        elif job["status"] in FAILURE_JOB_STATES:
            status = "superseded" if job["status"] == "superseded" else "failed"
        # An active Job state is authoritative over a previously succeeded run:
        # preview generation can finish while the declared modelling goal still
        # has bounded repair work to perform.
        elif job["status"] in ACTIVE_JOB_STATES:
            status = "running"
            last_error = None
        elif job["status"] in LIMITED_JOB_STATES:
            status = "evidence_limited"
        elif completion_goal == "reviewed_publication":
            if job["status"] == "published":
                proof_valid, proof_error = self._valid_release_proof(
                    str(job_id), str(run["run_id"]) if run else item.get("run_id")
                )
                status = "succeeded" if proof_valid else "blocked"
                last_error = None if proof_valid else proof_error
            elif job["status"] in GOAL_READY_JOB_STATES:
                unfinished = bool(run and self.state._connection().execute(
                    "SELECT 1 FROM orchestrator_tasks WHERE run_id=? "
                    "AND status NOT IN ('succeeded','skipped','failed','quarantined') LIMIT 1",
                    (run["run_id"],),
                ).fetchone())
                if unfinished or (run and run["status"] != "succeeded"):
                    status = "running"
                    last_error = None
                else:
                    status = "blocked"
                    last_error = (
                        "reviewed_publication requires Job status=published and a valid "
                        "hash-bound release-record proof"
                    )
            elif run and run["status"] == "succeeded":
                status = "blocked"
                last_error = "workflow ended without satisfying reviewed_publication"
        elif completion_goal == "lca_modeling_ready" and job["status"] in GOAL_READY_JOB_STATES:
            status = "succeeded"
            last_error = None
        elif completion_goal == "workflow_delivery" and run and run["status"] == "succeeded":
            status = "succeeded"
            last_error = None
        elif job["status"] == "paused":
            status = "paused"
        with self.state.transaction() as conn:
            conn.execute(
                "UPDATE autonomous_job_items SET status=?,run_id=?,last_error=?,updated_at=? "
                "WHERE item_id=?",
                (status, str(run["run_id"]) if run else item.get("run_id"),
                 last_error, utcnow(), item["item_id"]),
            )
        return {**item, "status": status,
                "run_id": str(run["run_id"]) if run else item.get("run_id"),
                "last_error": last_error}

    def _completion_goal_for_item(self, item: dict[str, Any]) -> str:
        row = self.state._connection().execute(
            "SELECT payload FROM autonomous_campaigns WHERE campaign_id=?",
            (item["campaign_id"],),
        ).fetchone()
        payload = json.loads(row["payload"]) if row else {}
        return str(payload.get("completion_goal") or "lca_modeling_ready")

    def _valid_release_proof(
        self, job_id: str, run_id: str | None,
    ) -> tuple[bool, str | None]:
        return verify_reviewed_publication(self.control, job_id, run_id)

    def _create_item_job(self, campaign: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        request = item["payload"]["request"]
        accepted = SkillInvoker(self.root).invoke(
            campaign["skill"], request,
            idempotency_key=f"autonomy:{campaign['campaign_id']}:{item['item_id']}",
        )
        job_id = str(accepted["job_id"])
        run_id = PersistentOrchestrator(self.root).materialize(job_id)
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute("UPDATE autonomous_job_items SET status='running',job_id=?,run_id=?,"
                         "last_error=NULL,updated_at=? WHERE item_id=?",
                         (job_id, run_id, now, item["item_id"]))
        self.control.events.append("autonomous_campaign", campaign["campaign_id"],
                                   "autonomy.job_created", {
                                       "item_id": item["item_id"], "job_id": job_id,
                                       "run_id": run_id, "deduplicated": accepted["deduplicated"],
                                   }, actor="autonomous-supervisor")
        return {**item, "status": "running", "job_id": job_id, "run_id": run_id}

    def _supervise_item(self, campaign: dict[str, Any], item: dict[str, Any], *,
                        execute_task: bool) -> dict[str, Any]:
        job_id = str(item["job_id"])
        store = AlignmentStore(self.state)
        claimed_wakeups = [
            str(row["wakeup_id"]) for row in store.pending_wakeups(job_id=job_id)
        ]
        # A queued repair is durable work in its own right.  Consume one before
        # starting a fresh audit so a later poison deviation cannot orphan a
        # repair that a previous cycle already committed.
        runnable_repair = self.state._connection().execute(
            "SELECT r.repair_run_id FROM system_repair_runs r WHERE r.source_job_id=? "
            "AND (r.status='queued' OR (r.status='failed' "
            "AND last_error='canary validation failed' "
            "AND json_extract(r.payload,'$.validation_replan') IS NULL "
            "AND json_extract(r.payload,'$.validation_replan_exhausted') IS NULL) "
            "OR (r.status='failed' AND r.last_error="
            "'coding Agent may not edit generated integrity manifests directly' "
            "AND json_extract(r.payload,'$.coding_retry_exhausted') IS NULL)) "
            "AND NOT EXISTS (SELECT 1 FROM system_repair_runs newer "
            "WHERE newer.source_job_id=r.source_job_id "
            "AND newer.created_at>r.created_at) "
            "ORDER BY r.created_at DESC LIMIT 1", (job_id,),
        ).fetchone()
        system_repairs = []
        if runnable_repair:
            repair_run_id = str(runnable_repair["repair_run_id"])
            system_repairs.append(_dispatch_repair(self.root, repair_run_id))
        pending_scm = self.state._connection().execute(
            "SELECT repair_run_id,updated_at FROM system_repair_runs "
            "WHERE source_job_id=? AND status='awaiting_scm_publication' "
            "ORDER BY updated_at LIMIT 1", (job_id,),
        ).fetchone()
        if pending_scm and _scm_publication_retry_due(str(pending_scm["updated_at"])):
            repair_run_id = str(pending_scm["repair_run_id"])
            system_repairs.append(_dispatch_scm(self.root, repair_run_id))
        allow_repair = int(item["repair_count"]) < int(campaign["max_auto_repairs_per_job"])
        audit = GoalAlignmentController(self.root).audit_job(
            job_id, auto_repair=allow_repair,
            trigger=f"autonomy:{campaign['campaign_id']}", execute_triage=False,
        )
        consumed_wakeups = store.consume_wakeups(
            job_id=job_id, consumer=self.supervisor_id, wakeup_ids=claimed_wakeups
        )
        executed_repair_ids = {
            str(repair["repair_run_id"]) for repair in system_repairs
        }
        for action in audit["actions"]:
            if str(action.get("status") or "") in {
                "failure_triage_queued", "failure_triage_investigating",
            }:
                triage_run_id = str(action.get("triage_run_id") or "")
                if triage_run_id:
                    dispatch_failure_triage(self.root, triage_run_id)
                    action["execution_status"] = "dispatched"
            repair_run_id = str(action.get("repair_run_id") or "")
            if (action.get("status") == "system_repair_queued"
                    and repair_run_id not in executed_repair_ids):
                result = _dispatch_repair(self.root, repair_run_id)
                action["execution_status"] = result["status"]
                system_repairs.append(result)
                executed_repair_ids.add(repair_run_id)
        repairs = sum(action.get("status") == "scheduled" for action in audit["actions"])
        repairs += sum(item.get("status") in {
            "awaiting_outcome_validation", "effective", "partially_effective", "ineffective"
        } for item in system_repairs)
        outcome_validation_pending = bool(self.state._connection().execute(
            "SELECT 1 FROM system_repair_runs WHERE source_job_id=? "
            "AND status='awaiting_outcome_validation' LIMIT 1", (job_id,),
        ).fetchone())
        if repairs:
            with self.state.transaction() as conn:
                conn.execute("UPDATE autonomous_job_items SET repair_count=repair_count+?,"
                             "last_audit_at=?,updated_at=? WHERE item_id=?",
                             (repairs, utcnow(), utcnow(), item["item_id"]))
        else:
            with self.state.transaction() as conn:
                conn.execute("UPDATE autonomous_job_items SET last_audit_at=?,updated_at=? WHERE item_id=?",
                             (utcnow(), utcnow(), item["item_id"]))
        awaiting = next(
            (repair for repair in system_repairs
             if repair.get("status") == "awaiting_approval"), None
        )
        if awaiting:
            status = "awaiting_approval"
            with self.state.transaction() as conn:
                conn.execute(
                    "UPDATE autonomous_job_items SET status=?,last_error=?,updated_at=? "
                    "WHERE item_id=?",
                    (status,
                     f"validated system repair awaits minimal promotion approval: "
                     f"{awaiting['repair_run_id']}", utcnow(), item["item_id"]),
                )
            return {"status": status, "job_id": job_id, "audit": audit,
                    "consumed_wakeups": consumed_wakeups,
                    "system_repair": awaiting}
        job = self.state.get("jobs", job_id)
        ready_work = bool(self.state._connection().execute(
            "SELECT 1 FROM orchestrator_tasks WHERE run_id=? AND status='ready' LIMIT 1",
            (item.get("run_id"),),
        ).fetchone())
        meta_work = bool(self.state._connection().execute(
            "SELECT 1 FROM control_plane_repair_jobs WHERE job_id=? "
            "AND status IN ('queued','running') LIMIT 1", (job_id,),
        ).fetchone())
        if (job and job["status"] in {"manual_review", "repairable", "blocked_budget"}
                and not repairs and not outcome_validation_pending
                and not ready_work and not meta_work):
            status = "blocked"
            with self.state.transaction() as conn:
                conn.execute("UPDATE autonomous_job_items SET status=?,last_error=?,updated_at=? "
                             "WHERE item_id=?", (status, f"job requires attention: {job['status']}",
                                                  utcnow(), item["item_id"]))
            return {"status": status, "job_id": job_id, "audit": audit,
                    "consumed_wakeups": consumed_wakeups}
        cycle = None
        if execute_task and job and job["status"] != "paused":
            cycle = WorkerLoop(
                self.root, worker_id=f"{self.supervisor_id}:{item['item_id'][-8:]}"
            ).run(job_id=job_id, once=True)
        synced = self._sync_item({**item, "repair_count": int(item["repair_count"]) + repairs})
        return {"status": synced["status"], "job_id": job_id,
                "worker_cycle": cycle.__dict__ if cycle else None, "audit": audit,
                "consumed_wakeups": consumed_wakeups}

    def _reactivate_for_goal_work(self, view: dict[str, Any]) -> dict[str, Any]:
        """Reopen a terminal campaign when later facts invalidate its goal claim."""
        campaign = view["campaign"]
        status = str(campaign["status"])
        if status not in {"completed", "needs_attention"}:
            return view
        store = AlignmentStore(self.state)
        affected: list[tuple[dict[str, Any], str]] = []
        for item in view["items"]:
            job_id = str(item.get("job_id") or "")
            if not job_id:
                continue
            pending = store.pending_wakeups(job_id=job_id)
            if pending:
                affected.append((item, "pending_supervision_wakeup"))
                continue
            recoverable_repair = self.state._connection().execute(
                "SELECT r.repair_run_id FROM system_repair_runs r WHERE r.source_job_id=? "
                "AND (r.status IN ('queued','coding','validating','awaiting_scm_publication') "
                "OR (r.status='failed' AND r.last_error='canary validation failed' "
                "AND json_extract(r.payload,'$.validation_replan') IS NULL "
                "AND json_extract(r.payload,'$.validation_replan_exhausted') IS NULL) "
                "OR (r.status='failed' AND r.last_error="
                "'coding Agent may not edit generated integrity manifests directly' "
                "AND json_extract(r.payload,'$.coding_retry_exhausted') IS NULL)) "
                "AND NOT EXISTS (SELECT 1 FROM system_repair_runs newer "
                "WHERE newer.source_job_id=r.source_job_id "
                "AND newer.created_at>r.created_at) "
                "LIMIT 1",
                (job_id,),
            ).fetchone()
            if recoverable_repair:
                affected.append((item, "recoverable_system_repair"))
                continue
            # Backfill the protocol for campaigns that ended before migration
            # v10 existed.  Only a deviation newer than a completed campaign is
            # eligible; needs_attention requires a genuinely new wakeup to
            # prevent a tight reopen/close loop.
            if status != "completed":
                continue
            deviation = self.state._connection().execute(
                "SELECT deviation_id,updated_at FROM deviation_reports "
                "WHERE job_id=? AND status='open' AND severity IN ('critical','high') "
                "AND updated_at>? ORDER BY updated_at DESC LIMIT 1",
                (job_id, campaign["updated_at"]),
            ).fetchone()
            if deviation:
                store.request_supervision(
                    job_id=job_id, run_id=item.get("run_id"),
                    reason="terminal_campaign_goal_reconciliation",
                    deviation_ids=[str(deviation["deviation_id"])],
                    observation_hash=str(deviation["updated_at"]),
                    context={"campaign_id": campaign["campaign_id"]},
                )
                affected.append((item, "newer_open_goal_deviation"))
        if not affected:
            return view
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute(
                "UPDATE autonomous_campaigns SET status='running',updated_at=? "
                "WHERE campaign_id=?", (now, campaign["campaign_id"]),
            )
            for item, _reason in affected:
                conn.execute(
                    "UPDATE autonomous_job_items SET status='running',last_error=NULL,"
                    "updated_at=? WHERE item_id=?", (now, item["item_id"]),
                )
        details = {
            "previous_status": status,
            "items": [{"item_id": item["item_id"], "job_id": item["job_id"],
                       "reason": reason} for item, reason in affected],
        }
        self.control.events.append(
            "autonomous_campaign", campaign["campaign_id"],
            "autonomy.campaign_reactivated", details,
            actor="autonomous-supervisor",
        )
        return self.campaign(str(campaign["campaign_id"]))

    def _finish_campaign_if_terminal(self, campaign_id: str) -> str:
        rows = list(self.state._connection().execute(
            "SELECT status,job_id,run_id FROM autonomous_job_items WHERE campaign_id=?",
            (campaign_id,),
        ))
        statuses = {str(row["status"]) for row in rows}
        if rows and all(str(row["status"]) in ITEM_TERMINAL for row in rows):
            campaign_row = self.state._connection().execute(
                "SELECT payload FROM autonomous_campaigns WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
            campaign_payload = json.loads(campaign_row["payload"]) if campaign_row else {}
            completion_goal = campaign_payload.get("completion_goal", "lca_modeling_ready")
            workflow_complete = statuses <= {"succeeded", "evidence_limited"}
            if completion_goal == "reviewed_publication":
                goal_complete = statuses <= {"succeeded"} and all(
                    self._valid_release_proof(
                        str(row["job_id"]), str(row["run_id"]) if row["run_id"] else None,
                    )[0]
                    for row in rows
                )
            else:
                goal_complete = statuses <= {"succeeded"} or (
                    completion_goal == "workflow_delivery" and workflow_complete
                )
            target = "completed" if goal_complete else "needs_attention"
            with self.state.transaction() as conn:
                conn.execute("UPDATE autonomous_campaigns SET status=?,updated_at=? WHERE campaign_id=?",
                             (target, utcnow(), campaign_id))
            return target
        return "running"

    def _tick_owned(self, campaign_id: str, *, execute_task: bool) -> dict[str, Any]:
        view = self.campaign(campaign_id)
        view = self._reactivate_for_goal_work(view)
        campaign = view["campaign"]
        if campaign["status"] == "paused":
            self._heartbeat(campaign_id, "paused")
            return {"campaign_id": campaign_id, "status": "paused", "action": "none"}
        if campaign["status"] in {"completed", "needs_attention"}:
            self._heartbeat(campaign_id, campaign["status"])
            return {"campaign_id": campaign_id, "status": campaign["status"], "action": "none"}
        items = [self._sync_item(item) for item in view["items"]]
        if (campaign["payload"].get("stop_on_failure")
                and any(item["status"] in {"failed", "blocked"} for item in items)):
            with self.state.transaction() as conn:
                conn.execute("UPDATE autonomous_campaigns SET status='needs_attention',updated_at=? "
                             "WHERE campaign_id=?", (utcnow(), campaign_id))
            self._heartbeat(campaign_id, "needs_attention")
            return {"campaign_id": campaign_id, "status": "needs_attention",
                    "created_jobs": [], "action": "stopped_on_failure"}
        active = [item for item in items if item["status"] in {"running", "created"}]
        pending = [item for item in items if item["status"] == "pending"]
        created: list[str] = []
        available = max(0, int(campaign["max_concurrency"]) - len(active))
        for item in pending[:available]:
            item = self._create_item_job(campaign, item)
            active.append(item); created.append(str(item["job_id"]))
        runnable = [item for item in active if item["status"] == "running"]
        # Rotate across active Jobs so one long Wiki does not starve later
        # requests in the same bounded-concurrency campaign.
        target = min(runnable, key=lambda item: str(item.get("last_audit_at") or "")) \
            if runnable else None
        action: dict[str, Any] | None = None
        if target:
            self._heartbeat(campaign_id, "running", item_id=target["item_id"])
            action = self._supervise_item(campaign, target, execute_task=execute_task)
        status = self._finish_campaign_if_terminal(campaign_id)
        self._heartbeat(campaign_id, status)
        return {"campaign_id": campaign_id, "status": status,
                "created_jobs": created, "action": action}

    def tick(self, campaign_id: str, *, execute_task: bool = True) -> dict[str, Any]:
        resource = f"autonomous-campaign:{campaign_id}"
        lease = self.control.leases.acquire(resource, self.supervisor_id, seconds=3600)
        try:
            return self._tick_owned(campaign_id, execute_task=execute_task)
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            self._heartbeat(campaign_id, "degraded", error=str(exc))
            raise
        finally:
            self.control.leases.release(lease)

    def _record_cycle_failure(self, campaign_id: str, exc: Exception, *,
                              consecutive_failures: int) -> str | None:
        heartbeat = self.state._connection().execute(
            "SELECT current_item_id FROM autonomous_supervisor_heartbeats "
            "WHERE campaign_id=?", (campaign_id,),
        ).fetchone()
        item_id = str(heartbeat["current_item_id"]) if (
            heartbeat and heartbeat["current_item_id"]
        ) else None
        message = f"{type(exc).__name__}: {exc}"
        if item_id:
            with self.state.transaction() as conn:
                conn.execute(
                    "UPDATE autonomous_job_items SET last_error=?,updated_at=? WHERE item_id=?",
                    (message, utcnow(), item_id),
                )
        self.control.events.append(
            "autonomous_campaign", campaign_id, "autonomy.supervision_cycle_failed",
            {"item_id": item_id, "error_type": type(exc).__name__,
             "message": str(exc), "consecutive_failures": consecutive_failures},
            actor=self.supervisor_id,
        )
        self._heartbeat(campaign_id, "degraded", item_id=item_id, error=message)
        return item_id

    def _open_cycle_circuit(self, campaign_id: str, item_id: str | None,
                            exc: Exception) -> dict[str, Any]:
        message = (
            f"supervision circuit opened after {self.MAX_CONSECUTIVE_CYCLE_FAILURES} "
            f"consecutive failures: {type(exc).__name__}: {exc}"
        )
        with self.state.transaction() as conn:
            conn.execute(
                "UPDATE autonomous_campaigns SET status='needs_attention',updated_at=? "
                "WHERE campaign_id=?", (utcnow(), campaign_id),
            )
            if item_id:
                conn.execute(
                    "UPDATE autonomous_job_items SET status='blocked',last_error=?,updated_at=? "
                    "WHERE item_id=?", (message, utcnow(), item_id),
                )
        self.control.events.append(
            "autonomous_campaign", campaign_id, "autonomy.supervision_circuit_opened",
            {"item_id": item_id, "message": message}, actor=self.supervisor_id,
        )
        self._heartbeat(campaign_id, "needs_attention", item_id=item_id, error=message)
        return {"campaign_id": campaign_id, "status": "needs_attention",
                "action": "supervision_circuit_opened", "error": message}

    def run(self, campaign_id: str, *, poll_seconds: float | None = None) -> dict[str, Any]:
        campaign = self.campaign(campaign_id)["campaign"]
        interval = float(poll_seconds or campaign["payload"].get("poll_seconds", 2))
        prior_owner = self.state._connection().execute(
            "SELECT attempt FROM goal_execution_owners "
            "WHERE execution_type='autonomous-campaign' AND execution_id=?",
            (campaign_id,),
        ).fetchone()
        ownership = ExecutionOwnership.create(
            self.control, "autonomous-campaign", campaign_id,
            attempt=int(prior_owner["attempt"] if prior_owner else 0) + 1,
            lease_seconds=60, heartbeat_seconds=10,
        )
        try:
            ownership.start()
        except LeaseLost:
            return {"campaign_id": campaign_id, "status": "already_running"}
        consecutive_failures = 0
        try:
            while True:
                try:
                    report = self._tick_owned(campaign_id, execute_task=True)
                except Exception as exc:
                    consecutive_failures += 1
                    item_id = self._record_cycle_failure(
                        campaign_id, exc, consecutive_failures=consecutive_failures,
                    )
                    ownership.current()
                    if consecutive_failures >= self.MAX_CONSECUTIVE_CYCLE_FAILURES:
                        return self._open_cycle_circuit(campaign_id, item_id, exc)
                    time.sleep(min(interval * (2 ** (consecutive_failures - 1)), 60.0))
                    continue
                consecutive_failures = 0
                if report["status"] in {"paused", "completed", "needs_attention"}:
                    return report
                ownership.current()
                time.sleep(interval)
        finally:
            ownership.close()

    def pause(self, campaign_id: str) -> dict[str, Any]:
        view = self.campaign(campaign_id)
        if view["campaign"]["status"] == "paused":
            return view
        with self.state.transaction() as conn:
            conn.execute("UPDATE autonomous_campaigns SET status='paused',updated_at=? WHERE campaign_id=?",
                         (utcnow(), campaign_id))
        for item in view["items"]:
            if item.get("job_id"):
                job = self.state.get("jobs", str(item["job_id"]))
                if job and job["status"] in ACTIVE_JOB_STATES:
                    self.control.pause_job(str(item["job_id"]), reason="autonomous campaign paused")
        return self.campaign(campaign_id)

    def resume(self, campaign_id: str) -> dict[str, Any]:
        view = self.campaign(campaign_id)
        campaign_status = str(view["campaign"]["status"])
        if campaign_status not in {"paused", "needs_attention"}:
            raise ValueError("only a paused or repaired needs-attention campaign can be resumed")
        recovered_items = 0
        for item in view["items"]:
            if item.get("job_id"):
                job = self.state.get("jobs", str(item["job_id"]))
                if job and job["status"] == "paused":
                    self.control.resume_job(str(item["job_id"]), reason="autonomous campaign resumed")
                elif (campaign_status == "needs_attention"
                      and item["status"] in {"blocked", "awaiting_approval"}
                      and job and job["status"] in {
                          "planned", "ready", "leased", "running", "stalled",
                          "retryable", "repairable",
                      }):
                    # A governed repair may have changed the causal input after
                    # the original campaign exhausted its budget.  Reopen this
                    # item as a new repair epoch; do not revive unchanged
                    # manual-review or failed Jobs.
                    with self.state.transaction() as conn:
                        conn.execute(
                            "UPDATE autonomous_job_items SET status='running',repair_count=0,"
                            "last_error=NULL,updated_at=? WHERE item_id=?",
                            (utcnow(), item["item_id"]),
                        )
                    recovered_items += 1
        if campaign_status == "needs_attention" and recovered_items == 0:
            raise ValueError("needs-attention campaign has no causally repaired runnable Job")
        with self.state.transaction() as conn:
            conn.execute("UPDATE autonomous_campaigns SET status='running',updated_at=? WHERE campaign_id=?",
                         (utcnow(), campaign_id))
        self.control.events.append("autonomous_campaign", campaign_id,
                                   "autonomy.campaign_resumed", {
                                       "from": campaign_status,
                                       "recovered_items": recovered_items,
                                   }, actor="autonomous-supervisor")
        return self.campaign(campaign_id)
