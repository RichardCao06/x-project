"""Durable state machine for one Wiki-v2 plan-to-publish run.

The SQL projection is intentionally small and rebuilt/audited from the kernel
event ledger.  Every transition has exact input/output CAS hashes and uses a
unique ``(run_id, stage)`` record as the idempotency fence.  A crashed caller
may submit the same envelope again; a changed envelope is rejected instead of
silently replacing evidence.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from lca_project.contracts import Job, JobState
from lca_project.control import ControlPlane
from lca_project.kernel.assurance import AssuranceError, gate_runtime, prompt_hash
from lca_project.kernel.proofs import ProofAuthority, ProofError
from lca_project.kernel.state import utcnow


class WikiRuntimeError(RuntimeError):
    """A safe, classified runtime failure."""

    def __init__(self, code: str, message: str, *, quarantine: bool = False) -> None:
        super().__init__(message)
        self.code, self.quarantine = code, quarantine


class WikiStageConflict(WikiRuntimeError):
    pass


class WikiStage(StrEnum):
    PLAN = "plan"
    PREPARED = "prepared"
    RESEARCH_READY = "research_ready"
    VERIFIED = "verified"
    FROZEN = "frozen"
    DRAFT_GATED = "draft_gated"
    DRAFT_APPLIED = "draft_applied"
    PREVIEWED = "previewed"
    RELEASE_GATED = "release_gated"
    REVIEWED_APPLIED = "reviewed_applied"
    PUBLISHED = "published"


STAGES: tuple[WikiStage, ...] = tuple(WikiStage)
AGENT_STAGES = frozenset({WikiStage.RESEARCH_READY, WikiStage.VERIFIED, WikiStage.FROZEN})
TERMINAL_STATUSES = frozenset({"published", "quarantined", "failed"})

# These are intentionally fail-closed.  An unknown failure becomes repairable
# rather than being silently retried or promoted.
FAULTS: dict[str, tuple[str, bool]] = {
    "network_access": ("AGENT_NETWORK_DENIED", True),
    "direct_apply": ("AGENT_SIDE_EFFECT_DENIED", True),
    "identity": ("NODE_IDENTITY_MISMATCH", True),
    "hash": ("HASH_BINDING_MISMATCH", True),
    "schema": ("SCHEMA_INVALID", False),
    "gate": ("GATE_NO_GO", False),
    "timeout": ("EXECUTOR_TIMEOUT", False),
    "retry_exhausted": ("RETRY_EXHAUSTED", True),
}


@dataclass(frozen=True)
class WikiRun:
    run_id: str
    job_id: str
    node_id: str
    status: str
    current_stage: WikiStage | None
    dossier_hash: str
    policy_version: str
    attempt: int
    created_at: str
    updated_at: str
    quarantine_code: str | None = None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class WikiRuntime:
    """Runs a strictly ordered Wiki vertical slice over the shared Kernel.

    ``root`` is the new project root.  The runtime never reads or writes the
    legacy source tree; all data it needs is passed as immutable values.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.control = ControlPlane(self.root)
        self.state = self.control.state
        self.artifacts = self.control.artifacts
        self.events = self.control.events
        self.proofs = ProofAuthority(self.root, self.state, self.artifacts, self.events)
        self._initialize()

    def _initialize(self) -> None:
        self.state._connection().executescript(
            """
            CREATE TABLE IF NOT EXISTS wiki_runtime_runs (
                run_id TEXT PRIMARY KEY, job_id TEXT UNIQUE NOT NULL, node_id TEXT NOT NULL,
                status TEXT NOT NULL, current_stage TEXT, dossier_hash TEXT NOT NULL,
                policy_version TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
                quarantine_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wiki_runtime_stages (
                run_id TEXT NOT NULL, stage TEXT NOT NULL, input_hashes TEXT NOT NULL,
                output_hashes TEXT NOT NULL, envelope_hash TEXT NOT NULL,
                completed_at TEXT NOT NULL, PRIMARY KEY(run_id, stage),
                FOREIGN KEY(run_id) REFERENCES wiki_runtime_runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS wiki_runtime_faults (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT, code TEXT NOT NULL,
                classification TEXT NOT NULL, detail_hash TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES wiki_runtime_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS wiki_runtime_stage_run_idx ON wiki_runtime_stages(run_id, stage);
            """
        )

    # ---- lifecycle -----------------------------------------------------
    def start(self, *, node_id: str, dossier: dict[str, Any], policy_version: str,
              idempotency_key: str | None = None, workflow: str = "wiki-v2-autonomous",
              batch_id: str | None = None, work_kind: str = "nomination") -> WikiRun:
        """Create a run and its kernel Job/Run records, or return its exact retry."""
        self._require_node(node_id)
        self._require_identity(dossier, node_id)
        if work_kind not in {"nomination", "repair"}:
            raise ValueError("work_kind must be nomination or repair")
        dossier_artifact = self.artifacts.put_json(dossier, metadata={"schema": "wiki-dossier-v1", "node_id": node_id})
        key = idempotency_key or f"wiki:{node_id}:{policy_version}:{dossier_artifact.digest}"
        existing = self.state._connection().execute(
            "SELECT * FROM wiki_runtime_runs WHERE job_id IN (SELECT id FROM jobs WHERE json_extract(payload,'$.idempotency_key')=?)", (key,)
        ).fetchone()
        if existing:
            result = self._run_row(existing)
            if result.dossier_hash != dossier_artifact.digest:
                raise WikiStageConflict("IDEMPOTENCY_CONFLICT", "idempotency key was reused with another dossier", quarantine=True)
            return result
        run_id, job_id = f"wiki_run_{uuid.uuid4().hex}", f"job_wiki_{uuid.uuid4().hex}"
        job = Job(target=f"wiki:{node_id}", workflow=workflow, scope={"node_id": node_id, "batch_id": batch_id, "work_kind": work_kind},
                  policy_version=policy_version, input_hashes=(dossier_artifact.digest,), job_id=job_id)
        self.control.submit_job(job, idempotency_key=key)
        # Kernel Job state is a coarse projection; detailed progress belongs to
        # this runtime's immutable stage ledger.
        self.control.transition_job(job_id, JobState.READY, reason="wiki runtime accepted frozen dossier")
        self.control.transition_job(job_id, JobState.LEASED, reason="wiki runtime local lease")
        self.control.transition_job(job_id, JobState.RUNNING, reason="wiki state machine started")
        self.state.create_run(run_id, job_id, {"runtime": "wiki-v2", "node_id": node_id, "dossier_hash": dossier_artifact.digest})
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute("INSERT INTO wiki_runtime_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (run_id, job_id, node_id, "running", None, dossier_artifact.digest, policy_version, 1, None, now, now))
        self.events.append("wiki_run", run_id, "wiki.run.started", {"job_id": job_id, "node_id": node_id, "dossier_hash": dossier_artifact.digest, "policy_version": policy_version, "batch_id": batch_id, "work_kind": work_kind}, actor="wiki-runtime")
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> WikiRun:
        row = self.state._connection().execute("SELECT * FROM wiki_runtime_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run_row(row)

    def stage_records(self, run_id: str) -> list[dict[str, Any]]:
        self.get_run(run_id)
        rows = self.state._connection().execute("SELECT * FROM wiki_runtime_stages WHERE run_id=? ORDER BY completed_at", (run_id,))
        return [{**dict(row), "input_hashes": json.loads(row["input_hashes"]), "output_hashes": json.loads(row["output_hashes"])} for row in rows]

    def resume(self, run_id: str) -> WikiRun:
        """Return durable state after a crash; it does not re-run a stage."""
        run = self.get_run(run_id)
        if run.status in TERMINAL_STATUSES:
            return run
        self.events.append("wiki_run", run_id, "wiki.run.resumed", {"current_stage": str(run.current_stage) if run.current_stage else None}, actor="wiki-runtime")
        return run

    # ---- stage admission ------------------------------------------------
    def advance(self, run_id: str, stage: WikiStage | str, envelope: dict[str, Any], *,
                actor: str = "deterministic-controller") -> tuple[WikiRun, tuple[str, ...]]:
        """Atomically freeze one stage's envelope and advance the projection.

        The envelope is stored as one artifact.  Optional ``outputs`` values
        are independently frozen and linked to the envelope and inputs.  This
        gives later gates stable, content-addressed inputs without trusting a
        mutable staging path.
        """
        stage = WikiStage(stage)
        run = self.get_run(run_id)
        self._check_admission(run, stage, envelope, actor)
        inputs = self._input_hashes(run, stage, envelope)
        self._validate_envelope(run, stage, envelope, inputs, actor)
        frozen_outputs: list[str] = []
        for value in envelope.get("outputs", []):
            frozen_outputs.append(self.artifacts.put_json(value, metadata={"schema": "wiki-stage-output-v1", "run_id": run_id, "stage": str(stage)}).digest)
        frozen = self.artifacts.put_json(envelope, metadata={"schema": "wiki-stage-envelope-v1", "run_id": run_id, "stage": str(stage), "actor": actor})
        for digest in (*inputs, *frozen_outputs):
            self.artifacts.link(digest, frozen.digest, relation="wiki_stage_input")
        outputs = tuple((*frozen_outputs, frozen.digest))
        # CAS digest is the exact idempotency envelope, preventing state drift.
        with self.state.transaction() as conn:
            old = conn.execute("SELECT * FROM wiki_runtime_stages WHERE run_id=? AND stage=?", (run_id, str(stage))).fetchone()
            if old:
                old_inputs, old_outputs = tuple(json.loads(old["input_hashes"])), tuple(json.loads(old["output_hashes"]))
                if old_inputs == inputs and old["envelope_hash"] == frozen.digest:
                    return self.get_run(run_id), old_outputs
                raise WikiStageConflict("STAGE_REPLAY_MISMATCH", f"{stage} was already completed with other evidence", quarantine=True)
            now = utcnow()
            conn.execute("INSERT INTO wiki_runtime_stages VALUES(?,?,?,?,?,?)", (run_id, str(stage), _canonical(inputs), _canonical(outputs), frozen.digest, now))
            status = "published" if stage is WikiStage.PUBLISHED else "running"
            conn.execute("UPDATE wiki_runtime_runs SET status=?,current_stage=?,updated_at=? WHERE run_id=?", (status, str(stage), now, run_id))
        self.events.append("wiki_run", run_id, "wiki.stage.completed", {"stage": str(stage), "input_hashes": list(inputs), "output_hashes": list(outputs), "envelope_hash": frozen.digest}, actor=actor)
        if stage is WikiStage.PUBLISHED:
            self.control.transition_job(run.job_id, JobState.CANDIDATE, reason="wiki publication candidate frozen")
            self.control.transition_job(run.job_id, JobState.GATED, reason="release gate passed")
            self.control.transition_job(run.job_id, JobState.APPLIED, reason="reviewed content hash locked")
            self.control.transition_job(run.job_id, JobState.PUBLISHED, reason="post-verify publication receipt frozen")
            self.state.finish_run(run_id, "succeeded")
        return self.get_run(run_id), outputs

    def submit_agent_output(self, run_id: str, stage: WikiStage | str, payload: dict[str, Any], *,
                            actor: str = "frozen-agent") -> tuple[WikiRun, tuple[str, ...]]:
        """Accept only frozen, non-executable agent artifacts in agent stages."""
        stage = WikiStage(stage)
        if stage not in AGENT_STAGES:
            raise WikiRuntimeError("AGENT_STAGE_DENIED", f"agents may not advance {stage}", quarantine=True)
        return self.advance(run_id, stage, payload, actor=actor)

    def fault(self, run_id: str, *, category: str, detail: dict[str, Any], stage: WikiStage | str | None = None) -> WikiRun:
        """Classify failure and deterministically quarantine unsafe executions."""
        run = self.get_run(run_id)
        code, quarantine = FAULTS.get(category, ("UNCLASSIFIED_FAILURE", False))
        detail_hash = self.artifacts.put_json(detail, metadata={"schema": "wiki-fault-v1", "run_id": run_id, "code": code}).digest
        classification = "quarantine" if quarantine else "repairable"
        with self.state.transaction() as conn:
            conn.execute("INSERT INTO wiki_runtime_faults VALUES(?,?,?,?,?,?,?)", (f"wiki_fault_{uuid.uuid4().hex}", run_id, str(stage) if stage else None, code, classification, detail_hash, utcnow()))
            if quarantine:
                conn.execute("UPDATE wiki_runtime_runs SET status='quarantined',quarantine_code=?,updated_at=? WHERE run_id=?", (code, utcnow(), run_id))
        self.events.append("wiki_run", run_id, "wiki.run.faulted", {"code": code, "classification": classification, "detail_hash": detail_hash, "stage": str(stage) if stage else None}, actor="wiki-runtime")
        if quarantine:
            self.control.transition_job(run.job_id, JobState.REPAIRABLE, reason=code)
            self.control.transition_job(run.job_id, JobState.QUARANTINED, reason=code)
            self.state.finish_run(run_id, "failed")
        return self.get_run(run_id)

    # ---- guards ---------------------------------------------------------
    def _check_admission(self, run: WikiRun, stage: WikiStage, envelope: dict[str, Any], actor: str) -> None:
        if run.status in TERMINAL_STATUSES:
            raise WikiRuntimeError("RUN_TERMINAL", f"run is {run.status}")
        expected_index = 0 if run.current_stage is None else STAGES.index(run.current_stage) + 1
        if stage in STAGES[:expected_index]:
            # Existing record will decide whether this is the same retry; never
            # let a replay leap to input validation with a newer state.
            return
        if expected_index >= len(STAGES) or stage is not STAGES[expected_index]:
            raise WikiRuntimeError("STAGE_ORDER_VIOLATION", f"expected {STAGES[expected_index] if expected_index < len(STAGES) else 'terminal'}, got {stage}", quarantine=True)
        if stage in AGENT_STAGES and not actor.startswith("frozen-agent"):
            raise WikiRuntimeError("AGENT_ATTESTATION_REQUIRED", f"{stage} needs a frozen-agent actor", quarantine=True)
        if stage not in AGENT_STAGES and actor.startswith("frozen-agent"):
            raise WikiRuntimeError("AGENT_SIDE_EFFECT_DENIED", "agent cannot advance deterministic/apply stages", quarantine=True)
        if not isinstance(envelope, dict):
            raise WikiRuntimeError("SCHEMA_INVALID", "stage envelope must be an object")

    def _input_hashes(self, run: WikiRun, stage: WikiStage, envelope: dict[str, Any]) -> tuple[str, ...]:
        supplied = envelope.get("input_hashes")
        if supplied is None:
            if stage is WikiStage.PLAN:
                return (run.dossier_hash,)
            previous = self.state._connection().execute("SELECT output_hashes FROM wiki_runtime_stages WHERE run_id=? AND stage=?", (run.run_id, str(STAGES[STAGES.index(stage) - 1]))).fetchone()
            if previous is None:
                raise WikiRuntimeError("STAGE_ORDER_VIOLATION", "previous stage evidence missing", quarantine=True)
            return tuple(json.loads(previous["output_hashes"]))
        if not isinstance(supplied, list) or not supplied or not all(isinstance(x, str) and len(x) == 64 for x in supplied):
            raise WikiRuntimeError("HASH_BINDING_MISMATCH", "input_hashes must be non-empty sha256 values", quarantine=True)
        return tuple(supplied)

    def _validate_envelope(self, run: WikiRun, stage: WikiStage, envelope: dict[str, Any], inputs: tuple[str, ...], actor: str) -> None:
        identity = envelope.get("node_identity")
        if not isinstance(identity, dict) or identity.get("node_id") != run.node_id:
            raise WikiRuntimeError("NODE_IDENTITY_MISMATCH", "frozen node_identity.node_id is required", quarantine=True)
        if stage is WikiStage.PLAN and inputs != (run.dossier_hash,):
            raise WikiRuntimeError("HASH_BINDING_MISMATCH", "plan must bind exactly to dossier hash", quarantine=True)
        if stage in AGENT_STAGES:
            expected = {WikiStage.RESEARCH_READY: "wiki-proposal-v1", WikiStage.VERIFIED: "wiki-verdict-v1", WikiStage.FROZEN: "wiki-attestation-v1"}[stage]
            if envelope.get("schema_version") != expected:
                raise WikiRuntimeError("SCHEMA_INVALID", f"{stage} requires {expected}")
            forbidden = self._agent_side_effect_fields(envelope)
            if forbidden:
                raise WikiRuntimeError("AGENT_SIDE_EFFECT_DENIED", f"agent output has forbidden fields: {sorted(forbidden)}", quarantine=True)
            if envelope.get("frozen_input_hash") not in inputs:
                raise WikiRuntimeError("HASH_BINDING_MISMATCH", "agent output is not bound to a frozen input", quarantine=True)
            agent_id = envelope.get("agent_id")
            definition_path = self.root / "agents" / str(agent_id) / "agent.json"
            if not definition_path.is_file():
                raise WikiRuntimeError("AGENT_ATTESTATION_REQUIRED", "registered agent definition is required", quarantine=True)
            definition = json.loads(definition_path.read_text(encoding="utf-8"))
            try:
                attestation = self.proofs.verify(
                    envelope.get("attestation_receipt"),
                    kind="agent-attestation",
                    subject=self.proof_subject(run.run_id, stage, inputs),
                )
                if attestation.get("prompt_hash") != prompt_hash(definition_path):
                    raise ProofError("prompt attestation mismatch")
                gate_runtime(definition, attestation)
            except (AssuranceError, ProofError) as exc:
                raise WikiRuntimeError("AGENT_ATTESTATION_REQUIRED", str(exc), quarantine=True) from exc
        if stage is WikiStage.DRAFT_GATED:
            try:
                self.proofs.verify_gates(
                    [envelope.get("gate_receipt")],
                    subject=self.proof_subject(run.run_id, stage, inputs),
                    required={"draft-content"}, input_hashes=set(inputs),
                )
            except ProofError as exc:
                raise WikiRuntimeError("GATE_NO_GO", "Draft Content Gate must be candidate-bound")
            if envelope.get("verdict") != "pass":
                raise WikiRuntimeError("GATE_NO_GO", "Draft Content Gate must pass")
        if stage is WikiStage.PREVIEWED and (envelope.get("preview") is not True or envelope.get("production") is not False):
            raise WikiRuntimeError("SCHEMA_INVALID", "preview must be explicitly non-production")
        if stage is WikiStage.RELEASE_GATED:
            gates = envelope.get("gate_receipts")
            if envelope.get("verdict") != "pass" or not isinstance(gates, list):
                raise WikiRuntimeError("GATE_NO_GO", "signed release gate receipts are required")
            try:
                self.proofs.verify_gates(
                    gates, subject=self.proof_subject(run.run_id, stage, inputs),
                    required={f"G{index}" for index in range(8)}, input_hashes=set(inputs),
                )
            except ProofError as exc:
                raise WikiRuntimeError("GATE_NO_GO", str(exc)) from exc
        if stage in {WikiStage.DRAFT_APPLIED, WikiStage.REVIEWED_APPLIED, WikiStage.PUBLISHED}:
            receipt = envelope.get("apply_receipt")
            target_hash = receipt.get("target_hash") if isinstance(receipt, dict) else None
            if not isinstance(target_hash, str) or len(target_hash) != 64 or any(ch not in "0123456789abcdef" for ch in target_hash):
                raise WikiRuntimeError("SCHEMA_INVALID", f"{stage} requires deterministic apply_receipt.target_hash")
            if actor.startswith("frozen-agent"):
                raise WikiRuntimeError("AGENT_SIDE_EFFECT_DENIED", "agent may not submit apply receipt", quarantine=True)
        if stage is WikiStage.REVIEWED_APPLIED:
            receipt = envelope["apply_receipt"]
            if not isinstance(receipt.get("expected_current"), str) or len(receipt["expected_current"]) != 64:
                raise WikiRuntimeError("HASH_BINDING_MISMATCH", "reviewed apply requires expected_current hash")
        if stage is WikiStage.PUBLISHED:
            if (envelope.get("post_verify") != "pass" or not isinstance(envelope.get("release_manifest_hash"), str)
                    or len(envelope["release_manifest_hash"]) != 64):
                raise WikiRuntimeError("GATE_NO_GO", "publish requires post-verify and release manifest hash")

    @staticmethod
    def proof_subject(run_id: str, stage: WikiStage | str, inputs: Iterable[str]) -> str:
        """Canonical subject used by trusted launchers/gates when minting proofs."""
        material = _canonical(sorted(inputs)).encode()
        import hashlib
        return f"wiki:{run_id}:{WikiStage(stage)}:{hashlib.sha256(material).hexdigest()}"

    @staticmethod
    def _require_node(node_id: str) -> None:
        if not isinstance(node_id, str) or not node_id or len(node_id) > 200:
            raise ValueError("node_id is required")

    @staticmethod
    def _require_identity(dossier: dict[str, Any], node_id: str) -> None:
        identity = dossier.get("node_identity") if isinstance(dossier, dict) else None
        if not isinstance(identity, dict) or identity.get("node_id") != node_id:
            raise WikiRuntimeError("NODE_IDENTITY_MISMATCH", "dossier must contain frozen node_identity.node_id", quarantine=True)

    @staticmethod
    def _agent_side_effect_fields(value: Any) -> set[str]:
        """Reject command/network/apply capabilities even when nested in output."""
        banned = {"command", "path", "write_path", "apply", "network", "url", "tool_call"}
        found: set[str] = set()
        if isinstance(value, dict):
            found |= banned & set(value)
            for item in value.values():
                found |= WikiRuntime._agent_side_effect_fields(item)
        elif isinstance(value, list):
            for item in value:
                found |= WikiRuntime._agent_side_effect_fields(item)
        return found

    @staticmethod
    def _run_row(row: Any) -> WikiRun:
        return WikiRun(row["run_id"], row["job_id"], row["node_id"], row["status"], WikiStage(row["current_stage"]) if row["current_stage"] else None, row["dossier_hash"], row["policy_version"], int(row["attempt"]), row["created_at"], row["updated_at"], row["quarantine_code"])
