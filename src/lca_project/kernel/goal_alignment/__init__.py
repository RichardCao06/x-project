"""Closed-loop supervision for goal-aligned autonomous production."""

from .controller import GoalAlignmentController
from .system_repair_agent import SystemRepairAgent
from .system_repair_scm import SystemRepairScmPublisher
from .change_controller import ChangeController
from .goal_registry import GoalRegistry
from .failure_triage_agent import FailureTriageAgent
from .failure_taxonomy import classify_failure
from .meta_supervisor import SystemMetaSupervisor
from .governance import GovernanceController, GovernanceError
from .autonomous_supervisor import verify_reviewed_publication

__all__ = [
    "ChangeController", "FailureTriageAgent", "GoalAlignmentController",
    "GoalRegistry", "SystemMetaSupervisor", "SystemRepairAgent", "classify_failure",
    "SystemRepairScmPublisher",
    "GovernanceController", "GovernanceError",
    "verify_reviewed_publication",
]
