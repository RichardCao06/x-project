"""Outer control loop that can repair the ordinary repair/orchestration plane."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import uuid

from ...control import ControlPlane
from ..leases import LeaseLost
from ..state import utcnow
from ..worker import WorkerLoop
from .action_graph import compile_action_graph, runnable_automatic_actions
from .change_controller import ChangeController
from .controller import GoalAlignmentController
from .store import AlignmentStore, canonical, digest
from .system_repair_agent import SystemRepairAgent


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


class SystemMetaSupervisor:
    """Observe control-plane progress without depending on its scalar Planner route."""

    def __init__(self, root: str | Path, *, control: ControlPlane | None = None,
                 supervisor_id: str | None = None) -> None:
        self.root = Path(root).resolve()
        self.control = control or ControlPlane(self.root)
        self.state = self.control.state
        self.store = AlignmentStore(self.state)
        self.supervisor_id = supervisor_id or f"meta:{uuid.uuid4().hex[:12]}"

    def _has_table(self, name: str) -> bool:
        return self.state._connection().execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _record(self, deviation_type: str, severity: str, evidence: dict[str, Any], *,
                job_id: str | None = None, campaign_id: str | None = None) -> dict[str, Any]:
        fingerprint = digest({"type": deviation_type, "evidence": evidence})
        meta_deviation_id = "mdev_" + fingerprint[:32]
        payload = {
            "schema_version": "system-meta-deviation-v1",
            "meta_deviation_id": meta_deviation_id,
            "deviation_type": deviation_type, "severity": severity,
            "job_id": job_id, "campaign_id": campaign_id,
            "evidence": evidence,
        }
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT INTO system_meta_deviations VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(meta_deviation_id) DO UPDATE SET updated_at=excluded.updated_at",
                (meta_deviation_id, job_id, campaign_id, deviation_type, severity,
                 fingerprint, "open", canonical(payload), now, now),
            )
        return {**payload, "status": "open", "fingerprint": fingerprint}

    def _latest_safe_triages(self, job_id: str | None) -> list[dict[str, Any]]:
        query = (
            "SELECT t.*,p.action AS projected_action,p.repair_level AS projected_level "
            "FROM failure_triage_runs t LEFT JOIN repair_plans p ON p.repair_plan_id=("
            "SELECT p2.repair_plan_id FROM repair_plans p2 "
            "WHERE p2.deviation_id=t.deviation_id ORDER BY p2.updated_at DESC LIMIT 1) "
            "WHERE t.status='completed'"
        )
        params: list[Any] = []
        if job_id:
            query += " AND t.source_job_id=?"; params.append(job_id)
        query += " ORDER BY t.updated_at DESC"
        latest: dict[str, dict[str, Any]] = {}
        for raw in self.state._connection().execute(query, tuple(params)):
            row = dict(raw); payload = _json(row["payload"]); result = payload.get("result") or {}
            source_job_id = str(row["source_job_id"])
            if source_job_id in latest or result.get("safe_autonomous_actions_remaining") is not True:
                continue
            automatic = [
                item for item in result.get("actions") or []
                if item.get("authority") in {"automatic", "automatic_analysis_and_validation"}
            ]
            if automatic:
                row["payload"] = payload; row["result"] = result
                latest[source_job_id] = row
        return list(latest.values())

    def audit(self, *, job_id: str | None = None) -> list[dict[str, Any]]:
        """Detect control-plane failures using deterministic outer invariants."""
        deviations: list[dict[str, Any]] = []
        for triage in self._latest_safe_triages(job_id):
            if (str(triage.get("projected_level") or "") == "manual"
                    or str(triage.get("projected_action") or "") == "request_operator"):
                result = triage["result"]
                deviations.append(self._record(
                    "REPAIR_PLAN_PROJECTION_LOSS", "critical", {
                        "triage_run_id": triage["triage_run_id"],
                        "deviation_id": triage["deviation_id"],
                        "cause_code": result.get("cause_code"),
                        "projected_action": triage.get("projected_action"),
                        "automatic_actions": [
                            item for item in result.get("actions") or []
                            if item.get("authority") in {
                                "automatic", "automatic_analysis_and_validation"
                            }
                        ],
                    }, job_id=str(triage["source_job_id"])))

        if self._has_table("orchestrator_tasks"):
            clauses, params = [
                "t.status='ready'", "c.status IN ('blocked','needs_attention','completed')"
            ], []
            if job_id:
                clauses.append("i.job_id=?"); params.append(job_id)
            rows = self.state._connection().execute(
                "SELECT DISTINCT i.job_id,i.run_id,i.item_id,c.campaign_id,"
                "c.status AS campaign_status FROM autonomous_job_items i "
                "JOIN autonomous_campaigns c ON c.campaign_id=i.campaign_id "
                "JOIN orchestrator_tasks t ON t.run_id=i.run_id WHERE "
                + " AND ".join(clauses), tuple(params),
            )
            for row in rows:
                deviations.append(self._record(
                    "PREMATURE_SUPERVISOR_TERMINATION", "critical", {
                        "run_id": row["run_id"], "item_id": row["item_id"],
                        "campaign_status": row["campaign_status"],
                        "ready_tasks": [
                            str(item["task_id"])
                            for item in self.state._connection().execute(
                                "SELECT task_id FROM orchestrator_tasks WHERE run_id=? "
                                "AND status='ready' ORDER BY rowid", (row["run_id"],)
                            )
                        ],
                    }, job_id=str(row["job_id"]), campaign_id=str(row["campaign_id"])))

        query = (
            "SELECT r.* FROM system_repair_runs r WHERE r.status='promoted' "
            "AND NOT EXISTS(SELECT 1 FROM repair_validation_receipts v "
            "WHERE v.repair_run_id=r.repair_run_id)"
        )
        query_params: list[Any] = []
        if job_id:
            query += " AND r.source_job_id=?"; query_params.append(job_id)
        for raw in self.state._connection().execute(query, tuple(query_params)):
            repair = dict(raw); payload = _json(repair["payload"]); request = payload.get("request") or {}
            research = ((request.get("evidence") or {}).get("research_outcome") or {}).get("metrics") or {}
            if not research or not request.get("proof_contract"):
                continue
            deviations.append(self._record(
                "UNPROVEN_SYSTEM_REPAIR", "high", {
                    "repair_run_id": repair["repair_run_id"],
                    "promoted_at": payload.get("promoted_at"),
                    "recovery_task": request.get("recovery_task"),
                }, job_id=str(repair["source_job_id"])))

        open_query = (
            "SELECT d.job_id,MAX(d.updated_at) AS newest FROM deviation_reports d "
            "WHERE d.status='open' AND d.severity IN ('critical','high')"
        )
        open_params: list[Any] = []
        if job_id:
            open_query += " AND d.job_id=?"; open_params.append(job_id)
        open_query += " GROUP BY d.job_id"
        for row in self.state._connection().execute(open_query, tuple(open_params)):
            has_wakeup = self.state._connection().execute(
                "SELECT 1 FROM goal_supervisor_wakeups WHERE job_id=? AND status='pending'",
                (row["job_id"],),
            ).fetchone()
            active = self.state._connection().execute(
                "SELECT 1 FROM autonomous_job_items i JOIN autonomous_campaigns c "
                "ON c.campaign_id=i.campaign_id "
                "JOIN goal_execution_owners o ON o.execution_type='autonomous-campaign' "
                "AND o.execution_id=c.campaign_id AND o.status='running' "
                "JOIN leases l ON l.resource=o.resource AND l.holder=o.owner_id "
                "AND l.fencing_token=o.fencing_token "
                "WHERE i.job_id=? AND c.status='running' AND o.heartbeat_at>? "
                "AND l.expires_at>?",
                (row["job_id"],
                 (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(),
                 utcnow()),
            ).fetchone()
            if not has_wakeup and not active:
                deviations.append(self._record(
                    "ORPHANED_GOAL_WORK", "high", {"newest_deviation_at": row["newest"]},
                    job_id=str(row["job_id"])))
        return deviations

    def _set_meta_status(self, meta_deviation_id: str, status: str) -> None:
        with self.state.transaction() as conn:
            conn.execute(
                "UPDATE system_meta_deviations SET status=?,updated_at=? "
                "WHERE meta_deviation_id=?", (status, utcnow(), meta_deviation_id),
            )

    def _ensure_repair_job(self, deviation: dict[str, Any], triage: dict[str, Any]) -> dict[str, Any]:
        graph = compile_action_graph(str(triage["triage_run_id"]), triage["result"])
        meta_repair_id = "mrep_" + digest({
            "meta_deviation_id": deviation["meta_deviation_id"],
            "graph_id": graph["graph_id"],
        })[:32]
        payload = {
            "schema_version": "control-plane-repair-job-v1",
            "meta_repair_id": meta_repair_id,
            "meta_deviation": deviation,
            "triage": triage["result"],
            "action_graph": graph,
        }
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO control_plane_repair_jobs "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (meta_repair_id, deviation["meta_deviation_id"], triage["triage_run_id"],
                 triage["source_job_id"], "queued", str(triage["result"].get("risk") or "high"),
                 digest(graph), canonical(payload), now, now),
            )
        return self._repair_job(meta_repair_id)

    def _repair_job(self, meta_repair_id: str) -> dict[str, Any]:
        row = self.state._connection().execute(
            "SELECT * FROM control_plane_repair_jobs WHERE meta_repair_id=?",
            (meta_repair_id,),
        ).fetchone()
        if row is None:
            raise KeyError(meta_repair_id)
        result = dict(row); result["payload"] = _json(result["payload"]); return result

    def _save_repair_job(self, record: dict[str, Any], status: str,
                         graph: dict[str, Any]) -> None:
        payload = dict(record["payload"]); payload["action_graph"] = graph
        with self.state.transaction() as conn:
            conn.execute(
                "UPDATE control_plane_repair_jobs SET status=?,action_graph_hash=?,payload=?,"
                "updated_at=? WHERE meta_repair_id=?",
                (status, digest(graph), canonical(payload), utcnow(), record["meta_repair_id"]),
            )

    def _task_id(self, target: str, run_id: str) -> str:
        """Resolve an action target only against the frozen Workflow DAG."""
        task_ids = [str(row["task_id"]) for row in self.state._connection().execute(
            "SELECT task_id FROM orchestrator_tasks WHERE run_id=? ORDER BY rowid", (run_id,),
        )]
        matches = [task_id for task_id in task_ids if task_id in target]
        return max(matches, key=len) if matches else ""

    def _authorized_rewind_task(self, graph: dict[str, Any], run_id: str) -> str:
        """Resolve only a declared operator rewind action against the frozen DAG."""
        if not run_id or not self._has_table("orchestrator_tasks"):
            return ""
        task_ids = [str(row["task_id"]) for row in self.state._connection().execute(
            "SELECT task_id FROM orchestrator_tasks WHERE run_id=? ORDER BY rowid",
            (run_id,),
        )]
        declared = str(graph.get("recovery_task") or "")
        if declared in task_ids:
            return declared
        for action in graph.get("actions") or []:
            if (action.get("kind") == "rewind_task"
                    and action.get("authority") == "operator"):
                target = str(action.get("target") or "")
                rewind_clause = target.split("preserve", 1)[0].split("保留", 1)[0]
                # A range such as "content_compose through editorial_review"
                # means rewind from the first DAG node, not the longest token.
                matches = [task_id for task_id in task_ids if task_id in rewind_clause]
                if matches:
                    return matches[0]
        return ""

    def _execute_plan_projection(self, deviation: dict[str, Any]) -> dict[str, Any]:
        triage_id = str(deviation["evidence"]["triage_run_id"])
        raw = self.state._connection().execute(
            "SELECT * FROM failure_triage_runs WHERE triage_run_id=?", (triage_id,),
        ).fetchone()
        if raw is None:
            raise KeyError(triage_id)
        triage = dict(raw); triage["payload"] = _json(triage["payload"])
        triage["result"] = triage["payload"]["result"]
        record = self._ensure_repair_job(deviation, triage)
        graph = record["payload"]["action_graph"]
        runnable = sorted(
            runnable_automatic_actions(graph),
            key=lambda item: 0 if item["kind"] == "retry_task" else 1,
        )
        had_failure = False
        awaiting_approval = False
        in_progress = False
        for action in runnable:
            action["status"] = "running"
            self._save_repair_job(record, "running", graph)
            kind = str(action["kind"])
            try:
                if kind == "retry_task":
                    source_run_id = str(triage.get("source_run_id") or "")
                    task_id = self._task_id(str(action["target"]), source_run_id)
                    ready = self.state._connection().execute(
                        "SELECT status,updated_at FROM orchestrator_tasks "
                        "WHERE run_id=? AND task_id=?", (source_run_id, task_id),
                    ).fetchone()
                    if (ready and ready["status"] == "succeeded"
                            and str(ready["updated_at"]) >= str(triage["updated_at"])):
                        action["status"] = "completed"
                        continue
                    if not ready or ready["status"] != "ready":
                        raise ValueError(f"automatic retry target is not ready: {task_id}")
                    cycle = WorkerLoop(
                        self.root, worker_id=f"{self.supervisor_id}:safe-branch"
                    ).run_once(run_id=source_run_id)
                    if cycle.task_id != task_id or cycle.status != "succeeded":
                        raise RuntimeError(
                            f"safe branch did not succeed: {cycle.task_id}:{cycle.status}"
                        )
                    action["status"] = "completed"
                elif kind in {"propose_code_change", "propose_gate_change", "propose_policy_change"}:
                    result = triage["result"]
                    source_deviation = self.state._connection().execute(
                        "SELECT payload FROM deviation_reports WHERE deviation_id=?",
                        (triage["deviation_id"],),
                    ).fetchone()
                    source_evidence = (
                        _json(source_deviation["payload"]).get("evidence") or {}
                        if source_deviation else {}
                    )
                    source_failure = source_evidence.get("failure") or {}
                    candidate = ChangeController(self.root, self.control).propose(
                        source_deviation_id=str(triage["deviation_id"]), target=kind,
                        risk=str(result.get("risk") or "high"),
                        change={
                            "action": kind, "diagnosis": result.get("cause_code"),
                            "source_job_id": triage["source_job_id"],
                            "source_run_id": triage.get("source_run_id"),
                            "meta_deviation_id": deviation["meta_deviation_id"],
                            "implementation_targets": result.get("implementation_targets") or [],
                        }, rollback={"strategy": "restore_previous_control_plane_version",
                                     "trigger": "meta repair regression"},
                    )
                    queued = SystemRepairAgent(self.root, self.control).queue(
                        candidate_id=str(candidate["candidate_id"]),
                        source_job_id=str(triage["source_job_id"]),
                        source_run_id=triage.get("source_run_id"),
                        request={
                            "cause_code": result.get("cause_code"),
                            "mechanism_family": source_evidence.get("mechanism_family"),
                            "source_failure_fingerprint": str(
                                source_evidence.get("failure_fingerprint")
                                or (source_failure.get("failure_fingerprint")
                                    if isinstance(source_failure, dict) else "")
                                or ""
                            ),
                            "explanation": result.get("summary"),
                            "failed_task": triage.get("task_id"),
                            # The recovery point is declared separately in the
                            # action graph.  It is resolved here for the repair
                            # request, but remains non-executable until an
                            # operator approves that graph action.
                            "recovery_task": (
                                self._authorized_rewind_task(graph, str(
                                    triage.get("source_run_id") or ""
                                )) or triage.get("task_id")
                            ),
                            "evidence": {"meta_deviation": deviation,
                                         "triage_evidence": result.get("evidence") or []},
                            "triage": result, "triage_run_id": triage_id,
                            "implementation_targets": result.get("implementation_targets") or [],
                            "validation_tests": result.get("validation_tests") or [],
                            "goal_assessment": result.get("goal_assessment"),
                            "causal_input_changes": result.get("causal_input_changes") or [],
                            "proof_contract": result.get("proof_contract") or [],
                            "goal_constraints": {
                                "do_not_modify_goal_or_self_grant_authority": True,
                                "automatic_scope_ends_before_medium_risk_promotion": True,
                                "preserve_audit_and_rollback": True,
                            },
                        },
                    )
                    repair = (SystemRepairAgent(self.root, self.control).execute(
                        str(queued["repair_run_id"])
                    ) if queued["status"] == "queued" else queued)
                    action["proof_contract"] = [
                        *action.get("proof_contract", []),
                        {"repair_run_id": repair["repair_run_id"],
                         "execution_status": repair["status"]},
                    ]
                    if repair["status"] == "awaiting_approval":
                        action["status"] = "completed"
                        awaiting_approval = True
                    elif repair["status"] in {
                        "failed", "rejected", "rolled_back", "ineffective",
                    }:
                        action["status"] = "failed"
                        had_failure = True
                    elif repair["status"] in {"queued", "coding", "validating"}:
                        # A concurrent repair executor owns the durable child.
                        # Keep this action retryable and the parent nonterminal.
                        action["status"] = "ready"
                        in_progress = True
                    else:
                        action["status"] = "completed"
                else:
                    action["status"] = "completed"
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                action["status"] = "failed"
                action["proof_contract"] = [
                    *action.get("proof_contract", []),
                    {"error": type(exc).__name__, "message": str(exc)},
                ]
                had_failure = True
                continue
        final_status = ("failed" if had_failure else
                        "awaiting_approval" if awaiting_approval else
                        "running" if in_progress else "completed")
        self._save_repair_job(record, final_status, graph)
        if final_status == "failed":
            # A failed automatic child is an honest operator-attention boundary,
            # not a resolved control-plane deviation.  Keep the evidence but
            # stop the two-second meta loop from replaying the same failed graph.
            self._set_meta_status(deviation["meta_deviation_id"], "needs_attention")
        elif final_status != "running":
            self._set_meta_status(deviation["meta_deviation_id"], "resolved")
        return {"meta_repair_id": record["meta_repair_id"], "status": final_status,
                "action_graph": graph}

    def approve(self, meta_repair_id: str) -> dict[str, Any]:
        """Authorize promotion, the declared rewind, and campaign resumption as one receipt."""
        record = self._repair_job(meta_repair_id)
        if record["status"] not in {
            "awaiting_approval", "awaiting_outcome_validation", "failed"
        }:
            raise ValueError("meta repair is not at a retryable approval boundary")
        graph = record["payload"]["action_graph"]
        completed = {
            str(action["action_id"]) for action in graph.get("actions") or []
            if action.get("status") == "completed"
        }
        operator_actions = [
            action for action in graph.get("actions") or []
            if action.get("authority") == "operator"
        ]
        if not operator_actions or any(
            not set(action.get("dependencies") or []) <= completed
            for action in operator_actions
        ):
            raise ValueError("operator actions still have incomplete automatic dependencies")

        repair_run_id = ""
        for action in graph.get("actions") or []:
            for proof in action.get("proof_contract") or []:
                if isinstance(proof, dict) and proof.get("repair_run_id"):
                    repair_run_id = str(proof["repair_run_id"])
        if not repair_run_id:
            raise ValueError("meta repair has no validated coding repair")
        repair_agent = SystemRepairAgent(self.root, self.control)
        repair_before = repair_agent.get(repair_run_id)
        source_run_id = str(repair_before.get("source_run_id") or "")
        recovery_task = self._authorized_rewind_task(graph, source_run_id)
        if not recovery_task:
            raise ValueError("operator action graph has no valid rewind task")

        for action in operator_actions:
            action["status"] = "running"
        self._save_repair_job(record, "promoting", graph)
        try:
            repair = (
                repair_agent.approve(repair_run_id, recovery_task=recovery_task)
                if repair_before["status"] == "awaiting_approval"
                else repair_agent.authorize_rewind(repair_run_id, recovery_task)
            )
            invalidated = list(repair["payload"].get("invalidated") or [])
            if recovery_task not in invalidated:
                raise RuntimeError("approved repair did not execute the declared rewind")

            campaign_rows = list(self.state._connection().execute(
                "SELECT c.campaign_id,c.status FROM autonomous_campaigns c "
                "JOIN autonomous_job_items i ON i.campaign_id=c.campaign_id "
                "WHERE i.job_id=?", (record["job_id"],),
            ))
            resumed: list[str] = []
            for campaign in campaign_rows:
                campaign_id = str(campaign["campaign_id"])
                if campaign["status"] in {"paused", "needs_attention"}:
                    from .autonomous_supervisor import AutonomousJobSupervisor
                    AutonomousJobSupervisor(
                        self.root, control=self.control
                    ).resume(campaign_id)
                resumed.append(campaign_id)

            proof = {
                "operator_approved": True,
                "repair_run_id": repair_run_id,
                "repair_status": repair["status"],
                "authorized_recovery_task": recovery_task,
                "invalidated_tasks": invalidated,
                "campaign_ids": resumed,
            }
            for action in operator_actions:
                action["status"] = "completed"
                action["proof_contract"] = [
                    *action.get("proof_contract", []), proof,
                ]
            self._save_repair_job(record, "awaiting_outcome_validation", graph)
            self.store.request_supervision(
                job_id=str(record["job_id"]), run_id=source_run_id or None,
                reason="meta_repair_promoted", deviation_ids=[],
                observation_hash=str(repair.get("patch_hash") or ""),
                context={"meta_repair_id": meta_repair_id,
                         "repair_run_id": repair_run_id},
            )
            self.control.events.append(
                "control_plane_repair", meta_repair_id,
                "system_meta.operator_actions_completed", proof,
                actor="operator",
            )
            return {"meta_repair_id": meta_repair_id,
                    "status": "awaiting_outcome_validation",
                    "repair_run_id": repair_run_id,
                    "repair_status": repair["status"],
                    "recovery_task": recovery_task,
                    "invalidated_tasks": invalidated,
                    "campaign_ids": resumed,
                    "action_graph": graph}
        except Exception:
            for action in operator_actions:
                if action.get("status") == "running":
                    action["status"] = "failed"
            self._save_repair_job(record, "failed", graph)
            raise

    def _execute_premature_termination(self, deviation: dict[str, Any]) -> dict[str, Any]:
        campaign_id, job_id = deviation.get("campaign_id"), deviation.get("job_id")
        now = utcnow()
        if campaign_id:
            with self.state.transaction() as conn:
                conn.execute("UPDATE autonomous_campaigns SET status='running',updated_at=? "
                             "WHERE campaign_id=?", (now, campaign_id))
                conn.execute("UPDATE autonomous_job_items SET status='running',last_error=NULL,"
                             "updated_at=? WHERE campaign_id=? AND job_id=?",
                             (now, campaign_id, job_id))
        cycle = WorkerLoop(
            self.root, worker_id=f"{self.supervisor_id}:ready-branch"
        ).run_once(run_id=str(deviation["evidence"]["run_id"]))
        if cycle.status == "succeeded":
            self._set_meta_status(deviation["meta_deviation_id"], "resolved")
        return {"status": cycle.status, "task_id": cycle.task_id,
                "run_id": cycle.run_id, "campaign_id": campaign_id}

    def _execute_unproven_repair(self, deviation: dict[str, Any]) -> dict[str, Any]:
        report = GoalAlignmentController(self.root, self.control).audit_job(
            str(deviation["job_id"]), auto_repair=False, trigger="system-meta-supervisor"
        )
        repair = self.state._connection().execute(
            "SELECT status FROM system_repair_runs WHERE repair_run_id=?",
            (deviation["evidence"]["repair_run_id"],),
        ).fetchone()
        if repair and repair["status"] != "promoted":
            self._set_meta_status(deviation["meta_deviation_id"], "resolved")
        return {"status": str(repair["status"]) if repair else "missing", "audit": report}

    def _execute_orphaned_work(self, deviation: dict[str, Any]) -> dict[str, Any]:
        job_id = str(deviation["job_id"])
        run = self.state._connection().execute(
            "SELECT run_id FROM orchestrator_runs WHERE job_id=? ORDER BY created_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        ids = [str(row["deviation_id"]) for row in self.state._connection().execute(
            "SELECT deviation_id FROM deviation_reports WHERE job_id=? AND status='open' "
            "AND severity IN ('critical','high') ORDER BY updated_at", (job_id,),
        )]
        wakeup = self.store.request_supervision(
            job_id=job_id, run_id=str(run["run_id"]) if run else None,
            reason="system_meta_orphaned_goal_work", deviation_ids=ids,
            observation_hash=str(deviation["evidence"]["newest_deviation_at"]),
            context={"meta_deviation_id": deviation["meta_deviation_id"]},
        )
        # Creating durable work is not the same as completing it.  Resolution
        # is acknowledged by AlignmentStore.consume_wakeups after a Supervisor
        # has actually reclaimed the Job.
        self._set_meta_status(deviation["meta_deviation_id"], "awaiting_supervision")
        return {"status": "wakeup_created", "wakeup_id": wakeup["wakeup_id"],
                "awaiting_consumer": True}

    def reconcile(self, *, job_id: str | None = None) -> dict[str, Any]:
        """Run one bounded meta cycle; never change the immutable governance layer."""
        try:
            lease = self.control.leases.acquire(
                "system-meta-supervisor", self.supervisor_id, seconds=3600
            )
        except LeaseLost:
            return {"status": "already_running", "actions": []}
        try:
            self.audit(job_id=job_id)
            query, params = "SELECT * FROM system_meta_deviations WHERE status='open'", []
            if job_id:
                query += " AND job_id=?"; params.append(job_id)
            priority = {
                "PREMATURE_SUPERVISOR_TERMINATION": 0,
                "UNPROVEN_SYSTEM_REPAIR": 1,
                "REPAIR_PLAN_PROJECTION_LOSS": 2,
                "ORPHANED_GOAL_WORK": 3,
            }
            rows = [dict(row) for row in self.state._connection().execute(query, tuple(params))]
            rows.sort(key=lambda row: (priority.get(str(row["deviation_type"]), 9),
                                       str(row["created_at"])))
            actions: list[dict[str, Any]] = []
            # One control-plane mutation per cycle keeps retries and authority
            # boundaries observable. Pure bookkeeping may be handled alongside it.
            for row in rows[:1]:
                row["payload"] = _json(row["payload"])
                deviation = {**row["payload"], "status": row["status"]}
                kind = str(row["deviation_type"])
                if kind == "REPAIR_PLAN_PROJECTION_LOSS":
                    result = self._execute_plan_projection(deviation)
                elif kind == "PREMATURE_SUPERVISOR_TERMINATION":
                    result = self._execute_premature_termination(deviation)
                elif kind == "UNPROVEN_SYSTEM_REPAIR":
                    result = self._execute_unproven_repair(deviation)
                else:
                    result = self._execute_orphaned_work(deviation)
                actions.append({"meta_deviation_id": row["meta_deviation_id"],
                                "deviation_type": kind, "result": result})
            self.control.events.append(
                "system_meta_supervisor", "global", "system_meta.reconciled",
                {"job_id": job_id, "actions": actions}, actor=self.supervisor_id,
            )
            return {"status": "progressed" if actions else "idle", "actions": actions}
        finally:
            self.control.leases.release(lease)
