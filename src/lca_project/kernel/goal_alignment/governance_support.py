"""Governed evolution of Goal, Autonomy, Assurance, and Capability contracts.

This module implements the first v2 governance slice while keeping the existing
v1 Goal Alignment controller intact.  Contracts are immutable by version; Jobs
bind to exact versions; Goal changes are amendments rather than in-place edits;
and alignment is evaluated with non-compensatory hard constraints.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from typing import Any, Iterable, Mapping

from lca_project.contracts.governance import (
    AlignmentAssessment,
    AlignmentVerdict,
    AutonomyDecision,
    AutonomyEligibility,
    ClauseStatus,
    ContractDocument,
    ContractKind,
    ContractRef,
    JobContractBinding,
    canonical_json,
    payload_digest,
)
from lca_project.kernel.state import StateStore, utcnow


_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_ALLOWED_OUTCOME_STATUS = {"absent", "present", "unknown"}
_SECURITY_REQUIREMENTS = {
    "independent_evaluator",
    "immutable_evidence",
    "proof_contract",
    "release_attestation",
    "rollback",
}


class GovernanceError(ValueError):
    """Raised when a governance invariant would be violated."""


def _decode(payload: str) -> dict[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise GovernanceError("stored governance payload is not an object")
    return value


def _as_row(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    if "payload" in value:
        value["payload"] = _decode(value["payload"])
    return value


def _clause_signature(goal: Mapping[str, Any]) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(sorted(
        (
            str(item["id"]),
            str(item["criticality"]),
            str(item["statement"]),
            str(item["proof_obligation"]),
        )
        for item in goal["clauses"]
    ))


def _critical_clause_signature(
    goal: Mapping[str, Any], criticality: str
) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted(
        (str(item["id"]), str(item["statement"]), str(item["proof_obligation"]))
        for item in goal["clauses"]
        if item["criticality"] == criticality
    ))


def _prohibited_signature(goal: Mapping[str, Any]) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted(
        (str(item["id"]), str(item["statement"]), str(item["detection"]))
        for item in goal["prohibited_outcomes"]
    ))


def _terminal_signature(goal: Mapping[str, Any]) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted(
        (str(name), str(item["kind"]), str(item["meaning"]))
        for name, item in goal["terminal_states"].items()
    ))


def _evaluation_signature(goal: Mapping[str, Any]) -> tuple[Any, ...]:
    evaluation = goal["evaluation"]
    return (
        tuple(sorted(str(item) for item in evaluation["cohort_refs"])),
        float(evaluation["selective_risk_ceiling"]),
        float(evaluation["false_pass_budget"]),
    )


def _normalize_delta(delta: Mapping[str, Any]) -> dict[str, list[str]]:
    expected = ("newly_allowed", "newly_blocked", "unchanged_samples", "unknown")
    result: dict[str, list[str]] = {}
    for key in expected:
        raw = delta.get(key, [])
        if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
            raise GovernanceError(f"acceptance_delta.{key} must be an array of non-empty strings")
        result[key] = sorted(set(raw))
    if not any(result.values()):
        raise GovernanceError("acceptance_delta must include evaluated samples or explicit unknowns")
    return result


def _goal_relaxation_indicators(
    current: Mapping[str, Any], target: Mapping[str, Any]
) -> tuple[str, ...]:
    """Detect obvious widening independently of an Agent-supplied cohort diff."""
    findings: list[str] = []
    rank = {"hard": 3, "required": 2, "optimize": 1, "diagnostic": 0}
    old_clauses = {str(item["id"]): item for item in current["clauses"]}
    new_clauses = {str(item["id"]): item for item in target["clauses"]}
    for clause_id, old in old_clauses.items():
        if old["criticality"] not in {"hard", "required"}:
            continue
        new = new_clauses.get(clause_id)
        if new is None:
            findings.append(f"required clause removed: {clause_id}")
        elif rank[new["criticality"]] < rank[old["criticality"]]:
            findings.append(
                f"clause criticality downgraded: {clause_id} "
                f"{old['criticality']}->{new['criticality']}"
            )

    old_prohibited = {str(item["id"]) for item in current["prohibited_outcomes"]}
    new_prohibited = {str(item["id"]) for item in target["prohibited_outcomes"]}
    for outcome_id in sorted(old_prohibited - new_prohibited):
        findings.append(f"prohibited outcome removed: {outcome_id}")

    old_success = {name for name, item in current["terminal_states"].items()
                   if item["kind"] == "success"}
    new_success = {name for name, item in target["terminal_states"].items()
                   if item["kind"] == "success"}
    for state in sorted(new_success - old_success):
        findings.append(f"new success terminal state: {state}")

    old_eval = current["evaluation"]
    new_eval = target["evaluation"]
    if float(new_eval["selective_risk_ceiling"]) > float(old_eval["selective_risk_ceiling"]):
        findings.append("selective-risk ceiling increased")
    if float(new_eval["false_pass_budget"]) > float(old_eval["false_pass_budget"]):
        findings.append("false-pass budget increased")
    return tuple(findings)


def _infer_goal_change(
    current: Mapping[str, Any],
    target: Mapping[str, Any],
    acceptance_delta: Mapping[str, list[str]],
) -> tuple[str, str, tuple[str, ...]]:
    findings: list[str] = []
    purpose_changed = current["purpose"] != target["purpose"]
    scope_changed = current["scope"] != target["scope"]
    authority_changed = sorted(current["reserved_authority"]) != sorted(
        target["reserved_authority"]
    )
    clauses_changed = _clause_signature(current) != _clause_signature(target)
    hard_changed = _critical_clause_signature(
        current, "hard"
    ) != _critical_clause_signature(target, "hard")
    required_changed = _critical_clause_signature(
        current, "required"
    ) != _critical_clause_signature(target, "required")
    prohibited_changed = _prohibited_signature(current) != _prohibited_signature(target)
    terminal_changed = _terminal_signature(current) != _terminal_signature(target)
    evaluation_changed = _evaluation_signature(current) != _evaluation_signature(target)
    static_relaxation = _goal_relaxation_indicators(current, target)

    if purpose_changed:
        findings.append("purpose changed")
    if scope_changed:
        findings.append("scope changed")
    if authority_changed:
        findings.append("reserved authority changed")
    if clauses_changed:
        findings.append("Goal clauses changed")
    if hard_changed:
        findings.append("hard clauses changed")
    if required_changed:
        findings.append("required clauses changed")
    if prohibited_changed:
        findings.append("prohibited-outcome semantics changed")
    if terminal_changed:
        findings.append("terminal-state semantics changed")
    if evaluation_changed:
        findings.append("evaluation cohort or risk budget changed")
    findings.extend(static_relaxation)

    if purpose_changed or scope_changed:
        return "goal_redirection", "critical", tuple(findings)
    if authority_changed:
        return "authority_change", "critical", tuple(findings)
    if static_relaxation or acceptance_delta["newly_allowed"]:
        if acceptance_delta["newly_allowed"]:
            findings.append("previously rejected samples become acceptable")
        return "goal_relaxation", "critical", tuple(findings)
    if acceptance_delta["unknown"]:
        findings.append("acceptance-set effect remains uncertain")
        return "semantic_refinement", "high", tuple(findings)
    semantic_changed = any((
        clauses_changed,
        prohibited_changed,
        terminal_changed,
        evaluation_changed,
    ))
    if acceptance_delta["newly_blocked"]:
        findings.append("acceptance set is narrowed")
        return "goal_tightening", "high" if semantic_changed else "medium", tuple(findings)
    if semantic_changed:
        return "semantic_refinement", "high", tuple(findings)
    return "structural_refactor", "low", tuple(findings)


def _scope_matches(allowed: Any, actual: Any, path: str = "input_scope") -> tuple[bool, list[str]]:
    findings: list[str] = []
    if isinstance(allowed, dict):
        if not isinstance(actual, dict):
            return False, [f"{path} must be an object"]
        for key, allowed_value in allowed.items():
            if key not in actual:
                findings.append(f"{path}.{key} is missing")
                continue
            matched, nested = _scope_matches(allowed_value, actual[key], f"{path}.{key}")
            if not matched:
                findings.extend(nested)
        return not findings, findings
    if isinstance(allowed, list):
        actual_values = actual if isinstance(actual, list) else [actual]
        if not actual_values:
            return False, [f"{path} must contain at least one certified value"]
        invalid = [item for item in actual_values if item not in allowed]
        if invalid:
            return False, [f"{path} values {invalid!r} are outside the certified scope"]
        return True, []
    if allowed == "*":
        return True, []
    if allowed != actual:
        return False, [f"{path}={actual!r} does not match certified value {allowed!r}"]
    return True, []


def _requirement_evidence(
    required: set[str], evidence: Mapping[str, Mapping[str, Any]] | None
) -> tuple[set[str], dict[str, str], list[str]]:
    satisfied: set[str] = set()
    hashes: dict[str, str] = {}
    findings: list[str] = []
    supplied = dict(evidence or {})
    for requirement in sorted(required & _SECURITY_REQUIREMENTS):
        record = supplied.get(requirement)
        if not isinstance(record, Mapping):
            continue
        artifact_ref = record.get("artifact_ref")
        certificate_hash = record.get("certificate_hash")
        if not isinstance(artifact_ref, str) or not artifact_ref.strip():
            findings.append(f"requirement evidence has no artifact_ref: {requirement}")
            continue
        if (
            not isinstance(certificate_hash, str)
            or len(certificate_hash) != 64
            or any(char not in "0123456789abcdef" for char in certificate_hash)
        ):
            findings.append(
                f"requirement evidence has no valid certificate_hash: {requirement}"
            )
            continue
        issuer_actor = record.get("issuer_actor")
        if not isinstance(issuer_actor, str) or not issuer_actor.strip():
            findings.append(f"requirement evidence has no issuer_actor: {requirement}")
            continue
        embedded_payload = record.get("payload")
        if embedded_payload is not None:
            if not isinstance(embedded_payload, Mapping):
                findings.append(f"requirement evidence payload is not an object: {requirement}")
                continue
            if payload_digest(dict(embedded_payload)) != certificate_hash:
                findings.append(f"requirement evidence payload hash mismatch: {requirement}")
                continue
        normalized = json.loads(canonical_json(dict(record)))
        satisfied.add(requirement)
        hashes[requirement] = payload_digest(normalized)
    return satisfied, hashes, findings
