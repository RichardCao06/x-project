"""Derive a goal-vector from durable Wiki protocol artifacts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import QualityObservation
from .research_outcome import ResearchOutcomeEvaluator


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _pass(value: dict[str, Any]) -> float:
    if value.get("candidate_eligible") is True or value.get("go") is True:
        return 1.0
    decision = str(value.get("decision") or value.get("verdict") or "").upper()
    if decision in {
        "PASS", "PASS_WITH_DEBT", "GO", "APPROVED", "ACCEPT",
        "ACCEPT_WITH_ADVISORIES",
    }:
        return 1.0
    if not value:
        return 0.0
    return 0.0


def _question_closure_score(value: dict[str, Any]) -> float:
    ledger = value.get("question_evidence_ledger") or {}
    metrics = ledger.get("metrics") if isinstance(ledger, dict) else {}
    total = int((metrics or {}).get("critical_questions_total") or 0)
    confirmed = int((metrics or {}).get("critical_questions_confirmed") or 0)
    if total:
        return round(min(1.0, confirmed / total), 6)
    return _pass(value)


class QualityTrajectory:
    FILES = {
        "research_plan": "research-plan.json",
        "research_plan_gate": "research-plan-gate.json",
        "search_execution_gate": "search-execution-gate.json",
        "terminology": "terminology-verdict.json",
        "source_diversity": "source-diversity-gate.json",
        "blueprint": "content-blueprint.json",
        "closure": "content-closure-gate.json",
        "editorial": "editorial-loop/editorial-policy-decision.json",
        "draft": "draft-content-gate.json",
        "table_search": "table-data/search-execution-gate.json",
        "table_verdict": "table-data/source-verdict.json",
        "table_population": "table-data/table-population-gate.json",
        "table_matrix": "table-data/search-matrix.executed.json",
        "table_selection": "table-data/evidence-selection.json",
        "maturity": "maturity-gate.json",
    }

    ARTIFACT_TASKS = {
        "research_plan": "research_plan",
        "research_plan_gate": "research_plan_gate",
        "search_execution_gate": "search_execution_gate",
        "terminology": "terminology_verify",
        "source_diversity": "source_diversity_gate",
        "blueprint": "content_blueprint",
        "closure": "content_closure_gate",
        "editorial": "editorial_review",
        "draft": "draft_content_gate",
        "table_search": "table_search_execution_gate",
        "table_verdict": "table_verify",
        "table_population": "table_population_gate",
        "table_matrix": "table_collect",
        "table_selection": "table_collect",
        "maturity": "maturity_gate",
    }

    @staticmethod
    def _values(value: Any) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return None
        if not isinstance(value, list):
            return None
        return [str(item) for item in value]

    @classmethod
    def _current_succeeded_tasks(
        cls, tasks: list[dict[str, Any]],
    ) -> tuple[set[str], dict[str, str]]:
        """Accept only succeeded tasks bound to current dependency outputs."""
        by_id = {str(item.get("task_id") or ""): item for item in tasks}
        accepted: set[str] = set()
        rejected: dict[str, str] = {}
        for task_id, task in by_id.items():
            if str(task.get("status") or "") != "succeeded":
                rejected[task_id] = "task_not_succeeded"
                continue
            dependencies = cls._values(task.get("dependencies")) or []
            recorded = cls._values(task.get("recorded_input_hashes"))
            expected = [str((by_id.get(parent) or {}).get("output_hash") or "")
                        for parent in dependencies]
            if dependencies and (recorded is None or not all(expected) or recorded != expected):
                rejected[task_id] = "current_upstream_hash_mismatch"
                continue
            accepted.add(task_id)
        return accepted, rejected

    def observe(self, *, job_id: str, run_id: str | None, goal: dict[str, Any],
                batch: Path | None, tasks: list[dict[str, Any]] | None = None,
                run_status: str | None = None) -> QualityObservation:
        docs = {key: _load(batch / relative) if batch else {}
                for key, relative in self.FILES.items()}
        rejected_protocols: dict[str, str] = {}
        task_statuses: dict[str, str] = {}
        if tasks is not None:
            accepted_tasks, rejected_tasks = self._current_succeeded_tasks(tasks)
            task_statuses = {str(item.get("task_id") or ""): str(item.get("status") or "")
                             for item in tasks}
            for protocol, task_id in self.ARTIFACT_TASKS.items():
                if docs[protocol] and task_id not in accepted_tasks:
                    docs[protocol] = {}
                    rejected_protocols[protocol] = rejected_tasks.get(
                        task_id, "no_current_succeeded_task"
                    )
        conflicts = docs["blueprint"].get("semantic_conflicts") or []
        unresolved = [item for item in conflicts if not isinstance(item, dict)
                      or item.get("resolved") is not True]
        maturity_checks = docs["maturity"].get("checks") or {}
        gap_ok = maturity_checks.get("explicit_gaps_have_search_provenance")
        if gap_ok is None:
            gap_ok = maturity_checks.get("evidence_gaps_provenanced")
        if gap_ok is None:
            gap_ok = maturity_checks.get("table_gaps_provenanced")
        terminology_status = str(docs["terminology"].get("status") or "")
        identity_ok = bool(docs["terminology"]) and terminology_status in {
            "CONFIRMED_EQUIVALENT", "UNRESOLVED"
        } and docs["terminology"].get("aliases_authorized_for_discovery") is True
        data_readiness = str(docs["maturity"].get("data_readiness") or "")
        research_outcome = ResearchOutcomeEvaluator().evaluate(
            docs,
            task_completion={"tasks": task_statuses, "run_status": run_status}
            if tasks is not None else None,
        )
        dimensions = {
            # Honest non-equivalence is valid identity handling; silently using
            # a discovery alias as canonical identity is not.
            "identity_fidelity": 1.0 if identity_ok else 0.0,
            "source_role_coverage": _question_closure_score(docs["source_diversity"]),
            "claim_provenance_coverage": min(_pass(docs["search_execution_gate"]),
                                               _pass(docs["closure"])),
            "semantic_closure": 1.0 if docs["blueprint"] and not unresolved
                                and _pass(docs["closure"]) else 0.0,
            "editorial_coherence": _pass(docs["editorial"]),
            "table_contract_validity": min(_pass(docs["table_verdict"]),
                                             _pass(docs["table_population"])),
            # ``no_eligible_public_data`` is an honest process outcome, not
            # proof that LCA modelling data is ready.
            "data_readiness": 1.0 if data_readiness == "data_ready" else 0.0,
            "gap_provenance": 1.0 if gap_ok is True else 0.0,
            "reader_utility": _pass(docs["draft"]),
        }
        weights = goal["quality_dimensions"]
        score = round(sum(dimensions[name] * float(weights[name]["weight"])
                          for name in dimensions), 6)
        evidence = {
            "batch": str(batch) if batch else None,
            "available_protocols": sorted(key for key, value in docs.items() if value),
            "maturity": docs["maturity"],
            "semantic_conflicts": unresolved,
            "research_outcome": research_outcome,
            "research_progress": {
                "question_closure_score": _question_closure_score(docs["source_diversity"]),
                "question_evidence_metrics": (
                    (docs["source_diversity"].get("question_evidence_ledger") or {}).get("metrics")
                    or {}
                ),
                "failed_requirement_ids": docs["source_diversity"].get(
                    "failed_requirement_ids"
                ) or [],
                "question_contract_sha256": docs["source_diversity"].get(
                    "question_contract_sha256"
                ),
                "strategy_hash": docs["source_diversity"].get("strategy_hash"),
            },
            "task_completion": {"run_status": run_status, "tasks": task_statuses},
            "rejected_protocols": rejected_protocols,
        }
        return QualityObservation(job_id, run_id, goal["goal_id"], dimensions, score, evidence)
