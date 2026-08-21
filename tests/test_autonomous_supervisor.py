from __future__ import annotations

from pathlib import Path
import shutil
from types import SimpleNamespace
import json
import threading
from urllib.request import Request, urlopen

import pytest

from lca_project.control import ControlPlane
from lca_project.dashboard import DashboardService
from lca_project.dashboard.server import DashboardHTTPServer
from lca_project.kernel.goal_alignment.autonomous_supervisor import AutonomousJobSupervisor
from lca_project.kernel.goal_alignment.change_controller import ChangeController
from lca_project.kernel.goal_alignment.system_repair_agent import SystemRepairAgent
from lca_project.kernel.goal_alignment.store import AlignmentStore
from lca_project.kernel.state import utcnow


ROOT = Path(__file__).resolve().parents[1]


def project_copy(tmp_path: Path) -> Path:
    root = tmp_path / "project"; root.mkdir()
    for name in ("skills", "workflows", "capabilities", "contracts", "policies", "agents"):
        shutil.copytree(ROOT / name, root / name)
    return root


def spec(node: str = "P003") -> dict[str, object]:
    return {"schema_version": "autonomous-job-campaign-v1",
            "name": f"wiki-{node}", "skill": "generate-node-wiki",
            "requests": [{"industry": "ict_equipment", "nodes": [node]}],
            "max_concurrency": 1, "max_auto_repairs_per_job": 3,
            "poll_seconds": 0.1, "stop_on_failure": False}


def test_campaign_idempotently_creates_and_materializes_job(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    supervisor = AutonomousJobSupervisor(root, supervisor_id="test")
    campaign = supervisor.create_campaign(spec())
    campaign_id = campaign["campaign"]["campaign_id"]

    first = supervisor.tick(campaign_id, execute_task=False)
    again = supervisor.tick(campaign_id, execute_task=False)
    view = supervisor.campaign(campaign_id)

    assert len(first["created_jobs"]) == 1
    assert again["created_jobs"] == []
    assert view["items"][0]["job_id"] == first["created_jobs"][0]
    assert view["items"][0]["run_id"]
    assert ControlPlane(root).state._connection().execute(
        "SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert ControlPlane(root).state._connection().execute(
        "SELECT COUNT(*) FROM orchestrator_tasks WHERE run_id=?",
        (view["items"][0]["run_id"],)).fetchone()[0] == 26


def test_campaign_respects_max_concurrency_when_creating_jobs(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    value = spec(); value["requests"] = [
        {"industry": "ict_equipment", "nodes": ["P003"]},
        {"industry": "ict_equipment", "nodes": ["A001"]},
    ]
    supervisor = AutonomousJobSupervisor(root)
    campaign_id = supervisor.create_campaign(value)["campaign"]["campaign_id"]
    result = supervisor.tick(campaign_id, execute_task=False)

    assert len(result["created_jobs"]) == 1
    assert [item["status"] for item in supervisor.campaign(campaign_id)["items"]] == [
        "running", "pending"
    ]


def test_campaign_rejects_duplicate_requests_and_unknown_skill(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    supervisor = AutonomousJobSupervisor(root)
    duplicate = spec(); duplicate["requests"] = duplicate["requests"] * 2
    with pytest.raises(ValueError, match="unique"):
        supervisor.create_campaign(duplicate)
    unknown = spec(); unknown["skill"] = "no-such-skill"
    with pytest.raises(ValueError, match="unknown Skill"):
        supervisor.create_campaign(unknown)
    invalid = spec(); invalid["requests"] = [
        {"industry": "ict_equipment", "nodes": ["INVALID"]}
    ]
    with pytest.raises(ValueError, match="does not match"):
        supervisor.create_campaign(invalid)
    assert supervisor.campaigns()["total"] == 0


def test_reviewed_publication_goal_requires_reviewed_requests(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    supervisor = AutonomousJobSupervisor(root)
    invalid = spec("A001")
    invalid["completion_goal"] = "reviewed_publication"
    with pytest.raises(ValueError, match="publication_mode=reviewed"):
        supervisor.create_campaign(invalid)

    reviewed = spec("A001")
    reviewed["requests"][0]["publication_mode"] = "reviewed"
    created = supervisor.create_campaign(reviewed)
    assert created["campaign"]["payload"]["completion_goal"] == "reviewed_publication"

    early_exit = spec("A002")
    early_exit["requests"][0]["publication_mode"] = "reviewed"
    early_exit["completion_goal"] = "lca_modeling_ready"
    with pytest.raises(ValueError, match="terminate before governed publication"):
        supervisor.create_campaign(early_exit)


def test_supervisor_drives_worker_and_marks_campaign_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project_copy(tmp_path)
    supervisor = AutonomousJobSupervisor(root, supervisor_id="test")
    campaign_id = supervisor.create_campaign(spec())["campaign"]["campaign_id"]
    supervisor.tick(campaign_id, execute_task=False)

    class FakeWorker:
        def __init__(self, worker_root: Path, **_: object) -> None:
            self.root = worker_root

        def run(self, *, job_id: str, once: bool) -> SimpleNamespace:
            assert once is True
            control = ControlPlane(self.root); job = control.state.get("jobs", job_id)
            control.state.upsert_entity("jobs", job_id, "candidate", job["payload"],
                                        program_id=job.get("program_id"),
                                        industry_id=job.get("industry_id"),
                                        workflow_id=job.get("workflow_id"))
            with control.state.transaction() as conn:
                conn.execute("UPDATE orchestrator_runs SET status='succeeded',updated_at=? "
                             "WHERE job_id=?", (utcnow(), job_id))
            return SimpleNamespace(status="succeeded", worker_id="fake", run_id=None,
                                   task_id="preview", attempt_id="fake", output_hash="proof",
                                   failure_code=None)

    monkeypatch.setattr(
        "lca_project.kernel.goal_alignment.autonomous_supervisor.WorkerLoop", FakeWorker
    )
    result = supervisor.tick(campaign_id, execute_task=True)

    assert result["status"] == "completed"
    assert supervisor.campaign(campaign_id)["items"][0]["status"] == "succeeded"


def test_evidence_limited_job_requires_attention_instead_of_false_completion(
    tmp_path: Path,
) -> None:
    root = project_copy(tmp_path)
    value = spec("A039"); value["max_auto_repairs_per_job"] = 0
    supervisor = AutonomousJobSupervisor(root, supervisor_id="test")
    campaign_id = supervisor.create_campaign(value)["campaign"]["campaign_id"]
    supervisor.tick(campaign_id, execute_task=False)
    item = supervisor.campaign(campaign_id)["items"][0]
    control = ControlPlane(root); job = control.state.get("jobs", item["job_id"])
    control.state.upsert_entity(
        "jobs", item["job_id"], "evidence_limited", job["payload"],
        program_id=job.get("program_id"), industry_id=job.get("industry_id"),
        workflow_id=job.get("workflow_id"),
    )
    with control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_runs SET status='succeeded',updated_at=? WHERE job_id=?",
                     (utcnow(), item["job_id"]))

    result = supervisor.tick(campaign_id, execute_task=False)

    assert result["status"] == "needs_attention"
    assert supervisor.campaign(campaign_id)["items"][0]["status"] == "evidence_limited"

    campaign_payload = supervisor.campaign(campaign_id)["campaign"]["payload"]
    campaign_payload["completion_goal"] = "workflow_delivery"
    with control.state.transaction() as conn:
        conn.execute("UPDATE autonomous_campaigns SET status='running',payload=? WHERE campaign_id=?",
                     (json.dumps(campaign_payload), campaign_id))
    assert supervisor._finish_campaign_if_terminal(campaign_id) == "completed"


def test_repairable_job_overrides_succeeded_run_projection(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    supervisor = AutonomousJobSupervisor(root)
    campaign_id = supervisor.create_campaign(spec("A039"))["campaign"]["campaign_id"]
    supervisor.tick(campaign_id, execute_task=False)
    item = supervisor.campaign(campaign_id)["items"][0]
    control = ControlPlane(root)
    job = control.state.get("jobs", item["job_id"])
    control.state.upsert_entity(
        "jobs", item["job_id"], "repairable", job["payload"],
        program_id=job.get("program_id"), industry_id=job.get("industry_id"),
        workflow_id=job.get("workflow_id"),
    )
    with control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_runs SET status='succeeded' WHERE run_id=?",
                     (item["run_id"],))

    synced = supervisor._sync_item(supervisor.campaign(campaign_id)["items"][0])

    assert synced["status"] == "running"


def test_reviewed_campaign_completes_only_with_bound_release_proof(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    value = spec("A001")
    value["requests"][0]["publication_mode"] = "reviewed"
    supervisor = AutonomousJobSupervisor(root)
    campaign_id = supervisor.create_campaign(value)["campaign"]["campaign_id"]
    supervisor.tick(campaign_id, execute_task=False)
    item = supervisor.campaign(campaign_id)["items"][0]
    control = ControlPlane(root)
    job = control.state.get("jobs", item["job_id"])
    control.state.upsert_entity(
        "jobs", item["job_id"], "candidate", job["payload"],
        program_id=job.get("program_id"), industry_id=job.get("industry_id"),
        workflow_id=job.get("workflow_id"),
    )
    with control.state.transaction() as conn:
        conn.execute(
            "UPDATE orchestrator_runs SET status='succeeded',updated_at=? WHERE run_id=?",
            (utcnow(), item["run_id"]),
        )
        conn.execute(
            "UPDATE orchestrator_tasks SET status='skipped' WHERE run_id=?",
            (item["run_id"],),
        )

    candidate_only = supervisor._sync_item(supervisor.campaign(campaign_id)["items"][0])
    assert candidate_only["status"] == "blocked"
    assert "status=published" in candidate_only["last_error"]

    control.state.upsert_entity(
        "jobs", item["job_id"], "published", job["payload"],
        program_id=job.get("program_id"), industry_id=job.get("industry_id"),
        workflow_id=job.get("workflow_id"),
    )

    without_proof = supervisor._sync_item(supervisor.campaign(campaign_id)["items"][0])
    assert without_proof["status"] == "blocked"
    assert "immutable output manifest" in without_proof["last_error"]

    proof_dir = root / "proof"
    proof_dir.mkdir()
    record_path = proof_dir / "release-record.json"
    record_path.write_text(json.dumps({
        "protocol": "release-record-v1", "publication_status": "published",
        "release_id": "release-test", "job_id": item["job_id"],
        "candidate_hashes": {"wiki.md": "b" * 64},
        "gate_report_sha256": "c" * 64,
        "reviewed_apply_sha256": "d" * 64,
        "publish_report_sha256": "e" * 64,
    }), encoding="utf-8")
    manifest = control.artifacts.put_task_output_manifest(
        root, [{"path": "proof/release-record.json"}], {"status": "ok"},
        run_id=item["run_id"], task_id="publish", attempt_id="publish-proof",
    )
    with control.state.transaction() as conn:
        conn.execute(
            "UPDATE orchestrator_tasks SET status='succeeded',output_hash=? "
            "WHERE run_id=? AND task_id='publish'",
            (manifest.digest, item["run_id"]),
        )
        conn.execute(
            "UPDATE autonomous_job_items SET status='running',last_error=NULL "
            "WHERE item_id=?", (item["item_id"],),
        )

    with_proof = supervisor._sync_item(supervisor.campaign(campaign_id)["items"][0])
    assert with_proof["status"] == "succeeded"
    assert supervisor._finish_campaign_if_terminal(campaign_id) == "completed"


def test_terminal_campaign_is_reactivated_by_durable_goal_wakeup(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    supervisor = AutonomousJobSupervisor(root, supervisor_id="wakeup-supervisor")
    campaign_id = supervisor.create_campaign(spec("A039"))["campaign"]["campaign_id"]
    supervisor.tick(campaign_id, execute_task=False)
    item = supervisor.campaign(campaign_id)["items"][0]
    with supervisor.state.transaction() as conn:
        conn.execute("UPDATE autonomous_campaigns SET status='completed' WHERE campaign_id=?",
                     (campaign_id,))
        conn.execute("UPDATE autonomous_job_items SET status='evidence_limited' WHERE item_id=?",
                     (item["item_id"],))
    AlignmentStore(supervisor.state).request_supervision(
        job_id=item["job_id"], run_id=item["run_id"], reason="new_goal_deviation",
        deviation_ids=["dev_new"], observation_hash="new-observation",
    )

    result = supervisor.tick(campaign_id, execute_task=False)

    assert result["status"] == "running"
    assert result["action"]["consumed_wakeups"]
    assert supervisor.campaign(campaign_id)["campaign"]["status"] == "running"


def test_reviewed_needs_attention_campaign_is_reactivated_by_new_wakeup(
    tmp_path: Path,
) -> None:
    root = project_copy(tmp_path)
    supervisor = AutonomousJobSupervisor(root, supervisor_id="reviewed-wakeup-supervisor")
    value = spec("A019")
    value["completion_goal"] = "reviewed_publication"
    value["requests"][0]["publication_mode"] = "reviewed"
    campaign_id = supervisor.create_campaign(value)["campaign"]["campaign_id"]
    supervisor.tick(campaign_id, execute_task=False)
    item = supervisor.campaign(campaign_id)["items"][0]
    with supervisor.state.transaction() as conn:
        conn.execute("UPDATE autonomous_campaigns SET status='needs_attention' WHERE campaign_id=?",
                     (campaign_id,))
        conn.execute("UPDATE autonomous_job_items SET status='blocked' WHERE item_id=?",
                     (item["item_id"],))
    AlignmentStore(supervisor.state).request_supervision(
        job_id=item["job_id"], run_id=item["run_id"], reason="new_reviewed_goal_deviation",
        deviation_ids=["dev_reviewed"], observation_hash="reviewed-observation",
    )

    result = supervisor.tick(campaign_id, execute_task=False)

    assert result["status"] == "running"
    assert result["action"]["consumed_wakeups"]
    view = supervisor.campaign(campaign_id)
    assert view["campaign"]["status"] == "running"
    assert view["items"][0]["status"] == "running"


def test_dashboard_reconciler_restarts_campaign_with_pending_wakeup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = project_copy(tmp_path)
    service = DashboardService(root)
    created = service.create_autonomy(spec("A039"), start=False)
    campaign_id = created["campaign"]["campaign_id"]
    item = created["items"][0]
    AlignmentStore(service.state).request_supervision(
        job_id=item["job_id"], run_id=item["run_id"], reason="late_quality_signal",
        deviation_ids=["dev_late"], observation_hash="late-observation",
    )
    started: list[str] = []
    monkeypatch.setattr(
        service, "start_autonomy",
        lambda value: started.append(value) or {"status": "started", "campaign_id": value},
    )

    result = service.reconcile_goal_wakeups_once()

    assert result["status"] == "started"
    assert started == [campaign_id]


def test_dashboard_reconciler_restarts_needs_attention_campaign_with_pending_wakeup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = project_copy(tmp_path)
    service = DashboardService(root)
    created = service.create_autonomy(spec("A019"), start=False)
    campaign_id = created["campaign"]["campaign_id"]
    item = created["items"][0]
    with service.state.transaction() as conn:
        conn.execute("UPDATE autonomous_campaigns SET status='needs_attention' WHERE campaign_id=?",
                     (campaign_id,))
        conn.execute("UPDATE autonomous_job_items SET status='blocked' WHERE item_id=?",
                     (item["item_id"],))
    AlignmentStore(service.state).request_supervision(
        job_id=item["job_id"], run_id=item["run_id"], reason="late_repair_signal",
        deviation_ids=["dev_late_repair"], observation_hash="late-repair-observation",
    )
    started: list[str] = []
    monkeypatch.setattr(
        service, "start_autonomy",
        lambda value: started.append(value) or {"status": "started", "campaign_id": value},
    )

    result = service.reconcile_goal_wakeups_once()

    assert result["status"] == "started"
    assert started == [campaign_id]


def test_supervisor_consumes_durable_queued_repair_before_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = project_copy(tmp_path)
    supervisor = AutonomousJobSupervisor(root, supervisor_id="repair-consumer")
    campaign_id = supervisor.create_campaign(spec("A039"))["campaign"]["campaign_id"]
    supervisor.tick(campaign_id, execute_task=False)
    view = supervisor.campaign(campaign_id)
    item = view["items"][0]
    candidate = ChangeController(root).propose(
        source_deviation_id="dev_orphan", target="propose_code_change", risk="medium",
        change={"diagnosis": "ORPHAN_REPAIR_TEST"},
        rollback={"strategy": "restore_source_snapshot"},
    )
    queued = SystemRepairAgent(root).queue(
        candidate_id=candidate["candidate_id"], source_job_id=item["job_id"],
        source_run_id=item["run_id"], request={"recovery_task": "content_compose"},
    )
    calls: list[str] = []

    class FakeRepairAgent:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def execute(self, repair_run_id: str) -> dict:
            calls.append(repair_run_id)
            return {"repair_run_id": repair_run_id, "status": "failed"}

    class FailingController:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def audit_job(self, *_: object, **__: object) -> dict:
            raise KeyError("poison deviation")

    monkeypatch.setattr(
        "lca_project.kernel.goal_alignment.autonomous_supervisor.SystemRepairAgent",
        FakeRepairAgent,
    )
    monkeypatch.setattr(
        "lca_project.kernel.goal_alignment.autonomous_supervisor.GoalAlignmentController",
        FailingController,
    )

    with pytest.raises(KeyError, match="poison deviation"):
        supervisor._supervise_item(
            view["campaign"], item, execute_task=False,
        )

    assert calls == [queued["repair_run_id"]]


def test_supervisor_retries_deferred_scm_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = project_copy(tmp_path)
    supervisor = AutonomousJobSupervisor(root, supervisor_id="scm-retry")
    campaign_id = supervisor.create_campaign(spec("A039"))["campaign"]["campaign_id"]
    supervisor.tick(campaign_id, execute_task=False)
    view = supervisor.campaign(campaign_id)
    item = view["items"][0]
    candidate = ChangeController(root).propose(
        source_deviation_id="dev_scm_retry", target="propose_code_change", risk="low",
        change={"diagnosis": "SCM_RETRY"}, rollback={"strategy": "restore"},
    )
    repair = SystemRepairAgent(root).queue(
        candidate_id=candidate["candidate_id"], source_job_id=item["job_id"],
        source_run_id=item["run_id"], request={"recovery_task": "content_compose"},
    )
    with supervisor.state.transaction() as conn:
        conn.execute(
            "UPDATE system_repair_runs SET status='awaiting_scm_publication',updated_at=? "
            "WHERE repair_run_id=?",
            ("2000-01-01T00:00:00+00:00", repair["repair_run_id"]),
        )
    calls: list[str] = []

    class FakeRepairAgent:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def publish_scm(self, repair_run_id: str) -> dict:
            calls.append(repair_run_id)
            return {"repair_run_id": repair_run_id, "status": "awaiting_scm_publication"}

    class EmptyController:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def audit_job(self, *_: object, **__: object) -> dict:
            return {"actions": []}

    monkeypatch.setattr(
        "lca_project.kernel.goal_alignment.autonomous_supervisor.SystemRepairAgent",
        FakeRepairAgent,
    )
    monkeypatch.setattr(
        "lca_project.kernel.goal_alignment.autonomous_supervisor.GoalAlignmentController",
        EmptyController,
    )

    supervisor._supervise_item(view["campaign"], item, execute_task=False)

    assert calls == [repair["repair_run_id"]]


def test_supervisor_cycle_failures_are_truthful_and_open_circuit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = project_copy(tmp_path)
    supervisor = AutonomousJobSupervisor(root, supervisor_id="circuit-test")
    campaign_id = supervisor.create_campaign(spec("A039"))["campaign"]["campaign_id"]
    supervisor.tick(campaign_id, execute_task=False)
    item = supervisor.campaign(campaign_id)["items"][0]
    prior_audit = item["last_audit_at"]
    AlignmentStore(supervisor.state).request_supervision(
        job_id=item["job_id"], run_id=item["run_id"], reason="poison_deviation",
        deviation_ids=["dev_poison"], observation_hash="poison",
    )

    class FailingController:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def audit_job(self, *_: object, **__: object) -> dict:
            raise KeyError("missing canonical triage")

    monkeypatch.setattr(
        "lca_project.kernel.goal_alignment.autonomous_supervisor.GoalAlignmentController",
        FailingController,
    )
    monkeypatch.setattr(
        "lca_project.kernel.goal_alignment.autonomous_supervisor.time.sleep",
        lambda _seconds: None,
    )

    result = supervisor.run(campaign_id, poll_seconds=0.01)
    final = supervisor.campaign(campaign_id)

    assert result["status"] == "needs_attention"
    assert result["action"] == "supervision_circuit_opened"
    assert final["campaign"]["status"] == "needs_attention"
    assert final["items"][0]["status"] == "blocked"
    assert final["items"][0]["last_audit_at"] == prior_audit
    assert "KeyError" in final["items"][0]["last_error"]
    assert final["supervisor"]["status"] == "needs_attention"
    assert final["supervisor"]["last_error"]
    assert supervisor.state._connection().execute(
        "SELECT COUNT(*) FROM events WHERE aggregate_id=? "
        "AND event_type='autonomy.supervision_cycle_failed'", (campaign_id,),
    ).fetchone()[0] == supervisor.MAX_CONSECUTIVE_CYCLE_FAILURES

    # A circuit-opened campaign requires an explicit operator resume; the
    # two-second wakeup reconciler must not recreate the crash loop.
    service = DashboardService(root)
    assert service.reconcile_goal_wakeups_once() == {"status": "idle", "campaigns": []}


def test_campaign_pause_and_resume_propagate_to_job(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    supervisor = AutonomousJobSupervisor(root)
    campaign_id = supervisor.create_campaign(spec())["campaign"]["campaign_id"]
    supervisor.tick(campaign_id, execute_task=False)
    job_id = supervisor.campaign(campaign_id)["items"][0]["job_id"]

    paused = supervisor.pause(campaign_id)
    assert paused["campaign"]["status"] == "paused"
    assert ControlPlane(root).state.get("jobs", job_id)["status"] == "paused"
    resumed = supervisor.resume(campaign_id)
    assert resumed["campaign"]["status"] == "running"
    assert ControlPlane(root).state.get("jobs", job_id)["status"] == "ready"


def test_zero_repair_budget_stops_false_block_for_attention(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    value = spec("A037"); value["max_auto_repairs_per_job"] = 0
    supervisor = AutonomousJobSupervisor(root)
    campaign_id = supervisor.create_campaign(value)["campaign"]["campaign_id"]
    supervisor.tick(campaign_id, execute_task=False)
    item = supervisor.campaign(campaign_id)["items"][0]
    control = ControlPlane(root); job = control.state.get("jobs", item["job_id"])
    with control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_tasks SET status='pending' WHERE run_id=?",
                     (item["run_id"],))
        conn.execute("UPDATE orchestrator_tasks SET status='manual_review',attempt=1,"
                     "failure_code='RESEARCH_PLAN_INVALID',failure_payload='{}' "
                     "WHERE run_id=? AND task_id='research_plan_gate'", (item["run_id"],))
        conn.execute("UPDATE orchestrator_runs SET status='manual_review' WHERE run_id=?",
                     (item["run_id"],))
    control.state.upsert_entity("jobs", item["job_id"], "manual_review", job["payload"],
                                program_id=job.get("program_id"),
                                industry_id=job.get("industry_id"),
                                workflow_id=job.get("workflow_id"))

    result = supervisor.tick(campaign_id, execute_task=False)
    view = supervisor.campaign(campaign_id)
    assert result["status"] == "needs_attention"
    assert view["items"][0]["status"] == "blocked"
    assert view["items"][0]["repair_count"] == 0


def test_manual_review_with_ready_independent_branch_is_not_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = project_copy(tmp_path)
    value = spec("A039"); value["max_auto_repairs_per_job"] = 0
    supervisor = AutonomousJobSupervisor(root, supervisor_id="meta-branch-test")
    campaign_id = supervisor.create_campaign(value)["campaign"]["campaign_id"]
    supervisor.tick(campaign_id, execute_task=False)
    item = supervisor.campaign(campaign_id)["items"][0]
    control = ControlPlane(root); job = control.state.get("jobs", item["job_id"])
    with control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_tasks SET status='pending' WHERE run_id=?",
                     (item["run_id"],))
        conn.execute("UPDATE orchestrator_tasks SET status='manual_review' "
                     "WHERE run_id=? AND task_id='editorial_review'", (item["run_id"],))
        conn.execute("UPDATE orchestrator_tasks SET status='ready' "
                     "WHERE run_id=? AND task_id='table_collect'", (item["run_id"],))
        conn.execute("UPDATE orchestrator_runs SET status='manual_review' WHERE run_id=?",
                     (item["run_id"],))
    control.state.upsert_entity(
        "jobs", item["job_id"], "manual_review", job["payload"],
        program_id=job.get("program_id"), industry_id=job.get("industry_id"),
        workflow_id=job.get("workflow_id"),
    )

    class FakeWorker:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def run(self, *, job_id: str, once: bool) -> SimpleNamespace:
            assert job_id == item["job_id"] and once is True
            return SimpleNamespace(status="succeeded", worker_id="fake", run_id=item["run_id"],
                                   task_id="table_collect", attempt_id="a", output_hash="h",
                                   failure_code=None)

    monkeypatch.setattr(
        "lca_project.kernel.goal_alignment.autonomous_supervisor.WorkerLoop", FakeWorker
    )

    result = supervisor.tick(campaign_id, execute_task=True)

    assert result["status"] == "running"
    assert result["action"]["worker_cycle"]["task_id"] == "table_collect"


def test_needs_attention_campaign_resumes_only_after_job_is_causally_repaired(
    tmp_path: Path,
) -> None:
    root = project_copy(tmp_path)
    supervisor = AutonomousJobSupervisor(root)
    campaign_id = supervisor.create_campaign(spec("A039"))["campaign"]["campaign_id"]
    supervisor.tick(campaign_id, execute_task=False)
    item = supervisor.campaign(campaign_id)["items"][0]
    control = ControlPlane(root)
    job = control.state.get("jobs", item["job_id"])
    with control.state.transaction() as conn:
        conn.execute("UPDATE autonomous_campaigns SET status='needs_attention' WHERE campaign_id=?",
                     (campaign_id,))
        conn.execute("UPDATE autonomous_job_items SET status='blocked',repair_count=3 "
                     "WHERE item_id=?", (item["item_id"],))
    control.state.upsert_entity(
        "jobs", item["job_id"], "manual_review", job["payload"],
        program_id=job.get("program_id"), industry_id=job.get("industry_id"),
        workflow_id=job.get("workflow_id"),
    )
    with pytest.raises(ValueError, match="no causally repaired runnable Job"):
        supervisor.resume(campaign_id)

    control.state.upsert_entity(
        "jobs", item["job_id"], "ready", job["payload"],
        program_id=job.get("program_id"), industry_id=job.get("industry_id"),
        workflow_id=job.get("workflow_id"),
    )
    resumed = supervisor.resume(campaign_id)

    assert resumed["campaign"]["status"] == "running"
    assert resumed["items"][0]["status"] == "running"
    assert resumed["items"][0]["repair_count"] == 0
    assert resumed["items"][0]["last_error"] is None


def test_dashboard_can_create_autonomous_job_without_starting_worker(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    result = DashboardService(root).create_autonomy(spec("A001"), start=False)

    assert result["campaign"]["status"] == "running"
    assert result["items"][0]["job_id"]
    assert result["items"][0]["run_id"]
    assert result["background"]["status"] == "not_started"


def test_dashboard_http_exposes_autonomous_creation_and_status(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    server = DashboardHTTPServer(("127.0.0.1", 0), root)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = Request(f"{base}/api/autonomy", method="POST",
                          data=json.dumps({"spec": spec("A015"), "start": False}).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            created = json.load(response)
            assert response.status == 201
        campaign_id = created["campaign"]["campaign_id"]
        with urlopen(f"{base}/api/autonomy/{campaign_id}", timeout=5) as response:
            status = json.load(response)
        assert status["items"][0]["job_id"]
        assert status["campaign"]["status"] == "running"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
