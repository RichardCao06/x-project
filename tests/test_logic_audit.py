from __future__ import annotations

import json
from pathlib import Path
import shutil

from lca_project.kernel.logic_audit import LogicAuditAgent
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


def semantic_result(_root: Path, _request: dict[str, object]) -> dict[str, object]:
    return {
        "assessment": "questionable",
        "summary_zh": "研究计划需要把问题拆解固化为可重放合同。",
        "reviewed_actions": ["读取冻结的研究计划产物"],
        "reviewed_conclusions": ["研究主题已声明但子问题合同未持久化"],
        "findings": [],
    }


def prepared_research_plan(root: Path) -> tuple[str, str, str]:
    accepted = SkillInvoker(root).invoke("generate-node-wiki", {
        "industry": "ICT设备制造", "nodes": ["A019"],
        "publication_mode": "reviewed",
    })
    job_id = str(accepted["job_id"])
    orchestrator = PersistentOrchestrator(root)
    run_id = orchestrator.materialize(job_id)
    workspace = root / "var" / "logic-audit-fixture"
    workspace.mkdir(parents=True)
    (workspace / "research-plan.json").write_text(json.dumps({
        "schema_version": "research-plan-v1",
        "research_questions": [
            "identity_and_terminology", "process_origin_and_boundary",
        ],
        "source_classes": ["manufacturer_technical"],
    }, ensure_ascii=False), encoding="utf-8")
    manifest = orchestrator.control.artifacts.put_task_output_manifest(
        workspace,
        [{"path": "research-plan.json", "role": "research_plan"}],
        {"status": "ok"}, run_id=run_id, task_id="research_plan",
        attempt_id="attempt_logic_fixture",
    )
    with orchestrator.control.state.transaction() as conn:
        conn.execute(
            "UPDATE orchestrator_tasks SET status='succeeded',output_hash=?,updated_at=? "
            "WHERE run_id=? AND task_id='research_plan'",
            (manifest.digest, utcnow(), run_id),
        )
    return job_id, run_id, manifest.digest


def workflow_snapshot(agent: LogicAuditAgent, job_id: str, run_id: str) -> dict[str, object]:
    conn = agent.state._connection()
    return {
        "job": dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()),
        "run": dict(conn.execute(
            "SELECT * FROM orchestrator_runs WHERE run_id=?", (run_id,)
        ).fetchone()),
        "task": dict(conn.execute(
            "SELECT * FROM orchestrator_tasks WHERE run_id=? AND task_id='research_plan'",
            (run_id,),
        ).fetchone()),
    }


def test_logic_audit_is_idempotent_and_cannot_mutate_workflow(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id, run_id, _ = prepared_research_plan(root)
    agent = LogicAuditAgent(root, runner=semantic_result)
    before = workflow_snapshot(agent, job_id, run_id)

    queued = agent.queue_stage(job_id, run_id, "research_plan")
    duplicate = agent.queue_stage(job_id, run_id, "research_plan")
    assert duplicate["audit_run_id"] == queued["audit_run_id"]

    completed = agent.execute(str(queued["audit_run_id"]))
    assert completed["status"] == "completed"
    assert completed["payload"]["authority"] == {
        "pipeline_effect": "none",
        "mutation_authority": "none",
        "automatic_promotion": False,
    }
    assert workflow_snapshot(agent, job_id, run_id) == before

    findings = agent.findings(audit_run_id=str(queued["audit_run_id"]))
    assert [item["finding_type"] for item in findings] == [
        "implicit_question_decomposition"
    ]
    assert len(findings[0]["artifact_refs"]) == 1
    assert len(findings[0]["artifact_refs"][0]) == 64


def test_logic_finding_requires_explicit_promotion_and_still_does_not_repair(
    tmp_path: Path,
) -> None:
    root = project_copy(tmp_path)
    job_id, run_id, _ = prepared_research_plan(root)
    agent = LogicAuditAgent(root, runner=semantic_result)
    run = agent.execute(agent.queue_stage(job_id, run_id, "research_plan")["audit_run_id"])
    finding = agent.findings(audit_run_id=run["audit_run_id"])[0]
    assert agent.state._connection().execute(
        "SELECT COUNT(*) FROM deviation_reports WHERE job_id=?", (job_id,)
    ).fetchone()[0] == 0

    promoted = agent.promote(finding["finding_id"])
    assert promoted["status"] == "promoted"
    assert agent.state._connection().execute(
        "SELECT COUNT(*) FROM deviation_reports WHERE job_id=?", (job_id,)
    ).fetchone()[0] == 1
    assert agent.state._connection().execute(
        "SELECT COUNT(*) FROM system_repair_runs"
    ).fetchone()[0] == 0
    assert workflow_snapshot(agent, job_id, run_id)["task"]["status"] == "succeeded"
