from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from typing import Any, Iterable, Mapping

from lca_project.contracts.governance import (
    ContractKind,
    ContractRef,
    canonical_json,
    payload_digest,
)
from lca_project.kernel.state import utcnow

from .governance_support import GovernanceError, _as_row


_COHORT_OUTCOMES = {"correct", "incorrect", "abstained"}


def _wilson_upper(errors: int, trials: int, *, z: float = 1.6448536269514722) -> float:
    """One-sided 95% Wilson upper confidence bound for a binomial rate."""
    if trials <= 0:
        return 1.0
    observed = errors / trials
    z2 = z * z
    denominator = 1.0 + z2 / trials
    center = (observed + z2 / (2.0 * trials)) / denominator
    spread = z * math.sqrt(
        observed * (1.0 - observed) / trials + z2 / (4.0 * trials * trials)
    ) / denominator
    return min(1.0, center + spread)


class CapabilityAssuranceMixin:
    """Dependency invalidation, Cohort certification, and online drift facts."""

    @staticmethod
    def _queue_reassessment(
        conn,
        *,
        trigger_kind: str,
        trigger_ref: str,
        subject_kind: str,
        subject_ref: str,
        reason: str,
        created_at: str,
        job_id: str | None = None,
        binding_hash: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> str:
        body = {
            "schema_version": "governance-reassessment-v1",
            "trigger_kind": trigger_kind,
            "trigger_ref": trigger_ref,
            "subject_kind": subject_kind,
            "subject_ref": subject_ref,
            "job_id": job_id,
            "binding_hash": binding_hash,
            "reason": reason,
            "details": dict(details or {}),
        }
        reassessment_id = "gra_" + payload_digest(body)[:32]
        conn.execute(
            "INSERT OR IGNORE INTO governance_reassessments "
            "(reassessment_id,trigger_kind,trigger_ref,subject_kind,subject_ref,job_id,"
            "binding_hash,status,reason,payload,created_at,resolved_at) "
            "VALUES(?,?,?,?,?,?,?,'pending',?,?,?,NULL)",
            (
                reassessment_id,
                trigger_kind,
                trigger_ref,
                subject_kind,
                subject_ref,
                job_id,
                binding_hash,
                reason,
                canonical_json({**body, "reassessment_id": reassessment_id}),
                created_at,
            ),
        )
        return reassessment_id

    def _queue_goal_reassessments(
        self, conn, *, proposal: Mapping[str, Any], created_at: str
    ) -> None:
        """Propagate a Goal activation without changing running Job semantics."""
        from_ref = str(proposal["from_ref"])
        to_ref = str(proposal["to_ref"])
        proposal_id = str(proposal["proposal_id"])
        reason = f"Goal activation {from_ref} -> {to_ref} requires reassessment"
        details = {"from_ref": from_ref, "to_ref": to_ref}

        bindings = conn.execute(
            "SELECT job_id,binding_hash FROM job_contract_bindings WHERE goal_ref=?",
            (from_ref,),
        ).fetchall()
        for binding in bindings:
            self._queue_reassessment(
                conn,
                trigger_kind="goal_change",
                trigger_ref=proposal_id,
                subject_kind="job_eligibility",
                subject_ref=str(binding["job_id"]),
                job_id=str(binding["job_id"]),
                binding_hash=str(binding["binding_hash"]),
                reason=reason,
                details=details,
                created_at=created_at,
            )
        assessments = conn.execute(
            "SELECT a.assessment_id,a.job_id,a.binding_hash "
            "FROM alignment_assessments a JOIN job_contract_bindings b ON b.job_id=a.job_id "
            "WHERE b.goal_ref=?",
            (from_ref,),
        ).fetchall()
        for assessment in assessments:
            self._queue_reassessment(
                conn,
                trigger_kind="goal_change",
                trigger_ref=proposal_id,
                subject_kind="artifact_maturity",
                subject_ref=str(assessment["assessment_id"]),
                job_id=str(assessment["job_id"]),
                binding_hash=str(assessment["binding_hash"]),
                reason=reason,
                details=details,
                created_at=created_at,
            )

        contracts = conn.execute(
            "SELECT contract_ref,contract_kind,payload FROM governance_contracts "
            "WHERE status='active' AND contract_kind IN ('autonomy','assurance','capability')"
        ).fetchall()
        source_goal = self.contract(from_ref)
        goal_domain = None if source_goal is None else source_goal["payload"].get("scope", {}).get("domain")
        for contract in contracts:
            payload = json.loads(str(contract["payload"]))
            kind = str(contract["contract_kind"])
            contract_ref = str(contract["contract_ref"])
            dependent = (
                payload.get("goal_contract_ref") == from_ref
                or payload.get("scope", {}).get("goal_contract_ref") == from_ref
            )
            if dependent:
                self._queue_reassessment(
                    conn,
                    trigger_kind="goal_change",
                    trigger_ref=proposal_id,
                    subject_kind="contract_recompile",
                    subject_ref=contract_ref,
                    reason=reason,
                    details=details,
                    created_at=created_at,
                )
            if kind == "capability" and payload.get("scope", {}).get("domain") == goal_domain:
                # Qualify the new Goal in the subject so the old, immutable Job
                # binding remains executable under its frozen Goal semantics.
                self._queue_reassessment(
                    conn,
                    trigger_kind="goal_change",
                    trigger_ref=proposal_id,
                    subject_kind="capability_recertification",
                    subject_ref=f"{contract_ref}#goal={to_ref}",
                    reason=reason,
                    details=details,
                    created_at=created_at,
                )

    def reassessments(
        self, *, status: str | None = None, subject_kind: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if subject_kind is not None:
            clauses.append("subject_kind=?")
            params.append(subject_kind)
        query = "SELECT * FROM governance_reassessments"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at,reassessment_id"
        return [
            _as_row(row)  # type: ignore[misc]
            for row in self.state._connection().execute(query, tuple(params))
        ]

    def resolve_reassessment(
        self,
        reassessment_id: str,
        *,
        actor: str,
        actor_role: str,
        disposition: str,
        evidence: Iterable[str],
    ) -> dict[str, Any]:
        if actor_role not in {"human_goal_owner", "human_governance_owner"}:
            raise GovernanceError("reassessment resolution requires a human governance owner")
        if disposition not in {"recompiled", "recertified", "reassessed", "retired"}:
            raise GovernanceError("unsupported reassessment disposition")
        evidence_values = sorted({str(item) for item in evidence if str(item).strip()})
        if not evidence_values:
            raise GovernanceError("reassessment resolution requires evidence")
        row = self.state._connection().execute(
            "SELECT * FROM governance_reassessments WHERE reassessment_id=?",
            (reassessment_id,),
        ).fetchone()
        result = _as_row(row)
        if result is None:
            raise KeyError(reassessment_id)
        if result["status"] == "resolved":
            return result
        now = utcnow()
        payload = dict(result["payload"])
        payload["resolution"] = {
            "actor": actor,
            "actor_role": actor_role,
            "disposition": disposition,
            "evidence": evidence_values,
            "resolved_at": now,
        }
        with self.state.transaction() as conn:
            changed = conn.execute(
                "UPDATE governance_reassessments SET status='resolved',payload=?,resolved_at=? "
                "WHERE reassessment_id=? AND status='pending'",
                (canonical_json(payload), now, reassessment_id),
            ).rowcount
            if changed != 1:
                raise GovernanceError("reassessment resolution lost its pending precondition")
        resolved = _as_row(self.state._connection().execute(
            "SELECT * FROM governance_reassessments WHERE reassessment_id=?",
            (reassessment_id,),
        ).fetchone())
        assert resolved is not None
        return resolved

    def certify_capability(
        self,
        *,
        from_ref: str,
        target_version: str,
        cohort_id: str,
        cases: Iterable[Mapping[str, Any]],
        evaluator_actor: str,
        authorizer_actor: str,
        authorizer_role: str,
        valid_until: str,
        thresholds: Mapping[str, float | int] | None = None,
    ) -> dict[str, Any]:
        """Compute and sign a representative Cohort report before replacement."""
        parsed = ContractRef.parse(from_ref)
        if parsed.kind is not ContractKind.CAPABILITY:
            raise GovernanceError("capability certification requires a Capability Envelope")
        if authorizer_role != "human_governance_owner":
            raise GovernanceError("capability certification requires a human_governance_owner")
        if not evaluator_actor.strip() or evaluator_actor == authorizer_actor:
            raise GovernanceError("candidate evaluation and authorization must be independent")
        if getattr(self, "proof_authority", None) is None:
            raise GovernanceError("capability certification requires the trusted Proof Authority")
        try:
            expiry = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GovernanceError("valid_until must be ISO-8601") from exc
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc):
            raise GovernanceError("capability certification cannot already be expired")
        source = self.contract(from_ref, status="active")
        if source is None:
            raise KeyError(from_ref)
        normalized: list[dict[str, Any]] = []
        case_ids: set[str] = set()
        for index, raw in enumerate(cases):
            case_id = str(raw.get("case_id") or "").strip()
            outcome = str(raw.get("outcome") or "").strip()
            should_abstain = raw.get("should_abstain")
            if not case_id or case_id in case_ids:
                raise GovernanceError(f"Cohort case {index} has a missing or duplicate case_id")
            if outcome not in _COHORT_OUTCOMES or not isinstance(should_abstain, bool):
                raise GovernanceError(f"Cohort case {case_id} has an invalid outcome")
            case_ids.add(case_id)
            normalized.append({
                "case_id": case_id,
                "outcome": outcome,
                "should_abstain": should_abstain,
                "stratum": str(raw.get("stratum") or "default"),
            })
        if not normalized:
            raise GovernanceError("capability certification requires a non-empty Cohort")
        completed = [item for item in normalized if item["outcome"] != "abstained"]
        errors = sum(item["outcome"] == "incorrect" for item in completed)
        abstention_cases = [item for item in normalized if item["should_abstain"]]
        correctly_abstained = sum(
            item["outcome"] == "abstained" for item in abstention_cases
        )
        metrics = {
            "sample_size": len(normalized),
            "coverage": len(completed) / len(normalized),
            "selective_risk": errors / len(completed) if completed else 1.0,
            "selective_risk_upper_bound": _wilson_upper(errors, len(completed)),
            "abstention_recall": (
                correctly_abstained / len(abstention_cases) if abstention_cases else 0.0
            ),
        }
        limits = {
            "min_sample_size": 30,
            "min_coverage": 0.5,
            "max_selective_risk_upper_bound": 0.05,
            "min_abstention_recall": 0.9,
            **dict(thresholds or {}),
        }
        minimum_size = limits["min_sample_size"]
        if (
            isinstance(minimum_size, bool)
            or not isinstance(minimum_size, int)
            or minimum_size < 1
        ):
            raise GovernanceError("min_sample_size must be a positive integer")
        for key in (
            "min_coverage",
            "max_selective_risk_upper_bound",
            "min_abstention_recall",
        ):
            value = limits[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise GovernanceError(f"{key} must be a ratio between zero and one")
        findings: list[str] = []
        if metrics["sample_size"] < int(limits["min_sample_size"]):
            findings.append("Cohort sample size is below the certification minimum")
        if metrics["coverage"] < float(limits["min_coverage"]):
            findings.append("Cohort coverage is below the certification minimum")
        if metrics["selective_risk_upper_bound"] > float(
            limits["max_selective_risk_upper_bound"]
        ):
            findings.append("selective-risk confidence bound exceeds the certification ceiling")
        if metrics["abstention_recall"] < float(limits["min_abstention_recall"]):
            findings.append("abstention recall is below the certification minimum")
        report_body = {
            "schema_version": "capability-certification-report-v1",
            "capability_ref": from_ref,
            "target_version": target_version,
            "cohort_id": cohort_id,
            "cohort_hash": payload_digest(normalized),
            "metrics": metrics,
            "thresholds": limits,
            "evaluator_actor": evaluator_actor,
            "authorizer_actor": authorizer_actor,
            "findings": findings,
            "verdict": "certified" if not findings else "rejected",
            "valid_until": valid_until,
        }
        certification_id = "ccr_" + payload_digest(report_body)[:32]
        report = self.sign_evidence_record(
            {**report_body, "certification_id": certification_id},
            subject=f"capability-certification:{certification_id}",
            producer="governance-evaluator",
        )
        report_hash = payload_digest(report)
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO capability_certifications "
                "(certification_id,capability_ref,cohort_id,status,report_hash,payload,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    certification_id,
                    from_ref,
                    cohort_id,
                    report_body["verdict"],
                    report_hash,
                    canonical_json(report),
                    utcnow(),
                ),
            )
        if findings:
            return report
        target = deepcopy(source["payload"])
        target["version"] = target_version
        receipt = report["authority_receipt"]
        target["certification"] = {
            "status": "certified",
            "cohort_id": cohort_id,
            "evidence_refs": [
                f"cohort://{cohort_id}",
                f"proof://{receipt['proof_id']}",
            ],
            "sample_size": metrics["sample_size"],
            "coverage": metrics["coverage"],
            "selective_risk_upper_bound": metrics["selective_risk_upper_bound"],
            "abstention_recall": metrics["abstention_recall"],
            "valid_until": valid_until,
        }
        replacement = self.replace_active_contract(
            from_ref=from_ref,
            target_payload=target,
            actor=authorizer_actor,
            actor_role=authorizer_role,
            rationale=f"independent Cohort certification {certification_id}",
            evidence=target["certification"]["evidence_refs"],
        )
        return {**report, "certified_contract_ref": replacement["contract_ref"]}

    def record_capability_observation(
        self,
        *,
        capability_ref: str,
        case_id: str,
        outcome: str,
        should_abstain: bool,
        actor: str,
    ) -> dict[str, Any]:
        """Record an online outcome and invalidate autonomy on detected drift."""
        contract = self.contract(capability_ref)
        if contract is None or contract["contract_kind"] != "capability":
            raise GovernanceError("online observation requires a Capability Envelope")
        if outcome not in _COHORT_OUTCOMES or not case_id.strip():
            raise GovernanceError("online observation has an invalid case or outcome")
        body = {
            "schema_version": "capability-observation-v1",
            "capability_ref": capability_ref,
            "case_id": case_id,
            "outcome": outcome,
            "should_abstain": should_abstain,
            "actor": actor,
        }
        observation_id = "cob_" + payload_digest(body)[:32]
        existing = _as_row(self.state._connection().execute(
            "SELECT * FROM capability_observations "
            "WHERE capability_ref=? AND case_id=?",
            (capability_ref, case_id),
        ).fetchone())
        if existing is not None:
            payload = existing["payload"]
            if (
                payload.get("outcome") != outcome
                or payload.get("should_abstain") != should_abstain
                or payload.get("actor") != actor
            ):
                raise GovernanceError(
                    f"immutable capability observation drift: {capability_ref} {case_id}"
                )
            return {
                **existing,
                "drift_detected": self.capability_has_pending_invalidation(capability_ref),
            }
        now = utcnow()
        drift_reason = None
        if outcome == "incorrect":
            drift_reason = "new false pass invalidated capability certification"
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO capability_observations "
                "(observation_id,capability_ref,case_id,outcome,should_abstain,payload,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    observation_id,
                    capability_ref,
                    case_id,
                    outcome,
                    int(should_abstain),
                    canonical_json({**body, "observation_id": observation_id}),
                    now,
                ),
            )
            if drift_reason:
                self._queue_reassessment(
                    conn,
                    trigger_kind="online_observation",
                    trigger_ref=observation_id,
                    subject_kind="capability_recertification",
                    subject_ref=capability_ref,
                    reason=drift_reason,
                    details={"case_id": case_id, "outcome": outcome},
                    created_at=now,
                )
        row = _as_row(self.state._connection().execute(
            "SELECT * FROM capability_observations WHERE observation_id=?",
            (observation_id,),
        ).fetchone())
        assert row is not None
        return {**row, "drift_detected": drift_reason is not None}

    def capability_has_pending_invalidation(self, capability_ref: str) -> bool:
        return self.state._connection().execute(
            "SELECT 1 FROM governance_reassessments "
            "WHERE status='pending' AND subject_kind='capability_recertification' "
            "AND subject_ref=? LIMIT 1",
            (capability_ref,),
        ).fetchone() is not None

    def readiness(self) -> dict[str, Any]:
        conn = self.state._connection()
        active = {
            str(row["contract_kind"]): int(row["count"])
            for row in conn.execute(
                "SELECT contract_kind,COUNT(*) AS count FROM governance_contracts "
                "WHERE status='active' GROUP BY contract_kind"
            )
        }
        pending = int(conn.execute(
            "SELECT COUNT(*) FROM governance_reassessments WHERE status='pending'"
        ).fetchone()[0])
        certified = int(conn.execute(
            "SELECT COUNT(*) FROM governance_contracts "
            "WHERE contract_kind='capability' AND status='active' "
            "AND json_extract(payload,'$.certification.status')='certified'"
        ).fetchone()[0])
        bindings = int(conn.execute("SELECT COUNT(*) FROM job_contract_bindings").fetchone()[0])
        checks = {
            "active_contracts_available": all(active.get(kind, 0) > 0 for kind in (
                "goal", "autonomy", "assurance", "capability"
            )),
            "certified_capability_available": certified > 0,
            "job_binding_available": bindings > 0,
            "no_pending_reassessments": pending == 0,
        }
        return {
            "schema_version": "governance-readiness-v1",
            "ready": all(checks.values()),
            "checks": checks,
            "pending_reassessments": pending,
        }
