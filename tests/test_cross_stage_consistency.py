from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from lca_project.control import ControlPlane
from lca_project.kernel.consistency import ConsistencyError, ConsistencyLedger
from lca_project.kernel.goal_alignment.autonomous_supervisor import AutonomousJobSupervisor
from lca_project.kernel.goal_alignment.change_controller import ChangeController
from lca_project.kernel.goal_alignment.system_repair_agent import SystemRepairAgent
from lca_project.kernel.orchestrator import PersistentOrchestrator
from lca_project.kernel.skills import SkillInvoker
from lca_project.kernel.state import utcnow


ROOT = Path(__file__).resolve().parents[1]


def project_copy(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for name in (
        "skills", "workflows", "capabilities", "contracts", "policies", "agents",
    ):
        shutil.copytree(ROOT / name, root / name)
    return root


def create_job(root: Path) -> tuple[str, str, PersistentOrchestrator]:
    accepted = SkillInvoker(root).invoke(
        "generate-node-wiki",
        {"industry": "ict_equipment", "nodes": ["A019"], "publication_mode": "reviewed"},
    )
    orchestrator = PersistentOrchestrator(root)
    run_id = orchestrator.materialize(accepted["job_id"])
    return str(accepted["job_id"]), run_id, orchestrator


def complete_with_file(
    root: Path, orchestrator: PersistentOrchestrator, run_id: str,
    task_id: str, logical_path: str, content: dict[str, object],
) -> str:
    workspace = root / "work"
    target = workspace / logical_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(content), encoding="utf-8")
    attempt_id, inputs = orchestrator.claim(run_id, task_id)
    manifest = orchestrator.control.artifacts.put_task_output_manifest(
        workspace, [{"path": logical_path, "role": "protocol_artifact"}],
        {"status": "ok"}, run_id=run_id, task_id=task_id,
        attempt_id=attempt_id, input_hashes=inputs,
    )
    return orchestrator.complete(
        attempt_id, {"status": "ok"}, output_manifest_hash=manifest.digest,
    )


def test_stage_outcome_and_artifact_generation_are_committed_with_task(
    tmp_path: Path,
) -> None:
    root = project_copy(tmp_path)
    job_id, run_id, orchestrator = create_job(root)

    output = complete_with_file(
        root, orchestrator, run_id, "plan", "protocol/plan.json", {"version": 1},
    )

    outcome = orchestrator.state._connection().execute(
        "SELECT * FROM stage_outcomes WHERE run_id=? AND task_id='plan'", (run_id,),
    ).fetchone()
    artifact = orchestrator.state._connection().execute(
        "SELECT * FROM artifact_generations WHERE job_id=? AND logical_path=?",
        (job_id, "protocol/plan.json"),
    ).fetchone()
    assert outcome is not None
    assert (outcome["execution_status"], outcome["gate_decision"], outcome["goal_effect"]) == (
        "completed", "NOT_APPLICABLE", "progress",
    )
    assert artifact is not None
    assert artifact["generation"] == 1 and artifact["status"] == "current"
    assert artifact["proof_digest"] == output


def test_artifact_path_takeover_requires_dag_descent(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    _, run_id, orchestrator = create_job(root)
    complete_with_file(
        root, orchestrator, run_id, "plan", "shared.json", {"producer": "plan"},
    )
    with orchestrator.state.transaction() as conn:
        # Fault injection: make prepare independent, then try to take over a
        # path owned by plan.  The ledger must reject the state transition.
        conn.execute(
            "UPDATE orchestrator_tasks SET dependencies='[]' "
            "WHERE run_id=? AND task_id='prepare'", (run_id,),
        )
        with pytest.raises(ConsistencyError, match="path takeover"):
            orchestrator.consistency.record_artifact_manifest(
                conn, run_id=run_id, task_id="prepare", attempt_id="attempt_fault",
                manifest_digest="a" * 64,
                manifest={"files": [{"path": "shared.json", "sha256": "b" * 64}]},
            )


def test_rewind_is_one_recovery_transaction_and_stales_outputs(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id, run_id, orchestrator = create_job(root)
    complete_with_file(root, orchestrator, run_id, "plan", "plan.json", {"v": 1})
    complete_with_file(root, orchestrator, run_id, "prepare", "prepare.json", {"v": 1})
    job = orchestrator.state.get("jobs", job_id)
    assert job is not None
    orchestrator.state.upsert_entity(
        "jobs", job_id, "manual_review", job["payload"],
        program_id=job.get("program_id"), industry_id=job.get("industry_id"),
        workflow_id=job.get("workflow_id"),
    )
    with orchestrator.state.transaction() as conn:
        conn.execute(
            "UPDATE orchestrator_runs SET status='manual_review' WHERE run_id=?", (run_id,),
        )

    invalidated = orchestrator.rewind_from(
        run_id, "plan", reason="fault-injection causal repair", actor="test",
    )

    tasks = {row.task_id: row.status for row in orchestrator.tasks(run_id)}
    assert invalidated[0] == "plan"
    assert tasks["plan"] == "ready" and tasks["prepare"] == "pending"
    assert orchestrator.state.get("jobs", job_id)["status"] == "ready"
    assert orchestrator.state._connection().execute(
        "SELECT status FROM orchestrator_runs WHERE run_id=?", (run_id,),
    ).fetchone()["status"] == "ready"
    assert {
        row["status"] for row in orchestrator.state._connection().execute(
            "SELECT status FROM artifact_generations WHERE run_id=?", (run_id,),
        )
    } == {"stale"}
    recovery = orchestrator.state._connection().execute(
        "SELECT * FROM recovery_transactions WHERE run_id=?", (run_id,),
    ).fetchone()
    assert recovery is not None and recovery["status"] == "committed"
    assert ConsistencyLedger(orchestrator.state).assert_run_invariants(run_id) == []


def test_repair_key_changes_only_when_causal_generation_changes(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id, run_id, orchestrator = create_job(root)
    changes = ChangeController(root)
    first = changes.propose(
        source_deviation_id="dev_one", target="propose_code_change", risk="low",
        change={"source": "first"}, rollback={"strategy": "restore"},
    )
    second = changes.propose(
        source_deviation_id="dev_two", target="propose_code_change", risk="low",
        change={"source": "second"}, rollback={"strategy": "restore"},
    )
    request = {
        "failed_task": "research_plan_gate",
        "source_failure_fingerprint": "same-failure",
        "causal_input_changes": [{"target": "research contract", "change": "v2"}],
    }
    agent = SystemRepairAgent(root)
    one = agent.queue(
        candidate_id=first["candidate_id"], source_job_id=job_id,
        source_run_id=run_id, request=request,
    )
    duplicate = agent.queue(
        candidate_id=second["candidate_id"], source_job_id=job_id,
        source_run_id=run_id, request=request,
    )
    assert duplicate["repair_run_id"] == one["repair_run_id"]

    orchestrator.create_binding_generation(
        run_id, "research_plan_gate", reason="causal input changed",
    )
    third = changes.propose(
        source_deviation_id="dev_three", target="propose_code_change", risk="low",
        change={"source": "third"}, rollback={"strategy": "restore"},
    )
    next_generation = agent.queue(
        candidate_id=third["candidate_id"], source_job_id=job_id,
        source_run_id=run_id, request=request,
    )
    assert next_generation["repair_run_id"] != one["repair_run_id"]
    assert next_generation["repair_key"] != one["repair_key"]


def test_reviewed_release_final_reconciliation_closes_all_projections(
    tmp_path: Path,
) -> None:
    root = project_copy(tmp_path)
    supervisor = AutonomousJobSupervisor(root)
    view = supervisor.create_campaign({
        "schema_version": "autonomous-job-campaign-v1",
        "name": "final reconciliation",
        "skill": "generate-node-wiki",
        "requests": [{
            "industry": "ict_equipment", "nodes": ["A019"],
            "publication_mode": "reviewed",
        }],
        "completion_goal": "reviewed_publication",
        "max_concurrency": 1,
        "max_auto_repairs_per_job": 3,
    })
    campaign_id = str(view["campaign"]["campaign_id"])
    supervisor.tick(campaign_id, execute_task=False)
    item = supervisor.campaign(campaign_id)["items"][0]
    job_id, run_id = str(item["job_id"]), str(item["run_id"])
    workspace = root / "release-proof"
    workspace.mkdir()
    record = {
        "protocol": "release-record-v1", "publication_status": "published",
        "release_id": "release_test", "job_id": job_id,
        "candidate_hashes": {"wiki/a019.md": "a" * 64},
        "gate_report_sha256": "b" * 64,
        "reviewed_apply_sha256": "c" * 64,
        "publish_report_sha256": "d" * 64,
    }
    (workspace / "release-record.json").write_text(json.dumps(record), encoding="utf-8")
    artifact = supervisor.control.artifacts.put_task_output_manifest(
        workspace, [{"path": "release-record.json", "role": "protocol_artifact"}],
        {"status": "ok"}, run_id=run_id, task_id="publish",
        attempt_id="attempt_publish_proof",
    )
    job = supervisor.state.get("jobs", job_id)
    assert job is not None
    supervisor.state.upsert_entity(
        "jobs", job_id, "published", job["payload"],
        program_id=job.get("program_id"), industry_id=job.get("industry_id"),
        workflow_id=job.get("workflow_id"),
    )
    with supervisor.state.transaction() as conn:
        conn.execute(
            "UPDATE orchestrator_tasks SET status='succeeded',output_hash=?,updated_at=? "
            "WHERE run_id=? AND task_id='publish'",
            (artifact.digest, utcnow(), run_id),
        )

    synced = supervisor._sync_item(item)

    assert synced["status"] == "succeeded"
    final = supervisor.campaign(campaign_id)
    assert final["campaign"]["status"] == "completed"
    assert final["items"][0]["status"] == "succeeded"
    assert supervisor.state._connection().execute(
        "SELECT status FROM final_reconciliations WHERE job_id=?", (job_id,),
    ).fetchone()["status"] == "committed"
