"""Sandbox/shadow/canary promotion controller for system-level changes.

The controller governs records and policy activation.  It never lets a running
Job mutate source code, Goal Contracts, or release permissions.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...control import ControlPlane
from ..state import utcnow
from .store import AlignmentStore, canonical, digest


class ChangeController:
    PHASE_TARGET = {"sandbox": "sandbox_validated", "shadow": "shadowed",
                    "canary": "canary_passed", "post_promotion": "monitored"}
    PREVIOUS = {"sandbox": {"proposed"}, "shadow": {"sandbox_validated"},
                "canary": {"shadowed"}, "post_promotion": {"promoted"}}

    def __init__(self, root: str | Path, control: ControlPlane | None = None) -> None:
        self.root = Path(root).resolve()
        self.control = control or ControlPlane(self.root)
        self.state = self.control.state
        self.store = AlignmentStore(self.state)

    def propose(self, *, source_deviation_id: str | None, target: str, risk: str,
                change: dict[str, Any], rollback: dict[str, Any]) -> dict[str, Any]:
        if risk not in {"low", "medium", "high", "critical"}:
            raise ValueError("change risk must be low, medium, high or critical")
        body = {"schema_version": "system-change-candidate-v1", "target": target,
                "risk": risk, "change": change, "rollback": rollback,
                "source_deviation_id": source_deviation_id,
                "running_job_mutation_authorized": False}
        candidate_hash = digest(body)
        candidate_id = "chg_" + candidate_hash[:32]
        payload = {**body, "candidate_id": candidate_id, "candidate_hash": candidate_hash}
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO system_change_candidates VALUES(?,?,?,?,?,?,?,?,?)",
                (candidate_id, source_deviation_id, target, risk, "proposed", candidate_hash,
                 canonical(payload), now, now),
            )
        self.control.events.append("system_change", candidate_id, "system_change.proposed",
                                   {"target": target, "risk": risk,
                                    "source_deviation_id": source_deviation_id},
                                   actor="goal-alignment-controller")
        return self.get(candidate_id)

    def get(self, candidate_id: str) -> dict[str, Any]:
        row = self.state._connection().execute(
            "SELECT * FROM system_change_candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def revise(self, candidate_id: str, *, reason: str) -> dict[str, Any]:
        """Create an auditable successor after a candidate was safely rejected."""
        predecessor = self.get(candidate_id)
        if predecessor["status"] != "rejected":
            raise ValueError("only a rejected candidate can be revised")
        row = self.state._connection().execute(
            "SELECT COUNT(*) AS count FROM system_change_candidates "
            "WHERE source_deviation_id=? AND target=?",
            (predecessor["source_deviation_id"], predecessor["target"]),
        ).fetchone()
        revision = int(row["count"] if row else 1) + 1
        source = predecessor["payload"]
        revised = self.propose(
            source_deviation_id=predecessor["source_deviation_id"],
            target=str(predecessor["target"]), risk=str(predecessor["risk"]),
            change={**(source.get("change") or {}),
                    "revision": revision,
                    "supersedes_candidate_id": candidate_id,
                    "revision_reason": reason},
            rollback=source.get("rollback") or {},
        )
        self.control.events.append(
            "system_change", revised["candidate_id"], "system_change.revised",
            {"supersedes_candidate_id": candidate_id, "reason": reason,
             "revision": revision}, actor="goal-alignment-controller",
        )
        return revised

    def certify(self, candidate_id: str, *, phase: str, suites: dict[str, bool],
                regressions: list[str] | None = None, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        if phase not in self.PHASE_TARGET:
            raise ValueError(f"unsupported validation phase: {phase}")
        candidate = self.get(candidate_id)
        if candidate["status"] not in self.PREVIOUS[phase]:
            raise ValueError(f"phase {phase} cannot follow {candidate['status']}")
        regressions = regressions or []
        verdict = "pass" if suites and all(suites.values()) and not regressions else "fail"
        certificate_id = "vct_" + digest({"candidate": candidate_id, "phase": phase,
                                           "suites": suites, "regressions": regressions})[:32]
        payload = {"schema_version": "validation-certificate-v1",
                   "certificate_id": certificate_id, "candidate_id": candidate_id,
                   "phase": phase, "verdict": verdict, "suites": suites,
                   "regressions": regressions, "evidence": evidence or {}}
        with self.state.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO validation_certificates VALUES(?,?,?,?,?,?)",
                         (certificate_id, candidate_id, phase, verdict, canonical(payload), utcnow()))
        if verdict == "pass":
            self._transition(candidate_id, self.PHASE_TARGET[phase], "advance",
                             f"{phase} validation passed", payload)
        else:
            self._transition(candidate_id, "rejected", "advance",
                             f"{phase} validation failed", payload)
        return payload

    def promote(self, candidate_id: str, *, operator: bool = False) -> dict[str, Any]:
        candidate = self.get(candidate_id)
        if candidate["status"] != "canary_passed":
            raise ValueError("promotion requires sandbox, shadow and canary certificates")
        if candidate["risk"] != "low" and not operator:
            raise ValueError("medium/high-risk changes require explicit operator promotion")
        certificates = list(self.state._connection().execute(
            "SELECT phase,verdict,payload FROM validation_certificates WHERE candidate_id=?",
            (candidate_id,),
        ))
        phases = {str(item["phase"]) for item in certificates if item["verdict"] == "pass"}
        suites: set[str] = set()
        for item in certificates:
            value = json.loads(item["payload"])
            suites.update(name for name, passed in (value.get("suites") or {}).items() if passed)
        if not {"sandbox", "shadow", "canary"}.issubset(phases):
            raise ValueError("promotion certificates are incomplete")
        if not {"golden", "mutation", "regression"}.issubset(suites):
            raise ValueError("promotion requires golden, mutation and regression suites")
        return self._transition(candidate_id, "promoted", "promote",
                                "validated change promoted", {"operator": operator})

    def rollback(self, candidate_id: str, *, reason: str) -> dict[str, Any]:
        candidate = self.get(candidate_id)
        if candidate["status"] not in {"promoted", "monitored"}:
            raise ValueError("only a promoted or monitored change can be rolled back")
        return self._transition(candidate_id, "rolled_back", "rollback", reason, {})

    def reject(self, candidate_id: str, *, reason: str) -> dict[str, Any]:
        candidate = self.get(candidate_id)
        if candidate["status"] not in {
            "proposed", "sandbox_validated", "shadowed", "canary_passed"
        }:
            raise ValueError("only an unpromoted candidate can be rejected")
        return self._transition(candidate_id, "rejected", "advance", reason, {})

    def _transition(self, candidate_id: str, target: str, action: str, reason: str,
                    evidence: dict[str, Any]) -> dict[str, Any]:
        current = self.get(candidate_id)
        receipt_id = "ppr_" + digest({"candidate": candidate_id, "from": current["status"],
                                      "to": target, "reason": reason})[:32]
        payload = {"schema_version": "policy-promotion-receipt-v1", "receipt_id": receipt_id,
                   "candidate_id": candidate_id, "action": action,
                   "from_status": current["status"], "to_status": target,
                   "reason": reason, "evidence": evidence}
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute("UPDATE system_change_candidates SET status=?,updated_at=? WHERE candidate_id=?",
                         (target, now, candidate_id))
            conn.execute("INSERT OR IGNORE INTO policy_promotion_receipts VALUES(?,?,?,?,?,?,?)",
                         (receipt_id, candidate_id, action, current["status"], target,
                          canonical(payload), now))
        self.control.events.append("system_change", candidate_id, f"system_change.{target}", payload,
                                   actor="change-controller")
        return {**self.get(candidate_id), "receipt": payload}
