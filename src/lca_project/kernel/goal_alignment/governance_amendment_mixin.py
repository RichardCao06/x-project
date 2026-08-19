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

class GoalAmendmentMixin:
    def propose_goal_change(
        self,
        *,
        from_ref: str,
        target_payload: Mapping[str, Any],
        acceptance_delta: Mapping[str, Any],
        rationale: str,
        evidence: Iterable[str],
        proposed_by: str,
        migration_plan: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not rationale.strip():
            raise GovernanceError("Goal change proposal requires a rationale")
        if not proposed_by.strip():
            raise GovernanceError("Goal change proposal requires proposed_by")
        current_ref = ContractRef.parse(from_ref)
        if current_ref.kind is not ContractKind.GOAL:
            raise GovernanceError("Goal amendments can only originate from a Goal Contract")
        current_row = self.contract(from_ref, status="active")
        if current_row is None:
            raise KeyError(from_ref)
        target = ContractDocument.from_mapping(target_payload)
        if target.ref.kind is not ContractKind.GOAL:
            raise GovernanceError("target_payload must be a Goal Contract")
        if target.ref.contract_id != current_ref.contract_id:
            raise GovernanceError("Goal amendment must preserve contract_id; create a new Goal otherwise")
        if target.ref.version == current_ref.version:
            raise GovernanceError("Goal amendment requires a new version")

        delta = _normalize_delta(acceptance_delta)
        change_class, risk, findings = _infer_goal_change(
            current_row["payload"], target.payload, delta
        )
        target_row = self.register_contract(target.payload)
        if target_row["status"] != "draft":
            raise GovernanceError("Goal amendment target must be a draft version")
        approval_required = change_class != "structural_refactor"
        evidence_values = sorted({str(item) for item in evidence if str(item).strip()})
        if not evidence_values:
            raise GovernanceError("Goal change proposal requires evidence")
        proposal_body = {
            "schema_version": "goal-change-proposal-v1",
            "from_ref": from_ref,
            "to_ref": str(target.ref),
            "change_class": change_class,
            "risk": risk,
            "approval_required": approval_required,
            "acceptance_delta": delta,
            "semantic_findings": list(findings),
            "rationale": rationale,
            "evidence": evidence_values,
            "proposed_by": proposed_by,
            "migration_plan": dict(migration_plan or {}),
        }
        proposal_hash = payload_digest(proposal_body)
        proposal_id = "gcp_" + proposal_hash[:32]
        payload = {**proposal_body, "proposal_id": proposal_id,
                   "target_contract_hash": target_row["contract_hash"]}
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO goal_change_proposals "
                "(proposal_id,from_ref,to_ref,change_class,risk,status,proposal_hash,payload,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    proposal_id,
                    from_ref,
                    str(target.ref),
                    change_class,
                    risk,
                    "proposed" if approval_required else "preauthorized",
                    proposal_hash,
                    canonical_json(payload),
                    now,
                    now,
                ),
            )
        return self.goal_change(proposal_id)

    def goal_change(self, proposal_id: str) -> dict[str, Any]:
        row = self.state._connection().execute(
            "SELECT * FROM goal_change_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        result = _as_row(row)
        if result is None:
            raise KeyError(proposal_id)
        return result

    def approve_goal_change(
        self,
        proposal_id: str,
        *,
        actor: str,
        actor_role: str,
        decision: str = "approve",
        rationale: str,
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise GovernanceError("decision must be approve or reject")
        if not rationale.strip():
            raise GovernanceError("Goal change approval requires a rationale")
        proposal = self.goal_change(proposal_id)
        if proposal["status"] not in {"proposed", "preauthorized"}:
            raise GovernanceError(f"proposal is already {proposal['status']}")
        required = bool(proposal["payload"]["approval_required"])
        if required and actor_role != "human_goal_owner":
            raise GovernanceError("semantic Goal changes require human_goal_owner approval")
        if not required and actor_role not in {"human_goal_owner", "governance_policy"}:
            raise GovernanceError("structural Goal changes require a governance authority")
        approval_body = {
            "schema_version": "governance-approval-v1",
            "proposal_id": proposal_id,
            "actor": actor,
            "actor_role": actor_role,
            "decision": decision,
            "rationale": rationale,
        }
        approval_id = "gap_" + payload_digest(approval_body)[:32]
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO governance_approvals "
                "(approval_id,proposal_id,actor,actor_role,decision,rationale,payload,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    approval_id,
                    proposal_id,
                    actor,
                    actor_role,
                    decision,
                    rationale,
                    canonical_json({**approval_body, "approval_id": approval_id}),
                    now,
                ),
            )
            conn.execute(
                "UPDATE goal_change_proposals SET status=?,updated_at=? WHERE proposal_id=?",
                ("approved" if decision == "approve" else "rejected", now, proposal_id),
            )
        return {**self.goal_change(proposal_id), "approval_id": approval_id}

    def activate_goal_change(self, proposal_id: str, *, actor: str) -> dict[str, Any]:
        proposal = self.goal_change(proposal_id)
        if proposal["status"] == "preauthorized":
            self.approve_goal_change(
                proposal_id,
                actor="governance-policy",
                actor_role="governance_policy",
                rationale="acceptance set and normative semantics are unchanged",
            )
            proposal = self.goal_change(proposal_id)
        if proposal["status"] != "approved":
            raise GovernanceError("Goal change must be approved before activation")
        now = utcnow()
        with self.state.transaction() as conn:
            superseded = conn.execute(
                "UPDATE governance_contracts SET status='superseded',superseded_by=?,updated_at=? "
                "WHERE contract_ref=? AND status='active'",
                (proposal["to_ref"], now, proposal["from_ref"]),
            ).rowcount
            if superseded != 1:
                raise GovernanceError("source Goal Contract is not active")
            changed = conn.execute(
                "UPDATE governance_contracts SET status='active',activated_at=?,updated_at=? "
                "WHERE contract_ref=? AND status='draft'",
                (now, now, proposal["to_ref"]),
            ).rowcount
            if changed != 1:
                raise GovernanceError("target Goal Contract is not an activatable draft")
            self._record_contract_event(
                conn, contract_ref=proposal["from_ref"], from_status="active",
                to_status="superseded", actor=actor, actor_role="change_controller",
                reason=f"Goal amendment {proposal_id}", created_at=now,
            )
            self._record_contract_event(
                conn, contract_ref=proposal["to_ref"], from_status="draft",
                to_status="active", actor=actor, actor_role="change_controller",
                reason=f"Goal amendment {proposal_id}", created_at=now,
            )
            conn.execute(
                "UPDATE goal_change_proposals SET status='activated',updated_at=? "
                "WHERE proposal_id=?",
                (now, proposal_id),
            )
            self._queue_goal_reassessments(conn, proposal=proposal, created_at=now)
        return {**self.goal_change(proposal_id), "activated_by": actor}

    def bind_job(self, binding: JobContractBinding) -> dict[str, Any]:
        if getattr(self, "require_job_exists", False):
            job = self.state._connection().execute(
                "SELECT 1 FROM jobs WHERE id=?", (binding.job_id,)
            ).fetchone()
            if job is None:
                raise GovernanceError(
                    f"production governance binding references an unknown Job: {binding.job_id}"
                )
        contracts: dict[str, dict[str, Any]] = {}
        for ref in (
            binding.goal_ref,
            binding.autonomy_ref,
            binding.assurance_ref,
            binding.capability_ref,
        ):
            row = self.contract(ref, status="active")
            if row is None:
                raise GovernanceError(f"contract is not registered: {ref}")
            contracts[ref] = row
        goal = contracts[binding.goal_ref]["payload"]
        autonomy = contracts[binding.autonomy_ref]["payload"]
        assurance = contracts[binding.assurance_ref]["payload"]
        capability = contracts[binding.capability_ref]["payload"]
        if assurance["goal_contract_ref"] != binding.goal_ref:
            raise GovernanceError("Assurance Contract is not bound to the selected Goal Contract")
        autonomy_goal = autonomy.get("scope", {}).get("goal_contract_ref")
        if autonomy_goal and autonomy_goal != binding.goal_ref:
            raise GovernanceError("Autonomy Contract is not bound to the selected Goal Contract")
        required_clauses = {
            str(item["id"]) for item in goal["clauses"]
            if item["criticality"] in {"hard", "required"}
        }
        missing_obligations = sorted(
            required_clauses - set(assurance["proof_obligations"])
        )
        if missing_obligations:
            raise GovernanceError(
                "Assurance Contract misses required Goal clauses: "
                + ", ".join(missing_obligations)
            )
        domains = {
            str(item.get("scope", {}).get("domain"))
            for item in (goal, autonomy, assurance, capability)
            if item.get("scope", {}).get("domain") is not None
        }
        if len(domains) > 1:
            raise GovernanceError("bound contracts have incompatible scope domains")
        existing = self.state._connection().execute(
            "SELECT * FROM job_contract_bindings WHERE job_id=?", (binding.job_id,)
        ).fetchone()
        if existing is not None:
            row = _as_row(existing)
            assert row is not None
            if row["binding_hash"] != binding.binding_hash:
                raise GovernanceError(f"immutable Job contract binding drift: {binding.job_id}")
            return row
        payload = binding.asdict()
        payload["contract_hashes"] = {
            ref: contracts[ref]["contract_hash"] for ref in contracts
        }
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT INTO job_contract_bindings "
                "(job_id,binding_hash,goal_ref,autonomy_ref,assurance_ref,capability_ref,payload,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    binding.job_id,
                    binding.binding_hash,
                    binding.goal_ref,
                    binding.autonomy_ref,
                    binding.assurance_ref,
                    binding.capability_ref,
                    canonical_json(payload),
                    utcnow(),
                ),
            )
        result = self.binding(binding.job_id)
        assert result is not None
        return result

    def binding(self, job_id: str) -> dict[str, Any] | None:
        return _as_row(self.state._connection().execute(
            "SELECT * FROM job_contract_bindings WHERE job_id=?", (job_id,)
        ).fetchone())
