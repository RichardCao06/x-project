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

class AlignmentAssessmentMixin:
    def assess_alignment(
        self,
        *,
        job_id: str,
        clause_results: Mapping[str, str],
        prohibited_outcomes: Mapping[str, str],
        capability_match: bool,
        terminal_state: str,
        claimed_complete: bool,
        evidence: Mapping[str, Any] | None = None,
    ) -> AlignmentAssessment:
        binding = self.binding(job_id)
        if binding is None:
            raise KeyError(job_id)
        goal_row = self.contract(binding["goal_ref"])
        assurance_row = self.contract(binding["assurance_ref"])
        if goal_row is None or assurance_row is None:
            raise GovernanceError("bound Goal or Assurance Contract is missing")
        goal = goal_row["payload"]
        assurance = assurance_row["payload"]
        clauses = {str(item["id"]): item for item in goal["clauses"]}
        normalized_results: dict[str, str] = {}
        findings: list[str] = []
        for clause_id, clause in clauses.items():
            raw = clause_results.get(clause_id, ClauseStatus.INSUFFICIENT.value)
            try:
                normalized = ClauseStatus(raw)
            except ValueError as exc:
                raise GovernanceError(f"unsupported clause status for {clause_id}: {raw}") from exc
            normalized_results[clause_id] = normalized.value
            if clause["criticality"] in {"hard", "required"}:
                obligation = assurance["proof_obligations"].get(clause_id)
                if obligation is None:
                    findings.append(f"Assurance Contract has no proof obligation for {clause_id}")

        evidence_payload = dict(evidence or {})
        raw_proofs = evidence_payload.get("proofs", {})
        proofs = raw_proofs if isinstance(raw_proofs, dict) else {}
        proof_findings: list[str] = []
        if raw_proofs and not isinstance(raw_proofs, dict):
            proof_findings.append("evidence.proofs must be an object")
        for clause_id, clause in clauses.items():
            if clause["criticality"] not in {"hard", "required"}:
                continue
            if normalized_results[clause_id] != ClauseStatus.PROVED.value:
                continue
            obligation = assurance["proof_obligations"].get(clause_id)
            proof = proofs.get(clause_id)
            if obligation is None:
                continue
            if not isinstance(proof, dict):
                proof_findings.append(f"proved clause has no proof record: {clause_id}")
                continue
            artifact_ref = proof.get("artifact_ref")
            certificate_hash = proof.get("certificate_hash")
            if not isinstance(artifact_ref, str) or not artifact_ref.strip():
                proof_findings.append(f"proof has no artifact_ref: {clause_id}")
            if (not isinstance(certificate_hash, str) or len(certificate_hash) != 64
                    or any(char not in "0123456789abcdef" for char in certificate_hash)):
                proof_findings.append(f"proof has no valid certificate_hash: {clause_id}")
            if proof.get("evaluator") != obligation["evaluator"]:
                proof_findings.append(f"proof evaluator mismatch: {clause_id}")
            supplied_types = proof.get("evidence_types", [])
            if not isinstance(supplied_types, list):
                supplied_types = []
            missing_types = sorted(set(obligation["evidence_types"]) - set(supplied_types))
            if missing_types:
                proof_findings.append(
                    f"proof evidence types missing for {clause_id}: {', '.join(missing_types)}"
                )
            if obligation["independence_required"]:
                producer = proof.get("producer_actor")
                evaluator = proof.get("evaluator_actor")
                if (not isinstance(producer, str) or not producer.strip()
                        or not isinstance(evaluator, str) or not evaluator.strip()):
                    proof_findings.append(f"proof independence actors missing: {clause_id}")
                elif producer == evaluator:
                    proof_findings.append(f"self-signed proof is forbidden: {clause_id}")
        findings.extend(proof_findings)

        normalized_outcomes: dict[str, str] = {}
        for outcome in goal["prohibited_outcomes"]:
            outcome_id = str(outcome["id"])
            status = prohibited_outcomes.get(outcome_id, "unknown")
            if status not in _ALLOWED_OUTCOME_STATUS:
                raise GovernanceError(f"unsupported prohibited-outcome status for {outcome_id}: {status}")
            normalized_outcomes[outcome_id] = status

        hard = {
            clause_id for clause_id, clause in clauses.items()
            if clause["criticality"] == "hard"
        }
        required = {
            clause_id for clause_id, clause in clauses.items()
            if clause["criticality"] == "required"
        }
        failed_hard = sorted(
            clause_id for clause_id in hard
            if normalized_results[clause_id] == ClauseStatus.FAILED.value
        )
        unresolved_hard = sorted(
            clause_id for clause_id in hard
            if normalized_results[clause_id] in {
                ClauseStatus.INSUFFICIENT.value,
                ClauseStatus.NOT_APPLICABLE.value,
            }
        )
        unresolved_required = sorted(
            clause_id for clause_id in required
            if normalized_results[clause_id] != ClauseStatus.PROVED.value
        )
        present_prohibited = sorted(
            outcome_id for outcome_id, status in normalized_outcomes.items()
            if status == "present"
        )
        unknown_prohibited = sorted(
            outcome_id for outcome_id, status in normalized_outcomes.items()
            if status == "unknown"
        )
        state = goal["terminal_states"].get(terminal_state)
        if state is None:
            findings.append(f"terminal state {terminal_state!r} is not defined by the Goal Contract")
            state_kind = "failure"
        else:
            state_kind = state["kind"]

        if failed_hard:
            findings.append("failed hard clauses: " + ", ".join(failed_hard))
        if unresolved_hard:
            findings.append("unresolved hard clauses: " + ", ".join(unresolved_hard))
        if unresolved_required:
            findings.append("unresolved required clauses: " + ", ".join(unresolved_required))
        if present_prohibited:
            findings.append("prohibited outcomes present: " + ", ".join(present_prohibited))
        if unknown_prohibited:
            findings.append("prohibited outcomes not disproved: " + ", ".join(unknown_prohibited))
        if not capability_match:
            findings.append("execution is outside the certified Capability Envelope")

        proof_gap = bool(proof_findings) or any(
            item.startswith("Assurance Contract has no proof obligation") for item in findings
        )
        success_ready = not (
            failed_hard
            or unresolved_hard
            or unresolved_required
            or present_prohibited
            or unknown_prohibited
            or proof_gap
            or not capability_match
        )
        if failed_hard or present_prohibited:
            verdict = AlignmentVerdict.MISALIGNED
        elif claimed_complete and (state_kind != "success" or not success_ready):
            findings.append("completion was claimed without complete non-compensatory proof")
            verdict = AlignmentVerdict.MISALIGNED
        elif state_kind == "success" and success_ready:
            verdict = AlignmentVerdict.ALIGNED_COMPLETE
        elif state_kind == "honest_incomplete" and not claimed_complete:
            verdict = AlignmentVerdict.ALIGNED_INCOMPLETE
        elif state_kind == "escalation" and not claimed_complete:
            verdict = AlignmentVerdict.HUMAN_JUDGMENT_REQUIRED
        elif unresolved_hard or proof_gap or not capability_match:
            verdict = AlignmentVerdict.HUMAN_JUDGMENT_REQUIRED
        else:
            verdict = AlignmentVerdict.MISALIGNED

        assessment_body = {
            "job_id": job_id,
            "binding_hash": binding["binding_hash"],
            "goal_ref": binding["goal_ref"],
            "assurance_ref": binding["assurance_ref"],
            "verdict": verdict.value,
            "terminal_state": terminal_state,
            "claimed_complete": claimed_complete,
            "clause_results": normalized_results,
            "prohibited_outcomes": normalized_outcomes,
            "capability_match": capability_match,
            "findings": findings,
            "evidence": evidence_payload,
        }
        assessment_hash = payload_digest(assessment_body)
        assessment_id = "ala_" + assessment_hash[:32]
        assessment = AlignmentAssessment(
            assessment_id=assessment_id,
            job_id=job_id,
            binding_hash=binding["binding_hash"],
            verdict=verdict,
            terminal_state=terminal_state,
            claimed_complete=claimed_complete,
            clause_results=normalized_results,
            prohibited_outcomes=normalized_outcomes,
            capability_match=capability_match,
            findings=tuple(findings),
            evidence=evidence_payload,
        )
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO alignment_assessments "
                "(assessment_id,job_id,binding_hash,verdict,terminal_state,assessment_hash,payload,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    assessment.assessment_id,
                    assessment.job_id,
                    assessment.binding_hash,
                    assessment.verdict.value,
                    assessment.terminal_state,
                    assessment_hash,
                    canonical_json(assessment.asdict()),
                    utcnow(),
                ),
            )
        return assessment

    def assessments(self, *, job_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM alignment_assessments"
        params: tuple[Any, ...] = ()
        if job_id is not None:
            query += " WHERE job_id=?"
            params = (job_id,)
        query += " ORDER BY created_at"
        return [_as_row(row) for row in self.state._connection().execute(query, params)]  # type: ignore[list-item]

    def status(self) -> dict[str, Any]:
        conn = self.state._connection()
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "governance_contracts",
                "contract_lifecycle_events",
                "goal_change_proposals",
                "governance_approvals",
                "job_contract_bindings",
                "alignment_assessments",
                "autonomy_eligibility_assessments",
            )
        }
        active = {
            row["contract_kind"]: int(row["count"])
            for row in conn.execute(
                "SELECT contract_kind,COUNT(*) AS count FROM governance_contracts "
                "WHERE status='active' GROUP BY contract_kind"
            )
        }
        return {
            "schema_version": "governance-status-v1",
            "counts": counts,
            "active_contracts": active,
        }
