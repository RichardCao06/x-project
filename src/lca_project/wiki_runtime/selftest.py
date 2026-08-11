"""Dependency-free executable smoke test: ``python -m lca_project.wiki_runtime.selftest``."""
from __future__ import annotations

import tempfile
from pathlib import Path
import shutil

from .runtime import STAGES, WikiRuntime, WikiStage
from lca_project.kernel.assurance import prompt_hash


def _envelope(stage: WikiStage, node_id: str, previous: tuple[str, ...] | None = None,
              root: Path | None = None, runtime: WikiRuntime | None = None,
              run_id: str | None = None) -> dict:
    payload = {"node_identity": {"node_id": node_id}, "outputs": [{"stage": str(stage)}]}
    if previous and stage in {WikiStage.RESEARCH_READY, WikiStage.VERIFIED, WikiStage.FROZEN}:
        payload["frozen_input_hash"] = previous[-1]
    if stage is WikiStage.RESEARCH_READY:
        payload["schema_version"] = "wiki-proposal-v1"
    elif stage is WikiStage.VERIFIED:
        payload["schema_version"] = "wiki-verdict-v1"
    elif stage is WikiStage.FROZEN:
        payload["schema_version"] = "wiki-attestation-v1"
    if stage in {WikiStage.RESEARCH_READY, WikiStage.VERIFIED, WikiStage.FROZEN}:
        agent_id = "researcher" if stage is WikiStage.RESEARCH_READY else "reviewer"
        definition = root / "agents" / agent_id / "agent.json"
        payload["agent_id"] = agent_id
        attestation = {"model": "gpt-5.6-terra" if agent_id == "researcher" else "gpt-5.6-sol",
            "reasoning_effort": "medium", "tools": ["artifact:read"], "argv": ["offline"],
            "sandbox": "read-only", "prompt_hash": prompt_hash(definition),
            "usage": {"input_tokens": 1, "output_tokens": 1, "cost": 0}, "network_used": False}
        payload["attestation_receipt"] = runtime.proofs.issue(
            kind="agent-attestation", producer="agent-runtime-launcher",
            subject=runtime.proof_subject(run_id, stage, previous or ()), claims=attestation,
        )
    if stage is WikiStage.DRAFT_GATED:
        payload["verdict"] = "pass"
        payload["gate_receipt"] = runtime.proofs.issue_gate(
            gate_id="draft-content", input_hashes=list(previous or ()), policy_version="test-v1",
            subject=runtime.proof_subject(run_id, stage, previous or ()), producer="draft-content-gate",
        )
    elif stage is WikiStage.PREVIEWED:
        payload.update({"preview": True, "production": False})
    elif stage is WikiStage.RELEASE_GATED:
        payload["verdict"] = "pass"
        payload["gate_receipts"] = [runtime.proofs.issue_gate(
            gate_id=f"G{index}", input_hashes=list(previous or ()), policy_version="test-v1",
            subject=runtime.proof_subject(run_id, stage, previous or ()), producer="release-checker",
        ) for index in range(8)]
    elif stage in {WikiStage.DRAFT_APPLIED, WikiStage.REVIEWED_APPLIED, WikiStage.PUBLISHED}:
        payload["apply_receipt"] = {"target_hash": "a" * 64}
        if stage is WikiStage.REVIEWED_APPLIED:
            payload["apply_receipt"]["expected_current"] = "b" * 64
        if stage is WikiStage.PUBLISHED:
            payload.update({"post_verify": "pass", "release_manifest_hash": "c" * 64})
    return payload


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shutil.copytree(Path(__file__).resolve().parents[3] / "agents", root / "agents")
        runtime, node = WikiRuntime(root), "P-test"
        run = runtime.start(node_id=node, dossier={"node_identity": {"node_id": node}}, policy_version="test-v1")
        previous: tuple[str, ...] | None = None
        for stage in STAGES:
            envelope = _envelope(stage, node, previous, root, runtime, run.run_id)
            actor = "frozen-agent/selftest" if stage in {WikiStage.RESEARCH_READY, WikiStage.VERIFIED, WikiStage.FROZEN} else "deterministic-controller"
            run, previous = runtime.advance(run.run_id, stage, envelope, actor=actor)
        assert run.status == "published" and len(runtime.stage_records(run.run_id)) == len(STAGES)
        assert runtime.resume(run.run_id).status == "published"
    print("wiki-runtime selftest: PASS")


if __name__ == "__main__":
    main()
