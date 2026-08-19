"""Governed evolution of Goal, Autonomy, Assurance, and Capability contracts.

The implementation is split into small mixins to keep source units reviewable
while preserving the public GovernanceController API.
"""
from __future__ import annotations

from typing import Any, Mapping

from lca_project.kernel.proofs import ProofError

from .governance_support import GovernanceError
from .governance_registry_mixin import ContractRegistryMixin
from .governance_amendment_mixin import GoalAmendmentMixin
from .governance_autonomy_mixin import AutonomyEligibilityMixin
from .governance_alignment_mixin import AlignmentAssessmentMixin
from .governance_assurance_mixin import CapabilityAssuranceMixin


class GovernanceController(
    CapabilityAssuranceMixin,
    AlignmentAssessmentMixin,
    AutonomyEligibilityMixin,
    GoalAmendmentMixin,
    ContractRegistryMixin,
):
    """Persistent control boundary for contracts, amendments, and assessments."""

    def __init__(
        self,
        state,
        *,
        require_job_exists: bool = False,
        proof_authority=None,
        require_trusted_proofs: bool = False,
    ) -> None:
        super().__init__(state)
        self.require_job_exists = require_job_exists
        self.proof_authority = proof_authority
        self.require_trusted_proofs = require_trusted_proofs
        if require_trusted_proofs and proof_authority is None:
            raise GovernanceError("trusted governance proofs require a ProofAuthority")

    def sign_evidence_record(
        self,
        record: Mapping[str, Any],
        *,
        subject: str,
        producer: str,
    ) -> dict[str, Any]:
        """Attach a locally signed receipt to one immutable evidence record."""
        if self.proof_authority is None:
            return dict(record)
        claims = dict(record)
        receipt = self.proof_authority.issue(
            kind="governance-evidence",
            subject=subject,
            producer=producer,
            claims=claims,
        )
        return {**claims, "authority_receipt": receipt}

    def verify_evidence_record(
        self, record: Mapping[str, Any], *, subject: str
    ) -> str | None:
        """Return a finding when a required authority receipt is invalid."""
        if not self.require_trusted_proofs:
            return None
        receipt = record.get("authority_receipt")
        if not isinstance(receipt, dict):
            return "trusted Proof Authority receipt is missing"
        assert self.proof_authority is not None
        try:
            claims = self.proof_authority.verify(
                receipt, kind="governance-evidence", subject=subject
            )
        except ProofError as exc:
            return f"trusted Proof Authority rejected evidence: {exc}"
        expected = {key: value for key, value in record.items() if key != "authority_receipt"}
        if claims != expected:
            return "trusted Proof Authority claims do not match the evidence record"
        return None


__all__ = ["GovernanceController", "GovernanceError"]
