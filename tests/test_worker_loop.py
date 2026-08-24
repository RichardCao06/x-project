from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from lca_project.kernel.orchestrator import PersistentOrchestrator, TaskRecord
from lca_project.kernel.executor import ExecutionResult
from lca_project.kernel.skills import SkillInvoker
from lca_project.kernel.worker import WorkerLoop
from lca_project.kernel.worker import WikiTaskBinding


ROOT = Path(__file__).resolve().parents[1]


def project_copy(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for name in ("skills", "workflows", "capabilities", "contracts", "policies", "agents"):
        shutil.copytree(ROOT / name, root / name)
    # The worker's industry alias resolver is project-root relative.  The
    # compatibility builder itself still verifies every copy against the
    # package-owned frozen vendor snapshot.
    shutil.copytree(ROOT / "vendor", root / "vendor")
    return root


def create_p030(root: Path) -> tuple[str, str]:
    accepted = SkillInvoker(root).invoke("generate-node-wiki", {
        "industry": "ICT设备制造", "nodes": ["P030"], "publication_mode": "preview",
    })
    run_id = PersistentOrchestrator(root).materialize(accepted["job_id"])
    return accepted["job_id"], run_id


def test_worker_executes_deterministic_plan_and_prepare(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id, run_id = create_p030(root)
    worker = WorkerLoop(root, worker_id="test-worker")

    planned = worker.run_once(run_id=run_id)
    assert planned.status == "succeeded" and planned.task_id == "plan"
    prepared = worker.run_once(run_id=run_id)
    assert prepared.status == "succeeded" and prepared.task_id == "prepare"

    tasks = PersistentOrchestrator(root).tasks(run_id)
    assert [item.status for item in tasks[:3]] == ["succeeded", "succeeded", "ready"]
    workspace = root / "var/workspaces/jobs" / job_id
    manifests = list(workspace.glob("runs/wiki-batches/ict_equipment/*/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["industry"] == "ict_equipment"
    assert manifest["nodes"][0]["node_ref"] == "ict_equipment::P030"
    assert (manifests[0].parent / "prepared.json").is_file()

    job = worker.control.state.get("jobs", job_id)
    assert job is not None and job["status"] == "running"
    events = list(worker.control.state._connection().execute(
        "SELECT event_type FROM events WHERE aggregate_id=? ORDER BY sequence", (run_id,)
    ))
    assert [row["event_type"] for row in events].count("task.succeeded") == 2


def test_p026_plan_uses_adapter_owned_failure_protocol(tmp_path: Path) -> None:
    """Regression for the historical P026 reserved PROCESS_EXIT failure."""
    root = project_copy(tmp_path)
    accepted = SkillInvoker(root).invoke("generate-node-wiki", {
        "industry": "ICT设备制造", "nodes": ["P026"], "publication_mode": "preview",
    })
    run_id = PersistentOrchestrator(root).materialize(accepted["job_id"])

    cycle = WorkerLoop(root, worker_id="p026-plan-regression").run_once(run_id=run_id)

    assert cycle.task_id == "plan"
    assert cycle.status == "succeeded"
    task = PersistentOrchestrator(root).tasks(run_id)[0]
    assert task.status == "succeeded"
    assert task.output_hash


def test_content_overuse_is_normalized_before_second_model_attempt(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    binding = WikiTaskBinding(root)
    run_id = "run_content_normalize"
    job = {
        "id": "job_content_normalize",
        "payload": {"scope": {"request": {
            "industry": "ICT设备制造", "nodes": ["A040"],
            "publication_mode": "preview",
        }}},
    }
    ctx = binding.context(run_id, job)
    batch = ctx["batch"]
    runtime = batch / "content-runtime"
    runtime.mkdir(parents=True)
    (batch / "manifest.json").write_text("{}", encoding="utf-8")
    (batch / "prepared.json").write_text("{}", encoding="utf-8")
    (runtime / "content-result.json").write_text(
        json.dumps({"sections": [{"heading": "定义", "paragraphs": []}]}),
        encoding="utf-8",
    )
    (runtime / "content-usage.json").write_text(json.dumps({
        "validation_error": "冻结研究 claim 最多映射三次: overused=['A040-29']",
    }), encoding="utf-8")
    task = TaskRecord(
        run_id=run_id, task_id="content_compose", capability_id="agent.propose",
        status="ready", attempt=1, output_hash=None,
        inputs={"action": "content_compose"},
    )

    envelope = binding.envelope(run_id, task, job)

    assert envelope["phase"] == "content_normalize"


def test_worker_gates_research_plan_before_nomination(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id, run_id = create_p030(root)
    worker = WorkerLoop(root, worker_id="research-plan-worker")
    assert worker.run_once(run_id=run_id).task_id == "plan"
    assert worker.run_once(run_id=run_id).task_id == "prepare"
    cycle = worker.run_once(run_id=run_id)
    assert cycle.status == "succeeded" and cycle.task_id == "research_plan"
    workspace = root / "var/workspaces/jobs" / job_id
    plan_path = next(workspace.glob("runs/wiki-batches/ict_equipment/*/research-plan.json"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["hint_policy"] == "advisory_nonexclusive"
    assert plan["historical_registry_policy"] == "candidate_only_refetch_and_reverify"
    assert all(row["current_job_status"] == "candidate_unverified"
               for row in plan["advisory_candidates"])
    tasks = {row.task_id: row.status for row in PersistentOrchestrator(root).tasks(run_id)}
    assert tasks["research_plan_gate"] == "ready"
    assert tasks["research_ready"] == "pending"


def test_worker_failure_is_persisted_and_does_not_unlock_downstream(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id, run_id = create_p030(root)
    worker = WorkerLoop(root, worker_id="test-worker")
    assert worker.run_once(run_id=run_id).status == "succeeded"
    assert worker.run_once(run_id=run_id).status == "succeeded"
    assert worker.run_once(run_id=run_id).task_id == "research_plan"
    assert worker.run_once(run_id=run_id).task_id == "research_plan_gate"

    # Replace the expensive real Agent invocation with an attested protocol
    # failure.  This tests the worker's fail-closed transaction boundary.
    capability = root / "capabilities/agent.propose@1.json"
    document = json.loads(capability.read_text(encoding="utf-8"))
    document["command"] = [
        "{python}", "-c",
        "import json,sys; json.dump({'status':'failed','failure':{'code':'AGENT_TEST_FAILURE'}},open(sys.argv[1],'w'))",
        "{output}", "{input}",
    ]
    capability.write_text(json.dumps(document), encoding="utf-8")
    failed = WorkerLoop(root, worker_id="test-worker-2").run_once(run_id=run_id)
    assert failed.status == "failed" and failed.failure_code == "AGENT_TEST_FAILURE"
    tasks = PersistentOrchestrator(root).tasks(run_id)
    assert tasks[4].status == "quarantined"
    assert all(item.status == "pending" for item in tasks[5:])
    job = worker.control.state.get("jobs", job_id)
    assert job is not None and job["status"] == "failed"


def test_repeated_source_diversity_block_stops_when_research_lineage_is_unchanged(
    tmp_path: Path, monkeypatch,
) -> None:
    root = project_copy(tmp_path)
    job_id, run_id = create_p030(root)
    orchestrator = PersistentOrchestrator(root)
    job = orchestrator.control.state.get("jobs", job_id)
    assert job is not None
    batch = WikiTaskBinding(root).context(run_id, job)["batch"]
    nomination = batch / "nomination-runtime"
    nomination.mkdir(parents=True)
    (batch / "manifest.json").write_text("{}", encoding="utf-8")
    (batch / "prepared.json").write_text("{}", encoding="utf-8")
    scout = batch / "research-scout-diversity-repair.json"
    scout.write_text(json.dumps({
        "protocol": "wiki-research-scout-v1", "node_id": "P030", "candidates": [],
        "diversity_repair": {"protocol": "wiki-source-diversity-repair-v1"},
    }), encoding="utf-8")
    scout_sha = hashlib.sha256(scout.read_bytes()).hexdigest()
    (nomination / "nomination-invocation.json").write_text(json.dumps({
        "research_scout": {"path": str(scout), "sha256": scout_sha},
    }), encoding="utf-8")
    for path in (
        nomination / "nomination-result.json", batch / "source-queue.json",
        batch / "source-evidence.json", batch / "research-plan.json",
        batch / "verify-output.json",
    ):
        path.write_text(json.dumps({"path": path.name}), encoding="utf-8")
    frozen = orchestrator.control.artifacts.put_json(
        {"protocol": "test-upstream-v1"}, metadata={"schema": "test"},
    )
    upstream = [
        "plan", "prepare", "research_plan", "research_plan_gate", "research_ready",
        "search_execution_gate", "verify", "terminology_verify",
    ]
    with orchestrator.control.state.transaction() as conn:
        for task_id in upstream:
            conn.execute(
                "UPDATE orchestrator_tasks SET status='succeeded',output_hash=? "
                "WHERE run_id=? AND task_id=?", (frozen.digest, run_id, task_id),
            )
        conn.execute(
            "UPDATE orchestrator_tasks SET status='ready' "
            "WHERE run_id=? AND task_id='source_diversity_gate'", (run_id,),
        )

    class BlockedExecutor:
        def execute(self, *args, **kwargs):
            return ExecutionResult("blocked", {"status": "blocked", "failure": {
                "code": "SOURCE_DIVERSITY_BLOCKED", "category": "business_validation",
                "scope": "task", "message": "reviewed minima not met",
            }}, "", "", tmp_path)

    monkeypatch.setattr(WorkerLoop, "_executor_for", lambda *args, **kwargs: BlockedExecutor())
    first = WorkerLoop(root, worker_id="diversity-first").run_once(run_id=run_id)
    assert first.status == "retry_scheduled"

    with orchestrator.control.state.transaction() as conn:
        for task_id in ("research_ready", "search_execution_gate", "verify", "terminology_verify"):
            conn.execute(
                "UPDATE orchestrator_tasks SET status='succeeded',output_hash=? "
                "WHERE run_id=? AND task_id=?", (frozen.digest, run_id, task_id),
            )
        conn.execute(
            "UPDATE orchestrator_tasks SET status='ready',output_hash=NULL,failure_code=NULL," 
            "failure_payload=NULL WHERE run_id=? AND task_id='source_diversity_gate'", (run_id,),
        )
        conn.execute("UPDATE orchestrator_runs SET status='ready' WHERE run_id=?", (run_id,))

    second = WorkerLoop(root, worker_id="diversity-second").run_once(run_id=run_id)

    assert second.status == "failed"
    tasks = {task.task_id: task for task in orchestrator.tasks(run_id)}
    assert tasks["source_diversity_gate"].status == "manual_review"
    assert tasks["research_ready"].status == "succeeded"
    rows = list(orchestrator.control.state._connection().execute(
        "SELECT failure_payload FROM orchestrator_attempts WHERE run_id=? "
        "AND task_id='source_diversity_gate' ORDER BY attempt", (run_id,),
    ))
    latest = json.loads(rows[-1]["failure_payload"])
    assert latest["effective_research_lineage_changed_before_retry"] is False
    assert latest["research_lineage"]["complete"] is True
    archive = json.loads(Path(latest["attempt_archive"]["path"]).read_text(encoding="utf-8"))
    assert archive["research_lineage"]["tuple_sha256"] == latest["research_lineage"]["tuple_sha256"]
    changed = {**latest["research_lineage"], "tuple_sha256": "f" * 64}
    assert WorkerLoop._research_lineage_changed(latest["research_lineage"], changed) is True


def test_preview_keeps_table_pipeline_executable(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id, run_id = create_p030(root)
    binding = WikiTaskBinding(root)
    assert {"table_collect", "table_verify", "table_population_gate", "table_apply"} <= binding.SUPPORTED
    assert {"release_gate", "reviewed_apply", "publish"} <= binding.SUPPORTED
    worker_source = (root / "src/lca_project/kernel/worker.py") if (root / "src").is_dir() else ROOT / "src/lca_project/kernel/worker.py"
    source = worker_source.read_text(encoding="utf-8")
    assert "preview preserves explicit quantitative evidence gaps" not in source


def test_manual_review_run_still_executes_an_independent_ready_branch(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id, run_id = create_p030(root)
    worker = WorkerLoop(root, worker_id="branch-worker")
    assert worker.run_once(run_id=run_id).task_id == "plan"
    job = worker.control.state.get("jobs", job_id)
    with worker.control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_runs SET status='manual_review' WHERE run_id=?",
                     (run_id,))
    worker.control.state.upsert_entity(
        "jobs", job_id, "manual_review", job["payload"],
        program_id=job.get("program_id"), industry_id=job.get("industry_id"),
        workflow_id=job.get("workflow_id"),
    )

    cycle = worker.run_once(run_id=run_id)

    assert cycle.status == "succeeded"
    assert cycle.task_id == "prepare"
