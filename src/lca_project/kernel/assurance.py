"""Deterministic gates around non-deterministic agent output."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class AssuranceError(ValueError):
    def __init__(self, gate: str, message: str) -> None:
        super().__init__(f"{gate}: {message}")
        self.gate = gate


def prompt_hash(agent_file: str | Path) -> str:
    definition_path = Path(agent_file)
    definition = json.loads(definition_path.read_text(encoding="utf-8"))
    prompt = (definition_path.parent / definition["prompt"]).read_bytes()
    return hashlib.sha256(prompt).hexdigest()


def gate_runtime(agent_definition: dict[str, Any], attestation: dict[str, Any]) -> None:
    """G0: reject model/tool/permission drift before output is consumed."""
    required = {"model", "reasoning_effort", "tools", "argv", "sandbox", "prompt_hash", "usage"}
    missing = required - attestation.keys()
    if missing:
        raise AssuranceError("G0", f"attestation missing {sorted(missing)}")
    for key in ("model", "reasoning_effort"):
        if attestation[key] != agent_definition.get(key):
            raise AssuranceError("G0", f"{key} drift")
    allowed = set(agent_definition.get("permissions", ()))
    if set(attestation["tools"]) - allowed:
        raise AssuranceError("G0", "undeclared tool used")
    if agent_definition.get("network", "deny") == "deny" and attestation.get("network_used"):
        raise AssuranceError("G0", "network access denied")
    usage = attestation["usage"]
    if not isinstance(usage, dict) or not {"input_tokens", "output_tokens", "cost"} <= usage.keys():
        raise AssuranceError("G0", "usage proof incomplete")


def gate_identity(frozen: dict[str, str], output: dict[str, Any]) -> None:
    """G1: node ID, identity and spine hash are an uncorrectable hard join."""
    for key in ("node_ref", "node_identity", "spine_hash"):
        if not frozen.get(key) or output.get(key) != frozen[key]:
            raise AssuranceError("G1", f"identity join failed for {key}")


def gate_claim_evidence(claim: dict[str, Any], source_payload: str) -> None:
    """G4: only exact, literal evidence may confirm a target-node claim."""
    if claim.get("verdict") == "CONFIRMED" and claim.get("node_alignment") != "EXACT":
        raise AssuranceError("G4", "CONFIRMED requires EXACT node alignment")
    excerpt = claim.get("excerpt", "")
    if not excerpt or excerpt not in source_payload:
        raise AssuranceError("G4", "excerpt is not a literal payload substring")
    if claim.get("claim_kind") not in {"external_fact", "graph_fact", "modeling_judgment"}:
        raise AssuranceError("G4", "claim_kind missing or invalid")


def gate_release_binding(candidate_hashes: set[str], gate_results: list[dict[str, Any]], required: set[str]) -> None:
    """G7: every required PASS must be policy- and candidate-bound."""
    passed: set[str] = set()
    for result in gate_results:
        if result.get("status") != "pass":
            continue
        if set(result.get("input_hashes", ())) != candidate_hashes:
            raise AssuranceError("G7", f"stale or foreign gate result: {result.get('gate_id')}")
        if not result.get("policy_version"):
            raise AssuranceError("G7", "gate policy version missing")
        passed.add(str(result.get("gate_id")))
    missing = required - passed
    if missing:
        raise AssuranceError("G7", f"required gates missing: {sorted(missing)}")

