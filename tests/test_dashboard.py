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
from lca_project.kernel.goal_alignment.change_controller import ChangeController
from lca_project.kernel.goal_alignment.store import AlignmentStore
from lca_project.kernel.skills import SkillInvoker
from lca_project.kernel.worker import WorkerLoop


ROOT = Path(__file__).resolve().parents[1]


def project_copy(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for name in ("skills", "workflows", "capabilities", "contracts", "policies", "agents", "vendor"):
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


def test_dashboard_separates_nonblocking_logic_review_from_gate_and_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = project_copy(tmp_path)
    created = DashboardService(root).create_job(
        "generate-node-wiki",
        {"industry": "ICT设备制造", "nodes": ["P030"], "publication_mode": "preview"},
        materialize=True,
    )
    job_id, run_id = created["job_id"], created["run_id"]
    cycle = WorkerLoop(root, worker_id="logic-dashboard-worker").run_once(run_id=run_id)
    assert cycle.status == "succeeded" and cycle.task_id == "plan"

    dashboard = DashboardService(root)
    projection = dashboard.job(job_id)["logic_audit"]
    assert projection["authority"] == {
        "pipeline_effect": "none",
        "mutation_authority": "none",
        "automatic_promotion": False,
        "promotion_requires_explicit_operator_action": True,
    }
    assert projection["summary"]["queued"] == 1
    assert projection["findings"] == []

    from lca_project.kernel.goal_alignment import work_dispatcher
    monkeypatch.setattr(work_dispatcher, "dispatch_logic_audit", lambda *_args: True)
    recovered = dashboard.reconcile_nonterminal_work_once()
    assert recovered["logic_audits"] == [projection["runs"][0]["audit_run_id"]]
    started = dashboard.start_logic_audit(job_id)
    assert started["status"] == "dispatched"
    assert started["dispatched"] == [projection["runs"][0]["audit_run_id"]]


def test_dashboard_job_detail_scopes_repair_lineage_to_requested_job(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    dashboard = DashboardService(root)
    first = dashboard.create_job(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["P003"]},
    )
    second = dashboard.create_job(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["A019"]},
    )
    store = AlignmentStore(dashboard.state)
    first_deviation = store.deviation(
        job_id=first["job_id"], run_id=None, goal_id="wiki_goal",
        value={"deviation_type": "first", "severity": "high",
               "evidence": {"job": "first"}, "summary": "first deviation"},
    )
    second_deviation = store.deviation(
        job_id=second["job_id"], run_id=None, goal_id="wiki_goal",
        value={"deviation_type": "second", "severity": "high",
               "evidence": {"job": "second"}, "summary": "second deviation"},
    )
    first_plan = store.repair_plan(first_deviation["deviation_id"], {
        "repair_level": "L2", "action": "fix_first", "status": "proposed",
    })
    second_plan = store.repair_plan(second_deviation["deviation_id"], {
        "repair_level": "L2", "action": "fix_second", "status": "proposed",
    })
    changes = ChangeController(root, dashboard.control)
    first_candidate = changes.propose(
        source_deviation_id=first_deviation["deviation_id"], target="first-target",
        risk="low", change={"job": "first"}, rollback={"strategy": "discard"},
    )
    second_candidate = changes.propose(
        source_deviation_id=second_deviation["deviation_id"], target="second-target",
        risk="low", change={"job": "second"}, rollback={"strategy": "discard"},
    )
    first_certificate = changes.certify(
        first_candidate["candidate_id"], phase="sandbox", suites={"golden": True},
    )
    changes.certify(
        second_candidate["candidate_id"], phase="sandbox", suites={"golden": True},
    )

    alignment = dashboard.job(first["job_id"])["goal_alignment"]

    assert [item["repair_plan_id"] for item in alignment["repair_plans"]] == [
        first_plan["repair_plan_id"],
    ]
    assert second_plan["repair_plan_id"] not in {
        item["repair_plan_id"] for item in alignment["repair_plans"]
    }
    assert [item["candidate_id"] for item in alignment["change_candidates"]] == [
        first_candidate["candidate_id"],
    ]
    assert [item["certificate_id"] for item in alignment["validation_certificates"]] == [
        first_certificate["certificate_id"],
    ]
    assert all(
        item["candidate_id"] == first_candidate["candidate_id"]
        for item in alignment["promotion_receipts"]
    )


def test_dashboard_job_detail_bounds_each_event_stream_before_merge(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    dashboard = DashboardService(root)
    created = dashboard.create_job(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["P003"]},
        materialize=True,
    )
    for ordinal in range(130):
        dashboard.control.events.append(
            "job", created["job_id"], "test.job_event", {"ordinal": ordinal},
            actor="test",
        )
        dashboard.control.events.append(
            "workflow_run", created["run_id"], "test.run_event", {"ordinal": ordinal},
            actor="test",
        )

    events = dashboard.job(created["job_id"])["events"]

    assert len(events) == 100
    sequences = [int(item["sequence"]) for item in events]
    assert sequences == sorted(sequences, reverse=True)
    assert {item["aggregate_type"] for item in events} == {"job", "workflow_run"}


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

    assert trace["schema_version"] == "dashboard-execution-trace-v2"
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


def test_dashboard_projects_unfetched_frozen_results_and_rejection_reason(
    tmp_path: Path,
) -> None:
    root = project_copy(tmp_path)
    dashboard = DashboardService(root)
    created = dashboard.create_job(
        "generate-node-wiki", {"industry": "ict_equipment", "nodes": ["P003"]},
        materialize=True,
    )
    job_id, run_id = created["job_id"], created["run_id"]
    batch = (root / "var/workspaces/jobs" / job_id / "runs/wiki-batches/ict_equipment"
             / "p003-frozen")
    batch.mkdir(parents=True)
    excluded_url = "https://example.org/reused-source.pdf"
    (batch / "source-evidence.json").write_text(json.dumps({"claims": [{
        "claim": {"claim_id": "P003-claim-2"},
        "query": {"query_id": "search-2", "search_hash": "search-2",
                  "text": "server manufacturing BIOS"},
        "candidates": [], "disposition": "sent_to_verification",
    }]}), encoding="utf-8")
    (batch / "frozen-provider-search-results.json").write_text(json.dumps({
        "queries": [{"search_hash": "search-2", "status": "found",
                     "query": "server manufacturing BIOS", "results": [{
                         "title": "Manufacturing guide", "url": excluded_url,
                         "provider": "research_scout", "snippet": "BIOS setup",
                     }]}],
        "provider_attempts": [{"search_hash": "search-2", "provider": "research_scout",
                               "status": "candidate", "results": 1}],
    }), encoding="utf-8")
    (batch / "research-scout-diversity-repair.json").write_text(json.dumps({
        "diversity_repair": {"excluded_urls": [excluded_url]},
    }), encoding="utf-8")
    AlignmentStore(dashboard.state).observation({
        "schema_version": "quality-observation-v1", "job_id": job_id,
        "run_id": run_id, "goal_id": "goal-test", "score": 0.1,
        "dimensions": {}, "evidence": {"batch": str(batch)},
    })

    trace = dashboard.job(job_id)["execution_trace"]
    query = next(item for item in trace["searches"] if item["query_id"] == "search-2")

    assert query["providers"] == [{
        "provider": "research_scout", "status": "candidate", "results": 1,
        "cache_hit": False, "error": None,
    }]
    assert query["results"][0]["url"] == excluded_url
    assert query["results"][0]["outcome"] == "rejected"
    assert query["results"][0]["decision_stage"] == "diversity_repair"
    assert query["results"][0]["reasons"] == ["excluded_by_diversity_repair"]


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


def test_dashboard_recovers_running_campaign_without_pending_wakeup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = project_copy(tmp_path)
    dashboard = DashboardService(root)
    created = dashboard.create_autonomy({
        "schema_version": "autonomous-job-campaign-v1",
        "name": "restart-recovery", "skill": "generate-node-wiki",
        "requests": [{"industry": "ict_equipment", "nodes": ["A001"]}],
        "completion_goal": "workflow_delivery", "max_concurrency": 1,
        "max_auto_repairs_per_job": 1, "poll_seconds": 1,
        "stop_on_failure": False,
    }, start=False)
    campaign_id = created["campaign"]["campaign_id"]
    assert dashboard.conn.execute(
        "SELECT COUNT(*) FROM goal_supervisor_wakeups WHERE status='pending'"
    ).fetchone()[0] == 0
    started: list[str] = []

    def fake_start(value: str) -> dict[str, str]:
        started.append(value)
        return {"status": "started", "campaign_id": value}

    monkeypatch.setattr(dashboard, "start_autonomy", fake_start)

    result = dashboard.reconcile_nonterminal_work_once()

    assert result["status"] == "recovered"
    assert result["campaigns"] == [campaign_id]
    assert started == [campaign_id]


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


def test_dashboard_projects_stage_gate_reasoning_from_verified_output(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    dashboard = DashboardService(root)
    created = dashboard.create_job(
        "generate-node-wiki",
        {"industry": "ict_equipment", "nodes": ["A019"]},
        materialize=True,
    )
    job_id, run_id = created["job_id"], created["run_id"]
    workspace = root / "var" / "workspaces" / "jobs" / job_id
    workspace.mkdir(parents=True, exist_ok=True)
    gate_file = workspace / "research-plan-gate.json"
    gate_file.write_text(json.dumps({
        "decision": "PASS",
        "checks": {
            "research_questions_complete": True,
            "english_field_translation_coverage_complete": False,
        },
        "advisory_checks": ["english_field_translation_coverage_complete"],
        "warnings": ["英文辅助检索词尚未覆盖全部字段"],
        "pipeline_continue": True,
        "maturity_ceiling": "evidence_limited",
    }), encoding="utf-8")
    manifest = dashboard.control.artifacts.put_task_output_manifest(
        workspace,
        [{"path": gate_file.name, "size": gate_file.stat().st_size}],
        {"status": "ok", "decision": "PASS"},
        run_id=run_id,
        task_id="research_plan_gate",
        attempt_id="attempt_stage_audit",
    )
    with dashboard.state.transaction() as conn:
        conn.execute(
            "UPDATE orchestrator_tasks SET status='succeeded',attempt=1,output_hash=? "
            "WHERE run_id=? AND task_id='research_plan_gate'",
            (manifest.digest, run_id),
        )

    trace = dashboard.job(job_id)["execution_trace"]
    stage = next(item for item in trace["stages"] if item["task_id"] == "research_plan_gate")

    assert stage["name_zh"] == "研究计划门禁"
    assert stage["agent"]["logical_actor_zh"] == "确定性流程执行器"
    assert stage["output"]["integrity"] == "verified"
    assert stage["output"]["documents"][0]["path"] == gate_file.name
    assert stage["gate"]["decision"] == "PASS"
    assert stage["gate"]["passed"] is True
    assert stage["gate"]["blocking_failures"] == []
    assert stage["gate"]["advisory_failures"] == [
        "english_field_translation_coverage_complete",
    ]
    assert stage["gate"]["reason_zh"] == (
        "所有阻断性检查均已满足，因此 Gate 放行。 "
        "未满足的建议项只限制成熟度，不阻止流程继续。"
    )
    assert stage["transition"]["allowed"] is True

    batch = workspace / "runs" / "wiki-batches" / "ict_equipment" / "a019-audit"
    batch.mkdir(parents=True)
    diversity_file = batch / "source-diversity-gate.json"
    diversity_file.write_text(json.dumps({
        "decision": "BLOCKED",
        "checks": {
            "reviewed_confirmed_urls": False,
            "reviewed_distinct_domains": False,
        },
        "metrics": {"confirmed_urls": 0, "confirmed_domains": 0},
        "pipeline_continue": False,
        "maturity_ceiling": "diagnostic_preview",
    }), encoding="utf-8")
    attempt_id = "attempt_failed_gate_audit"
    archive = workspace / "runs" / "attempts" / "source_diversity_gate" / attempt_id
    archive.mkdir(parents=True)
    archive_manifest = {
        "protocol": "task-attempt-archive-v1",
        "run_id": run_id,
        "task_id": "source_diversity_gate",
        "attempt_id": attempt_id,
        "execution_root": str(batch),
        "files": [{
            "path": diversity_file.name,
            "change": "modified",
            "sha256": hashlib.sha256(diversity_file.read_bytes()).hexdigest(),
            "size": diversity_file.stat().st_size,
        }],
    }
    (archive / "manifest.json").write_text(
        json.dumps(archive_manifest), encoding="utf-8",
    )
    failure = dashboard.control.artifacts.put_json({
        "protocol": "failure-envelope-v1",
        "message": "gate returned blocked (2)",
    })
    failure_payload = {
        "message": "gate returned blocked (2)",
        "failure_fingerprint": "fingerprint-source-diversity",
        "policy_decision": {
            "action": "quarantine",
            "reason": "repair budget exhausted for SOURCE_DIVERSITY_BLOCKED",
        },
    }
    with dashboard.state.transaction() as conn:
        conn.execute(
            "UPDATE orchestrator_tasks SET status='quarantined',attempt=1,output_hash=?,"
            "failure_code='SOURCE_DIVERSITY_BLOCKED',failure_payload=? "
            "WHERE run_id=? AND task_id='source_diversity_gate'",
            (failure.digest, json.dumps(failure_payload), run_id),
        )
        conn.execute(
            "INSERT INTO orchestrator_attempts(attempt_id,run_id,task_id,attempt,status,"
            "input_hashes,output_hash,failure_code,failure_payload,started_at,finished_at,worker_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (attempt_id, run_id, "source_diversity_gate", 1, "quarantined", "[]",
             failure.digest, "SOURCE_DIVERSITY_BLOCKED", json.dumps(failure_payload),
             "2026-08-24T00:00:00+00:00", "2026-08-24T00:00:01+00:00", "test-worker"),
        )
    AlignmentStore(dashboard.state).observation({
        "schema_version": "quality-observation-v1",
        "job_id": job_id,
        "run_id": run_id,
        "goal_id": "goal-stage-audit",
        "score": 0.2,
        "dimensions": {},
        "evidence": {"batch": str(batch)},
    })

    failed_trace = dashboard.job(job_id)["execution_trace"]
    failed_stage = next(
        item for item in failed_trace["stages"]
        if item["task_id"] == "source_diversity_gate"
    )
    assert failed_stage["status"] == "quarantined"
    assert failed_stage["output"]["protocol"] == "failure-envelope-v1"
    assert failed_stage["output"]["diagnostic_integrity"] == (
        "hash_verified_attempt_snapshot"
    )
    assert failed_stage["gate"]["decision"] == "BLOCKED"
    assert failed_stage["gate"]["blocking_failures"] == [
        "reviewed_confirmed_urls", "reviewed_distinct_domains",
    ]
    assert failed_stage["gate"]["evidence_source"] == (
        "hash_verified_attempt_snapshot"
    )
    assert failed_stage["transition"]["allowed"] is False

    official_digest = stage["output"]["documents"][0]["digest"]
    official_json = dashboard.json_artifact(official_digest)
    assert official_json["source_kind"] == "immutable_artifact"
    assert official_json["verified"] is True
    assert official_json["value"]["decision"] == "PASS"
    snapshot_json = dashboard.json_attempt_snapshot(
        job_id, "source_diversity_gate", attempt_id, diversity_file.name,
    )
    assert snapshot_json["source_kind"] == "attempt_archive_snapshot"
    assert snapshot_json["verified"] is True
    assert snapshot_json["value"]["decision"] == "BLOCKED"
    assert snapshot_json["metadata"]["attempt_id"] == attempt_id

    server = DashboardHTTPServer(("127.0.0.1", 0), root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/api/json/artifacts/{official_digest}", timeout=3) as response:
            assert json.load(response)["value"]["decision"] == "PASS"
        snapshot_url = (
            f"{base}/api/json/snapshots/{job_id}/source_diversity_gate/{attempt_id}"
            f"?path={diversity_file.name}"
        )
        with urlopen(snapshot_url, timeout=3) as response:
            assert json.load(response)["value"]["decision"] == "BLOCKED"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    with pytest.raises(KeyError):
        dashboard.json_attempt_snapshot(
            job_id, "source_diversity_gate", attempt_id, "../state.db",
        )
    diversity_file.write_text('{"decision":"PASS"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="hash does not match"):
        dashboard.json_attempt_snapshot(
            job_id, "source_diversity_gate", attempt_id, diversity_file.name,
        )


def test_dashboard_projects_question_contract_execution_and_evidence_as_one_chain() -> None:
    contract_hash = "a" * 64
    contract = {
        "dimension": "identity_and_terminology",
        "criticality": "required_for_model",
        "required_question_ids": ["identity.activity_definition"],
        "source_role_requirements": ["authoritative_or_current_job_manufacturer"],
        "preferred_source_classes": ["manufacturer_technical"],
        "subquestions": [{
            "question_id": "identity.activity_definition",
            "question": {"zh": "该节点究竟是什么活动？", "en": "What is this activity?"},
            "requirement_ids": ["activity.identity.definition"],
            "closure_rule": "all_bound_requirements_confirmed",
            "semantic_frame": {"predicate": "defines_activity_identity"},
            "query_intents": [{
                "intent_id": "identity.activity_definition.definition",
                "purpose": "definition", "priority": 1,
                "seed_terms": {"zh": ["定义"], "en": ["definition"]},
            }],
        }],
    }
    evidence = {
        "claim_id": "A019-identity-1",
        "requirement_id": "activity.identity.definition",
        "verdict": "CONFIRMED",
        "claim_kind": "external_fact",
        "url": "https://example.com/identity",
        "support_type": "direct",
    }
    ledger = {
        "question_contract_sha256": contract_hash,
        "questions": [{
            "question_id": "identity.activity_definition",
            "dimension": "identity_and_terminology",
            "criticality": "required_for_model",
            "question": {"zh": "该节点究竟是什么活动？"},
            "status": "confirmed",
            "closure_rule": "all_bound_requirements_confirmed",
            "bound_requirement_ids": ["activity.identity.definition"],
            "confirmed_requirement_ids": ["activity.identity.definition"],
            "missing_requirement_ids": [],
            "evidence": [evidence],
        }],
    }
    stages = [{
        "task_id": "research_plan",
        "output": {"documents": [{
            "path": "research-plan.json", "digest": "plan-digest", "integrity": "verified",
            "facts": {
                "research_question_contract_version": "wiki-research-question-v2",
                "question_contract_sha256": contract_hash,
                "research_question_contracts": [contract],
            },
        }]},
    }, {
        "task_id": "source_diversity_gate",
        "gate": {
            "decision": "PASS", "passed": True, "pipeline_continue": True,
            "reason_zh": "所有关键问题均已闭合。",
            "maturity_ceiling": "wiki_candidate",
            "question_evidence_ledger": ledger,
        },
        "output": {"documents": [{
            "path": "source-diversity-gate.json", "digest": "gate-digest",
            "integrity": "verified", "facts": {"question_evidence_ledger": ledger},
        }]},
    }]
    searches = [{
        "query_id": "query-1", "question_id": "identity.activity_definition",
        "intent_id": "identity.activity_definition.definition", "language": "zh",
        "query": "服务器 BIOS 配置 活动定义",
        "providers": [{"provider": "technical_search", "status": "ok"}],
        "results": [{"outcome": "accepted"}],
    }]
    citations = [{
        "claim_id": "A019-identity-1", "question_id": "identity.activity_definition",
        "requirement_id": "activity.identity.definition", "verdict": "CONFIRMED",
    }]

    projected = DashboardService._research_question_projection(
        stages, searches, citations,
    )

    assert projected["available"] is True
    assert projected["contract_integrity"] is True
    assert projected["metrics"]["required_questions_confirmed"] == 1
    assert projected["artifacts"]["plan"]["digest"] == "plan-digest"
    question = projected["questions"][0]
    assert question["status"] == "confirmed"
    assert question["required_for_model"] is True
    assert question["execution"]["queries"][0]["query"] == "服务器 BIOS 配置 活动定义"
    assert question["execution"]["accepted_count"] == 1
    assert question["evidence"][0]["url"] == "https://example.com/identity"


def test_dashboard_does_not_invent_question_contract_for_legacy_plan() -> None:
    projected = DashboardService._research_question_projection([{
        "task_id": "research_plan",
        "output": {"documents": [{
            "path": "research-plan.json", "digest": "legacy-plan",
            "facts": {"research_questions": ["identity_and_terminology"]},
        }]},
    }], [], [])

    assert projected["available"] is False
    assert projected["reason"] == "legacy_research_plan_without_question_contract"
    assert projected["legacy_questions"] == ["identity_and_terminology"]


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
