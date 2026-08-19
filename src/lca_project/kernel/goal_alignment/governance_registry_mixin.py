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

class ContractRegistryMixin:
    def __init__(self, state: StateStore) -> None:
        self.state = state
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Install v2 tables for old fixtures and newly initialized stores."""
        from ..governance_schema import install_governance_schema

        with self.state.transaction() as conn:
            install_governance_schema(conn)

    @staticmethod
    def _record_contract_event(
        conn: Any, *, contract_ref: str, from_status: str, to_status: str,
        actor: str, actor_role: str, reason: str, created_at: str,
        details: Mapping[str, Any] | None = None,
    ) -> str:
        body = {
            "schema_version": "contract-lifecycle-event-v1",
            "contract_ref": contract_ref,
            "from_status": from_status,
            "to_status": to_status,
            "actor": actor,
            "actor_role": actor_role,
            "reason": reason,
            "details": dict(details or {}),
            "created_at": created_at,
        }
        event_id = "cle_" + payload_digest(body)[:32]
        conn.execute(
            "INSERT INTO contract_lifecycle_events "
            "(event_id,contract_ref,from_status,to_status,actor,actor_role,reason,payload,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, contract_ref, from_status, to_status, actor, actor_role, reason,
             canonical_json({**body, "event_id": event_id}), created_at),
        )
        return event_id

    def register_contract(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        document = ContractDocument.from_mapping(payload)
        ref = str(document.ref)
        existing = self.state._connection().execute(
            "SELECT * FROM governance_contracts WHERE contract_ref=?", (ref,)
        ).fetchone()
        if existing is not None:
            row = _as_row(existing)
            assert row is not None
            if row["contract_hash"] != document.digest:
                raise GovernanceError(f"immutable contract drift: {ref}")
            return row
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT INTO governance_contracts "
                "(contract_ref,contract_kind,contract_id,version,contract_hash,status,payload,"
                "created_at,updated_at,activated_at,superseded_by) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    ref,
                    document.ref.kind.value,
                    document.ref.contract_id,
                    document.ref.version,
                    document.digest,
                    "draft",
                    canonical_json(document.payload),
                    now,
                    now,
                    None,
                ),
            )
        result = self.contract(ref)
        assert result is not None
        return result

    def contract(self, ref: str, *, status: str | None = None) -> dict[str, Any] | None:
        ContractRef.parse(ref)
        row = self.state._connection().execute(
            "SELECT * FROM governance_contracts WHERE contract_ref=?", (ref,)
        ).fetchone()
        result = _as_row(row)
        if result is not None and status is not None and result["status"] != status:
            raise GovernanceError(f"{ref} is {result['status']}, expected {status}")
        return result

    def contracts(self, *, kind: ContractKind | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM governance_contracts"
        params: tuple[Any, ...] = ()
        if kind is not None:
            query += " WHERE contract_kind=?"
            params = (kind.value,)
        query += " ORDER BY contract_kind,contract_id,created_at"
        return [_as_row(row) for row in self.state._connection().execute(query, params)]  # type: ignore[list-item]

    def activate_initial_contract(self, ref: str, *, actor: str, actor_role: str) -> dict[str, Any]:
        parsed = ContractRef.parse(ref)
        if parsed.kind is ContractKind.GOAL and actor_role != "human_goal_owner":
            raise GovernanceError("Goal activation requires a human_goal_owner")
        if (parsed.kind is not ContractKind.GOAL
                and actor_role not in {"human_goal_owner", "human_governance_owner"}):
            raise GovernanceError("contract activation requires a human governance owner")
        row = self.contract(ref)
        if row is None:
            raise KeyError(ref)
        if row["status"] == "active":
            return row
        if row["status"] != "draft":
            raise GovernanceError(f"{ref} cannot be activated from {row['status']}")
        active = self.state._connection().execute(
            "SELECT contract_ref FROM governance_contracts "
            "WHERE contract_kind=? AND contract_id=? AND status='active'",
            (parsed.kind.value, parsed.contract_id),
        ).fetchone()
        if active is not None and active["contract_ref"] != ref:
            raise GovernanceError("an active version already exists; use a Goal amendment or supersession")
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute(
                "UPDATE governance_contracts SET status='active',activated_at=?,updated_at=? "
                "WHERE contract_ref=?",
                (now, now, ref),
            )
            self._record_contract_event(
                conn, contract_ref=ref, from_status=str(row["status"]), to_status="active",
                actor=actor, actor_role=actor_role, reason="initial contract activation",
                created_at=now,
            )
        result = self.contract(ref)
        assert result is not None
        return {**result, "activated_by": actor, "activated_by_role": actor_role}

    def suspend_contract(
        self,
        ref: str,
        *,
        actor: str,
        actor_role: str,
        reason: str,
        evidence: Iterable[str],
    ) -> dict[str, Any]:
        """Revoke autonomous use of a contract version without mutating payload."""
        parsed = ContractRef.parse(ref)
        required_role = (
            "human_goal_owner"
            if parsed.kind is ContractKind.GOAL
            else "human_governance_owner"
        )
        if actor_role != required_role:
            raise GovernanceError(f"contract suspension requires {required_role}")
        if not reason.strip():
            raise GovernanceError("contract suspension requires a reason")
        evidence_values = sorted({str(item) for item in evidence if str(item).strip()})
        if not evidence_values:
            raise GovernanceError("contract suspension requires evidence")
        row = self.contract(ref)
        if row is None:
            raise KeyError(ref)
        if row["status"] == "suspended":
            return row
        if row["status"] not in {"active", "superseded"}:
            raise GovernanceError(f"{ref} cannot be suspended from {row['status']}")
        now = utcnow()
        with self.state.transaction() as conn:
            changed = conn.execute(
                "UPDATE governance_contracts SET status='suspended',updated_at=? "
                "WHERE contract_ref=? AND status=?",
                (now, ref, row["status"]),
            ).rowcount
            if changed != 1:
                raise GovernanceError("contract suspension lost its status precondition")
            self._record_contract_event(
                conn,
                contract_ref=ref,
                from_status=str(row["status"]),
                to_status="suspended",
                actor=actor,
                actor_role=actor_role,
                reason=reason,
                created_at=now,
                details={"evidence": evidence_values},
            )
        result = self.contract(ref)
        assert result is not None
        return result

    def replace_active_contract(
        self,
        *,
        from_ref: str,
        target_payload: Mapping[str, Any],
        actor: str,
        actor_role: str,
        rationale: str,
        evidence: Iterable[str],
    ) -> dict[str, Any]:
        """Govern a non-Goal contract replacement without in-place mutation."""
        source_ref = ContractRef.parse(from_ref)
        if source_ref.kind is ContractKind.GOAL:
            raise GovernanceError("Goal Contract changes require the amendment protocol")
        if actor_role != "human_governance_owner":
            raise GovernanceError("contract replacement requires a human_governance_owner")
        if not rationale.strip():
            raise GovernanceError("contract replacement requires a rationale")
        evidence_values = sorted({str(item) for item in evidence if str(item).strip()})
        if not evidence_values:
            raise GovernanceError("contract replacement requires evidence")
        source = self.contract(from_ref, status="active")
        if source is None:
            raise KeyError(from_ref)
        target = ContractDocument.from_mapping(target_payload)
        if target.ref.kind is not source_ref.kind:
            raise GovernanceError("replacement must preserve contract kind")
        if target.ref.contract_id != source_ref.contract_id:
            raise GovernanceError("replacement must preserve contract_id")
        if target.ref.version == source_ref.version:
            raise GovernanceError("replacement requires a new version")
        target_row = self.register_contract(target.payload)
        if target_row["status"] != "draft":
            raise GovernanceError("replacement target must be a draft version")
        now = utcnow()
        details = {
            "rationale": rationale,
            "evidence": evidence_values,
            "source_contract_hash": source["contract_hash"],
            "target_contract_hash": target_row["contract_hash"],
        }
        with self.state.transaction() as conn:
            superseded = conn.execute(
                "UPDATE governance_contracts SET status='superseded',superseded_by=?,updated_at=? "
                "WHERE contract_ref=? AND status='active'",
                (str(target.ref), now, from_ref),
            ).rowcount
            activated = conn.execute(
                "UPDATE governance_contracts SET status='active',activated_at=?,updated_at=? "
                "WHERE contract_ref=? AND status='draft'",
                (now, now, str(target.ref)),
            ).rowcount
            if superseded != 1 or activated != 1:
                raise GovernanceError("contract replacement lost its active/draft precondition")
            self._record_contract_event(
                conn,
                contract_ref=from_ref,
                from_status="active",
                to_status="superseded",
                actor=actor,
                actor_role=actor_role,
                reason="governed contract replacement",
                created_at=now,
                details=details,
            )
            self._record_contract_event(
                conn,
                contract_ref=str(target.ref),
                from_status="draft",
                to_status="active",
                actor=actor,
                actor_role=actor_role,
                reason="governed contract replacement",
                created_at=now,
                details=details,
            )
        result = self.contract(str(target.ref))
        assert result is not None
        return {**result, "replaced_ref": from_ref, "replacement_evidence": evidence_values}
