from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Iterable, Mapping

from lca_project.contracts.governance import (
    AlignmentAssessment, AlignmentVerdict, AutonomyDecision, AutonomyEligibility,
    ClauseStatus, ContractDocument, ContractKind, ContractRef, JobContractBinding,
    canonical_json, payload_digest,
)
from lca_project.kernel.state import StateStore, utcnow
from .governance_support import (
    GovernanceError, _as_row, _decode, _goal_relaxation_indicators,
    _infer_goal_change, _normalize_delta, _requirement_evidence, _scope_matches,
)

class AutonomyEligibilityMixin:
    def check_autonomy(
        self,
        *,
        job_id: str,
        action: str,
        risk: str,
        runtime_fingerprint: Mapping[str, Any],
        input_scope: Mapping[str, Any],
        requested_authority: Iterable[str] = (),
        satisfied_requirements: Iterable[str] = (),
        requirement_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> AutonomyEligibility:
        if risk not in _RISK_ORDER:
            raise GovernanceError(f"unsupported risk: {risk}")
        binding = self.binding(job_id)
        if binding is None:
            raise KeyError(job_id)
        payload = binding["payload"]
        rows = {
            key: self.contract(payload[key])
            for key in ("goal_ref", "autonomy_ref", "assurance_ref", "capability_ref")
        }
        if any(value is None for value in rows.values()):
            raise GovernanceError("a bound contract is missing")
        goal = rows["goal_ref"]["payload"]  # type: ignore[index]
        autonomy = rows["autonomy_ref"]["payload"]  # type: ignore[index]
        assurance = rows["assurance_ref"]["payload"]  # type: ignore[index]
        capability = rows["capability_ref"]["payload"]  # type: ignore[index]
        reasons: list[str] = []
        human_reasons: list[str] = []
        evidence_hashes: dict[str, str] = {}

        for key, row in rows.items():
            if row is not None and row["status"] in {"suspended", "expired"}:
                reasons.append(f"bound {key} is {row['status']}")
        if assurance["goal_contract_ref"] != payload["goal_ref"]:
            reasons.append("Assurance Contract does not match the bound Goal Contract")
        if action in autonomy["forbidden_actions"]:
            reasons.append(f"action {action!r} is forbidden")
        action_policy = autonomy["actions"].get(action)
        if action_policy is None:
            reasons.append(f"action {action!r} is not authorized")
        else:
            if _RISK_ORDER[risk] > _RISK_ORDER[action_policy["max_risk"]]:
                reasons.append(
                    f"risk {risk} exceeds action ceiling {action_policy['max_risk']}"
                )
            if not action_policy["automatic"]:
                human_reasons.append(f"action {action!r} requires approval")
            required = set(action_policy.get("requirements", []))
            evidence_satisfied, evidence_hashes, evidence_findings = _requirement_evidence(
                required, requirement_evidence
            )
            reasons.extend(evidence_findings)
            satisfied = {
                item
                for item in satisfied_requirements
                if item != "alignment_assessment" and item not in _SECURITY_REQUIREMENTS
            }
            satisfied.update(evidence_satisfied)
            if "alignment_assessment" in required:
                latest = self.state._connection().execute(
                    "SELECT verdict FROM alignment_assessments "
                    "WHERE job_id=? AND binding_hash=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (job_id, binding["binding_hash"]),
                ).fetchone()
                if (
                    latest is not None
                    and latest["verdict"] == AlignmentVerdict.ALIGNED_COMPLETE.value
                ):
                    satisfied.add("alignment_assessment")
            missing_requirements = sorted(required - satisfied)
            if missing_requirements:
                reasons.append(
                    "unsatisfied action requirements: " + ", ".join(missing_requirements)
                )

        reserved = set(goal["reserved_authority"]) | set(autonomy["reserved_authority"])
        protected = sorted(set(requested_authority) & reserved)
        if protected:
            human_reasons.append("reserved authority requested: " + ", ".join(protected))

        expected_fingerprint = capability["runtime_fingerprint"]
        for key, expected in expected_fingerprint.items():
            if runtime_fingerprint.get(key) != expected:
                reasons.append(
                    f"runtime fingerprint mismatch for {key}: "
                    f"expected {expected!r}, got {runtime_fingerprint.get(key)!r}"
                )
        scope_ok, scope_findings = _scope_matches(capability["input_scope"], dict(input_scope))
        if not scope_ok:
            reasons.extend(scope_findings)

        certification = capability["certification"]
        certification_status = certification["status"]
        if certification_status != "certified":
            reasons.append(
                "Capability Envelope is not certified for autonomous action: "
                f"{certification_status}"
            )
        ceiling = float(goal["evaluation"]["selective_risk_ceiling"])
        observed = float(certification["selective_risk_upper_bound"])
        if observed > ceiling:
            reasons.append(
                f"certified selective risk {observed:.6f} exceeds Goal ceiling {ceiling:.6f}"
            )
        valid_until = certification.get("valid_until")
        if valid_until:
            expiry = datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                reasons.append("Capability Envelope certification has expired")

        contract_hashes = {
            key: rows[key]["contract_hash"]  # type: ignore[index]
            for key in rows
        }
        if reasons:
            decision = AutonomyDecision.BLOCKED
            all_reasons = tuple(reasons + human_reasons)
        elif human_reasons:
            decision = AutonomyDecision.HUMAN_APPROVAL_REQUIRED
            all_reasons = tuple(human_reasons)
        else:
            decision = AutonomyDecision.AUTHORIZED
            all_reasons = ()
        eligibility_body = {
            "job_id": job_id,
            "action": action,
            "risk": risk,
            "binding_hash": binding["binding_hash"],
            "decision": decision.value,
            "reasons": list(all_reasons),
            "contract_hashes": contract_hashes,
            "requirement_evidence_hashes": evidence_hashes,
            "runtime_fingerprint": dict(runtime_fingerprint),
            "input_scope": dict(input_scope),
            "requested_authority": sorted(set(requested_authority)),
        }
        eligibility_hash = payload_digest(eligibility_body)
        eligibility = AutonomyEligibility(
            eligibility_id="ael_" + eligibility_hash[:32],
            decision=decision,
            action=action,
            risk=risk,
            binding_hash=binding["binding_hash"],
            reasons=all_reasons,
            contract_hashes=contract_hashes,
            requirement_evidence_hashes=evidence_hashes,
        )
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO autonomy_eligibility_assessments "
                "(eligibility_id,job_id,binding_hash,action,decision,eligibility_hash,payload,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    eligibility.eligibility_id,
                    job_id,
                    binding["binding_hash"],
                    action,
                    decision.value,
                    eligibility_hash,
                    canonical_json({**eligibility.asdict(), "inputs": eligibility_body}),
                    utcnow(),
                ),
            )
        return eligibility
