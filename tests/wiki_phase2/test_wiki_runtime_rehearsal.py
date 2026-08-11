from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from lca_project.wiki_runtime import WikiRuntime, WikiRuntimeError, WikiStage
from lca_project.wiki_runtime.rehearsal import WikiPhase2Rehearsal


ROOT = Path(__file__).resolve().parents[2]


def test_agent_output_contracts_require_signed_attestation_receipts() -> None:
    for name in ("wiki-proposal-v1", "wiki-verdict-v1", "wiki-attestation-v1"):
        schema = json.loads((ROOT / "contracts" / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert {"agent_id", "attestation_receipt"} <= set(schema["required"])
        assert schema["properties"]["attestation"] is False


def test_phase2_rehearsal_is_isolated_persistent_and_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = WikiPhase2Rehearsal(project).run()
    second = WikiPhase2Rehearsal(project).run()
    first_runs = [run for cohort in first["cohorts"] for run in cohort["run_ids"]]
    second_runs = [run for cohort in second["cohorts"] for run in cohort["run_ids"]]
    assert first_runs == second_runs and len(first_runs) == 3
    assert first["report_hash"] == second["report_hash"]
    assert first["workspace"] == "$WORKSPACE"
    assert first["source_checkout_access"] is False
    assert first["publish_authorized"] is False and first["stopped_at"] == "prepared"
    runtime = WikiRuntime(project)
    assert all(runtime.get_run(run_id).current_stage is WikiStage.PREPARED for run_id in first_runs)
    assert all(len(runtime.stage_records(run_id)) == 2 for run_id in first_runs)
    assert runtime.control.status()["counts"]["jobs"] == 3


def test_runtime_will_not_accept_unattested_agent_or_jump_to_publish(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "agents", tmp_path / "agents")
    runtime = WikiRuntime(tmp_path)
    node = "ict_equipment::P003"
    run = runtime.start(node_id=node, dossier={"node_identity": {"node_id": node}}, policy_version="wiki-production-v3")
    run, plan = runtime.advance(run.run_id, WikiStage.PLAN, {"node_identity": {"node_id": node}, "outputs": [{"plan": True}]})
    run, prepared = runtime.advance(run.run_id, WikiStage.PREPARED, {"node_identity": {"node_id": node},
        "input_hashes": list(plan), "outputs": [{"prepared": True}]})
    with pytest.raises(WikiRuntimeError, match="registered agent definition|attestation|signed proof"):
        runtime.submit_agent_output(run.run_id, WikiStage.RESEARCH_READY, {
            "schema_version": "wiki-proposal-v1", "node_identity": {"node_id": node},
            "frozen_input_hash": prepared[-1], "agent_id": "researcher",
            "attestation": {"model": "gpt-5.6-terra", "reasoning_effort": "medium",
                "tools": ["artifact:read"], "argv": ["offline"], "sandbox": "read-only",
                "prompt_hash": "a" * 64, "usage": {"input_tokens": 1, "output_tokens": 1, "cost": 0},
                "network_used": False}, "outputs": []}, actor="frozen-agent/untrusted")
    with pytest.raises(WikiRuntimeError, match="expected"):
        runtime.advance(run.run_id, WikiStage.PUBLISHED, {"node_identity": {"node_id": node},
            "apply_receipt": {"target_hash": "a" * 64}, "post_verify": "pass",
            "release_manifest_hash": "b" * 64})


def test_rehearsal_report_is_frozen_in_cas_and_event_ledger(tmp_path: Path) -> None:
    project = tmp_path / "project"; project.mkdir()
    report = WikiPhase2Rehearsal(project).run()
    assert WikiPhase2Rehearsal(project).run()["report_hash"] == report["report_hash"]
    runtime = WikiRuntime(project)
    frozen = json.loads(runtime.artifacts.get_bytes(report["report_hash"]))
    assert frozen["protocol"] == "wiki-phase2-rehearsal-v1"
    events = list(runtime.events.read("wiki_rehearsal", report["report_hash"]))
    assert len(events) == 1 and events[0].event_type == "wiki.rehearsal.completed"
