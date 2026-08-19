from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from lca_project.contracts import Job
from lca_project.control import ControlPlane, ProtocolError
from lca_project.kernel.governance_runtime import GovernanceIntegrationError
from lca_project.kernel.governed_release import GovernedReleaseManager
from lca_project.kernel.goal_alignment.governance import GovernanceError
from lca_project.kernel.release import ReleaseManager


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_FILES = (
    "wiki-goal-contract-v2.json",
    "wiki-autonomy-contract-v1.json",
    "wiki-assurance-contract-v1.json",
    "wiki-capability-envelope-v1.json",
)


def governed_root(
    tmp_path: Path,
    *,
    mode: str = "shadow",
    capability_status: str = "shadow",
    capability_file: str = "wiki-capability-envelope-v1.1.json",
) -> Path:
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    (root / "policies").mkdir()
    for directory in ("agents", "capabilities", "skills", "workflows"):
        shutil.copytree(ROOT / directory, root / directory)
    config = json.loads(
        (ROOT / "config/governance-v2.json").read_text(encoding="utf-8")
    )
    config["mode"] = mode
    config["bindings"][0]["contracts"]["capability"] = f"policies/{capability_file}"
    (root / "config/governance-v2.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    for name in CONTRACT_FILES:
        shutil.copy2(ROOT / "policies" / name, root / "policies" / name)
    shutil.copy2(
        ROOT / "policies/wiki-capability-envelope-v1.1.json", root / "policies"
    )
    for name in ("wiki-production-v4.json", "runtime-routing-v1.json"):
        shutil.copy2(ROOT / "policies" / name, root / "policies" / name)
    capability_path = root / "policies" / capability_file
    capability = json.loads(capability_path.read_text(encoding="utf-8"))
    capability["certification"]["status"] = capability_status
    capability_path.write_text(json.dumps(capability), encoding="utf-8")
    return root


def wiki_job(*, job_id: str = "job_governed") -> Job:
    return Job(
        job_id=job_id,
        target="wiki:A039",
        workflow="wiki-node-production@9",
        scope={
            "skill": "generate-node-wiki",
            "skill_version": "11",
            "request": {
                "industry": "ict_equipment",
                "nodes": ["A039"],
                "publication_mode": "reviewed",
            },
        },
        policy_version="wiki-production-v4",
        input_hashes=("frozen-input",),
    )


def test_shadow_runtime_automatically_binds_configured_job(tmp_path: Path) -> None:
    control = ControlPlane(governed_root(tmp_path))
    job_id, duplicate = control.submit_job(wiki_job())

    assert duplicate is False
    binding = control.governance.controller.binding(job_id)
    assert binding is not None
    stored = control.state.get("jobs", job_id)
    assert stored is not None
    assert stored["payload"]["governance"]["binding_hash"] == binding["binding_hash"]
    context = stored["payload"]["governance"]["release_context"]
    assert context == control.governance.release_context(job_id)
    assert context["input_scope"] == {
        "industry": "ict_equipment",
        "nodes": ["A039"],
        "publication_mode": "reviewed",
    }
    assert all("unknown" not in value for value in context["runtime_fingerprint"].values())
    assert control.governance.admit_execution(job_id) is True

    # The example Capability Envelope is intentionally shadow-only.  Shadow
    # mode records the blocked publication decision without changing behavior.
    assert control.governance.evaluate_release_task(
        job_id=job_id,
        risk="low",
    ) is False
    decisions = control.state._connection().execute(
        "SELECT decision FROM autonomy_eligibility_assessments WHERE job_id=?",
        (job_id,),
    ).fetchall()
    assert [row["decision"] for row in decisions] == ["blocked"]


def test_enforced_runtime_rejects_unmapped_job_before_persistence(tmp_path: Path) -> None:
    control = ControlPlane(governed_root(tmp_path, mode="enforced"))
    job = Job(
        job_id="job_unmapped",
        target="graph:steel",
        workflow="graph-industry-production@1",
        scope={"industry": "steel"},
        policy_version="graph-quality-v1",
        input_hashes=("frozen-input",),
    )

    with pytest.raises(ProtocolError, match="no exact contract binding"):
        control.submit_job(job)
    assert control.state.get("jobs", job.job_id) is None


def test_enforced_runtime_blocks_shadow_capability_and_revocation(tmp_path: Path) -> None:
    control = ControlPlane(governed_root(tmp_path, mode="enforced"))
    job_id, _ = control.submit_job(wiki_job(job_id="job_enforced"))
    with pytest.raises(GovernanceIntegrationError, match="not authorized"):
        control.governance.evaluate_release_task(
            job_id=job_id,
            risk="low",
        )

    binding = control.governance.controller.binding(job_id)
    assert binding is not None
    control.governance.controller.suspend_contract(
        binding["capability_ref"],
        actor="test-owner",
        actor_role="human_governance_owner",
        reason="new false pass",
        evidence=("incident://false-pass/test",),
    )
    with pytest.raises(GovernanceIntegrationError, match="is suspended"):
        control.governance.admit_execution(job_id)


def test_runtime_wraps_release_manager_in_configured_mode(tmp_path: Path) -> None:
    control = ControlPlane(governed_root(tmp_path))
    base = ReleaseManager(tmp_path / "releases", required_gates=set())
    wrapped = control.governance.wrap_release_manager(base)
    assert isinstance(wrapped, GovernedReleaseManager)
    assert wrapped.mode.value == "shadow"


def test_independent_cohort_certification_issues_trusted_replacement(
    tmp_path: Path,
) -> None:
    root = governed_root(
        tmp_path, capability_file="wiki-capability-envelope-v1.json"
    )
    control = ControlPlane(root)
    replacement = control.governance.controller.replace_active_contract(
        from_ref="capability://wiki-node-production-capability@1.0.0",
        target_payload=json.loads(
            (root / "policies/wiki-capability-envelope-v1.1.json").read_text(
                encoding="utf-8"
            )
        ),
        actor="platform-governance-owner",
        actor_role="human_governance_owner",
        rationale="bind certification to the controller-derived production runtime",
        evidence=("change://p0-runtime-fingerprint",),
    )
    assert replacement["contract_ref"].endswith("@1.1.0")
    assert control.governance.readiness()["ready"] is False
    cases = [
        {
            "case_id": f"ordinary-{index}",
            "outcome": "correct",
            "should_abstain": False,
            "stratum": "ordinary",
        }
        for index in range(195)
    ] + [
        {
            "case_id": f"boundary-{index}",
            "outcome": "abstained",
            "should_abstain": True,
            "stratum": "boundary",
        }
        for index in range(5)
    ]
    report = control.governance.controller.certify_capability(
        from_ref="capability://wiki-node-production-capability@1.1.0",
        target_version="1.2.0",
        cohort_id="wiki-governance-independent-v2",
        cases=cases,
        evaluator_actor="independent-assurance-evaluator",
        authorizer_actor="platform-governance-owner",
        authorizer_role="human_governance_owner",
        valid_until="2027-08-19T00:00:00Z",
        thresholds={
            "min_sample_size": 200,
            "min_coverage": 0.8,
            "max_selective_risk_upper_bound": 0.1,
            "min_abstention_recall": 0.95,
        },
    )

    assert report["verdict"] == "certified"
    assert report["certified_contract_ref"].endswith("@1.2.0")
    assert control.governance.controller.contract(
        "capability://wiki-node-production-capability@1.1.0"
    )["status"] == "superseded"
    receipt = report["authority_receipt"]
    claims = control.governance.proof_authority.verify(
        receipt,
        kind="governance-evidence",
        subject=f"capability-certification:{report['certification_id']}",
    )
    assert claims["cohort_hash"] == report["cohort_hash"]

    # A successful replacement atomically advances the configured workflow.
    mapping = control.governance.mapping_for("wiki-node-production@9")
    assert mapping is not None
    assert mapping.capability_ref == report["certified_contract_ref"]
    readiness = control.governance.readiness()
    assert readiness["ready"] is True
    assert readiness["checks"]["configured_workflows_ready"] is True

    new_job_id, _ = control.submit_job(wiki_job(job_id="job_after_certification"))
    new_binding = control.governance.controller.binding(new_job_id)
    assert new_binding is not None
    assert new_binding["capability_ref"] == report["certified_contract_ref"]


def test_runtime_readiness_rejects_stale_repository_fingerprint(tmp_path: Path) -> None:
    root = governed_root(tmp_path, capability_status="certified")
    control = ControlPlane(root)
    assert control.governance.readiness()["ready"] is True

    route = root / "skills/generate-node-wiki/skill.manifest.json"
    document = json.loads(route.read_text(encoding="utf-8"))
    document["version"] = "12"
    route.write_text(json.dumps(document), encoding="utf-8")

    readiness = control.governance.readiness()
    assert readiness["ready"] is False
    assert "runtime fingerprint is stale" in " ".join(
        readiness["workflows"][0]["findings"]
    )


def test_frozen_release_context_rejects_job_payload_drift(tmp_path: Path) -> None:
    control = ControlPlane(governed_root(tmp_path))
    job_id, _ = control.submit_job(wiki_job(job_id="job_context_drift"))
    stored = control.state.get("jobs", job_id)
    assert stored is not None
    payload = dict(stored["payload"])
    scope = dict(payload["scope"])
    request = dict(scope["request"])
    request["nodes"] = ["P003"]
    scope["request"] = request
    payload["scope"] = scope
    control.state.upsert_entity(
        "jobs", job_id, stored["status"], payload, workflow_id=stored["workflow_id"]
    )

    with pytest.raises(GovernanceIntegrationError, match="has drifted"):
        control.governance.release_context(job_id)


def test_shadow_runtime_applies_reviewed_wiki_through_release_service(
    tmp_path: Path,
) -> None:
    root = governed_root(tmp_path, capability_status="certified")
    control = ControlPlane(root)
    job_id, _ = control.submit_job(wiki_job(job_id="job_wiki_release"))
    workspace = root / "var/workspaces/jobs" / job_id
    batch = workspace / "runs/wiki-batches/ict_equipment/release-test"
    page = workspace / "wiki/ict_equipment/activities/A039--server-assembly.md"
    registry = workspace / "sources/ict_equipment/registry.json"
    page.parent.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    (workspace / "docs").mkdir(parents=True)
    batch.mkdir(parents=True)
    page.write_text("---\nstatus: reviewed\n---\n# A039\n", encoding="utf-8")
    registry.write_text("{}\n", encoding="utf-8")
    for name in (
        "ict_equipment-name-graph.json",
        "ict_equipment-name-graph.html",
        "ict_equipment-wiki-data.js",
        "ict_equipment-wiki.html",
        "ict_equipment-wiki-A039.html",
    ):
        (workspace / "docs" / name).write_text(f"candidate:{name}\n", encoding="utf-8")
    (batch / "gate-report.json").write_text(json.dumps({
        "protocol": {"version": "wiki-batch-v2", "kind": "gate-report"},
        "all_passed": True,
        "go_no_go": {"final_verdict": "GO"},
    }), encoding="utf-8")
    (batch / "reviewed-apply-report.json").write_text(json.dumps({
        "protocol": {"version": "wiki-batch-v2", "kind": "reviewed-apply-report"},
        "report": {"transaction": "committed", "files": 1},
    }), encoding="utf-8")
    (batch / "publish-report.json").write_text(json.dumps({
        "protocol": {"version": "wiki-batch-v2", "kind": "publish-report"},
        "bundle": {"status": "PASS"},
        "viewer": {"status": "PASS"},
        "node_entrypoints": [{"status": "PASS"}],
    }), encoding="utf-8")
    (batch / "journal.json").write_text(
        json.dumps({"state": "published"}), encoding="utf-8"
    )

    record = control.governance.publish_wiki_release(
        job_id=job_id,
        workspace=workspace,
        batch=batch,
        industry="ict_equipment",
        node="A039",
    )

    destination = root / "var/publications/wiki/ict_equipment"
    assert record["publication_status"] == "published"
    assert record["governance_decision"] == "blocked"
    assert (destination / "wiki/activities/A039--server-assembly.md").read_bytes() == (
        page.read_bytes()
    )
    assert (destination / "docs/ict_equipment-wiki-A039.html").is_file()
    assert (batch / "release-record.json").is_file()


def test_online_false_pass_invalidates_enforced_capability(tmp_path: Path) -> None:
    control = ControlPlane(governed_root(
        tmp_path, mode="enforced", capability_status="certified"
    ))
    job_id, _ = control.submit_job(wiki_job(job_id="job_drift"))
    observation = control.governance.controller.record_capability_observation(
        capability_ref="capability://wiki-node-production-capability@1.1.0",
        case_id="production-escape-42",
        outcome="incorrect",
        should_abstain=False,
        actor="post-release-monitor",
    )

    assert observation["drift_detected"] is True
    repeated = control.governance.controller.record_capability_observation(
        capability_ref="capability://wiki-node-production-capability@1.1.0",
        case_id="production-escape-42",
        outcome="incorrect",
        should_abstain=False,
        actor="post-release-monitor",
    )
    assert repeated["observation_id"] == observation["observation_id"]
    with pytest.raises(GovernanceError, match="immutable capability observation drift"):
        control.governance.controller.record_capability_observation(
            capability_ref="capability://wiki-node-production-capability@1.1.0",
            case_id="production-escape-42",
            outcome="correct",
            should_abstain=False,
            actor="post-release-monitor",
        )
    with pytest.raises(GovernanceIntegrationError, match="requires recertification"):
        control.governance.admit_execution(job_id)


def test_enforced_alignment_requires_proof_authority_receipts(tmp_path: Path) -> None:
    control = ControlPlane(governed_root(
        tmp_path, mode="enforced", capability_status="certified"
    ))
    job_id, _ = control.submit_job(wiki_job(job_id="job_trusted_proofs"))
    controller = control.governance.controller
    binding = controller.binding(job_id)
    assert binding is not None
    goal = controller.contract(binding["goal_ref"])["payload"]
    assurance = controller.contract(binding["assurance_ref"])["payload"]
    clauses = {
        item["id"]: (
            "proved" if item["criticality"] in {"hard", "required"}
            else "not_applicable"
        )
        for item in goal["clauses"]
    }
    outcomes = {item["id"]: "absent" for item in goal["prohibited_outcomes"]}
    proofs = {}
    for clause_id, obligation in assurance["proof_obligations"].items():
        record = {
            "artifact_ref": f"artifact://proof/{clause_id}",
            "certificate_hash": "a" * 64,
            "evaluator": obligation["evaluator"],
            "evidence_types": obligation["evidence_types"],
            "producer_actor": "worker-agent",
            "evaluator_actor": "independent-gate",
        }
        proofs[clause_id] = controller.sign_evidence_record(
            record,
            subject=f"governance-clause:{binding['binding_hash']}:{clause_id}",
            producer="governance-evaluator",
        )
    trusted = controller.assess_alignment(
        job_id=job_id,
        clause_results=clauses,
        prohibited_outcomes=outcomes,
        capability_match=True,
        terminal_state="modeling_ready",
        claimed_complete=True,
        evidence={"proofs": proofs},
    )
    assert trusted.verdict.value == "aligned_complete"

    tampered = dict(proofs)
    tampered_record = dict(tampered[next(iter(tampered))])
    tampered_record["certificate_hash"] = "b" * 64
    tampered[next(iter(tampered))] = tampered_record
    rejected = controller.assess_alignment(
        job_id=job_id,
        clause_results=clauses,
        prohibited_outcomes=outcomes,
        capability_match=True,
        terminal_state="modeling_ready",
        claimed_complete=True,
        evidence={"proofs": tampered},
    )
    assert rejected.verdict.value == "misaligned"
    assert any("claims do not match" in item for item in rejected.findings)
