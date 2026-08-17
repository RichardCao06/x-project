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
