from __future__ import annotations

import hashlib
import json
from pathlib import Path

from lca_project.governance_cli import main


ROOT = Path(__file__).resolve().parents[1]


def invoke(capsys, *args: str) -> tuple[int, dict]:
    code = main(list(args))
    output = capsys.readouterr().out
    return code, json.loads(output)


def test_cli_registers_binds_and_checks_autonomy(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    role_by_file = {
        "wiki-goal-contract-v2.json": "human_goal_owner",
        "wiki-autonomy-contract-v1.json": "human_governance_owner",
        "wiki-assurance-contract-v1.json": "human_governance_owner",
        "wiki-capability-envelope-v1.json": "human_governance_owner",
    }
    for filename, role in role_by_file.items():
        contract_path = ROOT / "policies" / filename
        if filename == "wiki-capability-envelope-v1.json":
            capability = json.loads(contract_path.read_text(encoding="utf-8"))
            capability["certification"]["status"] = "certified"
            contract_path = tmp_path / filename
            contract_path.write_text(json.dumps(capability), encoding="utf-8")
        code, result = invoke(
            capsys,
            "--root", str(root),
            "register", str(contract_path),
            "--activate", "--actor", "test-owner", "--role", role,
        )
        assert code == 0
        assert result["status"] == "active"

    code, result = invoke(
        capsys,
        "--root", str(root),
        "bind-job", "job_cli",
        "--goal", "goal://wiki-node-goal@2.0.0",
        "--autonomy", "autonomy://wiki-node-autonomy@1.0.0",
        "--assurance", "assurance://wiki-node-assurance@1.0.0",
        "--capability", "capability://wiki-node-production-capability@1.0.0",
    )
    assert code == 0
    assert result["job_id"] == "job_cli"

    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({
        "model": "sol-verifier@2026-08",
        "prompt": "wiki-applicability-v4",
        "toolset": "evidence-review-tools-v2",
        "workflow": "wiki-node-production@9",
    }), encoding="utf-8")
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({
        "process_family": "server_final_assembly",
        "document_type": "epd",
    }), encoding="utf-8")

    clauses = tmp_path / "clauses.json"
    clauses.write_text(json.dumps({
        "identity_boundary_exact": "proved",
        "evidence_provenance_complete": "proved",
        "maturity_honesty": "proved",
        "critical_field_readiness": "proved",
        "decision_utility": "proved",
        "research_process_integrity": "proved",
        "production_efficiency": "not_applicable",
    }), encoding="utf-8")
    prohibited = tmp_path / "prohibited.json"
    prohibited.write_text(json.dumps({
        "run_success_as_goal_success": "absent",
        "empty_or_gap_as_data_ready": "absent",
        "adjacent_object_substitution": "absent",
        "self_signed_assurance": "absent",
        "goalpost_movement": "absent",
    }), encoding="utf-8")
    assurance = json.loads((ROOT / "policies" / "wiki-assurance-contract-v1.json").read_text())
    proof_payload = {"proofs": {}}
    for clause_id, obligation in assurance["proof_obligations"].items():
        proof_payload["proofs"][clause_id] = {
            "artifact_ref": f"artifact://proof/{clause_id}",
            "certificate_hash": hashlib.sha256(clause_id.encode()).hexdigest(),
            "evaluator": obligation["evaluator"],
            "evidence_types": obligation["evidence_types"],
            "producer_actor": "worker-agent",
            "evaluator_actor": "independent-gate",
        }
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(proof_payload), encoding="utf-8")
    code, assessment = invoke(
        capsys,
        "--root", str(root),
        "assess-alignment", "job_cli",
        "--clause-results", str(clauses),
        "--prohibited-outcomes", str(prohibited),
        "--terminal-state", "modeling_ready",
        "--claimed-complete", "--capability-match",
        "--evidence", str(evidence),
    )
    assert code == 0
    assert assessment["verdict"] == "aligned_complete"

    requirement_evidence = tmp_path / "requirement-evidence.json"
    requirement_evidence.write_text(json.dumps({
        name: {
            "artifact_ref": f"artifact://requirement/{name}",
            "certificate_hash": hashlib.sha256(name.encode()).hexdigest(),
            "issuer_actor": "deterministic-control-plane",
        }
        for name in ("release_attestation", "rollback")
    }), encoding="utf-8")
    code, result = invoke(
        capsys,
        "--root", str(root),
        "check-autonomy", "job_cli", "publish", "--risk", "low",
        "--runtime", str(runtime), "--input-scope", str(scope),
        "--requirement-evidence", str(requirement_evidence),
    )
    assert code == 0
    assert result["decision"] == "authorized"

    code, status = invoke(capsys, "--root", str(root), "status")
    assert code == 0
    assert status["counts"]["governance_contracts"] == 4
    assert status["counts"]["job_contract_bindings"] == 1
    assert status["counts"]["autonomy_eligibility_assessments"] == 1
