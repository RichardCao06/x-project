from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from lca_project.kernel.conformance import check_conformance
from lca_project.kernel.orchestrator import PersistentOrchestrator
from lca_project.kernel.skills import SkillError, SkillInvoker, SkillRegistry, _frontmatter
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
    wiki_skill = SkillRegistry(root).get("generate-node-wiki")
    assert wiki_skill.workflow_ref == "wiki-node-production@9"
    assert wiki_skill.input_schema == "wiki-production-request-v2"
    assert wiki_skill.route_path.name == "skill.manifest.json"
    assert "节点 Wiki" in wiki_skill.description


def test_generate_node_wiki_uses_standard_skill_frontmatter() -> None:
    frontmatter = _frontmatter(ROOT / "skills" / "generate-node-wiki" / "SKILL.md")
    assert set(frontmatter) == {"name", "description"}
    assert frontmatter["name"] == "generate-node-wiki"


def test_skill_route_version_change_refreshes_same_stable_job(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    request = {"industry": "ict_equipment", "nodes": ["A001"], "publication_mode": "preview"}
    first = SkillInvoker(root).invoke("generate-node-wiki", request)
    route = root / "skills" / "generate-node-wiki" / "skill.manifest.json"
    document = json.loads(route.read_text(encoding="utf-8"))
    document["version"] = str(int(document["version"]) + 1)
    route.write_text(json.dumps(document), encoding="utf-8")
    replacement = SkillInvoker(root).invoke("generate-node-wiki", request)
    duplicate = SkillInvoker(root).invoke("generate-node-wiki", request)
    assert replacement["job_id"] == first["job_id"]
    assert replacement["deduplicated"] is True
    assert replacement["binding_refreshed"] is True
    assert duplicate["job_id"] == replacement["job_id"]
    assert duplicate["deduplicated"] is True
    assert duplicate["binding_refreshed"] is False


def test_execution_batch_label_does_not_create_duplicate_a001_job(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    base = {"industry": "ict_equipment", "nodes": ["A001"],
            "publication_mode": "preview"}
    first = SkillInvoker(root).invoke(
        "generate-node-wiki", {**base, "batch_id": "a001-first-attempt"},
    )
    renamed = SkillInvoker(root).invoke(
        "generate-node-wiki", {**base, "batch_id": "a001-retry-after-repair"},
    )

    assert renamed["job_id"] == first["job_id"]
    assert renamed["deduplicated"] is True
    assert renamed["binding_refreshed"] is True


def test_route_refresh_rewinds_materialized_job_with_new_binding_generation(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    request = {"industry": "ict_equipment", "nodes": ["A001"], "publication_mode": "preview"}
    first = SkillInvoker(root).invoke("generate-node-wiki", request)
    orchestrator = PersistentOrchestrator(root)
    run_id = orchestrator.materialize(first["job_id"])
    attempt, _ = orchestrator.claim(run_id, "plan")
    orchestrator.fail(attempt, "TEST_FAILURE", {"message": "repair me"}, repairable=True)
    route = root / "skills/generate-node-wiki/skill.manifest.json"
    document = json.loads(route.read_text(encoding="utf-8"))
    document["version"] = str(int(document["version"]) + 1)
    route.write_text(json.dumps(document), encoding="utf-8")

    refreshed = SkillInvoker(root).invoke("generate-node-wiki", request)

    assert refreshed["job_id"] == first["job_id"]
    assert refreshed["binding_refreshed"] is True
    assert PersistentOrchestrator(root).tasks(run_id)[0].status == "ready"
    generation = PersistentOrchestrator(root).state._connection().execute(
        "SELECT MAX(generation) FROM task_binding_generations WHERE run_id=? AND task_id='plan'",
        (run_id,),
    ).fetchone()[0]
    assert generation == 2


def test_generate_node_wiki_rejects_route_identity_drift(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    route = root / "skills" / "generate-node-wiki" / "skill.manifest.json"
    document = json.loads(route.read_text(encoding="utf-8"))
    document["name"] = "wrong-skill"
    route.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SkillError, match="differs from route manifest"):
        SkillRegistry(root)


@pytest.mark.parametrize("skill_request, message", [
    ({"industry": "ict_equipment", "nodes": []}, "minItems"),
    ({"industry": "ict_equipment", "nodes": ["BAD-ID"]}, "does not match"),
    ({"industry": "ict_equipment", "nodes": ["P003", "A039"]}, "maxItems"),
    ({"industry": "ict_equipment", "nodes": ["P003"], "publication_mode": "publish"}, "must be one of"),
])
def test_generate_node_wiki_rejects_invalid_requests(
    tmp_path: Path, skill_request: dict[str, object], message: str,
) -> None:
    root = project_copy(tmp_path)
    with pytest.raises(SkillError, match=message):
        SkillInvoker(root).invoke("generate-node-wiki", skill_request)


def test_skill_invocation_creates_idempotent_job_and_persistent_tasks(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    request = {"industry": "ict_equipment", "nodes": ["A039"], "batch_id": "acceptance-a039",
               "publication_mode": "reviewed"}
    first = SkillInvoker(root).invoke("generate-node-wiki", request)
    second = SkillInvoker(root).invoke("generate-node-wiki", request)
    assert first["job_id"] == second["job_id"] and second["deduplicated"] is True
    assert first["status"] == "accepted"
    assert first["policy"] == "wiki-production-v4"
    assert len(first["route_hash"]) == 64
    stored = SkillInvoker(root).control.state.get("jobs", first["job_id"])
    assert stored is not None
    assert stored["payload"]["target"] == "ict_equipment::A039"
    assert len(stored["payload"]["input_hashes"]) == 2
    run_id = PersistentOrchestrator(root).materialize(first["job_id"])
    resumed = PersistentOrchestrator(root)
    assert resumed.materialize(first["job_id"]) == run_id
    tasks = resumed.tasks(run_id)
    assert len(tasks) == 26 and tasks[0].task_id == "plan" and tasks[-1].task_id == "publish"
    assert tasks[0].inputs == {"action": "plan"}
    assert tasks[2].inputs == {"action": "research_plan"}
    assert tasks[3].inputs == {"action": "research_plan_gate"}
    assert tasks[4].inputs == {"action": "nomination", "runtime_profile": "terra-worker"}
    assert tasks[-1].inputs == {"action": "publish"}
    assert [item.task_id for item in resumed.ready(run_id)] == ["plan"]


def test_new_workflow_major_does_not_reuse_a_legacy_job(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    request = {"industry": "ict_equipment", "nodes": ["A040"],
               "publication_mode": "preview"}
    manifest = root / "skills/generate-node-wiki/skill.manifest.json"
    current = json.loads(manifest.read_text(encoding="utf-8"))
    legacy = dict(current)
    legacy["workflow"] = "workflow://wiki-node-production@8"
    manifest.write_text(json.dumps(legacy), encoding="utf-8")
    old = SkillInvoker(root).invoke("generate-node-wiki", request)

    manifest.write_text(json.dumps(current), encoding="utf-8")
    new = SkillInvoker(root).invoke("generate-node-wiki", request)

    assert new["job_id"] != old["job_id"]
    assert new["deduplicated"] is False
    assert new["workflow"] == "wiki-node-production@9"


def test_generate_node_wiki_defaults_to_preview(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    invoker = SkillInvoker(root)
    result = invoker.invoke("generate-node-wiki", {"industry": "ict_equipment", "nodes": ["P003"]})
    stored = invoker.control.state.get("jobs", result["job_id"])
    assert stored is not None
    assert stored["payload"]["scope"]["request"]["publication_mode"] == "preview"


def test_wiki_workflow_has_real_protocol_adapters(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    report = check_conformance(root, workflow_ref="wiki-node-production@7")
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


def test_graph_workflow_has_real_protocol_adapters(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    report = check_conformance(root, workflow_ref="graph-industry-production@1")
    assert report["status"] == "pass", report
    workflow = report["workflows"][0]
    assert workflow["tasks"] == ["plan", "conventions", "seed_activities", "seed_engineering",
                                 "seed_lca", "seed_products", "build", "closure",
                                 "mapping_activities", "mapping_products", "mapping_technology",
                                 "review_adversarial", "review_classifier", "review_curator",
                                 "review_engineer", "consolidate", "scorecard", "materialize",
                                 "gate", "release"]
    assert all(row["status"] == "ok" for row in workflow["capabilities"])


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


def test_historical_preview_table_skip_can_be_narrowly_reopened(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id = SkillInvoker(root).invoke(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["P003"]}
    )["job_id"]
    runner = PersistentOrchestrator(root)
    run_id = runner.materialize(job_id)
    table = {"table_collect", "table_verify", "table_population_gate", "table_apply"}
    release = {"release_gate", "reviewed_apply", "publish"}
    with runner.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_tasks SET status='succeeded' WHERE run_id=?", (run_id,))
        for task in table | release:
            conn.execute("UPDATE orchestrator_tasks SET status='skipped' WHERE run_id=? AND task_id=?",
                         (run_id, task))
        conn.execute("UPDATE orchestrator_runs SET status='succeeded' WHERE run_id=?", (run_id,))
        conn.execute("UPDATE jobs SET status='candidate' WHERE id=?", (job_id,))
    runner.reopen_skipped_table_branch(run_id)
    statuses = {row.task_id: row.status for row in runner.tasks(run_id)}
    assert statuses["table_collect"] == "ready"
    assert all(statuses[task] == "pending"
               for task in (table - {"table_collect"}) | release | {"preview"})
