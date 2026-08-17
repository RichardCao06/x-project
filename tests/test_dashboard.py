from __future__ import annotations

import json
from pathlib import Path
import shutil
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from lca_project.control import ControlPlane
from lca_project.dashboard import DashboardService
from lca_project.dashboard.server import DashboardHTTPServer
from lca_project.kernel.skills import SkillInvoker
from lca_project.kernel.worker import WorkerLoop


ROOT = Path(__file__).resolve().parents[1]


def project_copy(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for name in ("skills", "workflows", "capabilities", "contracts", "policies", "agents"):
        shutil.copytree(ROOT / name, root / name)
    return root


def test_dashboard_empty_read_model_is_stable(tmp_path: Path) -> None:
    dashboard = DashboardService(tmp_path)
    overview = dashboard.overview()
    assert overview["counts"]["jobs"] == 0
    assert overview["counts"]["workflow_runs"] == 0
    assert dashboard.jobs() == {"items": [], "total": 0, "limit": 50, "offset": 0, "states": []}
    assert dashboard.workflow_runs() == {"items": [], "total": 0}
    assert dashboard.artifacts()["items"] == []
    assert dashboard.events()["items"] == []
    assert dashboard.exceptions()["total"] == 0
    assert dashboard.workers() == {"items": [], "total": 0}


def test_dashboard_projects_job_dag_and_artifact_preview(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    accepted = SkillInvoker(root).invoke(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["P003"]}
    )
    dashboard = DashboardService(root)
    before = dashboard.job(accepted["job_id"])
    assert before["run"] is None
    assert before["job"]["payload"]["target"] == "ict_equipment::P003"

    materialized = dashboard.materialize(accepted["job_id"])
    assert materialized["status"] == "ok"
    detail = dashboard.job(accepted["job_id"])
    assert detail["run"]["workflow_ref"] == "wiki-node-production@9"
    assert len(detail["tasks"]) == 26
    assert detail["tasks"][0]["status"] == "ready"
    assert detail["tasks"][0]["inputs"] == {"action": "plan"}

    request_hash = accepted["request_hash"]
    artifact = dashboard.artifact(request_hash)
    assert artifact["preview_type"] == "json"
    assert artifact["preview"]["nodes"] == ["P003"]
    assert dashboard.overview()["counts"]["tasks"] == 26


def test_dashboard_skill_catalog_and_schema_controlled_creation(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    dashboard = DashboardService(root)
    catalog = dashboard.skill_catalog()
    assert catalog["total"] == 4
    wiki = next(item for item in catalog["items"] if item["name"] == "generate-node-wiki")
    assert wiki["workflow"] == "wiki-node-production@9"
    assert wiki["schema"]["properties"]["publication_mode"]["default"] == "preview"

    created = dashboard.create_job(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["P003"]},
        idempotency_key="dashboard:p003", materialize=True,
    )
    assert created["materialized"] is True
    assert created["tasks"] == 26
    assert dashboard.job(created["job_id"])["run"]["run_id"] == created["run_id"]
    duplicate = dashboard.create_job(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["P003"]},
        idempotency_key="dashboard:p003", materialize=True,
    )
    assert duplicate["job_id"] == created["job_id"]
    assert duplicate["deduplicated"] is True
    with pytest.raises(ValueError, match="maxItems"):
        dashboard.create_job(
            "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["P003", "A039"]}
        )
    with pytest.raises(ValueError, match="unknown Skill"):
        dashboard.create_job("not-registered", {"target": "x"})


def test_dashboard_worker_start_is_job_scoped_and_non_blocking(tmp_path: Path, monkeypatch) -> None:
    root = project_copy(tmp_path)
    created = DashboardService(root).create_job(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["P003"]},
        materialize=True,
    )
    started = threading.Event()

    def fake_run(self, **kwargs):
        started.set()

    monkeypatch.setattr("lca_project.dashboard.service.WorkerLoop.run", fake_run)
    dashboard = DashboardService(root)
    result = dashboard.start_worker(created["job_id"])
    assert result["status"] == "started"
    assert started.wait(1)
    with pytest.raises(KeyError):
        dashboard.start_worker("job_missing")


def test_dashboard_pause_and_resume_are_job_scoped(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    dashboard = DashboardService(root)
    created = dashboard.create_job(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["P003"]},
        materialize=True,
    )

    paused = dashboard.pause_job(created["job_id"])

    assert paused["status"] == "paused"
    detail = dashboard.job(created["job_id"])
    assert detail["job"]["status"] == "paused"
    assert detail["job"]["payload"]["paused_from"] == "ready"
    assert detail["run"]["status"] == "paused"
    assert WorkerLoop(root, worker_id="paused-worker").run(job_id=created["job_id"], once=True).status == "paused"
    with pytest.raises(ValueError, match="paused"):
        dashboard.start_worker(created["job_id"])

    resumed = dashboard.resume_job(created["job_id"])

    assert resumed["status"] == "resumed" and resumed["job_status"] == "ready"
    detail = dashboard.job(created["job_id"])
    assert detail["job"]["status"] == "ready"
    assert detail["run"]["status"] == "ready"


def test_dashboard_http_api_and_static_shell(tmp_path: Path) -> None:
    ControlPlane(tmp_path)
    server = DashboardHTTPServer(("127.0.0.1", 0), tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/api/health", timeout=3) as response:
            assert json.load(response) == {"service": "lca-dashboard", "status": "ok"}
            assert response.headers["Cache-Control"] == "no-store"
        with urlopen(f"{base}/", timeout=3) as response:
            html = response.read().decode()
            assert "LCA Control Atlas" in html
            assert response.headers["X-Content-Type-Options"] == "nosniff"
        with urlopen(f"{base}/api/skills", timeout=3) as response:
            assert json.load(response)["items"] == []
        request = Request(f"{base}/api/no-such-route", method="GET")
        try:
            urlopen(request, timeout=3)
        except Exception as exc:
            assert getattr(exc, "code", None) == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_dashboard_http_creates_only_through_registered_skill(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    server = DashboardHTTPServer(("127.0.0.1", 0), root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def post(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        request = Request(f"{base}/api/jobs", method="POST",
                          data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    try:
        status, created = post({
            "skill": "generate-node-wiki",
            "request": {"industry": "ict_equipment", "nodes": ["P003"]},
            "materialize": True,
        })
        assert status == 201
        assert created["workflow"] == "wiki-node-production@9"
        assert created["materialized"] is True
        assert created["tasks"] == 26
        status, rejected = post({
            "skill": "generate-node-wiki", "request": {"industry": "ict_equipment", "nodes": ["BAD"]}
        })
        assert status == 400
        assert "does not match" in str(rejected["message"])
        status, rejected = post({"target": "raw-job", "request": {}})
        assert status == 400
        assert "unknown task creation fields" in str(rejected["message"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
