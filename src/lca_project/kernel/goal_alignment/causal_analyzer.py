"""Evidence-bound deterministic causal classification."""
from __future__ import annotations

from .models import Deviation, Diagnosis


class CausalAnalyzer:
    TRIAGE_CAUSES = {
        "REPAIR_DID_NOT_CHANGE_CAUSAL_INPUT", "UNMODELLED_FAILURE",
        "RESEARCH_OUTCOME_CAUSE_REQUIRES_TRIAGE", "QUALITY_TRAJECTORY_REGRESSION",
    }

    @classmethod
    def requires_agent_triage(cls, diagnosis: Diagnosis) -> bool:
        return diagnosis.cause_code in cls.TRIAGE_CAUSES

    def analyze(self, deviation: Deviation) -> Diagnosis:
        evidence = deviation.evidence
        code = str(evidence.get("failure_code") or "")
        failure = evidence.get("failure") or {}
        message = str(failure.get("message") or "") if isinstance(failure, dict) else ""
        gate_result = failure.get("gate_result") or {} if isinstance(failure, dict) else {}
        gate_failures = {
            str(name) for name in (gate_result.get("failures") or [])
        } if isinstance(gate_result, dict) else set()
        if (code == "RESEARCH_PLAN_INVALID" and isinstance(failure, dict)
                and failure.get("identical_failure_repeated") is True):
            return Diagnosis("REPAIR_DID_NOT_CHANGE_CAUSAL_INPUT", 0.99, evidence,
                             "研究计划修复后同一 Gate 指纹再次出现，必须升级到问题驱动的 Agent Triage")
        if (code == "RESEARCH_PLAN_INVALID" and gate_failures
                and all(name.startswith("english_") for name in gate_failures)):
            return Diagnosis("GATE_GOAL_CONTRACT_DRIFT", 0.99, evidence,
                             "英文发现增强项被前置 Gate 错误提升为全流程硬阻塞")
        if (code == "CAPABILITY_PROCESS_FAILED"
                and evidence.get("contract") == "editorial_policy_vs_raw_review"
                and "Editorial Review GO" in message):
            return Diagnosis("EDITORIAL_POLICY_CONTRACT_MISMATCH", 0.99, evidence,
                             "编辑阶段按策略决定成功，下游却按原始审查结果失败，属于跨阶段合同漂移")
        if deviation.deviation_type == "false_block" and code == "RESEARCH_PLAN_INVALID":
            return Diagnosis("DISCOVERY_TRANSLATION_COVERAGE_GAP", 0.98, evidence,
                             "发现查询词表不完整或修复预算边界错误，并非证据目标不可达")
        if deviation.deviation_type == "false_pass":
            return Diagnosis("GATE_GOAL_CONTRACT_DRIFT", 0.96, evidence,
                             "状态机成功条件与 Goal Contract 的成熟度条件不一致")
        if deviation.deviation_type == "success_without_maturity":
            return Diagnosis("TERMINAL_STATE_WITHOUT_GOAL_PROOF", 0.99, evidence,
                             "终态聚合只观察任务结束，没有绑定目标证明")
        if deviation.deviation_type == "low_research_utility":
            return Diagnosis("RESEARCH_OUTCOME_CAUSE_REQUIRES_TRIAGE", 0.9, evidence,
                             "检索流程完成但字段级证据产出为零，需要区分查询、来源、抽取或字段合同缺口")
        if deviation.deviation_type in {"repeated_fault", "ineffective_repair"}:
            return Diagnosis("REPAIR_DID_NOT_CHANGE_CAUSAL_INPUT", 0.92, evidence,
                             "修复没有改变失败指纹所依赖的输入、策略或能力")
        if deviation.deviation_type == "unclassified_failure":
            family = str(evidence.get("mechanism_family") or "unknown")
            return Diagnosis("UNMODELLED_FAILURE", 0.65 if family != "unknown" else 0.5,
                             evidence,
                             f"失败已稳定归入机制族 {family}，但具体因果输入仍需只读 Agent 调查")
        if deviation.deviation_type == "quality_regression":
            return Diagnosis("QUALITY_TRAJECTORY_REGRESSION", 0.9, evidence,
                             "局部放行改善了可执行性，但降低了至少一个目标维度")
        return Diagnosis("UNMODELLED_GOAL_ESCAPE", 0.7, evidence,
                         "当前指标未能在人工反馈前覆盖该目标偏离")
