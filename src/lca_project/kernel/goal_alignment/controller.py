"""End-to-end self-healing and goal-alignment supervisor."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ...control import ControlPlane
from ..orchestrator import PersistentOrchestrator
from ..state import utcnow
from .causal_analyzer import CausalAnalyzer
from .change_controller import ChangeController
from .deviation_detector import DeviationDetector
from .failure_triage_agent import FailureTriageAgent
from .goal_registry import GoalRegistry
from .quality_trajectory import QualityTrajectory
from .research_translation_repair import build_repair_artifact, write_repair_artifact
from .repair_planner import RepairPlanner
from .system_repair_agent import SystemRepairAgent
from .store import AlignmentStore, canonical, digest
from .models import Deviation, Diagnosis, RepairProposal


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


class GoalAlignmentController:
    """Observe every run, explain deviations, and schedule bounded recovery."""

    SYSTEM_CHANGE_ACTIONS = {
        "propose_code_change", "propose_gate_change",
        "propose_policy_change", "propose_observability_change",
    }

    def __init__(self, root: str | Path, control: ControlPlane | None = None, *,
                 triage_agent: FailureTriageAgent | None = None) -> None:
        self.root = Path(root).resolve()
        self.control = control or ControlPlane(self.root)
        self.state = self.control.state
        self.store = AlignmentStore(self.state)
        self.goal = GoalRegistry(self.root, self.state).load()
        self.detector = DeviationDetector()
        self.analyzer = CausalAnalyzer()
        self.planner = RepairPlanner()
        self.triage = triage_agent or FailureTriageAgent(self.root, self.control)

    def _run(self, job_id: str) -> dict[str, Any] | None:
        row = self.state._connection().execute(
            "SELECT * FROM orchestrator_runs WHERE job_id=? ORDER BY created_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        return dict(row) if row else None

    def _pending_agent_triage(self, job_id: str) -> list[Deviation]:
        """Recover unknown failures hidden by a fast-path retry.

        The worker records deviations before it rewinds a retryable task.  On the
        following autonomy tick the task may already be ``ready``, so detection
        alone cannot see the failed shape anymore.  Open, untriaged reports are
        therefore durable work for the Agent, not disposable observations.
        """
        rows = self.state._connection().execute(
            "SELECT d.payload FROM deviation_reports d "
            "LEFT JOIN failure_triage_runs t ON t.deviation_id=d.deviation_id "
            "WHERE d.job_id=? AND d.status='open' AND t.triage_run_id IS NULL "
            "ORDER BY d.created_at DESC",
            (job_id,),
        )
        pending: list[Deviation] = []
        for row in rows:
            value = _payload(row["payload"])
            evidence = value.get("evidence") or {}
            # Quality deviations recorded before lineage-aware comparison are
            # not safe causal inputs.  They describe score changes but cannot
            # prove the two observations were comparable.
            if (value.get("deviation_type") == "quality_regression"
                    and evidence.get("lineage_compatible") is not True):
                continue
            task_id = str(evidence.get("task_id") or "")
            source_run_id = str(value.get("run_id") or "")
            current = self.state._connection().execute(
                "SELECT status FROM orchestrator_tasks WHERE run_id=? AND task_id=?",
                (source_run_id, task_id),
            ).fetchone() if source_run_id and task_id else None
            # A dependency rewind moves the formerly failed task back to
            # pending.  Its old failure is no longer current evidence and must
            # not delay the repaired upstream task.  A ready fast retry still
            # reaches Triage, preserving the original durable-intent rule.
            if current and current["status"] == "pending":
                continue
            deviation = Deviation(
                str(value.get("deviation_type") or ""),
                str(value.get("severity") or "high"),
                dict(value.get("evidence") or {}),
                str(value.get("summary") or "unclassified failure"),
            )
            if self.analyzer.requires_agent_triage(self.analyzer.analyze(deviation)):
                pending.append(deviation)
        return pending

    def _tasks(self, run_id: str | None) -> list[dict[str, Any]]:
        if not run_id:
            return []
        result = []
        for row in self.state._connection().execute(
            "SELECT * FROM orchestrator_tasks WHERE run_id=? ORDER BY rowid", (run_id,)
        ):
            item = dict(row)
            item["failure_payload"] = _payload(item.get("failure_payload"))
            attempt = self.state._connection().execute(
                "SELECT attempt,input_hashes FROM orchestrator_attempts "
                "WHERE run_id=? AND task_id=? AND status IN ('succeeded','reused') "
                "ORDER BY attempt DESC, finished_at DESC LIMIT 1",
                (run_id, item["task_id"]),
            ).fetchone()
            item["recorded_input_hashes"] = (
                json.loads(attempt["input_hashes"]) if attempt else None
            )
            item["repair_lineage"] = ([
                {
                    "type": ("prior_output" if prior["status"] in {"succeeded", "reused"}
                             else "failure"),
                    "hash": str(prior["output_hash"]),
                    "attempt": int(prior["attempt"]),
                }
                for prior in self.state._connection().execute(
                    "SELECT attempt,status,output_hash FROM orchestrator_attempts "
                    "WHERE run_id=? AND task_id=? AND attempt<? AND output_hash IS NOT NULL "
                    "AND status IN ('succeeded','reused','repairable','quarantined','manual_review') "
                    "ORDER BY attempt",
                    (run_id, item["task_id"], int(attempt["attempt"]) if attempt else 0),
                )
            ] if attempt else [])
            binding = self.state._connection().execute(
                "SELECT COALESCE(MAX(generation),0) AS generation "
                "FROM task_binding_generations WHERE run_id=? AND task_id=?",
                (run_id, item["task_id"]),
            ).fetchone()
            recovery = self.state._connection().execute(
                "SELECT COALESCE(MAX(epoch),0) AS epoch "
                "FROM task_repair_epochs WHERE run_id=? AND task_id=?",
                (run_id, item["task_id"]),
            ).fetchone()
            item["binding_generation"] = int(binding["generation"] if binding else 0)
            item["recovery_epoch"] = int(recovery["epoch"] if recovery else 0)
            result.append(item)
        return result

    def _batch(self, job: dict[str, Any], run_id: str | None) -> Path | None:
        request = (((job.get("payload") or {}).get("scope") or {}).get("request") or {})
        nodes = request.get("nodes") or []
        if not run_id or len(nodes) != 1:
            return None
        industry = str(request.get("industry") or "")
        fixture = self.root / "vendor/lca_cornerstone/fixtures/wiki-phase2"
        slug = industry
        if not (fixture / "wiki" / slug).is_dir():
            matches = []
            for graph in (fixture / "docs").glob("*-name-graph.json"):
                raw = _payload(graph.read_text(encoding="utf-8"))
                meta = raw.get("_meta") or {}
                if industry and industry in " ".join(str(meta.get(key, "")) for key in ("industry", "title")):
                    matches.append(graph.name.removesuffix("-name-graph.json"))
            if len(matches) != 1:
                return None
            slug = matches[0]
        batch_id = str(request.get("batch_id") or
                       f"{str(nodes[0]).lower()}-{run_id.removeprefix('run_')[:12]}")
        return self.root / "var/workspaces/jobs" / str(job["id"]) / "runs/wiki-batches" / slug / batch_id

    @staticmethod
    def _required_replay_tasks(
        proof_contract: list[dict[str, Any]], task_by_id: dict[str, dict[str, Any]],
    ) -> list[str]:
        """Resolve explicit task/verdict proof clauses to workflow task IDs.

        Agent-authored proof metrics are descriptive, but task names and target
        verdicts are stable.  Honor clauses such as ``content_compose task
        status=succeeded`` and ``independent editorial verdict=GO`` instead of
        declaring a repair effective after only its rewind entry task passes.
        """
        required: list[str] = []
        for clause in proof_contract:
            metric = str(clause.get("metric") or "").lower()
            artifact = str(clause.get("evidence_artifact") or "").lower()
            target = str(clause.get("target") or "").lower()
            task_bound = "task status" in metric or (
                "verdict" in metric and target in {"go", "pass", "succeeded"}
            )
            if not task_bound:
                continue
            haystack = f"{metric} {artifact}"
            for task_id in task_by_id:
                aliases = {
                    task_id.lower(),
                    task_id.lower().replace("_", " "),
                    task_id.lower().replace("_", "-"),
                }
                if any(alias in haystack for alias in aliases) and task_id not in required:
                    required.append(task_id)
        return required

    @staticmethod
    def _editorial_verdict_is_hash_bound(batch: Path | None, target: str) -> bool:
        """Verify the independent verdict against the exact patched content.

        A succeeded task row proves that the reviewer process completed, but it
        does not by itself prove either the requested verdict or which content
        was reviewed.  The editorial policy artifact binds both immutable file
        hashes, so outcome validation fails closed unless all three artifacts
        remain coherent.
        """
        if batch is None or target.upper() != "GO":
            return False
        content_path = batch / "content-runtime/content-result.json"
        review_path = batch / "editorial-loop/editorial-review.json"
        policy_path = batch / "editorial-loop/editorial-policy-decision.json"
        try:
            review = _payload(review_path.read_text(encoding="utf-8"))
            policy = _payload(policy_path.read_text(encoding="utf-8"))
            checks = review.get("checks")
            if (
                not content_path.is_file()
                or review.get("protocol") != "wiki-editorial-review-v1"
                or review.get("verdict") != "GO"
                or not isinstance(checks, dict)
                or not checks
                or not all(value is True for value in checks.values())
                or bool(review.get("issues"))
                or policy.get("protocol") != "wiki-editorial-policy-decision-v1"
                or policy.get("decision") != "accept"
            ):
                return False
            content_sha256 = hashlib.sha256(content_path.read_bytes()).hexdigest()
            review_sha256 = hashlib.sha256(review_path.read_bytes()).hexdigest()
            return (
                policy.get("content_sha256") == content_sha256
                and policy.get("review_sha256") == review_sha256
                and policy.get("raw_review_sha256") == review_sha256
            )
        except (OSError, TypeError, ValueError):
            return False

    def _evaluate_pending_system_repairs(self, job_id: str, run_id: str | None,
                                         tasks: list[dict[str, Any]],
                                         observation: Any) -> list[dict[str, Any]]:
        """Turn a promoted patch into a result only after official replay proof.

        Sandbox tests prove that a patch is safe enough to deploy.  They do not
        prove that the source Job became more useful.  Validation therefore
        waits for the rewound recovery branch (or the final maturity gate for
        research repairs) to succeed after promotion.
        """
        actions: list[dict[str, Any]] = []
        task_by_id = {str(item["task_id"]): item for item in tasks}
        rows = self.state._connection().execute(
            "SELECT * FROM system_repair_runs WHERE source_job_id=? "
            "AND status IN ('awaiting_outcome_validation','promoted') ORDER BY created_at",
            (job_id,),
        )
        for raw in rows:
            repair = dict(raw); payload = _payload(repair.get("payload"))
            request = payload.get("request") or {}
            if repair["status"] == "promoted":
                # Compatibility for pre-v10 research repairs.  Do not
                # reinterpret unrelated historical code promotions using a
                # research metric that was never part of their proof contract.
                research_baseline = ((request.get("evidence") or {}).get(
                    "research_outcome"
                ) or {}).get("metrics") or {}
                if not research_baseline or not request.get("proof_contract"):
                    continue
                with self.state.transaction() as conn:
                    conn.execute(
                        "UPDATE system_repair_runs SET status='awaiting_outcome_validation',"
                        "updated_at=? WHERE repair_run_id=? AND status='promoted'",
                        (utcnow(), repair["repair_run_id"]),
                    )
                    conn.execute(
                        "UPDATE repair_graphs SET status='awaiting_outcome_validation',"
                        "updated_at=? WHERE repair_run_id=?",
                        (utcnow(), repair["repair_run_id"]),
                    )
                repair["status"] = "awaiting_outcome_validation"
            promoted_at = str(payload.get("promoted_at") or "")
            cause = str(request.get("cause_code") or "")
            recovery_task = str(request.get("recovery_task") or "")
            research_repair = bool(
                cause in {"LOW_RESEARCH_UTILITY", "RESEARCH_COMPLETED_WITHOUT_LCA_PROGRESS"}
                or (request.get("evidence") or {}).get("research_outcome")
            )
            requested_proof = request.get("proof_contract") or []
            workflow_proof = next((
                item for item in requested_proof
                if item.get("metric") == "workflow_status"
            ), None)
            forced_verdict: str | None = None
            failed_proof_tasks: list[dict[str, Any]] = []
            required_replay_tasks = self._required_replay_tasks(requested_proof, task_by_id)
            if not research_repair and workflow_proof:
                if (not run_id or not tasks
                        or str((self._run(job_id) or {}).get("status") or "")
                        != str(workflow_proof.get("target") or "succeeded")):
                    continue
                current_run = self._run(job_id) or {}
                if str(current_run.get("updated_at") or "") <= promoted_at:
                    continue
                proof_task_id = "workflow"
                proof_task = current_run
            elif not research_repair and required_replay_tasks:
                replay_tasks = [task_by_id.get(task_id) for task_id in required_replay_tasks]
                if any(not item for item in replay_tasks):
                    continue
                fresh_tasks = [
                    item for item in replay_tasks
                    if str((item or {}).get("updated_at") or "") > promoted_at
                ]
                if len(fresh_tasks) != len(replay_tasks):
                    continue
                failed_proof_tasks = [
                    {"task_id": item["task_id"], "status": item["status"],
                     "updated_at": item["updated_at"]}
                    for item in fresh_tasks
                    if item.get("status") in {"manual_review", "quarantined"}
                ]
                if failed_proof_tasks:
                    forced_verdict = "ineffective"
                elif any(item.get("status") != "succeeded" for item in fresh_tasks):
                    continue
                editorial_clause = next((
                    item for item in requested_proof
                    if "editorial" in (
                        f"{item.get('metric') or ''} {item.get('evidence_artifact') or ''}"
                    ).lower()
                    and "verdict" in str(item.get("metric") or "").lower()
                ), None)
                if (
                    not failed_proof_tasks
                    and editorial_clause
                    and not self._editorial_verdict_is_hash_bound(
                        self._batch(self.state.get("jobs", job_id) or {}, run_id),
                        str(editorial_clause.get("target") or ""),
                    )
                ):
                    failed_proof_tasks.append({
                        "task_id": "editorial_review",
                        "status": "proof_mismatch",
                        "updated_at": str(task_by_id.get("editorial_review", {}).get(
                            "updated_at"
                        ) or ""),
                    })
                    forced_verdict = "ineffective"
                proof_task_id = ",".join(required_replay_tasks)
                proof_task = max(
                    fresh_tasks, key=lambda item: str(item.get("updated_at") or "")
                )
            else:
                proof_task_id = "maturity_gate" if research_repair else recovery_task
                proof_task = task_by_id.get(proof_task_id)
                if (not proof_task or proof_task.get("status") != "succeeded"
                        or str(proof_task.get("updated_at") or "") <= promoted_at):
                    continue

            goal_assessment = request.get("goal_assessment") or {}
            baseline_outcome = (request.get("evidence") or {}).get("research_outcome") or {}
            baseline = (baseline_outcome.get("metrics") or
                        goal_assessment.get("baseline_metrics") or {})
            current_outcome = observation.evidence.get("research_outcome") or {}
            current = current_outcome.get("metrics") or {}
            positive = ("accepted_observations", "populated_fields", "confirmed_sources")
            support = ("field_observations", "pages_fetched", "document_routes")
            reductions = ("internal_identifier_queries", "mixed_language_english_queries")
            core_improved = {
                key: {"baseline": int(baseline.get(key) or 0),
                      "current": int(current.get(key) or 0)}
                for key in positive
                if int(current.get(key) or 0) > int(baseline.get(key) or 0)
            }
            supporting_improved = {
                key: {"baseline": int(baseline.get(key) or 0),
                      "current": int(current.get(key) or 0)}
                for key in support
                if int(current.get(key) or 0) > int(baseline.get(key) or 0)
            }
            supporting_improved.update({
                key: {"baseline": int(baseline.get(key) or 0),
                      "current": int(current.get(key) or 0)}
                for key in reductions
                if int(baseline.get(key) or 0) > int(current.get(key) or 0)
            })
            baseline_score_raw = goal_assessment.get("baseline_score")
            baseline_score = (float(baseline_score_raw)
                              if isinstance(baseline_score_raw, (int, float)) else None)
            current_score = float(getattr(observation, "score", 0.0) or 0.0)
            quality_score_improved = bool(
                baseline_score is not None and current_score > baseline_score + 1e-9
            )
            declared_causal_inputs = [
                str(item.get("causal_input") or item.get("target") or "").strip()
                for item in request.get("causal_input_changes") or []
                if isinstance(item, dict)
            ]
            patch_bound = bool(repair.get("patch_hash"))
            causal_inputs_bound = bool(declared_causal_inputs)
            source_failure_fingerprint = str(
                request.get("source_failure_fingerprint")
                or ((request.get("evidence") or {}).get("failure_fingerprint")
                    if isinstance(request.get("evidence"), dict) else "")
                or ""
            )
            replay_failures = [
                item for item in (task_by_id.get(task_id) for task_id in required_replay_tasks)
                if item and item.get("failure_payload")
            ]
            fingerprint_absent = not any(
                source_failure_fingerprint and source_failure_fingerprint in str(
                    item.get("failure_payload") or ""
                )
                for item in replay_failures
            )
            proof_bound = bool(requested_proof) and not failed_proof_tasks
            effective_contract_satisfied = bool(
                patch_bound and causal_inputs_bound and proof_bound
                and fingerprint_absent
                and (quality_score_improved
                     or (current_outcome.get("closer_to_modelling_goal") is True
                         and bool(core_improved)))
            )
            if forced_verdict:
                verdict = forced_verdict
            elif not research_repair:
                verdict = ("effective" if effective_contract_satisfied
                           else "partially_effective"
                           if proof_bound and fingerprint_absent
                           else "ineffective")
            elif current_outcome.get("closer_to_modelling_goal") is True and core_improved:
                verdict = "effective"
            elif core_improved or supporting_improved:
                verdict = "partially_effective"
            else:
                verdict = "ineffective"
            proof = {
                "official_replay": True, "proof_task_id": proof_task_id,
                "proof_task_updated_at": proof_task.get("updated_at"),
                "promoted_at": promoted_at, "core_improvements": core_improved,
                "supporting_improvements": supporting_improved,
                "closer_to_modelling_goal": current_outcome.get("closer_to_modelling_goal"),
                "proof_contract": requested_proof,
                "required_replay_tasks": required_replay_tasks,
                "failed_proof_tasks": failed_proof_tasks,
                "patch_hash": repair.get("patch_hash"),
                "patch_bound": patch_bound,
                "declared_causal_inputs": declared_causal_inputs,
                "causal_inputs_bound": causal_inputs_bound,
                "source_failure_fingerprint": source_failure_fingerprint or None,
                "failure_fingerprint_absent_after_replay": fingerprint_absent,
                "baseline_quality_score": baseline_score,
                "current_quality_score": current_score,
                "quality_score_improved": quality_score_improved,
                "effective_contract_satisfied": effective_contract_satisfied,
            }
            receipt = self.store.repair_validation_receipt(
                repair_run_id=str(repair["repair_run_id"]), job_id=job_id,
                run_id=run_id, verdict=verdict, baseline=baseline,
                current=current, proof=proof,
            )
            payload["outcome_validation"] = receipt
            with self.state.transaction() as conn:
                conn.execute(
                    "UPDATE system_repair_runs SET status=?,payload=?,updated_at=? "
                    "WHERE repair_run_id=? AND status='awaiting_outcome_validation'",
                    (verdict, canonical(payload), utcnow(), repair["repair_run_id"]),
                )
                conn.execute(
                    "UPDATE repair_graphs SET status=?,"
                    "zero_gain_attempts=zero_gain_attempts+?,payload=?,updated_at=? "
                    "WHERE repair_run_id=?",
                    (verdict, 1 if verdict == "ineffective" else 0,
                     canonical({"outcome_validation": receipt}), utcnow(),
                     repair["repair_run_id"]),
                )
            action = {"status": f"repair_{verdict}",
                      "repair_run_id": repair["repair_run_id"], "proof": proof}
            actions.append(action)
            self.control.events.append(
                "system_repair", str(repair["repair_run_id"]),
                f"system_repair.{verdict}", receipt,
                actor="goal-alignment-controller",
            )
            if verdict != "effective":
                wakeup = self.store.request_supervision(
                    job_id=job_id, run_id=run_id,
                    reason=f"repair_outcome_{verdict}",
                    deviation_ids=[], observation_hash=str(observation.score) + ":" +
                    canonical(current), context={"repair_run_id": repair["repair_run_id"]},
                )
                action["wakeup_id"] = wakeup["wakeup_id"]
        return actions

    def _repair_iteration(self, deviation_id: str) -> tuple[int, str | None]:
        rows = list(self.state._connection().execute(
            "SELECT c.candidate_id,r.status FROM system_change_candidates c "
            "JOIN system_repair_runs r ON r.candidate_id=c.candidate_id "
            "WHERE c.source_deviation_id=? ORDER BY r.created_at", (deviation_id,),
        ))
        ineffective = [row for row in rows if str(row["status"]) in {
            "partially_effective", "ineffective"
        }]
        return len(ineffective), (str(rows[-1]["candidate_id"]) if rows else None)

    def audit_job(self, job_id: str, *, auto_repair: bool = False,
                  trigger: str = "manual", execute_triage: bool = True) -> dict[str, Any]:
        job = self.state.get("jobs", job_id)
        if job is None:
            raise KeyError(job_id)
        run = self._run(job_id)
        run_id = str(run["run_id"]) if run else None
        tasks = self._tasks(run_id)
        prior = self.state._connection().execute(
            "SELECT observation_id,score,payload FROM quality_observations WHERE job_id=? "
            "ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        batch = self._batch(job, run_id)
        observation = QualityTrajectory().observe(
            job_id=job_id, run_id=run_id, goal=self.goal, batch=batch,
            tasks=tasks, run_status=str(run.get("status") or "") if run else None,
        )
        observed = self.store.observation(observation.asdict())
        resolved = self._resolve_recovered(job_id, tasks, observation)
        outcome_actions = self._evaluate_pending_system_repairs(
            job_id, run_id, tasks, observation
        )
        deviations = self.detector.detect(job=job, run=run, tasks=tasks,
                                          observation=observation,
                                          previous_comparison=self._comparable_previous_score(
                                              prior, observed
                                          ))
        if auto_repair:
            # A bounded retry is a fast path, not permission to forget an
            # unknown failure.  Durable reports ensure first-occurrence faults
            # reach Agent Triage on the next autonomous supervision tick.
            detected = {
                canonical({"type": item.deviation_type, "evidence": item.evidence})
                for item in deviations
            }
            # Current terminal facts take precedence over older durable
            # deviations.  The older reports remain queued behind them rather
            # than occupying the repair channel while the newest failure goes
            # untriaged.
            deviations = [
                *deviations,
                *[item for item in self._pending_agent_triage(job_id)
                  if canonical({"type": item.deviation_type,
                                "evidence": item.evidence}) not in detected],
            ]
        records: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = [*resolved, *outcome_actions]
        for deviation in deviations:
            report = self.store.deviation(job_id=job_id, run_id=run_id,
                                          goal_id=self.goal["goal_id"],
                                          value=asdict(deviation))
            diagnosis = self.analyzer.analyze(deviation)
            triage_record: dict[str, Any] | None = None
            triage_result: dict[str, Any] | None = None
            if auto_repair and self.analyzer.requires_agent_triage(diagnosis):
                failed_task_id = str(diagnosis.evidence.get("task_id") or "")
                failed_task = next(
                    (item for item in tasks if str(item.get("task_id")) == failed_task_id), {}
                )
                queued = self.triage.queue(
                    deviation_id=str(report["deviation_id"]), source_job_id=job_id,
                    source_run_id=run_id, task_id=failed_task_id or None,
                    request={
                        "report": report,
                        "preliminary_diagnosis": asdict(diagnosis),
                        "failed_task": failed_task,
                        "workflow_run": run or {},
                        "task_graph": [{
                            "task_id": item.get("task_id"), "status": item.get("status"),
                            "capability_id": item.get("capability_id"),
                            "dependencies": item.get("dependencies"),
                            "recorded_input_hashes": item.get("recorded_input_hashes"),
                            "repair_lineage": item.get("repair_lineage"),
                            "output_hash": item.get("output_hash"),
                            "binding_generation": item.get("binding_generation", 0),
                            "recovery_epoch": item.get("recovery_epoch", 0),
                        } for item in tasks],
                        "goal_contract": self.goal,
                        "quality_observation": observation.asdict(),
                        "batch_path": str(batch) if batch else None,
                    },
                )
                # Interactive/manual audits retain their synchronous behaviour.
                # Supervisors pass ``execute_triage=False`` so the durable
                # triage row is dispatched outside their reconciliation lease.
                triage_record = (
                    self.triage.execute(str(queued["triage_run_id"]))
                    if execute_triage and queued["status"] != "completed"
                    else queued
                )
                actions.append({
                    "status": f"failure_triage_{triage_record['status']}",
                    "triage_run_id": triage_record["triage_run_id"],
                    "deviation_id": report["deviation_id"],
                })
                if triage_record["status"] == "completed":
                    triage_result = triage_record["payload"]["result"]
                    diagnosis = Diagnosis(
                        str(triage_result["cause_code"]),
                        float(triage_result["confidence"]),
                        {**diagnosis.evidence, "triage_run_id": triage_record["triage_run_id"],
                         "triage": triage_result},
                        str(triage_result["summary"]),
                    )
            diagnosis_record = self.store.diagnosis(report["deviation_id"], asdict(diagnosis))
            requires_triage = self.analyzer.requires_agent_triage(diagnosis)
            if triage_result:
                proposal = self.planner.from_triage(triage_result)
            elif requires_triage:
                # Unknown causes cannot silently fall through to a generic
                # observability/code candidate.  Only the Agent's evidence-
                # backed result may select a repair route.
                proposal = RepairProposal(
                    "manual", "await_agent_triage", "failure_triage_agent",
                    (), (), ("agent_causal_diagnosis",), False,
                )
            else:
                proposal = self.planner.plan(diagnosis)
            plan = self.store.repair_plan(report["deviation_id"], {
                "repair_level": proposal.level, "action": proposal.action,
                "authority": proposal.authority, "invalidates": list(proposal.invalidates),
                "preserves": list(proposal.preserves), "validation": list(proposal.validation),
                "automatic": proposal.automatic, "status": "proposed",
            })
            records.append({"deviation": report, "diagnosis": diagnosis_record,
                            "repair_plan": plan, "triage": triage_record})
            if proposal.level == "L2":
                risk = (str(triage_result["risk"]) if triage_result else
                        "low" if diagnosis.cause_code == "EDITORIAL_POLICY_CONTRACT_MISMATCH"
                        else "high" if deviation.severity == "critical" else "medium")
                change_controller = ChangeController(self.root, self.control)
                repair_iteration, predecessor = self._repair_iteration(
                    str(report["deviation_id"])
                )
                revision = ({"repair_iteration": repair_iteration,
                             "supersedes_candidate_id": predecessor}
                            if repair_iteration else {})
                candidate = change_controller.propose(
                    source_deviation_id=report["deviation_id"],
                    target=proposal.action, risk=risk,
                    change={"action": proposal.action, "diagnosis": diagnosis.cause_code,
                            "required_validation": list(proposal.validation),
                            "source_job_id": job_id, "source_run_id": run_id,
                            "evidence": diagnosis.evidence, **revision},
                    rollback={"strategy": "restore_previous_policy_hash",
                              "trigger": "quality regression or false pass"},
                )
                if (auto_repair and proposal.action in self.SYSTEM_CHANGE_ACTIONS
                        and candidate["status"] == "rejected"):
                    candidate = change_controller.revise(
                        str(candidate["candidate_id"]),
                        reason="previous patch was rejected; retry with latest validation evidence",
                    )
                actions.append({"status": "change_candidate_created",
                                "candidate_id": candidate["candidate_id"]})
                if auto_repair and proposal.action in self.SYSTEM_CHANGE_ACTIONS:
                    failed_task = str(diagnosis.evidence.get("task_id") or "")
                    recovery_task = str(
                        (triage_result or {}).get("recovery_task") or failed_task
                    )
                    queued = SystemRepairAgent(self.root, self.control).queue(
                        candidate_id=str(candidate["candidate_id"]), source_job_id=job_id,
                        source_run_id=run_id, request={
                            "cause_code": diagnosis.cause_code,
                            "explanation": diagnosis.explanation,
                            "failure_code": diagnosis.evidence.get("failure_code"),
                            "mechanism_family": diagnosis.evidence.get("mechanism_family"),
                            "failed_task": failed_task,
                            "recovery_task": recovery_task,
                            "evidence": diagnosis.evidence,
                            "triage": triage_result,
                            "triage_run_id": (triage_record or {}).get("triage_run_id"),
                            "implementation_targets": list(
                                (triage_result or {}).get("implementation_targets") or []
                            ),
                            "validation_tests": list(
                                (triage_result or {}).get("validation_tests") or []
                            ),
                            "goal_assessment": (triage_result or {}).get("goal_assessment"),
                            "causal_input_changes": list(
                                (triage_result or {}).get("causal_input_changes") or []
                            ),
                            "proof_contract": list(
                                (triage_result or {}).get("proof_contract") or []
                            ),
                            "goal_constraints": {
                                "do_not_invent_or_weaken_evidence": True,
                                "do_not_bypass_goal_or_maturity_gates": True,
                                "preserve_existing_passes": True,
                                "preserve_fail_closed_review": True,
                                "honor_hash_bound_editorial_policy": True,
                                "reviewed_mode_must_remain_strict": True,
                                "repair_must_change_a_named_causal_input": True,
                                "promotion_requires_proof_metric_improvement": True,
                            },
                        })
                    queue_status = ("system_repair_queued" if queued["status"] == "queued"
                                    else "system_repair_existing")
                    actions.append({"status": queue_status,
                                    "candidate_id": candidate["candidate_id"],
                                    "repair_run_id": queued["repair_run_id"],
                                    "execution_status": queued["status"]})
            elif auto_repair and proposal.automatic:
                actions.append(self._apply_repair(job_id, run_id, plan, batch=batch))
        if records and not auto_repair:
            wakeup = self.store.request_supervision(
                job_id=job_id, run_id=run_id, reason="worker_observed_goal_deviation",
                deviation_ids=[str(item["deviation"]["deviation_id"]) for item in records],
                observation_hash=str(observed["vector_hash"]),
                context={"trigger": trigger, "score": observation.score},
            )
            actions.append({"status": "supervision_requested",
                            "wakeup_id": wakeup["wakeup_id"]})
            self.control.events.append(
                "job", job_id, "goal_alignment.supervision_requested",
                {"wakeup_id": wakeup["wakeup_id"], "run_id": run_id,
                 "deviation_ids": wakeup["payload"]["deviation_ids"]},
                actor="goal-alignment-controller",
            )
        self.control.events.append("job", job_id, "goal_alignment.audited", {
            "run_id": run_id, "trigger": trigger, "score": observation.score,
                "deviations": len(records), "actions": actions,
        }, actor="goal-alignment-controller")
        return {"schema_version": "goal-alignment-audit-v1", "job_id": job_id,
                "run_id": run_id, "goal": self.goal, "quality": observed,
                "deviations": records, "actions": actions, "audited_at": utcnow()}

    def _resolve_recovered(self, job_id: str, tasks: list[dict[str, Any]],
                           observation: Any) -> list[dict[str, Any]]:
        """Close a deviation only when its original target now proves recovery."""
        by_id = {str(item["task_id"]): item for item in tasks}
        actions: list[dict[str, Any]] = []
        rows = list(self.state._connection().execute(
            "SELECT deviation_id,deviation_type,payload FROM deviation_reports "
            "WHERE job_id=? AND status='open'", (job_id,),
        ))
        for row in rows:
            report = _payload(row["payload"])
            evidence = report.get("evidence") or {}
            deviation_type = str(row["deviation_type"])
            target = by_id.get(str(evidence.get("task_id") or ""))
            proven = (deviation_type in {"false_block", "repeated_fault"}
                      and target is not None and target.get("status") == "succeeded")
            if deviation_type in {"false_pass", "success_without_maturity"}:
                proven = (observation.evidence.get("maturity") or {}).get(
                    "candidate_eligible") is True
            proof: Any = str(target.get("task_id")) if target else "maturity_gate"
            if deviation_type == "low_research_utility":
                baseline = ((evidence.get("research_outcome") or {}).get("metrics") or {})
                current_outcome = observation.evidence.get("research_outcome") or {}
                current = current_outcome.get("metrics") or {}
                improved = {
                    name: {"baseline": int(baseline.get(name) or 0),
                           "current": int(current.get(name) or 0)}
                    for name in ("accepted_observations", "populated_fields", "confirmed_sources")
                    if int(current.get(name) or 0) > int(baseline.get(name) or 0)
                }
                proven = (current_outcome.get("closer_to_modelling_goal") is True
                          and bool(improved))
                proof = {"research_outcome_improvements": improved,
                         "proof_contract": current_outcome.get("proof_contract") or []}
            if not proven:
                continue
            now = utcnow()
            with self.state.transaction() as conn:
                conn.execute("UPDATE deviation_reports SET status='resolved',updated_at=? "
                             "WHERE deviation_id=? AND status='open'", (now, row["deviation_id"]))
                conn.execute("UPDATE repair_plans SET status='validated',updated_at=? "
                             "WHERE deviation_id=? AND status IN ('proposed','scheduled')",
                             (now, row["deviation_id"]))
            action = {"status": "validated", "deviation_id": row["deviation_id"],
                      "proof": proof}
            actions.append(action)
            self.control.events.append("job", job_id, "goal_alignment.repair_validated", action,
                                       actor="goal-alignment-controller")
        return actions

    def _apply_repair(self, job_id: str, run_id: str | None,
                      plan: dict[str, Any], *, batch: Path | None = None) -> dict[str, Any]:
        if not run_id:
            return {"status": "not_applicable", "reason": "job has no workflow run"}
        action = plan["action"]
        if action == "rewind_research_plan":
            research_plan = batch / "research-plan.json" if batch else None
            if research_plan is None or not research_plan.is_file():
                status, detail = "not_applicable", {
                    "reason": "research plan artifact is unavailable; causal input cannot be repaired"
                }
            else:
                artifact = build_repair_artifact(_payload(research_plan.read_text(encoding="utf-8")))
                if artifact["status"] != "ready":
                    status, detail = "escalated", {
                        "reason": "unmatched fragments are outside the audited L1 repair vocabulary",
                        "unresolved_fragments": artifact["unresolved_fragments"],
                    }
                else:
                    repair_path = batch / "research-plan-translation-repair.json"
                    changed = write_repair_artifact(repair_path, artifact)
                    if not changed:
                        status, detail = "stopped", {
                            "reason": "translation repair did not change the causal input",
                            "repair_artifact": str(repair_path),
                            "artifact_sha256": artifact["artifact_sha256"],
                        }
                    else:
                        invalidated = PersistentOrchestrator(self.root).rewind_from(
                            run_id, "research_plan",
                            reason="Goal Alignment L1 repair: apply audited discovery translation override",
                            actor="goal-alignment-controller",
                        )
                        status = "scheduled"
                        detail = {
                            "invalidated": list(invalidated),
                            "repair_artifact": str(repair_path),
                            "artifact_sha256": artifact["artifact_sha256"],
                            "repairs": artifact["repairs"],
                            "causal_input_changed": True,
                        }
                        self.control.events.append(
                            "job", job_id, "goal_alignment.translation_repair_prepared", detail,
                            actor="goal-alignment-controller",
                        )
        elif action == "stop_blind_retry":
            status, detail = "stopped", {"reason": "identical failure fingerprint"}
        elif action in {"retry_triaged_task", "rewind_triaged_task"}:
            invalidates = list(plan.get("invalidates") or [])
            task_id = str(invalidates[0]) if invalidates else ""
            if not task_id:
                status, detail = "not_applicable", {"reason": "triage supplied no recovery task"}
            else:
                reopened = PersistentOrchestrator(self.root).rewind_from(
                    run_id, task_id,
                    reason=f"Agent triage selected {action}",
                    actor="goal-alignment-controller",
                )
                status, detail = "scheduled", {
                    "invalidated": list(reopened), "causal_input_changed": action != "retry_triaged_task",
                }
        else:
            status, detail = "not_authorized", {"reason": "action is not an L0/L1 allowlist member"}
        with self.state.transaction() as conn:
            conn.execute("UPDATE repair_plans SET status=?,updated_at=? WHERE repair_plan_id=?",
                         (status, utcnow(), plan["repair_plan_id"]))
        return {"status": status, "repair_plan_id": plan["repair_plan_id"],
                "action": action, **detail}

    def report_user_feedback(self, job_id: str, message: str, *,
                             category: str = "user_feedback") -> dict[str, Any]:
        job = self.state.get("jobs", job_id)
        if job is None:
            raise KeyError(job_id)
        run = self._run(job_id)
        deviation = self.detector.user_escape(message=message, category=category)
        report = self.store.deviation(job_id=job_id,
                                      run_id=str(run["run_id"]) if run else None,
                                      goal_id=self.goal["goal_id"], value=asdict(deviation))
        diagnosis = self.analyzer.analyze(deviation)
        diagnosis_record = self.store.diagnosis(report["deviation_id"], asdict(diagnosis))
        proposal = self.planner.plan(diagnosis)
        plan = self.store.repair_plan(report["deviation_id"], {
            "repair_level": proposal.level, "action": proposal.action,
            "authority": proposal.authority, "invalidates": [], "preserves": [],
            "validation": list(proposal.validation), "automatic": False, "status": "proposed",
        })
        return {"deviation": report, "diagnosis": diagnosis_record, "repair_plan": plan}

    def status(self, *, job_id: str | None = None) -> dict[str, Any]:
        def consistency_rows(table: str) -> list[dict[str, Any]]:
            query, params = f"SELECT * FROM {table}", []
            if job_id:
                query += " WHERE job_id=?"
                params.append(job_id)
            query += " ORDER BY created_at DESC LIMIT 200"
            result: list[dict[str, Any]] = []
            for row in self.state._connection().execute(query, tuple(params)):
                item = dict(row)
                if "payload" in item:
                    item["payload"] = _payload(item["payload"])
                if "authorized_successors" in item:
                    try:
                        item["authorized_successors"] = json.loads(
                            item["authorized_successors"]
                        )
                    except (TypeError, json.JSONDecodeError):
                        item["authorized_successors"] = []
                result.append(item)
            return result

        return {
            "goal_contracts": self.store.rows("goal_contracts", limit=20),
            "quality_observations": self.store.rows("quality_observations", job_id=job_id),
            "deviations": self.store.rows("deviation_reports", job_id=job_id),
            "repair_plans": self.store.rows("repair_plans", job_id=job_id),
            "change_candidates": self.store.rows(
                "system_change_candidates", job_id=job_id,
            ),
            "failure_triage_runs": self.triage.rows(job_id=job_id),
            "system_repair_runs": SystemRepairAgent(
                self.root, self.control
            ).rows(job_id=job_id),
            "validation_certificates": self.store.rows(
                "validation_certificates", job_id=job_id,
            ),
            "promotion_receipts": self.store.rows(
                "policy_promotion_receipts", job_id=job_id,
            ),
            "stage_outcomes": consistency_rows("stage_outcomes"),
            "artifact_generations": consistency_rows("artifact_generations"),
            "recovery_transactions": consistency_rows("recovery_transactions"),
            "repair_graphs": consistency_rows("repair_graphs"),
            "final_reconciliations": consistency_rows("final_reconciliations"),
        }
    @staticmethod
    def _comparable_previous_score(
        prior: Any, observation: Any,
    ) -> dict[str, Any] | None:
        """Compare only observations from the same artifact-lineage semantics.

        A rewind or a newly introduced stale-artifact filter can lower the
        visible score while making the measurement more truthful.  That is a
        recovery epoch / observability correction, not evidence that a policy
        change caused a quality regression.
        """
        if prior is None:
            return None
        prior_payload = _payload(prior["payload"])
        current_payload = observation if isinstance(observation, dict) else observation.asdict()
        prior_lineage = _payload(_payload(prior_payload.get("evidence")).get("lineage"))
        current_lineage = _payload(_payload(current_payload.get("evidence")).get("lineage"))

        def valid_lineage(value: dict[str, Any]) -> bool:
            claimed = str(value.get("lineage_digest") or "")
            body = {key: item for key, item in value.items() if key != "lineage_digest"}
            frontier = value.get("accepted_protocol_frontier")
            producer_hashes = _payload(value.get("producer_output_hashes"))
            recovery_epochs = _payload(value.get("recovery_epochs"))
            if (not claimed or claimed != digest(body)
                    or not isinstance(frontier, list) or not frontier):
                return False
            return all(
                isinstance(item, dict)
                and str(item.get("protocol") or "")
                and str(item.get("producer_task_id") or "")
                and str(item.get("output_hash") or "")
                and producer_hashes.get(str(item["producer_task_id"])) == item["output_hash"]
                and str(item["producer_task_id"]) in recovery_epochs
                for item in frontier
            )

        if not valid_lineage(prior_lineage) or not valid_lineage(current_lineage):
            return None
        prior_frontier = {
            str(item["protocol"]): str(item["producer_task_id"])
            for item in prior_lineage["accepted_protocol_frontier"]
        }
        current_frontier = {
            str(item["protocol"]): str(item["producer_task_id"])
            for item in current_lineage["accepted_protocol_frontier"]
        }
        run_epoch_match = (
            bool(prior_lineage.get("run_epoch"))
            and prior_lineage.get("run_epoch") == current_lineage.get("run_epoch")
        )
        frontier_compatible = all(
            current_frontier.get(protocol) == producer
            for protocol, producer in prior_frontier.items()
        )
        prior_epochs = _payload(prior_lineage.get("recovery_epochs"))
        current_epochs = _payload(current_lineage.get("recovery_epochs"))
        epochs_monotonic = all(
            int(_payload(current_epochs.get(task_id)).get(key) or 0)
            >= int(_payload(epoch).get(key) or 0)
            for task_id, epoch in prior_epochs.items()
            for key in ("binding_generation", "recovery_epoch")
        )
        if not (run_epoch_match and frontier_compatible and epochs_monotonic):
            return None
        if not (prior_payload.get("observation_id") or prior["observation_id"]):
            return None
        if not current_payload.get("observation_id"):
            return None
        prior_dimensions = _payload(prior_payload.get("dimensions"))
        current_dimensions = _payload(current_payload.get("dimensions"))
        dimension_deltas = {
            name: round(float(current_dimensions.get(name) or 0.0)
                        - float(prior_dimensions.get(name) or 0.0), 6)
            for name in sorted(set(prior_dimensions) | set(current_dimensions))
        }
        prior_hashes = _payload(prior_lineage.get("producer_output_hashes"))
        current_hashes = _payload(current_lineage.get("producer_output_hashes"))
        producer_hash_deltas = {
            task_id: {
                "previous": prior_hashes.get(task_id),
                "current": current_hashes.get(task_id),
            }
            for task_id in sorted(set(prior_hashes) | set(current_hashes))
            if prior_hashes.get(task_id) != current_hashes.get(task_id)
        }
        return {
            "previous_observation_id": str(
                prior_payload.get("observation_id") or prior["observation_id"]
            ),
            "current_observation_id": str(current_payload.get("observation_id") or ""),
            "previous_score": float(prior["score"]),
            "current_score": float(current_payload.get("score") or 0.0),
            "lineage_compatible": True,
            "frontier_compatibility": {
                "run_epoch_match": run_epoch_match,
                "prior_frontier_is_current_subset": frontier_compatible,
                "recovery_epochs_monotonic": epochs_monotonic,
            },
            "dimension_deltas": dimension_deltas,
            "producer_hash_deltas": producer_hash_deltas,
            "previous_lineage": prior_lineage,
            "current_lineage": current_lineage,
        }
