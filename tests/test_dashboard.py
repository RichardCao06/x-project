from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from lca_project.control import ControlPlane
from lca_project.dashboard import DashboardService
from lca_project.dashboard.server import DashboardHTTPServer
from lca_project.kernel.goal_alignment.store import AlignmentStore
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


def test_dashboard_projects_execution_search_and_evidence_audit(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    dashboard = DashboardService(root)
    created = dashboard.create_job(
        "generate-node-wiki",
        {"industry": "ict_equipment", "nodes": ["P003"]},
        materialize=True,
    )
    job_id, run_id = created["job_id"], created["run_id"]
    batch = (
        root / "var" / "workspaces" / "jobs" / job_id / "runs"
        / "wiki-batches" / "ict_equipment" / "p003-audit"
    )
    (batch / "table-data").mkdir(parents=True)
    selected_url = "https://example.org/lca-report.pdf"
    rejected_url = "https://example.com/product-page"
    technical_url = "https://example.net/unreadable-report.pdf"
    (batch / "table-data" / "search-matrix.executed.json").write_text(json.dumps({
        "queries": [{
            "query_hash": "query-1", "table": "params", "field": "能耗",
            "language": "zh", "query_strategy": "field_specific",
            "query": "P003 设备 能耗 kWh",
            "provider_attempts": [{
                "provider": "test_search", "status": "ok", "results": 3,
                "cache_hit": False,
            }],
            "results": [
                {"title": "LCA report", "url": selected_url,
                 "current_job_status": "candidate_unverified"},
                {"title": "Product page", "url": rejected_url,
                 "current_job_status": "candidate_unverified"},
                {"title": "Unreadable report", "url": technical_url,
                 "provider": "test_search", "fetch_status": "error",
                 "current_job_status": "candidate_unverified",
                 "error": {"code": "FileNotFoundError",
                           "message": "No such file or directory: pdftotext"}},
            ],
        }],
    }), encoding="utf-8")
    (batch / "table-data" / "evidence-selection.json").write_text(json.dumps({
        "candidate_audits": [
            {"query_hash": "query-1", "url": selected_url,
             "decision": "accepted", "reasons": ["field_specific_observation"]},
            {"query_hash": "query-1", "url": rejected_url,
             "decision": "rejected", "reasons": ["no_field_specific_observation"]},
            {"query_hash": "query-1", "url": technical_url,
             "decision": "rejected",
             "reasons": ["payload_not_fetched", "payload_or_hash_missing"],
             "extraction_support": "routed_html_pdf_pattern", "observations": []},
        ],
        "accepted_evidence": [{"url": selected_url}],
        "fields": [{
            "table": "params", "field": "能耗", "decision": "accepted",
            "candidate_count": 2, "reason": "field_specific_observation",
        }],
        "counts": {"populated": 1},
    }), encoding="utf-8")
    (batch / "source-evidence.json").write_text(json.dumps({
        "claims": [{
            "claim": {"claim_id": "P003-claim-1"},
            "query": {"query_id": "claim-query-1", "text": "P003 test method"},
            "candidates": [{
                "title": "Test method", "url": selected_url,
                "search_provider": "test_search",
            }],
            "disposition": "sent_to_verification",
        }],
    }), encoding="utf-8")
    (batch / "verify-output.json").write_text(json.dumps({
        "claims": [{
            "claim": {"claim_id": "P003-claim-1", "section": "定义",
                      "claim_kind": "external_fact", "claim_text": "设备执行测试。"},
            "fetchResult": {"url": selected_url},
            "verify": {"verdict": "CONFIRMED", "node_alignment": "EXACT",
                       "reasoning": "来源直接支持该声明。", "supporting_quote": "test"},
        }],
    }), encoding="utf-8")
    AlignmentStore(dashboard.state).observation({
        "schema_version": "quality-observation-v1", "job_id": job_id,
        "run_id": run_id, "goal_id": "goal-test", "score": 0.75,
        "dimensions": {"claim_provenance_coverage": 1.0, "data_readiness": 0.5},
        "evidence": {"batch": str(batch), "maturity": {
            "candidate_eligible": False, "maturity": "evidence_limited",
            "data_readiness": "partial_data", "pipeline_continue": True,
            "reason_codes": ["accepted_field_evidence_nonzero"],
        }, "research_outcome": {
            "metrics": {"queries_executed": 2, "populated_fields": 1},
        }},
    })

    trace = dashboard.job(job_id)["execution_trace"]

    assert trace["schema_version"] == "dashboard-execution-trace-v1"
    assert trace["summary"]["queries"] == 2
    assert trace["summary"]["candidate_results"] == 4
    assert trace["summary"]["candidate_accepted"] == 2
    assert trace["summary"]["candidate_rejected"] == 1
    assert trace["summary"]["candidate_technical_failures"] == 1
    assert trace["summary"]["confirmed_citations"] == 1
    assert trace["summary"]["populated_fields"] == 1
    table_query = next(item for item in trace["searches"] if item["kind"] == "table_field")
    assert table_query["query"] == "P003 设备 能耗 kWh"
    assert table_query["providers"][0]["provider"] == "test_search"
    assert table_query["results"][0]["selected"] is True
    assert table_query["results"][0]["outcome"] == "accepted"
    assert table_query["results"][1]["reasons"] == ["no_field_specific_observation"]
    assert table_query["results"][1]["outcome"] == "rejected"
    assert table_query["results"][2]["outcome"] == "technical_failure"
    assert table_query["results"][2]["evaluation_completed"] is False
    assert table_query["results"][2]["technical_error"] == {
        "code": "FileNotFoundError",
        "message": "No such file or directory: pdftotext",
    }
    assert table_query["results"][2]["reasons"] == [
        "payload_not_fetched", "payload_or_hash_missing",
    ]
    assert table_query["results"][2]["extraction_support"] == "routed_html_pdf_pattern"
    claim_query = next(item for item in trace["searches"] if item["kind"] == "claim_evidence")
    assert claim_query["results"][0]["outcome"] == "accepted"
    assert claim_query["results"][0]["reasons"] == ["来源直接支持该声明。"]
    assert trace["citations"][0]["selected"] is True
    assert trace["table_fields"][0]["decision"] == "accepted"
    assert trace["goal_status"] == {
        "goal_id": "lca_modeling_ready", "workflow_complete": False,
        "goal_complete": False, "candidate_eligible": False,
        "modeling_ready": False,
        "maturity": "evidence_limited", "data_readiness": "partial_data",
        "accepted_evidence": 1, "populated_fields": 1,
        "publication_proof_valid": False, "publication_proof_error": None,
        "pipeline_continue": True, "autonomy_active": True,
        "next_action": "继续自治修复",
        "blockers": ["accepted_field_evidence_nonzero"],
    }


def test_dashboard_skill_catalog_and_schema_controlled_creation(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    dashboard = DashboardService(root)
    catalog = dashboard.skill_catalog()
    assert catalog["total"] == 4
    wiki = next(item for item in catalog["items"] if item["name"] == "generate-node-wiki")
    assert wiki["workflow"] == "wiki-node-production@9"
    assert wiki["schema"]["properties"]["publication_mode"]["default"] == "preview"


def test_dashboard_exposes_reviewed_publication_as_declared_goal(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    dashboard = DashboardService(root)
    created = dashboard.create_autonomy({
        "schema_version": "autonomous-job-campaign-v1",
        "name": "reviewed-A001", "skill": "generate-node-wiki",
        "requests": [{
            "industry": "ict_equipment", "nodes": ["A001"],
            "publication_mode": "reviewed",
        }],
        "completion_goal": "reviewed_publication",
        "max_concurrency": 1, "max_auto_repairs_per_job": 3,
        "poll_seconds": 1, "stop_on_failure": False,
    }, start=False)

    goal = dashboard.job(created["items"][0]["job_id"])["execution_trace"]["goal_status"]

    assert goal["goal_id"] == "reviewed_publication"
    assert goal["goal_complete"] is False
    assert goal["publication_proof_valid"] is False
    assert goal["pipeline_continue"] is True
    assert "governed_reviewed_publication_not_proven" in goal["blockers"]

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


def test_dashboard_exposes_only_hash_bound_completed_preview_assets(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    dashboard = DashboardService(root)
    created = dashboard.create_job(
        "generate-node-wiki",
        {"industry": "ict_equipment", "nodes": ["P003"], "publication_mode": "preview"},
        materialize=True,
    )
    job_id, run_id = created["job_id"], created["run_id"]
    workspace = root / "var" / "workspaces" / "jobs" / job_id
    docs = workspace / "docs"
    batch = workspace / "runs" / "wiki-batches" / "ict_equipment" / "p003-test"
    docs.mkdir(parents=True)
    batch.mkdir(parents=True)
    viewer = docs / "ict_equipment-wiki-P003-preview.html"
    viewer.write_text(
        '<!doctype html><script src="ict_equipment-wiki-preview-data.js"></script><h1>P003</h1>',
        encoding="utf-8",
    )
    data_file = docs / "ict_equipment-wiki-preview-data.js"
    data_file.write_text("window.PREVIEW={};", encoding="utf-8")
    (docs / "ict_equipment-wiki-preview.html").write_text("generic", encoding="utf-8")
    graph_file = docs / "ict_equipment-name-graph-preview.html"
    graph_file.write_text("graph", encoding="utf-8")
    report = batch / "preview-report.json"
    report.write_text(json.dumps({
        "mode": "preview_unpublished",
        "maturity": "diagnostic_preview",
        "start_node": "P003",
        "artifacts": {
            "viewer": {"path": str(viewer), "sha256": hashlib.sha256(viewer.read_bytes()).hexdigest()},
            "data": {"path": str(data_file), "sha256": hashlib.sha256(data_file.read_bytes()).hexdigest()},
            "name_graph": {"path": str(graph_file), "sha256": hashlib.sha256(graph_file.read_bytes()).hexdigest()},
        },
    }), encoding="utf-8")
    manifest = dashboard.control.artifacts.put_task_output_manifest(
        workspace,
        [{"path": str(report.relative_to(workspace)), "size": report.stat().st_size}],
        {"status": "ok"},
        run_id=run_id,
        task_id="preview",
        attempt_id="attempt_test_preview",
    )
    with dashboard.state.transaction() as conn:
        conn.execute(
            "UPDATE orchestrator_tasks SET status='succeeded',output_hash=? "
            "WHERE run_id=? AND task_id='preview'",
            (manifest.digest, run_id),
        )

    detail = dashboard.job(job_id)
    assert detail["preview"]["maturity"] == "diagnostic_preview"
    assert detail["preview"]["url"].endswith("/ict_equipment-wiki-P003-preview.html")

    server = DashboardHTTPServer(("127.0.0.1", 0), root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}{detail['preview']['url']}", timeout=3) as response:
            assert "P003" in response.read().decode()
            assert response.headers["Cache-Control"] == "no-store"
        with urlopen(
            f"{base}/preview/{job_id}/ict_equipment-wiki-preview-data.js", timeout=3
        ) as response:
            assert response.read().decode() == "window.PREVIEW={};"
        with pytest.raises(HTTPError) as denied:
            urlopen(f"{base}/preview/{job_id}/not-authorized.txt", timeout=3)
        assert denied.value.code == 404
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
