from __future__ import annotations

import json
from pathlib import Path
import shutil

from lca_project.kernel.conformance import check_conformance
from lca_project.kernel.orchestrator import PersistentOrchestrator
from lca_project.kernel.skills import SkillInvoker, SkillRegistry
from lca_project.domains.wiki_reconcile import reconcile_wiki_run


ROOT = Path(__file__).resolve().parents[1]


def project_copy(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for name in ("skills", "workflows", "capabilities", "contracts", "policies", "agents"):
        shutil.copytree(ROOT / name, root / name)
    return root


def test_all_skills_are_machine_resolvable(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    skills = SkillRegistry(root).all()
    assert {item.name for item in skills} == {
        "bom-skeleton-probe", "cross-link-binding", "generate-node-wiki", "industry-graph",
    }
    assert SkillRegistry(root).get("generate-node-wiki").workflow_ref == "wiki-node-production@6"


def test_skill_invocation_creates_idempotent_job_and_persistent_tasks(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    request = {"industry": "ict_equipment", "nodes": ["A039"], "batch_id": "acceptance-a039",
               "publication_mode": "reviewed"}
    first = SkillInvoker(root).invoke("generate-node-wiki", request)
    second = SkillInvoker(root).invoke("generate-node-wiki", request)
    assert first["job_id"] == second["job_id"] and second["deduplicated"] is True
    run_id = PersistentOrchestrator(root).materialize(first["job_id"])
    resumed = PersistentOrchestrator(root)
    assert resumed.materialize(first["job_id"]) == run_id
    tasks = resumed.tasks(run_id)
    assert len(tasks) == 18 and tasks[0].task_id == "plan" and tasks[-1].task_id == "publish"
    assert tasks[0].inputs == {"action": "plan"}
    assert tasks[2].inputs == {"action": "nomination", "runtime_profile": "terra-worker"}
    assert tasks[-1].inputs == {"action": "publish"}
    assert [item.task_id for item in resumed.ready(run_id)] == ["plan"]


def test_wiki_workflow_has_real_protocol_adapters(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    report = check_conformance(root, workflow_ref="wiki-node-production@6")
    assert report["status"] == "pass", report
    assert all(row["status"] == "ok" for row in report["workflows"][0]["capabilities"])


def test_wiki_workflow_cannot_fall_back_to_agent_script_interpretation(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    workflow = root / "workflows" / "wiki-node-production@6.json"
    document = json.loads(workflow.read_text(encoding="utf-8"))
    document["steps"][2].pop("inputs")
    workflow.write_text(json.dumps(document), encoding="utf-8")
    report = check_conformance(root, workflow_ref="wiki-node-production@6")
    assert report["status"] == "fail"
    assert any("research_ready: missing or invalid executable action" in error
               for error in report["workflows"][0]["errors"])


def test_unmigrated_workflow_cannot_claim_production_ready(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    report = check_conformance(root, workflow_ref="graph-industry-production@1")
    assert report["status"] == "fail"
    assert any("graph.gate" in error for error in report["workflows"][0]["errors"])


def test_release_adapter_fails_closed_without_persisted_eligibility(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    input_path, output_path = tmp_path / "input.json", tmp_path / "output.json"
    input_path.write_text(json.dumps({"operation": "publish"}), encoding="utf-8")
    from lca_project.capability_runtime import main
    assert main(["release.apply", "--input", str(input_path), "--output", str(output_path)]) == 0
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result == {"status": "blocked", "failure": {"code": "RELEASE_ELIGIBILITY_REQUIRED"}}


def test_wiki_reconciler_advances_only_from_frozen_artifacts(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id = SkillInvoker(root).invoke(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["A039"]}
    )["job_id"]
    run_id = PersistentOrchestrator(root).materialize(job_id)
    batch = root / "var" / "workspaces" / "a039" / "batch"
    batch.mkdir(parents=True)
    (batch / "journal.json").write_text('{"state":"prepared"}', encoding="utf-8")
    (batch / "manifest.json").write_text('{"node":"A039"}', encoding="utf-8")
    report = reconcile_wiki_run(root, run_id, batch)
    assert [item["task_id"] for item in report["admitted"]] == ["plan"]
    (batch / "prepared.json").write_text('{"node":"A039"}', encoding="utf-8")
    (batch / "validation.json").write_text('{"status":"pass"}', encoding="utf-8")
    report = reconcile_wiki_run(root, run_id, batch)
    assert [item["task_id"] for item in report["admitted"]] == ["prepare"]
    assert report["tasks"][2]["status"] == "ready"


def test_repair_retry_is_bounded_and_bound_to_prior_failure(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id = SkillInvoker(root).invoke(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["A039"]}
    )["job_id"]
    runner = PersistentOrchestrator(root)
    run_id = runner.materialize(job_id)
    attempt, _ = runner.claim(run_id, "plan")
    runner.fail(attempt, "QUOTE_WINDOW_MISS", {"message": "quote absent"}, repairable=True)
    failure_hash = runner.tasks(run_id)[0].output_hash
    runner.recover(run_id, "plan")
    second, second_inputs = runner.claim(run_id, "plan")
    assert second_inputs[-1] == failure_hash
    runner.fail(second, "QUOTE_WINDOW_MISS", {"message": "still absent"}, repairable=True)
    try:
        runner.recover(run_id, "plan")
    except Exception as exc:
        assert "repair budget exhausted" in str(exc)
    assert runner.tasks(run_id)[0].status == "quarantined"
