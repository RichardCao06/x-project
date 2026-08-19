"""Locally signed proof receipts for gates and agent runtime attestations.

Untrusted workflow/agent payloads cannot mint these receipts.  The authority
binds a proof to CAS, SQLite and the append-only event ledger, then signs the
immutable payload digest with a project-local key readable only by the control
plane process.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
from typing import Any
import uuid

from .artifacts import ArtifactStore
from .events import EventLedger
from .state import StateStore, utcnow


class ProofError(ValueError):
    pass


TRUSTED_PRODUCERS = {
    "gate": {"draft-content-gate", "gate-dispatcher", "release-checker"},
    "agent-attestation": {"agent-runtime-launcher"},
    "governance-evidence": {
        "governance-controller",
        "governance-evaluator",
        "governed-release-manager",
    },
}


class ProofAuthority:
    def __init__(self, root: str | Path, state: StateStore, artifacts: ArtifactStore, events: EventLedger) -> None:
        self.root = Path(root).resolve()
        self.state, self.artifacts, self.events = state, artifacts, events
        self.key_path = self.root / "var" / "proof-authority.key"
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.key_path.exists():
            self.key_path.write_bytes(secrets.token_bytes(32))
            os.chmod(self.key_path, 0o600)
        self._initialize()

    def _initialize(self) -> None:
        self.state._connection().execute(
            """CREATE TABLE IF NOT EXISTS proof_receipts(
            proof_id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL,
            producer TEXT NOT NULL, artifact_digest TEXT NOT NULL, signature TEXT NOT NULL,
            created_at TEXT NOT NULL)"""
        )

    def _signature(self, proof_id: str, digest: str, kind: str, subject: str, producer: str) -> str:
        message = "\0".join((proof_id, digest, kind, subject, producer)).encode()
        return hmac.new(self.key_path.read_bytes(), message, hashlib.sha256).hexdigest()

    def issue(self, *, kind: str, subject: str, producer: str, claims: dict[str, Any]) -> dict[str, str]:
        if producer not in TRUSTED_PRODUCERS.get(kind, set()):
            raise ProofError(f"untrusted {kind} producer: {producer}")
        if not subject or not isinstance(claims, dict):
            raise ProofError("proof subject and claims are required")
        payload = {"schema_version": "proof-v1", "kind": kind, "subject": subject,
                   "producer": producer, "claims": claims}
        artifact = self.artifacts.put_json(payload, metadata={"schema": "proof-v1", "kind": kind})
        proof_id = f"proof_{uuid.uuid4().hex}"
        signature = self._signature(proof_id, artifact.digest, kind, subject, producer)
        with self.state.transaction() as conn:
            conn.execute("INSERT INTO proof_receipts VALUES(?,?,?,?,?,?,?)",
                         (proof_id, kind, subject, producer, artifact.digest, signature, utcnow()))
        self.events.append("proof", proof_id, "proof.issued", {"kind": kind, "subject": subject,
                           "producer": producer, "artifact_digest": artifact.digest}, actor="proof-authority")
        return {"proof_id": proof_id, "artifact_digest": artifact.digest, "signature": signature}

    def verify(self, receipt: dict[str, Any], *, kind: str, subject: str) -> dict[str, Any]:
        if not isinstance(receipt, dict):
            raise ProofError("signed proof receipt required")
        proof_id, digest, signature = (receipt.get("proof_id"), receipt.get("artifact_digest"), receipt.get("signature"))
        if not all(isinstance(item, str) and item for item in (proof_id, digest, signature)):
            raise ProofError("incomplete signed proof receipt")
        row = self.state._connection().execute("SELECT * FROM proof_receipts WHERE proof_id=?", (proof_id,)).fetchone()
        if row is None or row["kind"] != kind or row["subject"] != subject or row["artifact_digest"] != digest:
            raise ProofError("proof receipt is not registered for this subject")
        expected = self._signature(proof_id, digest, kind, subject, row["producer"])
        if not hmac.compare_digest(signature, row["signature"]) or not hmac.compare_digest(signature, expected):
            raise ProofError("proof signature invalid")
        events = list(self.events.read("proof", proof_id))
        if len(events) != 1 or events[0].payload.get("artifact_digest") != digest:
            raise ProofError("proof event ledger binding missing")
        payload = json.loads(self.artifacts.get_bytes(digest))
        if payload.get("kind") != kind or payload.get("subject") != subject or payload.get("producer") != row["producer"]:
            raise ProofError("proof CAS payload mismatch")
        return payload["claims"]

    def issue_gate(self, *, gate_id: str, input_hashes: list[str], policy_version: str,
                   subject: str, producer: str = "gate-dispatcher", status: str = "pass") -> dict[str, str]:
        return self.issue(kind="gate", subject=subject, producer=producer, claims={
            "gate_id": gate_id, "status": status, "input_hashes": sorted(input_hashes),
            "policy_version": policy_version})

    def verify_gates(self, receipts: list[dict[str, Any]], *, subject: str,
                     required: set[str], input_hashes: set[str]) -> None:
        passed: set[str] = set()
        for receipt in receipts:
            claims = self.verify(receipt, kind="gate", subject=subject)
            if claims.get("status") != "pass" or set(claims.get("input_hashes", ())) != input_hashes:
                raise ProofError(f"stale or foreign signed gate: {claims.get('gate_id')}")
            if not claims.get("policy_version"):
                raise ProofError("signed gate policy missing")
            passed.add(str(claims.get("gate_id")))
        missing = required - passed
        if missing:
            raise ProofError(f"signed gates missing: {sorted(missing)}")
