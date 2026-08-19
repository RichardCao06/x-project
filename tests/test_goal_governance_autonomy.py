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


def test_autonomy_is_conditioned_on_contracts_capability_scope_and_risk(
    controller: GovernanceController,
) -> None:
    binding = register_bundle(controller)
    requirements = requirement_evidence("release_attestation", "rollback")
    without_assessment = controller.check_autonomy(
        job_id=binding.job_id,
        action="publish",
        risk="low",
        runtime_fingerprint=runtime(),
        input_scope={
            "process_family": "server_final_assembly",
            "document_type": "epd",
        },
        requirement_evidence=requirements,
    )
    assert without_assessment.decision is AutonomyDecision.BLOCKED
    assert any("alignment_assessment" in item for item in without_assessment.reasons)

    controller.assess_alignment(
        job_id=binding.job_id,
        clause_results=complete_clause_results(),
        prohibited_outcomes=absent_prohibited(),
        capability_match=True,
        terminal_state="modeling_ready",
        claimed_complete=True,
        evidence=proof_evidence(),
    )
    eligible = controller.check_autonomy(
        job_id=binding.job_id,
        action="publish",
        risk="low",
        runtime_fingerprint=runtime(),
        input_scope={
            "process_family": "server_final_assembly",
            "document_type": "epd",
        },
        requirement_evidence=requirements,
    )
    assert eligible.decision is AutonomyDecision.AUTHORIZED

    mismatch = controller.check_autonomy(
        job_id=binding.job_id,
        action="publish",
        risk="low",
        runtime_fingerprint={**runtime(), "model": "unvalidated-model"},
        input_scope={
            "process_family": "server_final_assembly",
            "document_type": "epd",
        },
        requirement_evidence=requirements,
    )
    assert mismatch.decision is AutonomyDecision.BLOCKED
    assert any("runtime fingerprint mismatch" in item for item in mismatch.reasons)

    outside_scope = controller.check_autonomy(
        job_id=binding.job_id,
        action="publish",
        risk="low",
        runtime_fingerprint=runtime(),
        input_scope={
            "process_family": "novel_battery_recycling",
            "document_type": "epd",
        },
        requirement_evidence=requirements,
    )
    assert outside_scope.decision is AutonomyDecision.BLOCKED

    reserved = controller.check_autonomy(
        job_id=binding.job_id,
        action="publish",
        risk="low",
        runtime_fingerprint=runtime(),
        input_scope={
            "process_family": "server_final_assembly",
            "document_type": "epd",
        },
        requested_authority=["change_goal_semantics"],
        requirement_evidence=requirements,
    )
    assert reserved.decision is AutonomyDecision.HUMAN_APPROVAL_REQUIRED

    too_risky = controller.check_autonomy(
        job_id=binding.job_id,
        action="publish",
        risk="medium",
        runtime_fingerprint=runtime(),
        input_scope={
            "process_family": "server_final_assembly",
            "document_type": "epd",
        },
        requirement_evidence=requirements,
    )
    assert too_risky.decision is AutonomyDecision.BLOCKED


def test_alignment_is_non_compensatory_and_supports_honest_incompletion(
    controller: GovernanceController,
) -> None:
    binding = register_bundle(controller)
    complete = controller.assess_alignment(
        job_id=binding.job_id,
        clause_results=complete_clause_results(),
        prohibited_outcomes=absent_prohibited(),
        capability_match=True,
        terminal_state="modeling_ready",
        claimed_complete=True,
        evidence=proof_evidence(),
    )
    assert complete.verdict is AlignmentVerdict.ALIGNED_COMPLETE

    incomplete_results = complete_clause_results()
    incomplete_results["critical_field_readiness"] = "insufficient"
    honest = controller.assess_alignment(
        job_id=binding.job_id,
        clause_results=incomplete_results,
        prohibited_outcomes=absent_prohibited(),
        capability_match=True,
        terminal_state="needs_research",
        claimed_complete=False,
        evidence=proof_evidence(),
    )
    assert honest.verdict is AlignmentVerdict.ALIGNED_INCOMPLETE

    false_success = controller.assess_alignment(
        job_id=binding.job_id,
        clause_results=incomplete_results,
        prohibited_outcomes=absent_prohibited(),
        capability_match=True,
        terminal_state="modeling_ready",
        claimed_complete=True,
        evidence=proof_evidence(),
    )
    assert false_success.verdict is AlignmentVerdict.MISALIGNED
    assert any("without complete non-compensatory proof" in item
               for item in false_success.findings)

    hard_failure = complete_clause_results()
    hard_failure["identity_boundary_exact"] = "failed"
    failed = controller.assess_alignment(
        job_id=binding.job_id,
        clause_results=hard_failure,
        prohibited_outcomes=absent_prohibited(),
        capability_match=True,
        terminal_state="failed",
        claimed_complete=False,
        evidence=proof_evidence(),
    )
    assert failed.verdict is AlignmentVerdict.MISALIGNED


def test_capability_mismatch_escalates_without_weakening_goal(
    controller: GovernanceController,
) -> None:
    binding = register_bundle(controller)
    result = controller.assess_alignment(
        job_id=binding.job_id,
        clause_results=complete_clause_results(),
        prohibited_outcomes=absent_prohibited(),
        capability_match=False,
        terminal_state="human_judgment_required",
        claimed_complete=False,
        evidence=proof_evidence(),
    )
    assert result.verdict is AlignmentVerdict.HUMAN_JUDGMENT_REQUIRED
    assert controller.contract(binding.goal_ref)["payload"]["clauses"] == policy(
        "wiki-goal-contract-v2.json"
    )["clauses"]


def test_capability_must_be_certified_and_empty_scope_is_not_eligible(
    controller: GovernanceController,
) -> None:
    binding = register_bundle(controller, capability_status="shadow")
    controller.assess_alignment(
        job_id=binding.job_id,
        clause_results=complete_clause_results(),
        prohibited_outcomes=absent_prohibited(),
        capability_match=True,
        terminal_state="modeling_ready",
        claimed_complete=True,
        evidence=proof_evidence(),
    )
    shadow = controller.check_autonomy(
        job_id=binding.job_id,
        action="publish",
        risk="low",
        runtime_fingerprint=runtime(),
        input_scope={
            "process_family": "server_final_assembly",
            "document_type": "epd",
        },
        requirement_evidence=requirement_evidence("release_attestation", "rollback"),
    )
    assert shadow.decision is AutonomyDecision.BLOCKED
    assert any("not certified" in item for item in shadow.reasons)

    second = GovernanceController(StateStore(controller.state.path.parent / "certified.db"))
    try:
        certified_binding = register_bundle(second, job_id="job_certified")
        second.assess_alignment(
            job_id=certified_binding.job_id,
            clause_results=complete_clause_results(),
            prohibited_outcomes=absent_prohibited(),
            capability_match=True,
            terminal_state="modeling_ready",
            claimed_complete=True,
            evidence=proof_evidence(),
        )
        empty_scope = second.check_autonomy(
            job_id=certified_binding.job_id,
            action="publish",
            risk="low",
            runtime_fingerprint=runtime(),
            input_scope={"process_family": [], "document_type": "epd"},
            requirement_evidence=requirement_evidence("release_attestation", "rollback"),
        )
        assert empty_scope.decision is AutonomyDecision.BLOCKED
        assert any("at least one certified value" in item for item in empty_scope.reasons)
    finally:
        second.state.close()


def test_sensitive_requirements_cannot_be_self_asserted_as_strings(
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
    result = controller.check_autonomy(
        job_id=binding.job_id,
        action="publish",
        risk="low",
        runtime_fingerprint=runtime(),
        input_scope={
            "process_family": "server_final_assembly",
            "document_type": "epd",
        },
        satisfied_requirements=["release_attestation", "rollback"],
    )
    assert result.decision is AutonomyDecision.BLOCKED
    assert any("release_attestation" in item for item in result.reasons)


def test_suspension_revokes_an_existing_job_capability(
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
    controller.suspend_contract(
        binding.capability_ref,
        actor="platform-owner",
        actor_role="human_governance_owner",
        reason="A new false pass invalidated the certification.",
        evidence=["incident://false-pass/42"],
    )
    result = controller.check_autonomy(
        job_id=binding.job_id,
        action="publish",
        risk="low",
        runtime_fingerprint=runtime(),
        input_scope={
            "process_family": "server_final_assembly",
            "document_type": "epd",
        },
        requirement_evidence=requirement_evidence("release_attestation", "rollback"),
    )
    assert result.decision is AutonomyDecision.BLOCKED
    assert any("capability_ref is suspended" in item for item in result.reasons)
