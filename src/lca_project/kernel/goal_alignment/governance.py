"""Governed evolution of Goal, Autonomy, Assurance, and Capability contracts.

The implementation is split into small mixins to keep source units reviewable
while preserving the public GovernanceController API.
"""
from __future__ import annotations

from .governance_support import GovernanceError
from .governance_registry_mixin import ContractRegistryMixin
from .governance_amendment_mixin import GoalAmendmentMixin
from .governance_autonomy_mixin import AutonomyEligibilityMixin
from .governance_alignment_mixin import AlignmentAssessmentMixin


class GovernanceController(
    AlignmentAssessmentMixin,
    AutonomyEligibilityMixin,
    GoalAmendmentMixin,
    ContractRegistryMixin,
):
    """Persistent control boundary for contracts, amendments, and assessments."""


__all__ = ["GovernanceController", "GovernanceError"]
