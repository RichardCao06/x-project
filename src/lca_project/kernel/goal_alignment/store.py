"""Idempotent persistence for goal-alignment protocol records."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from ..state import StateStore, utcnow


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class AlignmentStore:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def upsert_goal(self, payload: dict[str, Any]) -> dict[str, Any]:
        goal_id = str(payload["goal_id"])
        contract_hash = digest(payload)
        existing = self.state._connection().execute(
            "SELECT contract_hash,payload FROM goal_contracts WHERE goal_id=?", (goal_id,)
        ).fetchone()
        if existing and str(existing["contract_hash"]) != contract_hash:
            raise ValueError(f"immutable Goal Contract drift: {goal_id}")
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO goal_contracts VALUES(?,?,?,?,?,?,?,?)",
                (goal_id, str(payload["version"]), contract_hash, str(payload["scope"]),
                 "active", canonical(payload), now, now),
            )
        return {**payload, "contract_hash": contract_hash, "status": "active"}

    def observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        vector_hash = digest(payload.get("dimensions", {}))
        observation_id = "qob_" + digest({"job": payload["job_id"], "run": payload.get("run_id"),
                                           "vector": vector_hash})[:32]
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO quality_observations VALUES(?,?,?,?,?,?,?,?)",
                (observation_id, payload["job_id"], payload.get("run_id"), payload["goal_id"],
                 vector_hash, float(payload["score"]), canonical(payload), utcnow()),
            )
        return {**payload, "observation_id": observation_id, "vector_hash": vector_hash}

    def deviation(self, *, job_id: str, run_id: str | None, goal_id: str,
                  value: dict[str, Any]) -> dict[str, Any]:
        fingerprint = digest({"type": value["deviation_type"], "evidence": value["evidence"]})
        deviation_id = "dev_" + digest({"job": job_id, "fingerprint": fingerprint})[:32]
        payload = {"schema_version": "deviation-report-v1", "deviation_id": deviation_id,
                   "job_id": job_id, "run_id": run_id, "goal_id": goal_id,
                   "fingerprint": fingerprint, **value}
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT INTO deviation_reports VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(deviation_id) DO UPDATE SET updated_at=excluded.updated_at",
                (deviation_id, job_id, run_id, goal_id, value["deviation_type"], value["severity"],
                 fingerprint, "open", canonical(payload), now, now),
            )
        return payload

    def diagnosis(self, deviation_id: str, value: dict[str, Any]) -> dict[str, Any]:
        diagnosis_id = "dia_" + digest({"deviation": deviation_id, "cause": value["cause_code"]})[:32]
        payload = {"schema_version": "causal-diagnosis-v1", "diagnosis_id": diagnosis_id,
                   "deviation_id": deviation_id, **value}
        with self.state.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO causal_diagnoses VALUES(?,?,?,?,?,?)",
                         (diagnosis_id, deviation_id, value["cause_code"],
                          float(value["confidence"]), canonical(payload), utcnow()))
        return payload

    def repair_plan(self, deviation_id: str, value: dict[str, Any]) -> dict[str, Any]:
        repair_plan_id = "rpl_" + digest({"deviation": deviation_id, "action": value["action"]})[:32]
        payload = {"schema_version": "repair-plan-v1", "repair_plan_id": repair_plan_id,
                   "deviation_id": deviation_id, **value}
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT INTO repair_plans VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(repair_plan_id) DO UPDATE SET status=excluded.status,"
                "payload=excluded.payload,updated_at=excluded.updated_at",
                (repair_plan_id, deviation_id, value["repair_level"], value["action"],
                 value.get("status", "proposed"), canonical(payload), now, now),
            )
        return payload

    def rows(self, table: str, *, job_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        allowed = {"goal_contracts", "quality_observations", "deviation_reports",
                   "causal_diagnoses", "repair_plans", "system_change_candidates",
                   "validation_certificates", "policy_promotion_receipts",
                   "goal_supervisor_wakeups", "repair_validation_receipts"}
        if table not in allowed:
            raise ValueError(f"unsupported alignment table: {table}")
        where, params = "", []
        if job_id and table in {"quality_observations", "deviation_reports",
                               "goal_supervisor_wakeups", "repair_validation_receipts"}:
            where, params = " WHERE job_id=?", [job_id]
        order = "created_at"
        result = []
        for row in self.state._connection().execute(
            f"SELECT * FROM {table}{where} ORDER BY {order} DESC LIMIT ?", (*params, limit)
        ):
            item = dict(row)
            if "payload" in item:
                item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def request_supervision(self, *, job_id: str, run_id: str | None, reason: str,
                            deviation_ids: list[str], observation_hash: str,
                            context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Persist idempotent work for a Supervisor, independent of process life."""
        dedupe_key = digest({
            "job_id": job_id, "run_id": run_id, "reason": reason,
            "deviation_ids": sorted(deviation_ids),
            "observation_hash": observation_hash,
        })
        wakeup_id = "gsw_" + dedupe_key[:32]
        payload = {
            "schema_version": "goal-supervisor-wakeup-v1",
            "wakeup_id": wakeup_id, "job_id": job_id, "run_id": run_id,
            "reason": reason, "deviation_ids": sorted(deviation_ids),
            "observation_hash": observation_hash, "context": context or {},
        }
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO goal_supervisor_wakeups "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (wakeup_id, job_id, run_id, reason, dedupe_key, "pending",
                 canonical(payload), now, now),
            )
        row = self.state._connection().execute(
            "SELECT * FROM goal_supervisor_wakeups WHERE wakeup_id=?", (wakeup_id,)
        ).fetchone()
        result = dict(row); result["payload"] = json.loads(result["payload"])
        return result

    def pending_wakeups(self, *, job_id: str | None = None) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM goal_supervisor_wakeups WHERE status='pending'", []
        if job_id:
            query += " AND job_id=?"; params.append(job_id)
        query += " ORDER BY created_at"
        result: list[dict[str, Any]] = []
        for row in self.state._connection().execute(query, tuple(params)):
            item = dict(row); item["payload"] = json.loads(item["payload"]); result.append(item)
        return result

    def consume_wakeups(self, *, job_id: str, consumer: str,
                        wakeup_ids: list[str] | None = None) -> list[str]:
        rows = self.pending_wakeups(job_id=job_id)
        if wakeup_ids is not None:
            selected = set(wakeup_ids)
            rows = [row for row in rows if str(row["wakeup_id"]) in selected]
        if not rows:
            return []
        now = utcnow()
        with self.state.transaction() as conn:
            for row in rows:
                payload = dict(row["payload"])
                payload["consumed_by"] = consumer
                payload["consumed_at"] = now
                conn.execute(
                    "UPDATE goal_supervisor_wakeups SET status='consumed',payload=?,"
                    "updated_at=? WHERE wakeup_id=? AND status='pending'",
                    (canonical(payload), now, row["wakeup_id"]),
                )
        return [str(row["wakeup_id"]) for row in rows]

    def repair_validation_receipt(self, *, repair_run_id: str, job_id: str,
                                  run_id: str | None, verdict: str,
                                  baseline: dict[str, Any], current: dict[str, Any],
                                  proof: dict[str, Any]) -> dict[str, Any]:
        baseline_hash, current_hash = digest(baseline), digest(current)
        receipt_id = "rvr_" + digest({
            "repair_run_id": repair_run_id, "current_hash": current_hash,
        })[:32]
        payload = {
            "schema_version": "repair-validation-receipt-v1",
            "receipt_id": receipt_id, "repair_run_id": repair_run_id,
            "job_id": job_id, "run_id": run_id, "verdict": verdict,
            "baseline": baseline, "current": current, "proof": proof,
        }
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO repair_validation_receipts "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (receipt_id, repair_run_id, job_id, run_id, verdict,
                 baseline_hash, current_hash, canonical(payload), utcnow()),
            )
        return payload
