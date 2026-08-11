from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

from lca_project.kernel.assurance import AssuranceError, gate_claim_evidence, gate_identity, gate_runtime
from lca_project.kernel.executor import ExecutionError, SandboxedExecutor
from lca_project.kernel.registry import Capability
from lca_project.kernel.registry import CapabilityRegistry
from lca_project.kernel.release import ReleaseError, ReleaseManager
from lca_project.kernel.proofs import ProofAuthority, ProofError
from lca_project.control import ControlPlane
from lca_project.cli import main


def test_cap_002_undeclared_side_effect_is_rolled_back(tmp_path: Path) -> None:
    protected = tmp_path / "authority"
    protected.mkdir()
    target = protected / "record.txt"
    target.write_text("trusted", encoding="utf-8")
    script = tmp_path / "mutate.py"
    script.write_text(
        "import json,pathlib,sys; pathlib.Path(sys.argv[1]).write_text('corrupt'); "
        "pathlib.Path(sys.argv[2]).write_text(json.dumps({'status':'ok'}))",
        encoding="utf-8",
    )
    cap = Capability.from_mapping({"id": "mutant", "version": "1", "command": [sys.executable, str(script), str(target), "{output}"], "side_effects": "none"})
    runner = SandboxedExecutor(tmp_path / "scratch", protected_roots=(protected,))
    with pytest.raises(ExecutionError) as failure:
        runner.execute(cap, {}, run_id="r", task_id="t")
    assert failure.value.code == "SIDE_EFFECT"
    assert target.read_text(encoding="utf-8") == "trusted"


def test_executor_does_not_format_json_braces_in_python_code(tmp_path: Path) -> None:
    code = "import json,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(json.dumps({'status':'ok'}))"
    cap = Capability.from_mapping({"id": "braces", "version": "1", "command": [sys.executable, "-c", code, "{output}"]})
    result = SandboxedExecutor(tmp_path / "scratch", protected_roots=(tmp_path,)).execute(cap, {}, run_id="r", task_id="t")
    assert result.status == "ok"


def test_executor_fails_closed_without_protected_boundary(tmp_path: Path) -> None:
    cap = Capability.from_mapping({"id": "readonly", "version": "1", "command": [sys.executable, "-c", "pass"]})
    with pytest.raises(ExecutionError) as failure:
        SandboxedExecutor(tmp_path / "scratch").execute(cap, {}, run_id="r", task_id="t")
    assert failure.value.code == "POLICY"


def test_real_manifest_python_placeholder_resolves(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    cap = CapabilityRegistry.load_directory(root / "capabilities").get("bom.probe")
    # Protect only the authority boundary.  Snapshotting the entire live
    # project would race with legitimate concurrent workers and could roll
    # their newly frozen evidence back as if it were a capability side effect.
    runner = SandboxedExecutor(tmp_path / "scratch", protected_roots=(root / "policies",), project_root=root)
    with pytest.raises(ExecutionError) as failure:
        runner.execute(cap, {}, run_id="r", task_id="t")
    assert failure.value.code in {"PROCESS_EXIT", "OUTPUT_PROTOCOL"}


def test_agt_001_and_002_runtime_drift_fails_g0() -> None:
    definition = {"model": "gpt-5.6-sol", "reasoning_effort": "medium", "permissions": ["artifact:read"], "network": "deny"}
    base = {"model": "gpt-5.6-sol", "reasoning_effort": "medium", "tools": ["artifact:read"], "argv": [],
            "sandbox": "read-only", "prompt_hash": "a" * 64,
            "usage": {"input_tokens": 1, "output_tokens": 1, "cost": 0}, "network_used": False}
    gate_runtime(definition, base)
    with pytest.raises(AssuranceError, match="model drift"):
        gate_runtime(definition, {**base, "model": "other"})
    with pytest.raises(AssuranceError, match="network access"):
        gate_runtime(definition, {**base, "network_used": True})


def test_agt_003_identity_swap_is_not_auto_corrected() -> None:
    frozen = {"node_ref": "ict::P042", "node_identity": "server", "spine_hash": "a" * 64}
    with pytest.raises(AssuranceError, match="node_ref"):
        gate_identity(frozen, {**frozen, "node_ref": "ict::P040"})


def test_agt_004_and_005_evidence_requires_literal_exact_alignment() -> None:
    source = "The target device consumes electricity during operation."
    valid = {"verdict": "CONFIRMED", "node_alignment": "EXACT", "excerpt": "consumes electricity", "claim_kind": "external_fact"}
    gate_claim_evidence(valid, source)
    with pytest.raises(AssuranceError, match="EXACT"):
        gate_claim_evidence({**valid, "node_alignment": "ADJACENT"}, source)
    with pytest.raises(AssuranceError, match="literal"):
        gate_claim_evidence({**valid, "excerpt": "运行阶段消耗电力"}, source)


def test_rel_002_target_hash_changes_after_plan(tmp_path: Path) -> None:
    destination = tmp_path / "production"
    destination.mkdir()
    target = destination / "page.md"
    target.write_bytes(b"old")
    manager = ReleaseManager(tmp_path / "releases", required_gates=set())
    staged = manager.stage({"page.md": b"new"}, expected_current={"page.md": hashlib.sha256(b"old").hexdigest()})
    target.write_bytes(b"concurrent edit")
    with pytest.raises(ReleaseError, match="stale release plan"):
        manager.apply(staged, destination)
    assert target.read_bytes() == b"concurrent edit"


def test_rel_003_gate_pass_is_hard_bound_to_candidate(tmp_path: Path) -> None:
    control = ControlPlane(tmp_path)
    authority = ProofAuthority(tmp_path, control.state, control.artifacts, control.events)
    manager = ReleaseManager(tmp_path / "releases", required_gates={"G6", "G7"}, proof_authority=authority)
    candidate = b"candidate"
    wrong = "0" * 64
    digest = hashlib.sha256(candidate).hexdigest()
    subject = manager.subject_for({"page.md": digest})
    gates = [authority.issue_gate(gate_id=gate, input_hashes=[wrong], policy_version="p1", subject=subject)
             for gate in ("G6", "G7")]
    with pytest.raises(ReleaseError, match="stale or foreign"):
        manager.stage({"page.md": candidate}, gate_results=gates)


def test_release_default_cannot_apply_without_gates_or_target_hash(tmp_path: Path) -> None:
    control = ControlPlane(tmp_path)
    authority = ProofAuthority(tmp_path, control.state, control.artifacts, control.events)
    manager = ReleaseManager(tmp_path / "releases", proof_authority=authority)
    staged = manager.stage({"page.md": b"candidate"})
    with pytest.raises(ReleaseError, match="signed gates missing"):
        manager.apply(staged, tmp_path / "production")


def test_raw_fabricated_g0_to_g7_cannot_authorize_release(tmp_path: Path) -> None:
    fabricated = [{"gate_id": f"G{i}", "status": "pass", "input_hashes": ["a" * 64],
                   "policy_version": "forged"} for i in range(8)]
    with pytest.raises(ReleaseError, match="signed Gate Proof Authority"):
        ReleaseManager(tmp_path / "releases").stage({"page.md": b"candidate"}, gate_results=fabricated)


def test_signed_gates_authorize_only_the_exact_release(tmp_path: Path) -> None:
    control = ControlPlane(tmp_path)
    authority = ProofAuthority(tmp_path, control.state, control.artifacts, control.events)
    manager = ReleaseManager(tmp_path / "releases", proof_authority=authority)
    candidate = b"candidate"
    digest = hashlib.sha256(candidate).hexdigest()
    subject = manager.subject_for({"page.md": digest})
    receipts = [authority.issue_gate(gate_id=f"G{i}", input_hashes=[digest], policy_version="p1", subject=subject)
                for i in range(8)]
    staged = manager.stage({"page.md": candidate}, expected_current={"page.md": None}, gate_results=receipts)
    manager.apply(staged, tmp_path / "production")
    assert (tmp_path / "production/page.md").read_bytes() == candidate


def test_proof_receipt_tamper_and_unregistered_receipt_are_rejected(tmp_path: Path) -> None:
    control = ControlPlane(tmp_path)
    authority = ProofAuthority(tmp_path, control.state, control.artifacts, control.events)
    receipt = authority.issue_gate(gate_id="G0", input_hashes=["a" * 64], policy_version="p1", subject="subject")
    with pytest.raises(ProofError, match="signature"):
        authority.verify({**receipt, "signature": "0" * 64}, kind="gate", subject="subject")
    with pytest.raises(ProofError, match="not registered"):
        authority.verify({**receipt, "proof_id": "proof_missing"}, kind="gate", subject="subject")


def test_release_rejects_symlink_parent_escape(tmp_path: Path) -> None:
    destination = tmp_path / "production"
    outside = tmp_path / "outside"
    destination.mkdir(); outside.mkdir()
    (destination / "linked").symlink_to(outside, target_is_directory=True)
    manager = ReleaseManager(tmp_path / "releases", required_gates=set())
    staged = manager.stage({"linked/escape.txt": b"no"}, expected_current={"linked/escape.txt": None})
    with pytest.raises(ReleaseError, match="symlink"):
        manager.apply(staged, destination)
    assert not (outside / "escape.txt").exists()


def test_e2e_reconcile_desired_state_is_idempotent(tmp_path: Path) -> None:
    desired = tmp_path / "desired.json"
    desired.write_text(json.dumps({
        "schema_version": "desired-state-v1",
        "jobs": [{"target": "steel::graph", "workflow": "graph-industry-production",
                  "policy_version": "graph-quality-v1", "input_hashes": ["a" * 64]}],
    }), encoding="utf-8")
    assert main(["--root", str(tmp_path), "reconcile", "--once", "--desired", str(desired)]) == 0
    assert main(["--root", str(tmp_path), "reconcile", "--once", "--desired", str(desired)]) == 0
    from lca_project.control import ControlPlane
    assert ControlPlane(tmp_path).status()["counts"]["jobs"] == 1
