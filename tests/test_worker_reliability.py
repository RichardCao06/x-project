from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import time

import pytest

from lca_project.kernel.leases import LeaseLost, LeaseManager
from lca_project.kernel.artifacts import ArtifactStore
from lca_project.kernel.orchestrator import OrchestratorError, PersistentOrchestrator
from lca_project.kernel.skills import SkillInvoker
from lca_project.kernel.state import StateStore
from lca_project.kernel.worker import WorkerLoop
from lca_project.kernel.workers import LeaseHeartbeat, WorkerRegistry, WorkerWatchdog
from lca_project.kernel.verification import OptimizationVerifier
from lca_project.contracts import JobState


ROOT = Path(__file__).resolve().parents[1]


def project_copy(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for name in ("skills", "workflows", "capabilities", "contracts", "policies", "agents", "vendor"):
        shutil.copytree(ROOT / name, root / name)
    return root


def create_run(root: Path) -> tuple[str, str]:
    accepted = SkillInvoker(root).invoke("generate-node-wiki", {
        "industry": "ICT设备制造", "nodes": ["P030"], "publication_mode": "preview",
    })
    run_id = PersistentOrchestrator(root).materialize(accepted["job_id"])
    return str(accepted["job_id"]), run_id


def test_schema_migration_records_worker_registry(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    migration = state._connection().execute(
        "SELECT version,name FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert [(row["version"], row["name"]) for row in migration] == [
        (1, "worker-and-attempt-ownership"),
        (2, "structured-failure-payloads"),
        (3, "effective-bindings-and-reuse-receipts"),
        (4, "global-search-rate-slots"),
        (5, "task-binding-generations"),
        (6, "goal-alignment-control-plane"),
        (7, "autonomous-job-campaigns"),
        (8, "system-repair-agent-runs"),
        (9, "failure-triage-agent-runs"),
        (10, "goal-supervision-wakeups-and-repair-receipts"),
        (11, "system-meta-supervision"),
        (12, "task-repair-epochs"),
        (13, "goal-contract-governance-v2"),
        (14, "governance-reassessment-and-capability-assurance"),
        (15, "system-repair-scm-publications"),
        (16, "goal-execution-ownership"),
        (17, "dashboard-goal-alignment-query-indexes"),
        (18, "dashboard-event-query-indexes"),
        (19, "independent-logic-audits"),
    ]
    assert state._connection().execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='worker_instances'"
    ).fetchone()
    expected_indexes = {
        "deviation_reports_job_created_idx",
        "repair_plans_deviation_created_idx",
        "system_change_candidates_deviation_created_idx",
        "system_change_candidates_created_idx",
        "failure_triage_runs_job_created_idx",
        "system_repair_runs_job_created_idx",
        "events_event_type_idx",
        "logic_audit_runs_job_created_idx",
        "logic_audit_runs_status_idx",
        "logic_audit_findings_run_idx",
        "logic_audit_findings_status_idx",
    }
    actual_indexes = {
        str(row["name"]) for row in state._connection().execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert expected_indexes <= actual_indexes


def test_empty_verification_cannot_claim_formal_pass(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    report, _ = OptimizationVerifier(root).verify()
    assert report["status"] == "insufficient_data"
    assert report["sample_coverage"]["formal_acceptance_eligible"] is False


def test_task_output_is_replayable_after_workspace_is_removed(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id, run_id = create_run(root)
    cycle = WorkerLoop(root, worker_id="manifest-worker").run_once(run_id=run_id)
    assert cycle.status == "succeeded" and cycle.output_hash

    orchestrator = PersistentOrchestrator(root)
    manifest = orchestrator.control.artifacts.verify_task_output_manifest(cycle.output_hash)
    assert manifest["task_id"] == "plan"
    assert manifest["files"]
    assert {item["relation"] for item in manifest["lineage"]} >= {
        "task_input", "capability_manifest", "workflow_binding", "production_policy",
        "repair_policy", "node_profile", "workspace_manifest", "attempt_archive",
    }
    logic_audit = orchestrator.control.state._connection().execute(
        "SELECT stage_id,status FROM logic_audit_runs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    assert dict(logic_audit) == {"stage_id": "plan", "status": "queued"}
    archive = root / "var/workspaces/jobs" / job_id / "runs/attempts/plan" / str(cycle.attempt_id) / "manifest.json"
    assert json.loads(archive.read_text(encoding="utf-8"))["protocol"] == "task-attempt-archive-v1"
    workspace = root / "var/workspaces/jobs" / job_id
    shutil.rmtree(workspace)

    replayed = orchestrator.control.artifacts.verify_task_output_manifest(cycle.output_hash)
    assert replayed == manifest
    for item in replayed["files"]:
        assert orchestrator.control.artifacts.get_bytes(item["sha256"])


def test_materialized_task_output_detects_physical_drift(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    page = workspace / "wiki/activities/A039--test.md"
    page.parent.mkdir(parents=True)
    page.write_text("materialized\n", encoding="utf-8")
    store = ArtifactStore(tmp_path / "artifacts", StateStore(tmp_path / "state.db"))
    manifest = store.put_task_output_manifest(
        workspace,
        [{"path": "wiki/activities/A039--test.md",
          "role": "materialized_output"}],
        {"status": "ok"}, run_id="run_test", task_id="draft_apply",
        attempt_id="attempt_test",
    )

    assert store.verify_materialized_outputs(workspace, manifest.digest)[0]["path"].endswith(
        "A039--test.md"
    )
    page.write_text("drifted\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="materialized output drift"):
        store.verify_materialized_outputs(workspace, manifest.digest)


def test_heartbeat_renews_lease_independently(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    registry = WorkerRegistry(state)
    registry.register("heartbeat-worker")
    leases = LeaseManager(state)
    lease = leases.acquire("task:x", "heartbeat-worker", 2)
    heartbeat = LeaseHeartbeat(
        leases, registry, lease, "heartbeat-worker", 2, 0.05
    ).start()
    time.sleep(0.16)
    renewed = heartbeat.close()
    assert renewed.expires_at > lease.expires_at
    row = state._connection().execute(
        "SELECT progress_seq FROM worker_instances WHERE worker_id='heartbeat-worker'"
    ).fetchone()
    assert row["progress_seq"] >= 2
    assert leases.release(renewed)


def test_stale_fencing_token_cannot_commit_after_takeover(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    _, run_id = create_run(root)
    orchestrator = PersistentOrchestrator(root)
    task = orchestrator.ready(run_id)[0]
    resource = f"workflow-task:{run_id}:{task.task_id}"
    registry = WorkerRegistry(orchestrator.control.state)
    registry.register("old-worker")
    old = orchestrator.control.leases.acquire(resource, "old-worker", 30)
    attempt_id, _ = orchestrator.claim(
        run_id, task.task_id, worker_id="old-worker",
        lease_resource=resource, fencing_token=old.fencing_token,
    )
    with orchestrator.control.state.transaction() as conn:
        conn.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE resource=?",
                     (resource,))
    successor = orchestrator.control.leases.acquire(resource, "new-worker", 30)
    assert successor.fencing_token > old.fencing_token

    with pytest.raises(OrchestratorError, match="stale worker"):
        orchestrator.complete(
            attempt_id, {"status": "ok"}, worker_id="old-worker",
            lease_resource=resource, fencing_token=old.fencing_token,
        )
    attempt = orchestrator.control.state._connection().execute(
        "SELECT status FROM orchestrator_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    assert attempt["status"] == "running"


def test_release_preserves_monotonic_fencing_generation(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    leases = LeaseManager(state)
    first = leases.acquire("system-repair:repair-1", "first-owner", 30)

    assert leases.release(first)
    with pytest.raises(LeaseLost):
        leases.assert_valid(first)

    successor = leases.acquire("system-repair:repair-1", "successor-owner", 30)
    assert successor.fencing_token == first.fencing_token + 1


def test_watchdog_requeues_orphan_once_and_replacement_resumes(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id, run_id = create_run(root)
    orchestrator = PersistentOrchestrator(root)
    task = orchestrator.ready(run_id)[0]
    resource = f"workflow-task:{run_id}:{task.task_id}"
    registry = WorkerRegistry(orchestrator.control.state)
    registry.register("lost-worker")
    registry.heartbeat(
        "lost-worker", status="running", job_id=job_id, run_id=run_id,
        task_id=task.task_id,
    )
    lease = orchestrator.control.leases.acquire(resource, "lost-worker", 30)
    attempt_id, _ = orchestrator.claim(
        run_id, task.task_id, worker_id="lost-worker",
        lease_resource=resource, fencing_token=lease.fencing_token,
    )
    with orchestrator.control.state.transaction() as conn:
        conn.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE resource=?",
                     (resource,))

    watchdog = WorkerWatchdog(orchestrator.control.state, orchestrator.control.events)
    first = watchdog.sweep(stale_after_seconds=30)
    second = watchdog.sweep(stale_after_seconds=30)
    assert first.recovered == 1 and first.attempt_ids == (attempt_id,)
    assert second.recovered == 0
    attempt = orchestrator.control.state._connection().execute(
        "SELECT status,failure_code FROM orchestrator_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    assert (attempt["status"], attempt["failure_code"]) == ("abandoned", "WORKER_LOST")
    assert orchestrator.tasks(run_id)[0].status == "ready"
    assert orchestrator.control.state.get("jobs", job_id)["status"] == "stalled"
    events = orchestrator.control.state._connection().execute(
        "SELECT event_type,COUNT(*) AS n FROM events WHERE event_id LIKE ? GROUP BY event_type",
        (f"watchdog:{attempt_id}:%",),
    ).fetchall()
    assert {row["event_type"]: row["n"] for row in events} == {
        "job.transitioned": 1, "task.requeued": 1, "worker.lost": 1,
    }

    resumed = WorkerLoop(root, worker_id="replacement-worker").run_once(run_id=run_id)
    assert resumed.status == "succeeded" and resumed.task_id == task.task_id
    assert orchestrator.control.state.get("jobs", job_id)["status"] == "running"


def test_identical_retry_failure_stops_at_manual_review_and_keeps_both_attempts(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id, run_id = create_run(root)
    capability = root / "capabilities/wiki.batch@3.json"
    document = json.loads(capability.read_text(encoding="utf-8"))
    document["command"] = ["{python}", "-c", "raise SystemExit(7)", "{input}", "{output}"]
    capability.write_text(json.dumps(document), encoding="utf-8")

    first = WorkerLoop(root, worker_id="same-failure-1").run_once(run_id=run_id)
    second = WorkerLoop(root, worker_id="same-failure-2").run_once(run_id=run_id)

    assert first.status == "retry_scheduled"
    assert second.status == "failed"
    assert PersistentOrchestrator(root).control.state.get("jobs", job_id)["status"] == "manual_review"
    attempts = list((root / "var/workspaces/jobs" / job_id / "runs/attempts/plan").glob("*/manifest.json"))
    assert len(attempts) == 2
    rows = list(PersistentOrchestrator(root).control.state._connection().execute(
        "SELECT failure_payload FROM orchestrator_attempts WHERE run_id=? AND task_id='plan' ORDER BY attempt",
        (run_id,),
    ))
    payloads = [json.loads(row["failure_payload"]) for row in rows]
    assert payloads[0]["failure_fingerprint"] == payloads[1]["failure_fingerprint"]
    assert payloads[1]["identical_failure_repeated"] is True
    assert payloads[1]["policy_decision"]["action"] == "manual_review"


def test_operator_rewind_invalidates_target_and_descendants(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id, run_id = create_run(root)
    worker = WorkerLoop(root, worker_id="rewind-worker")
    assert worker.run_once(run_id=run_id).task_id == "plan"
    assert worker.run_once(run_id=run_id).task_id == "prepare"
    orchestrator = PersistentOrchestrator(root)
    orchestrator.control.transition_job(job_id, JobState.REPAIRABLE, reason="test repair")
    invalidated = orchestrator.rewind_from(run_id, "prepare", reason="test rewind")
    tasks = {row.task_id: row for row in orchestrator.tasks(run_id)}
    assert invalidated[0] == "prepare"
    assert tasks["plan"].status == "succeeded"
    assert tasks["prepare"].status == "ready" and tasks["prepare"].output_hash is None
    assert tasks["research_plan"].status == "pending"
    assert orchestrator.control.state.get("jobs", job_id)["status"] == "ready"
    assert orchestrator.try_reuse(run_id, "prepare") is None


def test_rewind_preserves_latest_materialized_output_lineage(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id, run_id = create_run(root)
    orchestrator = PersistentOrchestrator(root)
    workspace = root / "var/workspaces/jobs" / job_id
    page = workspace / "wiki/ict/activities/A039--lineage.md"
    registry = workspace / "sources/ict/registry.json"
    page.parent.mkdir(parents=True, exist_ok=True)
    registry.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("draft output\n", encoding="utf-8")
    registry.write_text('{"sources":{"draft":{}}}\n', encoding="utf-8")
    outputs = [
        {"path": str(page.relative_to(workspace)), "role": "materialized_output"},
        {"path": str(registry.relative_to(workspace)), "role": "materialized_output"},
    ]
    draft = orchestrator.control.artifacts.put_task_output_manifest(
        workspace, outputs, {"status": "ok"}, run_id=run_id,
        task_id="draft_apply", attempt_id="attempt_draft",
    )
    page.write_text("table descendant output\n", encoding="utf-8")
    registry.write_text('{"sources":{"table":{}}}\n', encoding="utf-8")
    table = orchestrator.control.artifacts.put_task_output_manifest(
        workspace, outputs, {"status": "ok"}, run_id=run_id,
        task_id="table_apply", attempt_id="attempt_table",
    )
    with orchestrator.state.transaction() as conn:
        conn.execute(
            "UPDATE orchestrator_tasks SET status='succeeded',attempt=1,output_hash=? "
            "WHERE run_id=? AND task_id='draft_apply'", (draft.digest, run_id),
        )
        conn.execute(
            "UPDATE orchestrator_tasks SET status='succeeded',attempt=1,output_hash=? "
            "WHERE run_id=? AND task_id='table_apply'", (table.digest, run_id),
        )

    orchestrator.rewind_from(run_id, "draft_apply", reason="lineage regression")

    preserved = orchestrator.materialized_output_lineage(run_id)
    assert preserved["targets"][str(page.relative_to(workspace))]["classification"] == (
        "legitimate_descendant_output"
    )
    assert preserved["targets"][str(registry.relative_to(workspace))]["sha256"] == (
        hashlib.sha256(registry.read_bytes()).hexdigest()
    )
    assert {item["sha256"] for item in preserved["manifests"]} == {
        draft.digest, table.digest,
    }
    event = [item for item in orchestrator.control.events.read(
        "workflow_run", run_id
    ) if item.event_type == "workflow.rewound"][-1]
    assert event.payload["materialization_lineage"] == preserved


def test_source_diversity_envelope_resets_attempt_for_repair_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = project_copy(tmp_path)
    job_id, run_id = create_run(root)
    orchestrator = PersistentOrchestrator(root)
    with orchestrator.control.state.transaction() as conn:
        conn.execute(
            "UPDATE orchestrator_tasks SET status='succeeded',attempt=11 "
            "WHERE run_id=? AND task_id='source_diversity_gate'",
            (run_id,),
        )
        conn.execute(
            "UPDATE orchestrator_tasks SET status='ready' "
            "WHERE run_id=? AND task_id='freeze'",
            (run_id,),
        )

    invalidated = orchestrator.rewind_from(
        run_id, "source_diversity_gate", reason="validated source gate repair",
        actor="system-repair-agent", reset_attempts=True,
    )
    source_gate = next(
        task for task in orchestrator.tasks(run_id)
        if task.task_id == "source_diversity_gate"
    )
    worker = WorkerLoop(root, worker_id="source-gate-epoch-worker")
    monkeypatch.setattr(
        worker.wiki, "envelope",
        lambda _run_id, task, _job: {
            "operation": "source-diversity-gate", "attempt": task.attempt,
        },
    )

    envelope = worker._execution_envelope(
        worker.wiki, source_gate,
        worker.control.state.get("jobs", job_id),
    )
    statuses = {task.task_id: task.status for task in orchestrator.tasks(run_id)}

    assert source_gate.attempt == 11
    assert envelope["attempt"] == 0
    assert orchestrator.repair_epoch_attempt(run_id, source_gate.task_id, 11) == 0
    assert invalidated[0] == "source_diversity_gate"
    assert statuses["source_diversity_gate"] == "ready"
    assert statuses["freeze"] == "pending"
    assert orchestrator.control.state.get("jobs", job_id)["status"] == "ready"


def test_baseline_and_verification_are_immutable_artifacts(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id, run_id = create_run(root)
    assert WorkerLoop(root, worker_id="verification-worker").run_once(
        run_id=run_id
    ).status == "succeeded"
    verifier = OptimizationVerifier(root)
    baseline, baseline_hash = verifier.freeze_baseline(job_id=job_id)
    report, report_hash = verifier.verify(job_id=job_id)
    assert baseline["protocol"] == "wiki-optimization-baseline-v1"
    assert report["protocol"] == "wiki-optimization-verification-v1"
    assert report["status"] == "pass"
    assert report["metrics"]["A1_task_output_cas_freeze_rate"] == 1.0
    assert report["metrics"]["A6_artifact_lineage_complete_rate"] == 1.0
    assert json.loads(verifier.artifacts.get_bytes(baseline_hash))["scope"]["job_id"] == job_id
    assert json.loads(verifier.artifacts.get_bytes(report_hash))["status"] == "pass"


def test_verification_fails_closed_for_missing_cas_content(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    job_id, run_id = create_run(root)
    cycle = WorkerLoop(root, worker_id="corruption-worker").run_once(run_id=run_id)
    verifier = OptimizationVerifier(root)
    manifest = verifier.artifacts.verify_task_output_manifest(str(cycle.output_hash))
    file_record = verifier.state.get("artifacts", manifest["files"][0]["sha256"])
    Path(file_record["uri"]).unlink()
    report, _ = verifier.verify(job_id=job_id)
    assert report["status"] == "fail"
    assert report["metrics"]["A1_task_output_cas_freeze_rate"] == 0.0
    assert report["violations"]["manifest_errors"]


def test_effective_input_hash_reuses_only_compatible_output(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    _, run_id = create_run(root)
    worker = WorkerLoop(root, worker_id="reuse-worker")
    first = worker.run_once(run_id=run_id)
    orchestrator = PersistentOrchestrator(root)
    with orchestrator.control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_tasks SET status='ready' WHERE run_id=? AND task_id='plan'",
                     (run_id,))
        conn.execute("UPDATE orchestrator_runs SET status='ready' WHERE run_id=?", (run_id,))
    reused = orchestrator.try_reuse(run_id, "plan")
    assert reused and reused[1] == first.output_hash
    receipt = json.loads(orchestrator.control.artifacts.get_bytes(reused[2]))
    assert receipt["protocol"] == "task-reuse-receipt-v1"
    assert receipt["source_attempt_id"] == first.attempt_id

    capability = root / "capabilities/wiki.batch@3.json"
    capability.write_text(capability.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with orchestrator.control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_tasks SET status='ready' WHERE run_id=? AND task_id='plan'",
                     (run_id,))
        conn.execute("UPDATE orchestrator_runs SET status='ready' WHERE run_id=?", (run_id,))
    assert orchestrator.try_reuse(run_id, "plan") is None
