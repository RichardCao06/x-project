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


def test_non_goal_replacement_is_human_governed_and_old_binding_remains_frozen(
    controller: GovernanceController,
) -> None:
    binding = register_bundle(controller)
    target = policy("wiki-capability-envelope-v1.json")
    target["version"] = "1.1.0"
    target["certification"]["status"] = "certified"
    target["certification"]["cohort_id"] = "wiki-node-governance-cohort-v2"
    with pytest.raises(GovernanceError, match="human_governance_owner"):
        controller.replace_active_contract(
            from_ref=binding.capability_ref,
            target_payload=target,
            actor="capability-agent",
            actor_role="agent",
            rationale="agent self-promotion",
            evidence=["cohort://v2"],
        )
    result = controller.replace_active_contract(
        from_ref=binding.capability_ref,
        target_payload=target,
        actor="platform-owner",
        actor_role="human_governance_owner",
        rationale="Independent cohort v2 recertified the same capability boundary.",
        evidence=["cohort://v2", "review://capability-board/17"],
    )
    assert result["status"] == "active"
    assert result["contract_ref"].endswith("@1.1.0")
    assert controller.contract(binding.capability_ref)["status"] == "superseded"
    assert controller.binding(binding.job_id)["capability_ref"] == binding.capability_ref


def test_semantic_prohibited_outcome_change_is_not_preauthorized(
    controller: GovernanceController,
) -> None:
    goal = policy("wiki-goal-contract-v2.json")
    row = controller.register_contract(goal)
    controller.activate_initial_contract(
        row["contract_ref"], actor="owner", actor_role="human_goal_owner"
    )
    target = deepcopy(goal)
    target["version"] = "2.0.1"
    target["prohibited_outcomes"][0]["statement"] += " Including indirect claims."
    proposal = controller.propose_goal_change(
        from_ref=row["contract_ref"],
        target_payload=target,
        acceptance_delta={
            "newly_allowed": [],
            "newly_blocked": [],
            "unchanged_samples": ["cohort://wiki-node-golden@2"],
            "unknown": [],
        },
        rationale="Clarify a prohibited-outcome boundary.",
        evidence=["review://goal-semantics/12"],
        proposed_by="goal-analyst-agent",
    )
    assert proposal["change_class"] == "semantic_refinement"
    assert proposal["risk"] == "high"
    assert proposal["status"] == "proposed"


def test_assurance_must_cover_every_hard_and_required_goal_clause(
    controller: GovernanceController,
) -> None:
    register_bundle(controller, bind=False)
    incomplete = deepcopy(policy("wiki-assurance-contract-v1.json"))
    incomplete["contract_id"] = "incomplete-wiki-assurance"
    incomplete["proof_obligations"].pop("decision_utility")
    registered = controller.register_contract(incomplete)
    controller.activate_initial_contract(
        registered["contract_ref"],
        actor="owner",
        actor_role="human_governance_owner",
    )
    with pytest.raises(GovernanceError, match="misses required Goal clauses"):
        controller.bind_job(JobContractBinding(
            job_id="job_incomplete_assurance",
            goal_ref="goal://wiki-node-goal@2.0.0",
            autonomy_ref="autonomy://wiki-node-autonomy@1.0.0",
            assurance_ref="assurance://incomplete-wiki-assurance@1.0.0",
            capability_ref="capability://wiki-node-production-capability@1.0.0",
        ))


def test_goal_change_approval_cannot_be_reversed_after_decision(
    controller: GovernanceController,
) -> None:
    goal = policy("wiki-goal-contract-v2.json")
    registered = controller.register_contract(goal)
    controller.activate_initial_contract(
        registered["contract_ref"], actor="owner", actor_role="human_goal_owner"
    )
    target = deepcopy(goal)
    target["version"] = "2.3.0"
    target["clauses"] = [
        item for item in target["clauses"] if item["id"] != "decision_utility"
    ]
    proposal = controller.propose_goal_change(
        from_ref=registered["contract_ref"],
        target_payload=target,
        acceptance_delta={
            "newly_allowed": ["fixture://relaxed"],
            "newly_blocked": [],
            "unchanged_samples": [],
            "unknown": [],
        },
        rationale="Test an explicit relaxation decision.",
        evidence=["review://goal-board/21"],
        proposed_by="goal-analyst-agent",
    )
    controller.approve_goal_change(
        proposal["proposal_id"],
        actor="goal-owner",
        actor_role="human_goal_owner",
        decision="approve",
        rationale="Approved for this regression fixture.",
    )
    with pytest.raises(GovernanceError, match="already approved"):
        controller.approve_goal_change(
            proposal["proposal_id"],
            actor="second-owner",
            actor_role="human_goal_owner",
            decision="reject",
            rationale="A later actor cannot reverse the recorded decision.",
        )


def test_embedded_requirement_evidence_must_match_its_certificate_hash(
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
        evidence=proof_evidence(),
    )
    evidence = requirement_evidence("release_attestation", "rollback")
    evidence["release_attestation"]["payload"] = {"tampered": True}
    result = controller.check_autonomy(
        job_id=binding.job_id,
        action="publish",
        risk="low",
        runtime_fingerprint=runtime(),
        input_scope={
            "process_family": "server_final_assembly",
            "document_type": "epd",
        },
        requirement_evidence=evidence,
    )
    assert result.decision is AutonomyDecision.BLOCKED
    assert any("payload hash mismatch" in item for item in result.reasons)


def test_contract_validation_rejects_unknown_fields_and_invalid_versions() -> None:
    goal = policy("wiki-goal-contract-v2.json")
    goal["purpsoe"] = goal["purpose"]
    with pytest.raises(ValueError, match="unsupported fields"):
        ContractDocument.from_mapping(goal)

    malformed = policy("wiki-goal-contract-v2.json")
    malformed["version"] = "2.0.0 invalid"
    with pytest.raises(ValueError, match="version has an unsupported format"):
        ContractDocument.from_mapping(malformed)

    duplicate = policy("wiki-autonomy-contract-v1.json")
    duplicate["actions"]["publish"]["requirements"].append("rollback")
    with pytest.raises(ValueError, match="duplicate values"):
        ContractDocument.from_mapping(duplicate)
