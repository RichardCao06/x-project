"""Governance adapter for the existing hash-locked ReleaseManager.

The adapter keeps the release implementation unchanged and adds an explicit
shadow/enforced control boundary.  In enforced mode a release cannot be
applied unless the bound Goal, Autonomy, Assurance, and Capability contracts
produce an authorized publish decision.  Shadow mode records the same decision
without changing production behavior, which supports a safe migration.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from lca_project.contracts.governance import AutonomyEligibility, canonical_json, payload_digest
from lca_project.kernel.goal_alignment.governance import GovernanceController
from lca_project.kernel.state import utcnow


class GovernanceMode(StrEnum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    ENFORCED = "enforced"


class ReleaseGovernanceError(RuntimeError):
    """Raised when enforced governance refuses a release."""


class ReleaseDelegate(Protocol):
    release_root: Path

    def stage(self, files: dict[str, bytes], **kwargs: Any) -> Any: ...

    def apply(self, staged: Any, destination: str | Path) -> Path: ...

    def rollback(self, staged: Any, destination: str | Path) -> None: ...


@dataclass(frozen=True)
class ReleaseGovernanceRecord:
    release_id: str
    job_id: str
    mode: GovernanceMode
    action: str
    status: str
    release_subject: str
    eligibility: dict[str, Any]
    created_at: str
    backup_path: str | None = None
    error: str | None = None
    schema_version: str = "release-governance-record-v1"

    def asdict(self) -> dict[str, Any]:
        value = asdict(self)
        value["mode"] = self.mode.value
        return value


class GovernedReleaseManager:
    """Compose governance with an existing ReleaseManager without API breakage."""

    def __init__(
        self,
        delegate: ReleaseDelegate,
        governance: GovernanceController,
        *,
        mode: GovernanceMode | str = GovernanceMode.SHADOW,
        audit_root: str | Path | None = None,
    ) -> None:
        self.delegate = delegate
        self.governance = governance
        self.mode = GovernanceMode(mode)
        release_root = Path(delegate.release_root)
        self.audit_root = Path(audit_root) if audit_root is not None else release_root / "governance"
        self.audit_root.mkdir(parents=True, exist_ok=True)
        self.last_eligibility: AutonomyEligibility | None = None

    def stage(self, files: dict[str, bytes], **kwargs: Any) -> Any:
        return self.delegate.stage(files, **kwargs)

    @staticmethod
    def _release_subject(staged: Any) -> str:
        manifest = dict(getattr(staged, "manifest"))
        return "release:" + payload_digest(manifest)

    @staticmethod
    def _generated_requirement_evidence(
        *, staged: Any, destination: str | Path, binding_hash: str
    ) -> dict[str, dict[str, Any]]:
        manifest = dict(getattr(staged, "manifest"))
        expected_current = dict(getattr(staged, "expected_current", {}))
        release_id = str(getattr(staged, "id"))
        subject = GovernedReleaseManager._release_subject(staged)
        attestation_body = {
            "schema_version": "release-attestation-evidence-v1",
            "release_id": release_id,
            "release_subject": subject,
            "binding_hash": binding_hash,
            "candidate_hashes": manifest,
        }
        rollback_body = {
            "schema_version": "rollback-plan-evidence-v1",
            "release_id": release_id,
            "release_subject": subject,
            "binding_hash": binding_hash,
            "destination": str(Path(destination).resolve()),
            "expected_current": expected_current,
        }
        return {
            "release_attestation": {
                "artifact_ref": f"release://{release_id}/attestation",
                "certificate_hash": payload_digest(attestation_body),
                "issuer_actor": "governed-release-manager",
                "payload": attestation_body,
            },
            "rollback": {
                "artifact_ref": f"release://{release_id}/rollback-plan",
                "certificate_hash": payload_digest(rollback_body),
                "issuer_actor": "governed-release-manager",
                "payload": rollback_body,
            },
        }

    def _write_record(self, record: ReleaseGovernanceRecord) -> Path:
        release_dir = self.audit_root / record.release_id
        release_dir.mkdir(parents=True, exist_ok=True)
        body = canonical_json(record.asdict())
        safe_time = record.created_at.replace(":", "-").replace("+", "_")
        digest = payload_digest(record.asdict())
        path = release_dir / f"{safe_time}-{record.status}-{digest}.json"
        # Audit records are immutable, append-only facts.  Exclusive creation
        # prevents a later evaluation from rewriting an earlier decision.
        with path.open("x", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def apply(
        self,
        staged: Any,
        destination: str | Path,
        *,
        job_id: str,
        risk: str,
        runtime_fingerprint: Mapping[str, Any],
        input_scope: Mapping[str, Any],
        requested_authority: Iterable[str] = (),
        satisfied_requirements: Iterable[str] = (),
        requirement_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> Path:
        if self.mode is GovernanceMode.DISABLED:
            return self.delegate.apply(staged, destination)
        binding = self.governance.binding(job_id)
        if binding is None:
            raise ReleaseGovernanceError(f"Job has no immutable governance binding: {job_id}")
        if not callable(getattr(self.delegate, "rollback", None)):
            raise ReleaseGovernanceError("governed release delegate must provide rollback")

        generated = self._generated_requirement_evidence(
            staged=staged,
            destination=destination,
            binding_hash=binding["binding_hash"],
        )
        supplied = dict(requirement_evidence or {})
        protected = sorted(set(generated) & set(supplied))
        if protected:
            raise ReleaseGovernanceError(
                "caller cannot override release-controller evidence: " + ", ".join(protected)
            )
        generated.update(supplied)
        eligibility = self.governance.check_autonomy(
            job_id=job_id,
            action="publish",
            risk=risk,
            runtime_fingerprint=runtime_fingerprint,
            input_scope=input_scope,
            requested_authority=requested_authority,
            satisfied_requirements=satisfied_requirements,
            requirement_evidence=generated,
        )
        self.last_eligibility = eligibility
        release_id = str(getattr(staged, "id"))
        subject = self._release_subject(staged)
        evaluated_status = "authorized" if eligibility.authorized else "not_authorized"
        self._write_record(ReleaseGovernanceRecord(
            release_id=release_id,
            job_id=job_id,
            mode=self.mode,
            action="publish",
            status=evaluated_status,
            release_subject=subject,
            eligibility=eligibility.asdict(),
            created_at=utcnow(),
        ))
        if self.mode is GovernanceMode.ENFORCED and not eligibility.authorized:
            raise ReleaseGovernanceError(
                "release is not authorized: " + "; ".join(eligibility.reasons)
            )

        try:
            backup = self.delegate.apply(staged, destination)
        except Exception as exc:
            self._write_record(ReleaseGovernanceRecord(
                release_id=release_id,
                job_id=job_id,
                mode=self.mode,
                action="publish",
                status="apply_failed",
                release_subject=subject,
                eligibility=eligibility.asdict(),
                created_at=utcnow(),
                error=f"{type(exc).__name__}: {exc}",
            ))
            raise
        self._write_record(ReleaseGovernanceRecord(
            release_id=release_id,
            job_id=job_id,
            mode=self.mode,
            action="publish",
            status="applied" if eligibility.authorized else "shadow_applied",
            release_subject=subject,
            eligibility=eligibility.asdict(),
            created_at=utcnow(),
            backup_path=str(backup),
        ))
        return backup
