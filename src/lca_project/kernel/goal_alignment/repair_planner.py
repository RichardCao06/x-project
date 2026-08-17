"""Route diagnoses to bounded L0/L1 repairs or governed L2 changes."""
from __future__ import annotations

from .models import Diagnosis, RepairProposal


class RepairPlanner:
    @staticmethod
    def from_triage(result: dict) -> RepairProposal:
        """Translate an Agent hypothesis into bounded authority, not a fingerprint rule."""
        level, action = str(result["repair_level"]), str(result["repair_action"])
        task = str(result.get("recovery_task") or "")
        tests = tuple(str(item) for item in result.get("validation_tests") or [])
        if level == "L2" and action.startswith("propose_"):
            return RepairProposal(
                "L2", action, "change_controller", (), (),
                tuple(dict.fromkeys((*tests, "shadow", "canary", "regression"))), False,
            )
        # External authority applies at the smallest mutating boundary.  An
        # internal implementation defect with concrete code targets may still
        # be coded and validated autonomously; medium/high-risk promotion will
        # pause later in ChangeController rather than discarding the repair.
        safe_code_analysis = any(
            item.get("kind") == "propose_code_change"
            and item.get("authority") == "automatic_analysis_and_validation"
            for item in result.get("actions") or []
        )
        if (action == "request_operator"
                and result.get("safe_autonomous_actions_remaining") is True
                and safe_code_analysis
                and result.get("implementation_targets")):
            return RepairProposal(
                "L2", "propose_code_change", "change_controller", (), (),
                tuple(dict.fromkeys((*tests, "shadow", "canary", "regression"))), False,
            )
        if action == "retry_task":
            return RepairProposal("L0", "retry_triaged_task", "automatic",
                                  (task,) if task else (), (), (task,) if task else (), True)
        if action == "rewind_task":
            return RepairProposal("L1", "rewind_triaged_task", "automatic",
                                  (task,) if task else (), (), (task,) if task else (), True)
        if action == "expand_research":
            return RepairProposal("L1", "rewind_research_plan", "automatic",
                                  ("research_plan",), (), ("search_execution_gate",), True)
        return RepairProposal("manual", action, "operator", (), (), tests, False)

    def plan(self, diagnosis: Diagnosis) -> RepairProposal:
        cause = diagnosis.cause_code
        if cause == "EDITORIAL_POLICY_CONTRACT_MISMATCH":
            return RepairProposal("L2", "propose_code_change", "change_controller", (), (),
                                  ("golden", "mutation", "regression", "shadow", "canary"), False)
        if cause == "DISCOVERY_TRANSLATION_COVERAGE_GAP":
            return RepairProposal("L1", "rewind_research_plan", "automatic",
                                  ("research_plan", "research_plan_gate"), ("plan", "prepare"),
                                  ("research_plan_gate", "search_execution_gate"), True)
        if cause == "REPAIR_DID_NOT_CHANGE_CAUSAL_INPUT":
            return RepairProposal("L1", "stop_blind_retry", "automatic", (), (),
                                  ("operator_diagnosis",), True)
        if cause in {"GATE_GOAL_CONTRACT_DRIFT", "TERMINAL_STATE_WITHOUT_GOAL_PROOF"}:
            return RepairProposal("L2", "propose_gate_change", "change_controller", (), (),
                                  ("golden", "mutation", "regression", "shadow", "canary"), False)
        if cause == "QUALITY_TRAJECTORY_REGRESSION":
            return RepairProposal("L2", "propose_policy_change", "change_controller", (), (),
                                  ("golden", "regression", "shadow", "canary"), False)
        return RepairProposal("L2", "propose_observability_change", "change_controller", (), (),
                              ("golden", "mutation", "regression"), False)
