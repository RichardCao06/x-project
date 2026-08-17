from __future__ import annotations

import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from lca_project.control import ControlPlane
from lca_project.contracts import Job, JobState
from lca_project.kernel.goal_alignment import (
    ChangeController, FailureTriageAgent, GoalAlignmentController, GoalRegistry,
    SystemMetaSupervisor,
)
from lca_project.kernel.goal_alignment.action_graph import (
    compile_action_graph, runnable_automatic_actions,
)
from lca_project.kernel.goal_alignment import SystemRepairAgent
from lca_project.kernel.goal_alignment.causal_analyzer import CausalAnalyzer
from lca_project.kernel.goal_alignment.deviation_detector import DeviationDetector
from lca_project.kernel.goal_alignment.models import Deviation, QualityObservation
from lca_project.kernel.goal_alignment.quality_trajectory import QualityTrajectory
from lca_project.kernel.goal_alignment.repair_planner import RepairPlanner
from lca_project.kernel.goal_alignment.store import AlignmentStore
from lca_project.kernel.orchestrator import PersistentOrchestrator
from lca_project.kernel.skills import SkillInvoker
from lca_project.kernel.state import utcnow


ROOT = Path(__file__).resolve().parents[1]


def project_copy(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for name in ("skills", "workflows", "capabilities", "contracts", "policies", "agents"):
        shutil.copytree(ROOT / name, root / name)
    return root


def observation(*, candidate: bool = False, score: float = 0.0) -> QualityObservation:
    return QualityObservation("job_test", "run_test", "wiki-node-goal-v1",
                              {name: score for name in (
                                  "identity_fidelity", "source_role_coverage",
                                  "claim_provenance_coverage", "semantic_closure",
                                  "editorial_coherence", "table_contract_validity",
                                  "data_readiness", "gap_provenance", "reader_utility")},
                              score, {"maturity": {"candidate_eligible": candidate}})


def test_goal_contract_is_versioned_weighted_and_immutable(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    plane = ControlPlane(root)
    registry = GoalRegistry(root, plane.state)

    first = registry.load()
    assert first["schema_version"] == "goal-contract-v1"
    assert len(first["contract_hash"]) == 64
    assert sum(item["weight"] for item in first["quality_dimensions"].values()) == 1

    changed = json.loads((root / "policies/wiki-goal-contract-v1.json").read_text())
    changed["description"] = "silent goal drift"
    path = root / "policies/drift.json"
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable Goal Contract drift"):
        registry.load(path)


def test_a040_mutation_is_detected_as_false_pass() -> None:
    job = {"id": "job_test", "status": "candidate"}
    deviations = DeviationDetector().detect(
        job=job, run={"status": "ready"}, tasks=[], observation=observation(candidate=False)
    )
    assert [item.deviation_type for item in deviations] == ["false_pass"]
    assert deviations[0].severity == "critical"


def test_rewind_or_measurement_correction_is_not_quality_regression() -> None:
    prior = {
        "score": 0.33,
        "payload": json.dumps({"evidence": {"task_completion": {"tasks": {
            "content_compose": "succeeded", "maturity_gate": "succeeded",
        }}}}),
    }
    current = observation(score=0.15)
    current.evidence["task_completion"] = {"tasks": {
        "content_compose": "ready", "maturity_gate": "pending",
    }}

    comparable = GoalAlignmentController._comparable_previous_score(prior, current)

    assert comparable is None


def test_comparable_regression_carries_lineage_proof() -> None:
    current = observation(score=0.15)
    deviations = DeviationDetector().detect(
        job={"status": "ready"}, run={"status": "ready"},
        tasks=[{"task_id": "content_compose", "status": "succeeded"}],
        observation=current, previous_score=0.33,
    )

    regression = next(item for item in deviations
                      if item.deviation_type == "quality_regression")
    assert regression.evidence["lineage_compatible"] is True
    assert regression.evidence["task_statuses"]["content_compose"] == "succeeded"


def test_quality_regression_requires_evidence_backed_agent_triage() -> None:
    diagnosis = CausalAnalyzer().analyze(Deviation(
        "quality_regression", "high",
        {"previous_score": 0.33, "current_score": 0.15},
        "quality score regressed",
    ))

    assert diagnosis.cause_code == "QUALITY_TRAJECTORY_REGRESSION"
    assert CausalAnalyzer.requires_agent_triage(diagnosis)


def test_system_repair_rejects_missing_required_causal_input_and_proof() -> None:
    request = {
        "causal_input_changes": [],
        "proof_contract": [],
        "goal_constraints": {
            "repair_must_change_a_named_causal_input": True,
            "promotion_requires_proof_metric_improvement": True,
        },
    }

    with pytest.raises(RuntimeError, match="named causal input"):
        SystemRepairAgent._validate_repair_request(request)
    request["causal_input_changes"] = [{"target": "quality regression routing"}]
    with pytest.raises(RuntimeError, match="Proof Contract"):
        SystemRepairAgent._validate_repair_request(request)
    request["proof_contract"] = [{"metric": "dimension_score"}]
    SystemRepairAgent._validate_repair_request(request)


def test_system_repair_deduplicates_same_triage_across_candidates(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    changes = ChangeController(root)
    first = changes.propose(
        source_deviation_id="dev_same", target="propose_code_change", risk="low",
        change={"source": "ordinary-controller"}, rollback={"strategy": "restore"},
    )
    second = changes.propose(
        source_deviation_id="dev_same", target="propose_code_change", risk="low",
        change={"source": "meta-controller"}, rollback={"strategy": "restore"},
    )
    agent = SystemRepairAgent(root)
    request = {"triage_run_id": "tri_same", "recovery_task": "",
               "source_failure_fingerprint": "failure_same"}

    queued = agent.queue(
        candidate_id=first["candidate_id"], source_job_id="job_same",
        source_run_id="run_same", request=request,
    )
    duplicate = agent.queue(
        candidate_id=second["candidate_id"], source_job_id="job_same",
        source_run_id="run_same", request=request,
    )

    assert duplicate["repair_run_id"] == queued["repair_run_id"]
    assert len(agent.rows(job_id="job_same")) == 1

    third = changes.propose(
        source_deviation_id="dev_new_report", target="propose_code_change", risk="low",
        change={"source": "later-attempt"}, rollback={"strategy": "restore"},
    )
    same_failure = agent.queue(
        candidate_id=third["candidate_id"], source_job_id="job_same",
        source_run_id="run_same", request={
            "triage_run_id": "tri_later", "recovery_task": "",
            "evidence": {"failure": {"failure_fingerprint": "failure_same"}},
        },
    )
    assert same_failure["repair_run_id"] == queued["repair_run_id"]


def test_golden_candidate_maps_to_complete_goal_vector(tmp_path: Path) -> None:
    batch = tmp_path / "batch"
    documents = {
        "search-execution-gate.json": {"decision": "PASS"},
        "terminology-verdict.json": {"status": "UNRESOLVED",
                                      "aliases_authorized_for_discovery": True},
        "source-diversity-gate.json": {"decision": "PASS"},
        "content-blueprint.json": {"semantic_conflicts": []},
        "content-closure-gate.json": {"candidate_eligible": True},
        "editorial-loop/editorial-policy-decision.json": {"decision": "accept"},
        "draft-content-gate.json": {"candidate_eligible": True},
        "table-data/source-verdict.json": {"verdict": "PASS"},
        "table-data/table-population-gate.json": {"verdict": "GO"},
        "maturity-gate.json": {"candidate_eligible": True, "data_readiness": "data_ready",
                                "checks": {"explicit_gaps_have_search_provenance": True}},
    }
    for relative, value in documents.items():
        target = batch / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value), encoding="utf-8")
    goal = json.loads((ROOT / "policies/wiki-goal-contract-v1.json").read_text())
    result = QualityTrajectory().observe(job_id="golden", run_id="run", goal=goal, batch=batch)

    assert result.score == 1.0
    assert set(result.dimensions.values()) == {1.0}


def test_a037_translation_gap_is_detected_as_false_block() -> None:
    tasks = [{"task_id": "research_plan_gate", "status": "manual_review",
              "attempt": 1, "failure_code": "RESEARCH_PLAN_INVALID",
              "failure_payload": {"message": "unmatched fragment: 机架"}}]
    deviations = DeviationDetector().detect(
        job={"id": "job_test", "status": "manual_review"},
        run={"status": "manual_review"}, tasks=tasks, observation=observation()
    )
    assert [item.deviation_type for item in deviations] == ["false_block"]


def test_editorial_policy_contract_mismatch_routes_to_coding_agent() -> None:
    failure = {"message": "ValueError: 内容没有通过独立 Editorial Review GO，不得进入确定性组装"}
    deviations = DeviationDetector().detect(
        job={"id": "job_test", "status": "manual_review"},
        run={"status": "manual_review"},
        tasks=[{"task_id": "draft_content_gate", "status": "manual_review",
                "attempt": 2, "failure_code": "CAPABILITY_PROCESS_FAILED",
                "failure_payload": failure}],
        observation=observation(),
    )

    assert deviations[0].deviation_type == "ineffective_repair"
    diagnosis = CausalAnalyzer().analyze(deviations[0])
    assert diagnosis.cause_code == "EDITORIAL_POLICY_CONTRACT_MISMATCH"
    proposal = RepairPlanner().plan(diagnosis)
    assert proposal.level == "L2"
    assert proposal.action == "propose_code_change"


def table_deadlock_triage_result() -> dict:
    return {
        "problem_class": "process_deadlock",
        "cause_code": "TABLE_COLLECTION_BOOTSTRAP_DEADLOCK",
        "summary": "table collection requires evidence before it can create the search that finds it",
        "causal_chain": ["confirmed sources are zero", "builder exits before search matrix creation"],
        "evidence": [
            {"fact": "builder checks confirmed rows before creating queries",
             "source": "scripts/build_wiki_table_collection.py"},
        ],
        "repair_level": "L2", "repair_action": "propose_code_change",
        "recovery_task": "table_collect", "risk": "low", "confidence": 0.97,
        "requires_external_authority": False,
        "implementation_targets": ["scripts/build_wiki_table_collection.py"],
        "validation_tests": ["tests/test_table_collection_runtime.py"],
        "goal_assessment": {
            "result_finished": False, "closer_to_goal": False,
            "why_not_closer": ["search matrix is never created"],
        },
        "causal_input_changes": [{
            "target": "table collection bootstrap",
            "change": "build the search matrix before requiring confirmed evidence",
            "expected_effect": "the collector can discover its first evidence",
        }],
        "proof_contract": [{
            "metric": "queries_executed", "baseline": 0, "target": ">0",
            "evidence_artifact": "table-data/search-matrix.executed.json",
        }],
        "actions": [{"kind": "propose_code_change",
                     "target": "scripts/build_wiki_table_collection.py",
                     "authority": "automatic_analysis_and_validation"}],
        "safe_autonomous_actions_remaining": True,
    }


def workspace_overwrite_triage_result() -> dict:
    return {
        "problem_class": "implementation_defect",
        "cause_code": "MUTABLE_WORKSPACE_STATE_OVERWRITTEN_BY_REFRESH",
        "summary": "repair promotion overwrote a task-owned Wiki page",
        "causal_chain": ["draft apply committed", "workspace refresh restored vendor seed"],
        "evidence": [{"fact": "current page equals old_sha256",
                      "source": "apply-transaction.json"}],
        "repair_level": "manual", "repair_action": "request_operator",
        "recovery_task": "draft_apply", "risk": "medium", "confidence": 0.99,
        "requires_external_authority": True,
        "implementation_targets": [
            "src/lca_project/domains/wiki_workspace.py",
            "src/lca_project/kernel/goal_alignment/system_repair_agent.py",
        ],
        "validation_tests": ["tests/wiki_phase2/test_wiki_isolation_replay.py"],
        "goal_assessment": {
            "result_finished": False, "closer_to_goal": False,
            "why_not_closer": ["the repaired Wiki page is overwritten"],
        },
        "causal_input_changes": [{
            "target": "workspace refresh ownership",
            "change": "preserve task-owned mutable outputs during refresh",
            "expected_effect": "validated content remains materialized",
        }],
        "proof_contract": [{
            "metric": "rehydrated_page_hash_matches_candidate", "baseline": False,
            "target": True, "evidence_artifact": "apply-transaction.json",
        }],
        "actions": [{"kind": "propose_code_change",
                     "target": "src/lca_project/domains/wiki_workspace.py",
                     "authority": "automatic_analysis_and_validation"}],
        "safe_autonomous_actions_remaining": True,
    }


def low_research_utility_triage_result() -> dict:
    return {
        "problem_class": "implementation_defect",
        "cause_code": "ZERO_YIELD_QUERY_AND_EXTRACTION_PIPELINE",
        "summary": "generic queries and missing field extraction produced zero modelling evidence",
        "causal_chain": [
            "queries leak internal identifiers and mixed-language field labels",
            "fetched pages produce no field observations",
            "all modelling fields remain empty",
        ],
        "evidence": [{
            "fact": "329 candidates produced zero accepted observations",
            "source": "table-data/evidence-selection.json",
        }],
        "repair_level": "L2", "repair_action": "propose_code_change",
        "recovery_task": "table_search", "risk": "low", "confidence": 0.96,
        "requires_external_authority": False,
        "implementation_targets": [
            "scripts/build_wiki_table_collection.py",
            "scripts/execute_wiki_table_search.py",
        ],
        "validation_tests": ["tests/test_table_collection_runtime.py"],
        "goal_assessment": {
            "result_finished": True, "closer_to_goal": False,
            "why_not_closer": ["no accepted observations or populated modelling fields"],
        },
        "causal_input_changes": [{
            "target": "table query and field extraction strategy",
            "change": "remove internal IDs, normalize language, add document routes and field extractors",
            "expected_effect": "convert fetched technical documents into field-level evidence",
        }],
        "proof_contract": [{
            "metric": "accepted_observations", "baseline": 0, "target": ">0",
            "evidence_artifact": "table-data/evidence-selection.json",
        }],
        "actions": [{"kind": "propose_code_change",
                     "target": "scripts/build_wiki_table_collection.py",
                     "authority": "automatic_analysis_and_validation"}],
        "safe_autonomous_actions_remaining": True,
    }


def test_internal_manual_triage_still_prepares_a_validated_system_change() -> None:
    proposal = RepairPlanner.from_triage(workspace_overwrite_triage_result())

    assert proposal.level == "L2"
    assert proposal.action == "propose_code_change"
    assert proposal.authority == "change_controller"


def test_contract_mismatch_preserves_safe_code_analysis_before_operator_boundary() -> None:
    result = workspace_overwrite_triage_result()
    result["problem_class"] = "contract_mismatch"

    proposal = RepairPlanner.from_triage(result)

    assert proposal.level == "L2"
    assert proposal.action == "propose_code_change"
    assert proposal.authority == "change_controller"


def test_compound_triage_is_compiled_to_independently_authorized_action_graph() -> None:
    result = workspace_overwrite_triage_result()
    result["actions"] = [
        {"kind": "propose_code_change", "target": "repair planner",
         "authority": "automatic_analysis_and_validation"},
        {"kind": "retry_task", "target": "run_x:table_collect",
         "authority": "automatic"},
        {"kind": "request_operator", "target": "promote medium-risk patch",
         "authority": "operator"},
    ]

    graph = compile_action_graph("tri_compound", result)

    assert graph["recovery_task"] == "draft_apply"
    assert [item["kind"] for item in runnable_automatic_actions(graph)] == [
        "propose_code_change", "retry_task"
    ]
    approval = graph["actions"][2]
    assert approval["status"] == "awaiting_authority"
    assert approval["dependencies"] == [
        graph["actions"][0]["action_id"], graph["actions"][1]["action_id"]
    ]


def test_triage_pseudo_recovery_name_falls_back_to_failed_dag_task() -> None:
    result = workspace_overwrite_triage_result()
    result["recovery_task"] = "repair_claim_binding_priority"

    validated = FailureTriageAgent._validate_result(
        result, failed_task="content_compose",
        allowed_tasks={"freeze", "content_compose", "editorial_review"},
    )

    assert validated["recovery_task"] == "content_compose"


def test_meta_supervisor_detects_and_executes_safe_analysis_lost_by_manual_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = project_copy(tmp_path)
    control = ControlPlane(root)
    deviation = AlignmentStore(control.state).deviation(
        job_id="job_meta", run_id="run_meta", goal_id="wiki-node-goal-v1",
        value={"deviation_type": "unclassified_failure", "severity": "high",
               "evidence": {"task_id": "editorial_review"}, "summary": "compound"},
    )
    triage = FailureTriageAgent(
        root, control, runner=lambda _sandbox, _request: {
            **workspace_overwrite_triage_result(), "problem_class": "contract_mismatch",
        },
    )
    queued = triage.queue(
        deviation_id=deviation["deviation_id"], source_job_id="job_meta",
        source_run_id="run_meta", task_id="editorial_review",
        request={"failure": {"message": "compound"}},
    )
    completed = triage.execute(queued["triage_run_id"])
    AlignmentStore(control.state).repair_plan(deviation["deviation_id"], {
        "repair_level": "manual", "action": "request_operator", "authority": "operator",
        "invalidates": [], "preserves": [], "validation": [], "automatic": False,
        "status": "proposed",
    })

    found = SystemMetaSupervisor(root, control=control).audit(job_id="job_meta")

    projection = next(item for item in found
                      if item["deviation_type"] == "REPAIR_PLAN_PROJECTION_LOSS")
    assert projection["evidence"]["triage_run_id"] == completed["triage_run_id"]
    assert projection["evidence"]["automatic_actions"][0]["kind"] == "propose_code_change"

    class FakeChangeController:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def propose(self, **_: object) -> dict:
            return {"candidate_id": "chg_meta"}

    class FakeRepairAgent:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def queue(self, **_: object) -> dict:
            return {"repair_run_id": "srr_meta", "status": "queued"}

        def execute(self, repair_run_id: str) -> dict:
            return {"repair_run_id": repair_run_id, "status": "awaiting_approval"}

    monkeypatch.setattr(
        "lca_project.kernel.goal_alignment.meta_supervisor.ChangeController",
        FakeChangeController,
    )
    monkeypatch.setattr(
        "lca_project.kernel.goal_alignment.meta_supervisor.SystemRepairAgent",
        FakeRepairAgent,
    )

    report = SystemMetaSupervisor(root, control=control).reconcile(job_id="job_meta")

    assert report["status"] == "progressed"
    assert report["actions"][0]["result"]["status"] == "awaiting_approval"
    repair_job = control.state._connection().execute(
        "SELECT status FROM control_plane_repair_jobs WHERE job_id='job_meta'"
    ).fetchone()
    assert repair_job["status"] == "awaiting_approval"


def test_meta_supervisor_resolves_rewind_range_to_earliest_dag_task(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    accepted = SkillInvoker(root).invoke(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["A039"]}
    )
    run_id = PersistentOrchestrator(root).materialize(accepted["job_id"])
    graph = {"actions": [{
        "kind": "rewind_task", "authority": "operator",
        "target": f"{run_id}:content_compose through editorial_review",
    }]}

    resolved = SystemMetaSupervisor(root)._authorized_rewind_task(graph, run_id)

    assert resolved == "content_compose"


def test_completed_zero_yield_research_is_not_counted_as_data_ready(tmp_path: Path) -> None:
    batch = tmp_path / "batch"
    documents = {
        "table-data/source-verdict.json": {"verdict": "PASS"},
        "table-data/table-population-gate.json": {"verdict": "GO"},
        "table-data/search-matrix.executed.json": {
            "document_routes": [],
            "queries": [{
                "language": "en", "query": "A039 刀片服务器 P018 主板 PCBA",
                "results": [{"fetch_status": "fetched", "url": f"https://e/{index}"}
                            for index in range(20)],
            }],
        },
        "table-data/evidence-selection.json": {
            "counts": {"fields": 33, "populated": 0, "explicit_gaps": 33},
            "candidate_audits": [{"observations": []} for _ in range(20)],
            "accepted_evidence": [],
            "reason_counts": {"no_field_specific_observation": 4},
        },
        "source-diversity-gate.json": {"decision": "LIMITED", "metrics": {"confirmed_urls": 0}},
        "maturity-gate.json": {
            "candidate_eligible": False, "data_readiness": "no_eligible_public_data",
            "checks": {"explicit_gaps_have_search_provenance": True},
        },
    }
    for relative, value in documents.items():
        path = batch / relative; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    goal = json.loads((ROOT / "policies/wiki-goal-contract-v1.json").read_text())

    observed = QualityTrajectory().observe(
        job_id="a039", run_id="run", goal=goal, batch=batch
    )
    outcome = observed.evidence["research_outcome"]

    assert observed.dimensions["data_readiness"] == 0.0
    assert outcome["workflow_finished"] is True
    assert outcome["closer_to_modelling_goal"] is False
    assert outcome["needs_investigation"] is True
    assert {"HIGH_VOLUME_ZERO_YIELD", "INTERNAL_IDENTIFIER_QUERY_LEAKAGE",
            "MIXED_LANGUAGE_ENGLISH_QUERY", "MISSING_DOCUMENT_ROUTES",
            "FIELD_EXTRACTION_ZERO_YIELD", "ZERO_MODEL_FIELDS_POPULATED"} <= set(
                outcome["reason_codes"]
            )
    deviations = DeviationDetector().detect(
        job={"id": "a039", "status": "evidence_limited"},
        run={"status": "succeeded"}, tasks=[], observation=observed,
    )
    assert [item.deviation_type for item in deviations] == ["low_research_utility"]
    diagnosis = CausalAnalyzer().analyze(deviations[0])
    assert CausalAnalyzer.requires_agent_triage(diagnosis)
    assert DeviationDetector().detect(
        job={"id": "a039", "status": "ready"}, run={"status": "ready"},
        tasks=[], observation=observed,
    ) == []


def test_low_utility_success_automatically_starts_triage_and_repair_agent(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    (root / "vendor/lca_cornerstone/fixtures/wiki-phase2/wiki/ict_equipment").mkdir(
        parents=True
    )
    accepted = SkillInvoker(root).invoke(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["A039"]}
    )
    orchestrator = PersistentOrchestrator(root)
    run_id = orchestrator.materialize(accepted["job_id"])
    job = orchestrator.control.state.get("jobs", accepted["job_id"])
    orchestrator.control.state.upsert_entity(
        "jobs", accepted["job_id"], "evidence_limited", job["payload"],
        program_id=job.get("program_id"), industry_id=job.get("industry_id"),
        workflow_id=job.get("workflow_id"),
    )
    with orchestrator.control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_runs SET status='succeeded',updated_at=? WHERE run_id=?",
                     (utcnow(), run_id))
    batch = (root / "var/workspaces/jobs" / accepted["job_id"] / "runs/wiki-batches"
             / "ict_equipment" / f"a039-{run_id.removeprefix('run_')[:12]}")
    documents = {
        "table-data/search-matrix.executed.json": {
            "document_routes": [],
            "queries": [{"language": "en", "query": "A039 刀片服务器",
                         "results": [{"fetch_status": "fetched"} for _ in range(20)]}],
        },
        "table-data/evidence-selection.json": {
            "counts": {"fields": 33, "populated": 0, "explicit_gaps": 33},
            "candidate_audits": [{"observations": []}], "accepted_evidence": [],
        },
        "source-diversity-gate.json": {"metrics": {"confirmed_urls": 0}},
        "maturity-gate.json": {
            "candidate_eligible": False, "data_readiness": "no_eligible_public_data",
            "checks": {"explicit_gaps_have_search_provenance": True},
        },
    }
    for relative, value in documents.items():
        path = batch / relative; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    triage = FailureTriageAgent(
        root, orchestrator.control,
        runner=lambda _sandbox, _request: low_research_utility_triage_result(),
    )

    controller = GoalAlignmentController(
        root, orchestrator.control, triage_agent=triage
    )
    controller._tasks = lambda _run_id: [  # type: ignore[method-assign]
        {"task_id": task_id, "status": "succeeded", "dependencies": [],
         "recorded_input_hashes": [], "output_hash": f"hash-{task_id}"}
        for task_id in ("source_diversity_gate", "table_collect", "maturity_gate")
    ]
    observed = controller.audit_job(
        accepted["job_id"], auto_repair=False, trigger="worker-after-task"
    )
    wakeup = next(action for action in observed["actions"]
                  if action["status"] == "supervision_requested")
    assert controller.store.pending_wakeups(job_id=accepted["job_id"])[0][
        "wakeup_id"
    ] == wakeup["wakeup_id"]

    report = controller.audit_job(
        accepted["job_id"], auto_repair=True, trigger="low-utility-success"
    )

    deviation = next(item for item in report["deviations"]
                     if item["deviation"]["deviation_type"] == "low_research_utility")
    assert deviation["triage"]["status"] == "completed"
    queued = next(action for action in report["actions"]
                  if action["status"] == "system_repair_queued")
    request = SystemRepairAgent(root).get(queued["repair_run_id"])["payload"]["request"]
    assert request["goal_assessment"]["closer_to_goal"] is False
    assert request["causal_input_changes"][0]["target"]
    assert request["proof_contract"][0]["metric"] == "accepted_observations"


def test_pending_or_lineage_stale_maturity_cannot_finish_workflow(tmp_path: Path) -> None:
    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "maturity-gate.json").write_text(json.dumps({
        "candidate_eligible": True, "data_readiness": "data_ready",
        "checks": {"explicit_gaps_have_search_provenance": True},
    }), encoding="utf-8")
    goal = json.loads((ROOT / "policies/wiki-goal-contract-v1.json").read_text())

    pending = QualityTrajectory().observe(
        job_id="a039", run_id="run", goal=goal, batch=batch,
        run_status="manual_review", tasks=[
            {"task_id": "maturity_gate", "status": "pending", "dependencies": ["table_apply"],
             "recorded_input_hashes": ["old-table"], "output_hash": "old-maturity"},
            {"task_id": "table_apply", "status": "succeeded", "dependencies": [],
             "recorded_input_hashes": [], "output_hash": "current-table"},
        ],
    )
    stale = QualityTrajectory().observe(
        job_id="a039", run_id="run", goal=goal, batch=batch,
        run_status="succeeded", tasks=[
            {"task_id": "maturity_gate", "status": "succeeded", "dependencies": ["table_apply"],
             "recorded_input_hashes": ["old-table"], "output_hash": "old-maturity"},
            {"task_id": "table_apply", "status": "succeeded", "dependencies": [],
             "recorded_input_hashes": [], "output_hash": "current-table"},
        ],
    )

    for observed, reason in ((pending, "task_not_succeeded"),
                             (stale, "current_upstream_hash_mismatch")):
        assert observed.evidence["maturity"] == {}
        assert observed.evidence["research_outcome"]["workflow_finished"] is False
        assert observed.evidence["research_outcome"]["evaluated"] is False
        assert observed.evidence["rejected_protocols"]["maturity"] == reason


def test_system_repair_claims_must_bind_causal_change_and_proof_to_patch() -> None:
    request = {
        "causal_input_changes": [{"target": "query strategy"}],
        "proof_contract": [{"metric": "accepted_observations"}],
    }
    valid = {
        "causal_input_changes_applied": [{
            "target": "query strategy", "implementation": "normalized external vocabulary",
            "changed_files": ["scripts/search.py"],
        }],
        "proof_instrumentation": [{
            "metric": "accepted_observations",
            "evidence_artifact": "table-data/evidence-selection.json",
            "test": "tests/test_search.py",
        }],
    }

    SystemRepairAgent._validate_goal_repair_claims(request, valid, ["scripts/search.py"])
    with pytest.raises(RuntimeError, match="Proof Contract metric"):
        SystemRepairAgent._validate_goal_repair_claims(
            request, {**valid, "proof_instrumentation": []}, ["scripts/search.py"]
        )


def test_system_repair_allows_only_governed_research_route_config() -> None:
    allowed = SystemRepairAgent.ALLOWED_PREFIXES

    assert "config/wiki-table-document-routes.json" in allowed
    assert "config/" not in allowed
    assert "config/search-providers.json" not in allowed
    assert SystemRepairAgent._is_allowed_path("config/wiki-table-document-routes.json")
    assert not SystemRepairAgent._is_allowed_path("config/wiki-table-document-routes.json.bak")
    assert not SystemRepairAgent._is_allowed_path("config/search-providers.json")
    assert SystemRepairAgent._is_allowed_path("scripts/new_repair.py")


def test_unknown_repeated_failure_requires_problem_based_agent_triage() -> None:
    failure = {"message": "unexpected table precondition", "identical_failure_repeated": True,
               "failure_fingerprint": "same"}
    deviations = DeviationDetector().detect(
        job={"id": "job_test", "status": "manual_review"},
        run={"status": "manual_review"},
        tasks=[{"task_id": "table_collect", "status": "manual_review", "attempt": 2,
                "failure_code": "CAPABILITY_PROCESS_FAILED", "failure_payload": failure}],
        observation=observation(),
    )

    assert [item.deviation_type for item in deviations] == ["repeated_fault"]
    assert deviations[0].evidence["failure"] == failure
    assert CausalAnalyzer.requires_agent_triage(
        CausalAnalyzer().analyze(deviations[0])
    )


def test_failure_triage_agent_persists_problem_based_route(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    agent = FailureTriageAgent(root, runner=lambda _sandbox, _request: table_deadlock_triage_result())
    deviation = AlignmentStore(agent.state).deviation(
        job_id="job_unknown", run_id="run_unknown", goal_id="wiki-node-goal-v1",
        value={"deviation_type": "unclassified_failure", "severity": "high",
               "evidence": {"task_id": "table_collect"}, "summary": "unknown"},
    )
    queued = agent.queue(
        deviation_id=deviation["deviation_id"], source_job_id="job_unknown",
        source_run_id="run_unknown",
        task_id="table_collect", request={"failure": {"message": "unknown"}},
    )

    result = agent.execute(queued["triage_run_id"])

    assert result["status"] == "completed"
    assert result["payload"]["result"]["cause_code"] == "TABLE_COLLECTION_BOOTSTRAP_DEADLOCK"
    proposal = RepairPlanner.from_triage(result["payload"]["result"])
    assert proposal.level == "L2" and proposal.action == "propose_code_change"


def test_controller_routes_unknown_failure_from_triage_to_coding_agent(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    accepted = SkillInvoker(root).invoke(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["A039"]}
    )
    orchestrator = PersistentOrchestrator(root)
    run_id = orchestrator.materialize(accepted["job_id"])
    now = utcnow()
    failure = {"message": "unexpected table precondition", "identical_failure_repeated": True,
               "failure_fingerprint": "table-deadlock"}
    with orchestrator.control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_tasks SET status='manual_review',attempt=2,"
                     "failure_code='CAPABILITY_PROCESS_FAILED',failure_payload=?,updated_at=? "
                     "WHERE run_id=? AND task_id='table_collect'",
                     (json.dumps(failure), now, run_id))
        conn.execute("UPDATE orchestrator_runs SET status='manual_review',updated_at=? WHERE run_id=?",
                     (now, run_id))
    job = orchestrator.control.state.get("jobs", accepted["job_id"])
    orchestrator.control.state.upsert_entity(
        "jobs", accepted["job_id"], str(JobState.MANUAL_REVIEW), job["payload"],
        program_id=job.get("program_id"), industry_id=job.get("industry_id"),
        workflow_id=job.get("workflow_id"),
    )
    triage = FailureTriageAgent(
        root, orchestrator.control,
        runner=lambda _sandbox, _request: table_deadlock_triage_result(),
    )

    report = GoalAlignmentController(
        root, orchestrator.control, triage_agent=triage
    ).audit_job(accepted["job_id"], auto_repair=True, trigger="unknown-failure-test")

    assert any(action["status"] == "failure_triage_completed" for action in report["actions"])
    queued = next(action for action in report["actions"]
                  if action["status"] == "system_repair_queued")
    repair = SystemRepairAgent(root).get(queued["repair_run_id"])
    request = repair["payload"]["request"]
    assert request["cause_code"] == "TABLE_COLLECTION_BOOTSTRAP_DEADLOCK"
    assert request["validation_tests"] == ["tests/test_table_collection_runtime.py"]


def test_first_unknown_failure_survives_fast_retry_and_reaches_agent_triage(
    tmp_path: Path,
) -> None:
    root = project_copy(tmp_path)
    accepted = SkillInvoker(root).invoke(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["A039"]}
    )
    orchestrator = PersistentOrchestrator(root)
    run_id = orchestrator.materialize(accepted["job_id"])
    failure = {"message": "new table contract failure",
               "identical_failure_repeated": False,
               "failure_fingerprint": "first-unknown"}
    now = utcnow()
    with orchestrator.control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_tasks SET status='repairable',attempt=1,"
                     "failure_code='CAPABILITY_PROCESS_FAILED',failure_payload=?,updated_at=? "
                     "WHERE run_id=? AND task_id='table_collect'",
                     (json.dumps(failure), now, run_id))
        conn.execute("UPDATE orchestrator_runs SET status='repairable',updated_at=? WHERE run_id=?",
                     (now, run_id))
    job = orchestrator.control.state.get("jobs", accepted["job_id"])
    orchestrator.control.state.upsert_entity(
        "jobs", accepted["job_id"], str(JobState.REPAIRABLE), job["payload"],
        program_id=job.get("program_id"), industry_id=job.get("industry_id"),
        workflow_id=job.get("workflow_id"),
    )

    # The worker's observational audit records the unknown failure without
    # inventing a generic system-change route.
    observed = GoalAlignmentController(root, orchestrator.control).audit_job(
        accepted["job_id"], auto_repair=False, trigger="worker-failure"
    )
    assert observed["deviations"][0]["repair_plan"]["action"] == "await_agent_triage"
    assert not any(action["status"] == "change_candidate_created"
                   for action in observed["actions"])

    # Simulate the repair policy hiding the failed shape behind a ready retry.
    with orchestrator.control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_tasks SET status='ready',failure_code=NULL,"
                     "failure_payload=NULL,updated_at=? WHERE run_id=? AND task_id='table_collect'",
                     (utcnow(), run_id))
        conn.execute("UPDATE orchestrator_runs SET status='ready',updated_at=? WHERE run_id=?",
                     (utcnow(), run_id))
    triage = FailureTriageAgent(
        root, orchestrator.control,
        runner=lambda _sandbox, _request: table_deadlock_triage_result(),
    )
    repaired = GoalAlignmentController(
        root, orchestrator.control, triage_agent=triage
    ).audit_job(accepted["job_id"], auto_repair=True, trigger="autonomy-next-tick")

    assert any(action["status"] == "failure_triage_completed"
               for action in repaired["actions"])
    assert any(action["status"] == "system_repair_queued"
               for action in repaired["actions"])


def test_investigating_triage_is_not_restarted(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    calls = 0

    def runner(_sandbox: Path, _request: dict) -> dict:
        nonlocal calls
        calls += 1
        return table_deadlock_triage_result()

    agent = FailureTriageAgent(root, runner=runner)
    deviation = AlignmentStore(agent.state).deviation(
        job_id="job_unknown", run_id="run_unknown", goal_id="wiki-node-goal-v1",
        value={"deviation_type": "unclassified_failure", "severity": "high",
               "evidence": {"task_id": "table_collect"}, "summary": "unknown"},
    )
    queued = agent.queue(
        deviation_id=deviation["deviation_id"], source_job_id="job_unknown",
        source_run_id="run_unknown", task_id="table_collect", request={"failure": {}},
    )
    with agent.state.transaction() as conn:
        conn.execute("UPDATE failure_triage_runs SET status='investigating' "
                     "WHERE triage_run_id=?", (queued["triage_run_id"],))

    result = agent.execute(queued["triage_run_id"])

    assert result["status"] == "investigating"
    assert calls == 0


def test_controller_queues_system_repair_for_editorial_contract_mismatch(
    tmp_path: Path,
) -> None:
    root = project_copy(tmp_path)
    accepted = SkillInvoker(root).invoke(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["A039"]}
    )
    orchestrator = PersistentOrchestrator(root)
    run_id = orchestrator.materialize(accepted["job_id"])
    now = utcnow()
    failure = {"message": "ValueError: 内容没有通过独立 Editorial Review GO，不得进入确定性组装"}
    with orchestrator.control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_tasks SET status='manual_review',attempt=2,"
                     "failure_code='CAPABILITY_PROCESS_FAILED',failure_payload=?,updated_at=? "
                     "WHERE run_id=? AND task_id='draft_content_gate'",
                     (json.dumps(failure, ensure_ascii=False), now, run_id))
        conn.execute("UPDATE orchestrator_runs SET status='manual_review',updated_at=? WHERE run_id=?",
                     (now, run_id))
    job = orchestrator.control.state.get("jobs", accepted["job_id"])
    orchestrator.control.state.upsert_entity(
        "jobs", accepted["job_id"], str(JobState.MANUAL_REVIEW), job["payload"],
        program_id=job.get("program_id"), industry_id=job.get("industry_id"),
        workflow_id=job.get("workflow_id"),
    )

    report = GoalAlignmentController(root).audit_job(
        accepted["job_id"], auto_repair=True, trigger="editorial-contract-regression"
    )

    queued = next(action for action in report["actions"]
                  if action["status"] == "system_repair_queued")
    repair = SystemRepairAgent(root).get(queued["repair_run_id"])
    assert repair["status"] == "queued"
    assert repair["source_job_id"] == accepted["job_id"]
    assert repair["payload"]["request"]["recovery_task"] == "draft_content_gate"


def test_system_repair_agent_codes_validates_and_promotes_low_risk_patch(
    tmp_path: Path,
) -> None:
    root = project_copy(tmp_path)
    candidate = ChangeController(root).propose(
        source_deviation_id="dev_contract", target="propose_code_change", risk="low",
        change={"diagnosis": "EDITORIAL_POLICY_CONTRACT_MISMATCH"},
        rollback={"strategy": "restore"},
    )

    def fake_agent(sandbox: Path, _: dict) -> dict:
        implementation = sandbox / "src/lca_project/example_repair.py"
        regression = sandbox / "tests/test_example_repair.py"
        implementation.parent.mkdir(parents=True, exist_ok=True)
        regression.parent.mkdir(parents=True, exist_ok=True)
        implementation.write_text("VALUE = 'fixed'\n", encoding="utf-8")
        regression.write_text("def test_fixed():\n    assert True\n", encoding="utf-8")
        return {"summary": "fix contract", "changed_files": [
            "src/lca_project/example_repair.py", "tests/test_example_repair.py"],
            "tests_added": ["tests/test_example_repair.py"], "risk_notes": []}

    def fake_validator(_: Path, phase: str, tests: tuple[str, ...]) -> dict:
        return {"phase": phase, "passed": True, "tests": list(tests)}

    agent = SystemRepairAgent(root, agent_runner=fake_agent, validator=fake_validator)
    queued = agent.queue(candidate_id=candidate["candidate_id"], source_job_id="job_test",
                         source_run_id=None, request={"recovery_task": ""})
    result = agent.execute(queued["repair_run_id"])

    assert result["status"] == "awaiting_outcome_validation"
    assert result["payload"]["outcome_validation"]["official_replay_required"] is True
    assert (root / "src/lca_project/example_repair.py").is_file()
    assert ChangeController(root).get(candidate["candidate_id"])["status"] == "promoted"
    certificates = ControlPlane(root).state._connection().execute(
        "SELECT COUNT(*) FROM validation_certificates WHERE candidate_id=? AND verdict='pass'",
        (candidate["candidate_id"],),
    ).fetchone()[0]
    assert certificates == 3


def test_medium_risk_repair_is_prepared_then_promoted_with_minimal_approval(
    tmp_path: Path,
) -> None:
    root = project_copy(tmp_path)
    accepted = SkillInvoker(root).invoke(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["A039"]}
    )
    orchestrator = PersistentOrchestrator(root)
    run_id = orchestrator.materialize(accepted["job_id"])
    with orchestrator.control.state.transaction() as conn:
        conn.execute(
            "UPDATE orchestrator_tasks SET attempt=4 WHERE run_id=? "
            "AND task_id='content_compose'", (run_id,),
        )
    candidate = ChangeController(root).propose(
        source_deviation_id="dev_medium", target="propose_code_change", risk="medium",
        change={"diagnosis": "MUTABLE_WORKSPACE_STATE_OVERWRITTEN_BY_REFRESH"},
        rollback={"strategy": "restore"},
    )

    def fake_agent(sandbox: Path, _: dict) -> dict:
        implementation = sandbox / "src/lca_project/example_repair.py"
        test = sandbox / "tests/test_example_repair.py"
        implementation.parent.mkdir(parents=True, exist_ok=True)
        test.parent.mkdir(parents=True, exist_ok=True)
        implementation.write_text("VALUE = 'prepared'\n", encoding="utf-8")
        test.write_text("def test_prepared(): assert True\n", encoding="utf-8")
        return {"summary": "prepare repair", "changed_files": [
            "src/lca_project/example_repair.py", "tests/test_example_repair.py"],
            "tests_added": ["tests/test_example_repair.py"], "risk_notes": []}

    validator = lambda _root, phase, tests: {
        "phase": phase, "passed": True, "tests": list(tests)
    }
    agent = SystemRepairAgent(root, agent_runner=fake_agent, validator=validator)
    queued = agent.queue(
        candidate_id=candidate["candidate_id"], source_job_id=accepted["job_id"],
        source_run_id=run_id, request={"recovery_task": "editorial_review"},
    )

    prepared = agent.execute(queued["repair_run_id"])
    assert prepared["status"] == "awaiting_approval"
    assert not (root / "src/lca_project/example_repair.py").exists()

    promoted = agent.approve(
        queued["repair_run_id"], recovery_task="content_compose"
    )
    assert promoted["status"] == "awaiting_outcome_validation"
    assert (root / "src/lca_project/example_repair.py").is_file()
    content = next(
        task for task in orchestrator.tasks(run_id) if task.task_id == "content_compose"
    )
    assert content.status == "ready"
    assert content.attempt == 4
    assert orchestrator.repair_epoch_attempt(run_id, "content_compose", 4) == 0
    assert promoted["payload"]["operator_authorization"][
        "authorized_recovery_task"
    ] == "content_compose"


def test_official_replay_marks_more_observations_but_zero_usable_data_as_partial(
    tmp_path: Path,
) -> None:
    root = project_copy(tmp_path)
    accepted = SkillInvoker(root).invoke(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["A039"]}
    )
    orchestrator = PersistentOrchestrator(root)
    run_id = orchestrator.materialize(accepted["job_id"])
    candidate = ChangeController(root).propose(
        source_deviation_id="dev_low_utility", target="propose_code_change", risk="low",
        change={"diagnosis": "RESEARCH_OUTCOME_CAUSE_REQUIRES_TRIAGE"},
        rollback={"strategy": "restore"},
    )
    repair_run_id = "srr_outcome_validation"
    payload = {
        "schema_version": "system-repair-run-v1", "repair_run_id": repair_run_id,
        "candidate_id": candidate["candidate_id"], "source_job_id": accepted["job_id"],
        "source_run_id": run_id, "promoted_at": "2000-01-01T00:00:00+00:00",
        "request": {"cause_code": "RESEARCH_OUTCOME_CAUSE_REQUIRES_TRIAGE",
                    "recovery_task": "maturity_gate", "evidence": {
                        "research_outcome": {"metrics": {
                            "field_observations": 0, "accepted_observations": 0,
                            "populated_fields": 0, "confirmed_sources": 0,
                        }}}, "proof_contract": []},
    }
    with orchestrator.control.state.transaction() as conn:
        conn.execute(
            "INSERT INTO system_repair_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (repair_run_id, candidate["candidate_id"], accepted["job_id"], run_id,
             "awaiting_outcome_validation", "test-model", None, "request-hash", None,
             json.dumps(payload), None, utcnow(), utcnow()),
        )
        conn.execute(
            "UPDATE orchestrator_tasks SET status='succeeded',updated_at=? "
            "WHERE run_id=? AND task_id='maturity_gate'", (utcnow(), run_id),
        )
    controller = GoalAlignmentController(root, orchestrator.control)
    current = SimpleNamespace(score=0.2, evidence={"research_outcome": {
        "closer_to_modelling_goal": False,
        "metrics": {"field_observations": 9, "accepted_observations": 0,
                    "populated_fields": 0, "confirmed_sources": 0},
        "proof_contract": [],
    }})

    actions = controller._evaluate_pending_system_repairs(
        accepted["job_id"], run_id, controller._tasks(run_id), current
    )

    assert actions[0]["status"] == "repair_partially_effective"
    assert SystemRepairAgent(root).get(repair_run_id)["status"] == "partially_effective"
    assert AlignmentStore(orchestrator.control.state).pending_wakeups(
        job_id=accepted["job_id"]
    )[0]["reason"] == "repair_outcome_partially_effective"


def test_system_repair_codex_invocation_uses_compatible_approval_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = project_copy(tmp_path)
    run_dir = root / "agent-output"
    run_dir.mkdir()
    seen: list[str] = []

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        seen.extend(command)
        (run_dir / "agent-result.json").write_text(
            '{"summary":"fixed","changed_files":["src/x.py","tests/test_x.py"],'
            '"tests_added":["tests/test_x.py"],"risk_notes":[]}', encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("lca_project.kernel.goal_alignment.system_repair_agent.subprocess.run",
                        fake_run)
    result = SystemRepairAgent(root)._run_codex(root, {
        "run_dir": str(run_dir), "repair_request": {"cause_code": "example"},
    })

    assert result["summary"] == "fixed"
    assert "--approve-for-me" in seen
    assert "--sandbox" not in seen


def test_system_repair_refreshes_only_existing_vendor_integrity_anchor(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandbox"
    target = sandbox / "vendor/lca_cornerstone/scripts/tool.py"
    manifest_path = sandbox / "docs/wiki-phase2-migration-manifest.json"
    target.parent.mkdir(parents=True)
    manifest_path.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    manifest_path.write_text(json.dumps({
        "anchor_hashes": {"scripts/tool.py": "old", "scripts/other.py": "keep"},
    }), encoding="utf-8")
    before = SystemRepairAgent._snapshot(sandbox)
    target.write_text("after\n", encoding="utf-8")

    updated = SystemRepairAgent._refresh_integrity_anchors(
        sandbox, before, ["vendor/lca_cornerstone/scripts/tool.py"]
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert updated == ["scripts/tool.py"]
    assert manifest["anchor_hashes"]["scripts/tool.py"] != "old"
    assert manifest["anchor_hashes"]["scripts/other.py"] == "keep"


def test_rejected_system_change_can_be_revised_with_audit_lineage(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    controller = ChangeController(root)
    candidate = controller.propose(
        source_deviation_id="dev_revision", target="propose_code_change", risk="low",
        change={"diagnosis": "example"}, rollback={"strategy": "restore"},
    )
    controller.certify(candidate["candidate_id"], phase="sandbox", suites={"golden": False})

    revised = controller.revise(candidate["candidate_id"], reason="validator updated")

    assert revised["status"] == "proposed"
    assert revised["candidate_id"] != candidate["candidate_id"]
    assert revised["payload"]["change"]["supersedes_candidate_id"] == candidate["candidate_id"]


def test_post_promotion_failure_restores_new_files_and_rejects_candidate(
    tmp_path: Path,
) -> None:
    root = project_copy(tmp_path)
    candidate = ChangeController(root).propose(
        source_deviation_id="dev_rollback", target="propose_code_change", risk="low",
        change={"diagnosis": "example"}, rollback={"strategy": "restore"},
    )

    def fake_agent(sandbox: Path, _: dict) -> dict:
        source = sandbox / "src/lca_project/new_repair.py"
        test = sandbox / "tests/test_new_repair.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        test.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("VALUE = 1\n", encoding="utf-8")
        test.write_text("def test_value(): assert True\n", encoding="utf-8")
        return {"summary": "repair", "changed_files": [
            "src/lca_project/new_repair.py", "tests/test_new_repair.py"],
            "tests_added": ["tests/test_new_repair.py"], "risk_notes": []}

    def validator(_: Path, phase: str, tests: tuple[str, ...]) -> dict:
        return {"phase": phase, "passed": phase != "post_promotion",
                "tests": list(tests)}

    agent = SystemRepairAgent(root, agent_runner=fake_agent, validator=validator)
    queued = agent.queue(candidate_id=candidate["candidate_id"], source_job_id="job_test",
                         source_run_id=None, request={"recovery_task": ""})
    result = agent.execute(queued["repair_run_id"])

    assert result["status"] == "failed"
    assert not (root / "src/lca_project/new_repair.py").exists()
    assert not (root / "tests/test_new_repair.py").exists()
    assert ChangeController(root).get(candidate["candidate_id"])["status"] == "rejected"


def test_controller_schedules_bounded_a037_repair_and_persists_chain(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    accepted = SkillInvoker(root).invoke(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["A037"]}
    )
    orchestrator = PersistentOrchestrator(root)
    run_id = orchestrator.materialize(accepted["job_id"])
    (root / "vendor/lca_cornerstone/fixtures/wiki-phase2/wiki/ict_equipment").mkdir(
        parents=True
    )
    batch = (root / "var/workspaces/jobs" / accepted["job_id"] / "runs/wiki-batches"
             / "ict_equipment" / f"a037-{run_id.removeprefix('run_')[:12]}")
    batch.mkdir(parents=True)
    (batch / "research-plan.json").write_text(json.dumps({
        "node_id": "A037",
        "node_name": "系统集成 | 机架PDU 1U",
        "terminology": {"query_translation": {
            "source_terms": ["机架"],
            "unmatched_fragments": ["机架"],
        }},
    }, ensure_ascii=False), encoding="utf-8")
    now = utcnow()
    with orchestrator.control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_tasks SET status='manual_review',attempt=1,"
                     "failure_code='RESEARCH_PLAN_INVALID',failure_payload=?,updated_at=? "
                     "WHERE run_id=? AND task_id='research_plan_gate'",
                     (json.dumps({"message": "unmatched fragment: 机架"}), now, run_id))
        conn.execute("UPDATE orchestrator_runs SET status='manual_review',updated_at=? WHERE run_id=?",
                     (now, run_id))
    job = orchestrator.control.state.get("jobs", accepted["job_id"])
    orchestrator.control.state.upsert_entity(
        "jobs", accepted["job_id"], str(JobState.MANUAL_REVIEW), job["payload"],
        program_id=job.get("program_id"), industry_id=job.get("industry_id"),
        workflow_id=job.get("workflow_id"),
    )

    report = GoalAlignmentController(root).audit_job(
        accepted["job_id"], auto_repair=True, trigger="a037-regression"
    )

    assert report["deviations"][0]["diagnosis"]["cause_code"] == "DISCOVERY_TRANSLATION_COVERAGE_GAP"
    assert report["actions"][0]["status"] == "scheduled"
    assert report["actions"][0]["causal_input_changed"] is True
    repair_artifact = json.loads(
        (batch / "research-plan-translation-repair.json").read_text(encoding="utf-8")
    )
    assert repair_artifact["repairs"] == {"机架": "rack-mounted"}
    assert orchestrator.tasks(run_id)[2].task_id == "research_plan"
    assert orchestrator.tasks(run_id)[2].status == "ready"
    status = GoalAlignmentController(root).status(job_id=accepted["job_id"])
    assert status["deviations"] and status["repair_plans"]

    # A repeated failure against the same source plan must not schedule another
    # rewind because the repair artifact no longer changes the causal input.
    with orchestrator.control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_tasks SET status='manual_review',attempt=2,"
                     "failure_code='RESEARCH_PLAN_INVALID',failure_payload=?,updated_at=? "
                     "WHERE run_id=? AND task_id='research_plan_gate'",
                     (json.dumps({"message": "unmatched fragment: 机架"}), utcnow(), run_id))
        conn.execute("UPDATE orchestrator_runs SET status='manual_review',updated_at=? WHERE run_id=?",
                     (utcnow(), run_id))
    job = orchestrator.control.state.get("jobs", accepted["job_id"])
    orchestrator.control.state.upsert_entity(
        "jobs", accepted["job_id"], str(JobState.MANUAL_REVIEW), job["payload"],
        program_id=job.get("program_id"), industry_id=job.get("industry_id"),
        workflow_id=job.get("workflow_id"),
    )
    repeated = GoalAlignmentController(root).audit_job(
        accepted["job_id"], auto_repair=True, trigger="same-causal-input"
    )
    assert any(action["status"] == "stopped" for action in repeated["actions"])

    with orchestrator.control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_tasks SET status='succeeded',output_hash='proof' "
                     "WHERE run_id=? AND task_id='research_plan_gate'", (run_id,))
    validated = GoalAlignmentController(root).audit_job(
        accepted["job_id"], trigger="a037-repair-validation"
    )
    assert any(action["status"] == "validated" for action in validated["actions"])
    status = GoalAlignmentController(root).status(job_id=accepted["job_id"])
    assert status["deviations"][0]["status"] == "resolved"
    assert any(item["status"] == "validated" for item in status["repair_plans"])


def test_l2_change_requires_golden_mutation_regression_and_three_phases(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    controller = ChangeController(root)
    candidate = controller.propose(
        source_deviation_id="dev_a040", target="wiki-maturity-gate",
        risk="low", change={"action": "bind candidate state to maturity proof"},
        rollback={"strategy": "restore prior policy"},
    )
    candidate_id = candidate["candidate_id"]
    with pytest.raises(ValueError, match="requires sandbox"):
        controller.promote(candidate_id)

    assert controller.certify(candidate_id, phase="sandbox",
                              suites={"golden": True})["verdict"] == "pass"
    assert controller.certify(candidate_id, phase="shadow",
                              suites={"mutation": True})["verdict"] == "pass"
    assert controller.certify(candidate_id, phase="canary",
                              suites={"regression": True})["verdict"] == "pass"
    promoted = controller.promote(candidate_id)
    assert promoted["status"] == "promoted"
    rolled_back = controller.rollback(candidate_id, reason="post-promotion regression")
    assert rolled_back["status"] == "rolled_back"


def test_high_risk_change_cannot_self_promote(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    controller = ChangeController(root)
    candidate = controller.propose(source_deviation_id=None, target="goal-contract",
                                   risk="high", change={"action": "change goal"},
                                   rollback={"strategy": "restore"})
    cid = candidate["candidate_id"]
    controller.certify(cid, phase="sandbox", suites={"golden": True})
    controller.certify(cid, phase="shadow", suites={"mutation": True})
    controller.certify(cid, phase="canary", suites={"regression": True})
    with pytest.raises(ValueError, match="explicit operator"):
        controller.promote(cid)
    assert controller.promote(cid, operator=True)["status"] == "promoted"
