from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from lca_project.contracts.governance import (
    AlignmentVerdict,
    AutonomyDecision,
    ContractDocument,
    JobContractBinding,
)
from lca_project.kernel.goal_alignment.governance import GovernanceController, GovernanceError
from lca_project.kernel.governed_release import (
    GovernanceMode, GovernedReleaseManager, ReleaseGovernanceError,
)
from lca_project.kernel.state import StateStore


ROOT = Path(__file__).resolve().parents[1]


def policy(name: str) -> dict:
    return json.loads((ROOT / "policies" / name).read_text(encoding="utf-8"))


@pytest.fixture()
def controller(tmp_path: Path) -> GovernanceController:
    state = StateStore(tmp_path / "state.db")
    result = GovernanceController(state)
    yield result
    state.close()


def register_bundle(
    controller: GovernanceController,
    *,
    bind: bool = True,
    capability_status: str = "certified",
    job_id: str = "job_A039",
) -> JobContractBinding:
    capability = policy("wiki-capability-envelope-v1.json")
    capability["certification"]["status"] = capability_status
    values = (
        policy("wiki-goal-contract-v2.json"),
        policy("wiki-autonomy-contract-v1.json"),
        policy("wiki-assurance-contract-v1.json"),
        capability,
    )
    for value in values:
        registered = controller.register_contract(value)
        role = (
            "human_goal_owner"
            if registered["contract_kind"] == "goal"
            else "human_governance_owner"
        )
        controller.activate_initial_contract(
            registered["contract_ref"], actor="test-owner", actor_role=role
        )
    binding = JobContractBinding(
        job_id=job_id,
        goal_ref="goal://wiki-node-goal@2.0.0",
        autonomy_ref="autonomy://wiki-node-autonomy@1.0.0",
        assurance_ref="assurance://wiki-node-assurance@1.0.0",
        capability_ref="capability://wiki-node-production-capability@1.0.0",
    )
    if bind:
        controller.bind_job(binding)
    return binding


def complete_clause_results() -> dict[str, str]:
    return {
        "identity_boundary_exact": "proved",
        "evidence_provenance_complete": "proved",
        "maturity_honesty": "proved",
        "critical_field_readiness": "proved",
        "decision_utility": "proved",
        "research_process_integrity": "proved",
        "production_efficiency": "not_applicable",
    }


def absent_prohibited() -> dict[str, str]:
    return {
        "run_success_as_goal_success": "absent",
        "empty_or_gap_as_data_ready": "absent",
        "adjacent_object_substitution": "absent",
        "self_signed_assurance": "absent",
        "goalpost_movement": "absent",
    }


def runtime() -> dict[str, str]:
    return {
        "model": "sol-verifier@2026-08",
        "prompt": "wiki-applicability-v4",
        "toolset": "evidence-review-tools-v2",
        "workflow": "wiki-node-production@9",
    }


def proof_evidence(**extra) -> dict:
    assurance = policy("wiki-assurance-contract-v1.json")
    proofs = {}
    for clause_id, obligation in assurance["proof_obligations"].items():
        proofs[clause_id] = {
            "artifact_ref": f"artifact://proof/{clause_id}",
            "certificate_hash": hashlib.sha256(clause_id.encode()).hexdigest(),
            "evaluator": obligation["evaluator"],
            "evidence_types": obligation["evidence_types"],
            "producer_actor": "worker-agent",
            "evaluator_actor": "independent-gate",
        }
    return {"proofs": proofs, **extra}


def requirement_evidence(*names: str) -> dict[str, dict[str, str]]:
    return {
        name: {
            "artifact_ref": f"artifact://requirement/{name}",
            "certificate_hash": hashlib.sha256(name.encode()).hexdigest(),
            "issuer_actor": "deterministic-control-plane",
        }
        for name in names
    }


def test_example_contracts_are_strictly_validated() -> None:
    refs = {
        str(ContractDocument.from_mapping(policy(name)).ref)
        for name in (
            "wiki-goal-contract-v2.json",
            "wiki-autonomy-contract-v1.json",
            "wiki-assurance-contract-v1.json",
            "wiki-capability-envelope-v1.json",
        )
    }
    assert refs == {
        "goal://wiki-node-goal@2.0.0",
        "autonomy://wiki-node-autonomy@1.0.0",
        "assurance://wiki-node-assurance@1.0.0",
        "capability://wiki-node-production-capability@1.0.0",
    }


def test_contract_version_is_immutable_but_new_version_is_allowed(
    controller: GovernanceController,
) -> None:
    goal = policy("wiki-goal-contract-v2.json")
    first = registered = controller.register_contract(goal)
    controller.activate_initial_contract(registered["contract_ref"], actor="owner", actor_role="human_goal_owner")
    assert len(first["contract_hash"]) == 64

    drifted = deepcopy(goal)
    drifted["purpose"]["value_statement"] = "silently changed"
    with pytest.raises(GovernanceError, match="immutable contract drift"):
        controller.register_contract(drifted)

    next_version = deepcopy(goal)
    next_version["version"] = "2.0.1"
    next_version["metadata"] = {"change": "non-semantic wording metadata"}
    registered = controller.register_contract(next_version)
    assert registered["contract_ref"] == "goal://wiki-node-goal@2.0.1"


def test_goal_relaxation_requires_human_goal_owner_and_keeps_old_job_binding(
    controller: GovernanceController,
) -> None:
    binding = register_bundle(controller)
    target = deepcopy(policy("wiki-goal-contract-v2.json"))
    target["version"] = "2.1.0"
    target["clauses"] = [
        item for item in target["clauses"] if item["id"] != "decision_utility"
    ]
    proposal = controller.propose_goal_change(
        from_ref=binding.goal_ref,
        target_payload=target,
        acceptance_delta={
            "newly_allowed": ["fixture://missing-decision-utility"],
            "newly_blocked": [],
            "unchanged_samples": ["fixture://A039"],
            "unknown": [],
        },
        rationale="Evaluate whether decision utility can be removed.",
        evidence=["deviation://dev_001"],
        proposed_by="goal-analyst-agent",
    )
    assert proposal["change_class"] == "goal_relaxation"
    assert proposal["risk"] == "critical"
    assert proposal["status"] == "proposed"

    with pytest.raises(GovernanceError, match="human_goal_owner"):
        controller.approve_goal_change(
            proposal["proposal_id"],
            actor="goal-analyst-agent",
            actor_role="agent",
            rationale="agent self approval",
        )

    approved = controller.approve_goal_change(
        proposal["proposal_id"],
        actor="lca-goal-owner",
        actor_role="human_goal_owner",
        rationale="The accountable owner accepts the changed risk.",
    )
    assert approved["status"] == "approved"
    activated = controller.activate_goal_change(proposal["proposal_id"], actor="change-controller")
    assert activated["status"] == "activated"
    assert controller.contract(binding.goal_ref)["status"] == "superseded"
    assert controller.contract("goal://wiki-node-goal@2.1.0")["status"] == "active"
    assert controller.binding(binding.job_id)["goal_ref"] == binding.goal_ref
    pending = controller.reassessments(status="pending")
    assert {item["subject_kind"] for item in pending} == {
        "job_eligibility",
        "contract_recompile",
        "capability_recertification",
    }
    # Running Jobs retain the old, immutable Goal semantics.  The queue is for
    # recompiling/recertifying the new Goal and reevaluating historical facts.
    job_item = next(item for item in pending if item["subject_kind"] == "job_eligibility")
    resolved = controller.resolve_reassessment(
        job_item["reassessment_id"],
        actor="lca-goal-owner",
        actor_role="human_goal_owner",
        disposition="reassessed",
        evidence=["replay://job_A039/goal-2.1"],
    )
    assert resolved["status"] == "resolved"


def test_structural_goal_change_can_use_policy_pre_authorization(
    controller: GovernanceController,
) -> None:
    goal = policy("wiki-goal-contract-v2.json")
    registered = controller.register_contract(goal)
    controller.activate_initial_contract(registered["contract_ref"], actor="owner", actor_role="human_goal_owner")
    target = deepcopy(goal)
    target["version"] = "2.0.1"
    target["metadata"] = {"owner_team": "lca-governance"}
    proposal = controller.propose_goal_change(
        from_ref="goal://wiki-node-goal@2.0.0",
        target_payload=target,
        acceptance_delta={
            "newly_allowed": [],
            "newly_blocked": [],
            "unchanged_samples": ["cohort://wiki-node-golden@2"],
            "unknown": [],
        },
        rationale="Add ownership metadata without changing acceptance semantics.",
        evidence=["replay://golden-v2"],
        proposed_by="goal-analyst-agent",
    )
    assert proposal["change_class"] == "structural_refactor"
    assert proposal["status"] == "preauthorized"
    controller.activate_goal_change(proposal["proposal_id"], actor="change-controller")
    assert controller.contract("goal://wiki-node-goal@2.0.1")["status"] == "active"


def test_assurance_must_target_the_bound_goal(controller: GovernanceController) -> None:
    register_bundle(controller, bind=False)
    incompatible = deepcopy(policy("wiki-assurance-contract-v1.json"))
    incompatible["contract_id"] = "wrong-goal-assurance"
    incompatible["goal_contract_ref"] = "goal://different-goal@1.0.0"
    registered = controller.register_contract(incompatible)
    controller.activate_initial_contract(
        registered["contract_ref"],
        actor="owner",
        actor_role="human_governance_owner",
    )
    with pytest.raises(GovernanceError, match="not bound"):
        controller.bind_job(JobContractBinding(
            job_id="job_wrong",
            goal_ref="goal://wiki-node-goal@2.0.0",
            autonomy_ref="autonomy://wiki-node-autonomy@1.0.0",
            assurance_ref="assurance://wrong-goal-assurance@1.0.0",
            capability_ref="capability://wiki-node-production-capability@1.0.0",
        ))


def test_governance_records_are_persistent_and_auditable(
    controller: GovernanceController,
) -> None:
    binding = register_bundle(controller)
    controller.assess_alignment(
        job_id=binding.job_id,
        clause_results=complete_clause_results(),
        prohibited_outcomes=absent_prohibited(),
        capability_match=True,
        terminal_state="modeling_ready",
        claimed_complete=True,
        evidence=proof_evidence(release_manifest="artifact://release/sha256:abc"),
    )
    status = controller.status()
    assert status["counts"] == {
        "governance_contracts": 4,
        "contract_lifecycle_events": 4,
        "goal_change_proposals": 0,
        "governance_approvals": 0,
        "job_contract_bindings": 1,
        "alignment_assessments": 1,
        "autonomy_eligibility_assessments": 0,
        "governance_reassessments": 0,
        "capability_certifications": 0,
        "capability_observations": 0,
    }
    persisted = controller.assessments(job_id=binding.job_id)
    assert persisted[0]["payload"]["evidence"]["release_manifest"].startswith("artifact://")


def test_governance_migrations_install_v2_tables(tmp_path: Path) -> None:
    import sqlite3
    from lca_project.kernel.migrations import migrate

    conn = sqlite3.connect(tmp_path / "migration.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        assert migrate(conn) == 20
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    tables = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "governance_contracts",
        "contract_lifecycle_events",
        "goal_change_proposals",
        "governance_approvals",
        "job_contract_bindings",
        "alignment_assessments",
        "autonomy_eligibility_assessments",
        "governance_reassessments",
        "capability_certifications",
        "capability_observations",
        "logic_audit_runs",
        "logic_audit_findings",
    } <= tables
    assert conn.execute(
        "SELECT name FROM schema_migrations WHERE version=13"
    ).fetchone()["name"] == "goal-contract-governance-v2"
    assert conn.execute(
        "SELECT name FROM schema_migrations WHERE version=14"
    ).fetchone()["name"] == "governance-reassessment-and-capability-assurance"
    conn.close()


def test_static_semantic_diff_detects_relaxation_even_when_agent_diff_hides_it(
    controller: GovernanceController,
) -> None:
    goal = policy("wiki-goal-contract-v2.json")
    registered = controller.register_contract(goal)
    controller.activate_initial_contract(
        registered["contract_ref"], actor="owner", actor_role="human_goal_owner"
    )
    target = deepcopy(goal)
    target["version"] = "2.2.0"
    target["clauses"] = [
        item for item in target["clauses"] if item["id"] != "decision_utility"
    ]
    proposal = controller.propose_goal_change(
        from_ref="goal://wiki-node-goal@2.0.0",
        target_payload=target,
        acceptance_delta={
            "newly_allowed": [],
            "newly_blocked": [],
            "unchanged_samples": ["fixture://agent-claims-no-change"],
            "unknown": [],
        },
        rationale="Agent claims the clause removal is structural.",
        evidence=["replay://independent-diff-required"],
        proposed_by="goal-analyst-agent",
    )
    assert proposal["change_class"] == "goal_relaxation"
    assert proposal["risk"] == "critical"
    assert any("required clause removed" in item
               for item in proposal["payload"]["semantic_findings"])


def test_proved_clause_requires_independent_structured_proof(
    controller: GovernanceController,
) -> None:
    binding = register_bundle(controller)
    missing = controller.assess_alignment(
        job_id=binding.job_id,
        clause_results=complete_clause_results(),
        prohibited_outcomes=absent_prohibited(),
        capability_match=True,
        terminal_state="modeling_ready",
        claimed_complete=True,
        evidence={},
    )
    assert missing.verdict is AlignmentVerdict.MISALIGNED
    assert any("proved clause has no proof record" in item for item in missing.findings)

    self_signed_evidence = proof_evidence()
    self_signed_evidence["proofs"]["identity_boundary_exact"]["evaluator_actor"] = "worker-agent"
    self_signed = controller.assess_alignment(
        job_id=binding.job_id,
        clause_results=complete_clause_results(),
        prohibited_outcomes=absent_prohibited(),
        capability_match=True,
        terminal_state="modeling_ready",
        claimed_complete=True,
        evidence=self_signed_evidence,
    )
    assert self_signed.verdict is AlignmentVerdict.MISALIGNED
    assert any("self-signed proof is forbidden" in item for item in self_signed.findings)
