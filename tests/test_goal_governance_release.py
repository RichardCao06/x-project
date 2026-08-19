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


class _FakeReleaseDelegate:
    def __init__(self, root: Path) -> None:
        self.release_root = root
        self.applied = 0

    def stage(self, files: dict[str, bytes], **_: object):
        class Staged:
            pass

        staged = Staged()
        staged.id = "release_fixture"
        staged.root = self.release_root / "staged" / staged.id
        staged.root.mkdir(parents=True, exist_ok=True)
        staged.manifest = {
            name: hashlib.sha256(content).hexdigest() for name, content in files.items()
        }
        staged.expected_current = {name: None for name in files}
        staged.gate_results = ()
        staged.files = files
        return staged

    def apply(self, staged, destination: str | Path) -> Path:
        self.applied += 1
        destination = Path(destination)
        for name, content in staged.files.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        backup = self.release_root / "backups" / staged.id
        backup.mkdir(parents=True, exist_ok=True)
        return backup

    def rollback(self, staged, destination: str | Path) -> None:
        for name in staged.files:
            target = Path(destination) / name
            if target.exists():
                target.unlink()


def test_governed_release_supports_shadow_and_enforced_modes(
    controller: GovernanceController,
    tmp_path: Path,
) -> None:
    shadow_binding = register_bundle(
        controller, capability_status="shadow", job_id="job_shadow"
    )
    delegate = _FakeReleaseDelegate(tmp_path / "release-shadow")
    shadow = GovernedReleaseManager(delegate, controller, mode=GovernanceMode.SHADOW)
    staged = shadow.stage({"wiki/A039.md": b"candidate"})
    shadow.apply(
        staged,
        tmp_path / "destination-shadow",
        job_id=shadow_binding.job_id,
        risk="low",
        runtime_fingerprint=runtime(),
        input_scope={
            "process_family": "server_final_assembly",
            "document_type": "epd",
        },
    )
    assert delegate.applied == 1
    records = [
        json.loads(path.read_text())
        for path in sorted(
            (delegate.release_root / "governance" / "release_fixture").glob("*.json")
        )
    ]
    assert {record["status"] for record in records} == {
        "not_authorized",
        "shadow_applied",
    }
    assert all(record["eligibility"]["decision"] == "blocked" for record in records)
    assert len({record["created_at"] for record in records}) == 2

    enforced_delegate = _FakeReleaseDelegate(tmp_path / "release-enforced-blocked")
    enforced = GovernedReleaseManager(
        enforced_delegate, controller, mode=GovernanceMode.ENFORCED
    )
    with pytest.raises(ReleaseGovernanceError, match="not authorized"):
        enforced.apply(
            enforced_delegate.stage({"wiki/A039.md": b"candidate"}),
            tmp_path / "destination-enforced-blocked",
            job_id=shadow_binding.job_id,
            risk="low",
            runtime_fingerprint=runtime(),
            input_scope={
                "process_family": "server_final_assembly",
                "document_type": "epd",
            },
        )
    assert enforced_delegate.applied == 0


def test_governed_release_applies_only_after_complete_alignment(
    controller: GovernanceController,
    tmp_path: Path,
) -> None:
    binding = register_bundle(controller, job_id="job_release")
    controller.assess_alignment(
        job_id=binding.job_id,
        clause_results=complete_clause_results(),
        prohibited_outcomes=absent_prohibited(),
        capability_match=True,
        terminal_state="modeling_ready",
        claimed_complete=True,
        evidence=proof_evidence(),
    )
    delegate = _FakeReleaseDelegate(tmp_path / "release-authorized")
    manager = GovernedReleaseManager(delegate, controller, mode="enforced")
    staged = manager.stage({"wiki/A039.md": b"candidate"})
    backup = manager.apply(
        staged,
        tmp_path / "destination-authorized",
        job_id=binding.job_id,
        risk="low",
        runtime_fingerprint=runtime(),
        input_scope={
            "process_family": "server_final_assembly",
            "document_type": "epd",
        },
    )
    assert delegate.applied == 1
    assert backup.is_dir()
    assert manager.last_eligibility is not None
    assert manager.last_eligibility.decision is AutonomyDecision.AUTHORIZED
    assert set(manager.last_eligibility.requirement_evidence_hashes) == {
        "release_attestation",
        "rollback",
    }
    records = list(
        (delegate.release_root / "governance" / "release_fixture").glob("*.json")
    )
    assert len(records) == 2
