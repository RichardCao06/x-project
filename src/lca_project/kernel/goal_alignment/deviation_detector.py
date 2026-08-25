"""Detect execution success that diverges from the declared Wiki goal."""
from __future__ import annotations

from typing import Any

from .models import Deviation, QualityObservation
from .failure_taxonomy import taxonomy_record


class DeviationDetector:
    def detect(self, *, job: dict[str, Any], run: dict[str, Any] | None,
               tasks: list[dict[str, Any]], observation: QualityObservation,
               previous_score: float | dict[str, Any] | None = None) -> list[Deviation]:
        result: list[Deviation] = []
        by_id = {str(item["task_id"]): item for item in tasks}
        failed = [item for item in tasks if item.get("status") in {
            "repairable", "manual_review", "quarantined", "failed"
        }]
        for task in failed:
            specialized = False
            code = str(task.get("failure_code") or "")
            payload = task.get("failure_payload") or {}
            if isinstance(payload, str):
                payload = {}
            message = str(payload.get("message") or "")
            taxonomy = taxonomy_record(
                task_id=str(task.get("task_id") or ""), failure_code=code,
                payload=payload,
            )
            if (code == "CAPABILITY_PROCESS_FAILED"
                    and str(task.get("task_id")) == "draft_content_gate"
                    and "Editorial Review GO" in message):
                result.append(Deviation(
                    "ineffective_repair", "high",
                    {"task_id": task["task_id"], "failure_code": code,
                     "attempt": int(task.get("attempt") or 0),
                     "failure": payload,
                     **taxonomy,
                     "contract": "editorial_policy_vs_raw_review"},
                    "Editorial Policy 已放行，但下游仍按原始 NO_GO 拒绝同一内容",
                ))
                specialized = True
            if code == "RESEARCH_PLAN_INVALID":
                result.append(Deviation(
                    "false_block", "high",
                    {"task_id": task["task_id"], "failure_code": code,
                     "attempt": int(task.get("attempt") or 0),
                     "failure": payload, **taxonomy},
                    "可恢复的发现查询翻译缺口被错误升级为人工阻塞",
                ))
                specialized = True
            if not specialized and payload.get("identical_failure_repeated") is True:
                result.append(Deviation(
                    "repeated_fault", "high",
                    {"task_id": task["task_id"], "failure_code": code,
                     "attempt": int(task.get("attempt") or 0),
                     "failure_fingerprint": payload.get("failure_fingerprint"),
                     "failure": payload, **taxonomy},
                    "相同失败指纹重复出现，盲目重试没有改变系统行为",
                ))
            elif not specialized:
                result.append(Deviation(
                    "unclassified_failure", "high",
                    {"task_id": task["task_id"], "failure_code": code,
                     "attempt": int(task.get("attempt") or 0), "failure": payload,
                     **taxonomy},
                    "确定性规则无法解释该失败，需要基于问题本身进行只读根因调查",
                ))
        maturity = observation.evidence.get("maturity") or {}
        candidate = job.get("status") in {"candidate", "gated", "applied", "published"}
        if candidate and maturity.get("candidate_eligible") is not True:
            result.append(Deviation(
                "false_pass", "critical",
                {"job_status": job.get("status"), "maturity": maturity,
                 "dimensions": observation.dimensions},
                "Job 已进入候选/发布状态，但目标成熟度并未通过",
            ))
        run_succeeded = bool(run and run.get("status") == "succeeded")
        if run_succeeded and not maturity:
            result.append(Deviation(
                "success_without_maturity", "critical",
                {"run_status": run.get("status"), "maturity_gate_task": by_id.get("maturity_gate")},
                "Workflow 报告成功，但没有可验证的目标成熟度记录",
            ))
        research_outcome = observation.evidence.get("research_outcome") or {}
        terminal_research = run_succeeded or job.get("status") in {
            "diagnostic_preview", "evidence_limited", "candidate", "gated",
            "applied", "published",
        }
        if terminal_research and research_outcome.get("needs_investigation") is True:
            result.append(Deviation(
                "low_research_utility", "critical" if run_succeeded else "high",
                {"task_id": "maturity_gate", "job_status": job.get("status"),
                 "run_status": (run or {}).get("status"),
                 "research_outcome": research_outcome},
                "流程已经结束，但没有产生让 LCA 建模目标前进的字段级证据",
            ))
        comparison = self._eligible_quality_regression(previous_score, observation)
        if comparison is not None:
            result.append(Deviation(
                "quality_regression", "high",
                {"previous_score": comparison["previous_score"],
                 "current_score": observation.score,
                 "lineage_compatible": True,
                 "prior_lineage": comparison["prior_lineage"],
                 "current_lineage": comparison["current_lineage"],
                 "dimension_deltas": comparison["dimension_deltas"],
                 "changed_producers": comparison["changed_producers"],
                 "task_statuses": {
                     task_id: str(item.get("status") or "")
                     for task_id, item in by_id.items()
                 }},
                "本次执行的目标质量向量相对上一观测发生回退",
            ))
        return result

    @staticmethod
    def _eligible_quality_regression(
        value: float | dict[str, Any] | None,
        observation: QualityObservation,
    ) -> dict[str, Any] | None:
        """Fail closed unless a complete frontier-aware comparison is supplied."""
        if not isinstance(value, dict) or value.get("lineage_compatible") is not True:
            return None
        prior = value.get("prior_lineage")
        current = value.get("current_lineage")
        deltas = value.get("dimension_deltas")
        changed = value.get("changed_producers")
        if not all(isinstance(item, dict) for item in (prior, current, deltas, changed)):
            return None
        frontier = prior.get("protocol_frontier")
        if (not isinstance(frontier, list) or not frontier
                or frontier != current.get("protocol_frontier")
                or prior.get("quality_checkpoint") != current.get("quality_checkpoint")):
            return None
        if (prior.get("accepted_protocols") != frontier
                or current.get("accepted_protocols") != frontier):
            return None
        if (prior.get("quality_checkpoint") != "terminal"
                and prior.get("run_id") != current.get("run_id")):
            return None
        for lineage in (prior, current):
            producers = lineage.get("producer_output_hashes")
            if not isinstance(producers, dict) or set(producers) != set(frontier):
                return None
            if any(not str((producers.get(name) or {}).get("task_id") or "")
                   or not str((producers.get(name) or {}).get("output_hash") or "")
                   for name in frontier):
                return None
        expected_changed = {
            name: {"prior": prior["producer_output_hashes"][name],
                   "current": current["producer_output_hashes"][name]}
            for name in frontier
            if prior["producer_output_hashes"][name]
            != current["producer_output_hashes"][name]
        }
        if not changed or changed != expected_changed:
            return None
        current_dimensions = current.get("dimension_vector")
        prior_dimensions = prior.get("dimension_vector")
        if (not isinstance(current_dimensions, dict) or not isinstance(prior_dimensions, dict)
                or current_dimensions != observation.dimensions
                or set(current_dimensions) != set(prior_dimensions)
                or set(deltas) != set(current_dimensions)):
            return None
        try:
            previous = float(value["previous_score"])
            current_score = float(value["current_score"])
            expected_deltas = {
                name: round(float(current_dimensions[name]) - float(prior_dimensions[name]), 6)
                for name in current_dimensions
            }
        except (KeyError, TypeError, ValueError):
            return None
        if (abs(current_score - observation.score) > 1e-9
                or observation.score + 1e-9 >= previous
                or deltas != expected_deltas
                or not any(delta < -1e-9 for delta in expected_deltas.values())):
            return None
        return value

    @staticmethod
    def user_escape(*, message: str, category: str = "user_feedback") -> Deviation:
        return Deviation("user_escape", "high", {"category": category, "message": message},
                         "人工反馈指出当前自动指标未覆盖的目标偏离")
