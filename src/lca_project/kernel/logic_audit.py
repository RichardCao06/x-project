"""Independent, read-only logic review for persisted Workflow facts.

Logic audits are advisory observations, not Gates and not deviation reports.
They never change Job/Run/Task state and cannot dispatch triage or repair.  An
operator may explicitly promote one finding through the existing user-feedback
boundary; that promotion still creates only a proposed investigation record.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable, Iterable

from ..control import ControlPlane
from .goal_alignment.execution_ownership import ExecutionOwnership
from .leases import LeaseLost
from .state import utcnow


LogicAuditRunner = Callable[[Path, dict[str, Any]], dict[str, Any]]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return {} if default is None else default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {} if default is None else default


class LogicAuditError(ValueError):
    """A read-only audit could not produce a valid observation report."""


class LogicAuditAgent:
    """Review action/premise/conclusion coherence without workflow authority."""

    MODEL = "gpt-5.6-terra"
    POLICY_VERSION = "logic-audit-policy-v1"
    TERMINAL = {"completed", "failed"}
    TERMINAL_STAGE = {
        "succeeded", "failed", "repairable", "manual_review", "quarantined",
        "skipped", "blocked", "blocked_budget",
    }
    TERMINAL_JOB = {
        "candidate", "gated", "applied", "published", "diagnostic_preview",
        "evidence_limited", "failed", "quarantined", "superseded",
    }
    SEMANTIC_STAGES = {
        "research_plan", "research_plan_gate", "research_ready",
        "search_execution_gate", "verify", "terminology_verify",
        "source_diversity_gate", "content_blueprint", "content_compose",
        "content_closure_gate", "editorial_review", "draft_content_gate",
        "table_collect", "table_search_execution_gate", "table_verify",
        "table_population_gate", "maturity_gate", "release_gate",
    }
    FINDING_TYPES = {
        "conclusion_without_premises", "precondition_unproven", "scope_overreach",
        "quantifier_escalation", "identity_join_incomplete", "non_sequitur_transition",
        "contradictory_premises", "circular_justification",
        "heuristic_presented_as_fact", "unresolved_presented_as_pass",
        "implicit_question_decomposition", "plan_execution_coverage_gap",
        "decision_reason_missing", "concept_drift", "alternative_unexamined",
        "insufficient_observability", "other",
    }
    SEVERITIES = {"info", "low", "medium", "high", "critical"}

    def __init__(self, root: str | Path, control: ControlPlane | None = None, *,
                 runner: LogicAuditRunner | None = None) -> None:
        self.root = Path(root).resolve()
        self.control = control or ControlPlane(self.root)
        self.state = self.control.state
        self.runner = runner or self._run_codex

    @classmethod
    def _compact(cls, value: Any, *, depth: int = 0) -> Any:
        if depth >= 7:
            return "<depth-limited>"
        if isinstance(value, dict):
            return {
                str(key): cls._compact(item, depth=depth + 1)
                for key, item in list(value.items())[:100]
            }
        if isinstance(value, list):
            return [cls._compact(item, depth=depth + 1) for item in value[:100]]
        if isinstance(value, str) and len(value) > 8000:
            return value[:8000] + "…<truncated>"
        return value

    def _artifact_projection(self, digest: str | None, *,
                             document_limit: int = 40) -> dict[str, Any]:
        if not digest:
            return {"digest": None, "integrity": "missing", "documents": []}
        try:
            value = json.loads(self.control.artifacts.get_bytes(str(digest)))
        except (KeyError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return {
                "digest": str(digest), "integrity": "unavailable",
                "error": str(exc), "documents": [],
            }
        if not isinstance(value, dict) or value.get("protocol") != "task-output-manifest-v1":
            return {
                "digest": str(digest), "integrity": "verified", "documents": [{
                    "logical_path": "CAS-root", "digest": str(digest),
                    "value": self._compact(value),
                }],
            }
        try:
            manifest = self.control.artifacts.verify_task_output_manifest(str(digest))
        except (KeyError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return {
                "digest": str(digest), "integrity": "invalid",
                "error": str(exc), "documents": [],
            }
        documents: list[dict[str, Any]] = []
        for item in (manifest.get("files") or [])[:max(1, document_limit)]:
            if (str(item.get("media_type") or "") != "application/json"
                    or int(item.get("size") or 0) > 2_000_000):
                continue
            child_digest = str(item.get("sha256") or "")
            try:
                child = json.loads(self.control.artifacts.get_bytes(child_digest))
            except (KeyError, OSError, RuntimeError, ValueError, json.JSONDecodeError):
                continue
            documents.append({
                "logical_path": str(item.get("path") or ""),
                "digest": child_digest, "value": self._compact(child),
            })
        return {
            "digest": str(digest), "integrity": "verified", "documents": documents,
            "input_lineage": self._compact(manifest.get("lineage") or []),
        }

    def _stage_dossier(self, job_id: str, run_id: str, stage_id: str) -> dict[str, Any]:
        conn = self.state._connection()
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        run = conn.execute(
            "SELECT * FROM orchestrator_runs WHERE run_id=? AND job_id=?",
            (run_id, job_id),
        ).fetchone()
        task = conn.execute(
            "SELECT * FROM orchestrator_tasks WHERE run_id=? AND task_id=?",
            (run_id, stage_id),
        ).fetchone()
        if job is None or run is None or task is None:
            raise KeyError(stage_id)
        task_value = dict(task)
        dependencies = [str(item) for item in _json(task_value.get("dependencies"), [])]
        parents = []
        for dependency in dependencies:
            row = conn.execute(
                "SELECT task_id,status,output_hash,failure_code,failure_payload "
                "FROM orchestrator_tasks WHERE run_id=? AND task_id=?",
                (run_id, dependency),
            ).fetchone()
            if row:
                value = dict(row)
                value["failure_payload"] = _json(value.get("failure_payload"))
                value["output"] = self._artifact_projection(value.get("output_hash"))
                parents.append(value)
        children = []
        for row in conn.execute(
            "SELECT task_id,status,dependencies FROM orchestrator_tasks WHERE run_id=? ORDER BY rowid",
            (run_id,),
        ):
            child_dependencies = [str(item) for item in _json(row["dependencies"], [])]
            if stage_id in child_dependencies:
                # A later child status is not a causal input to this frozen
                # stage review.  Including it would manufacture a new subject
                # hash every time the pipeline advanced.
                children.append({"task_id": row["task_id"]})
        attempts = []
        for row in conn.execute(
            "SELECT * FROM orchestrator_attempts WHERE run_id=? AND task_id=? ORDER BY started_at",
            (run_id, stage_id),
        ):
            value = dict(row)
            value["input_hashes"] = _json(value.get("input_hashes"), [])
            value["failure_payload"] = _json(value.get("failure_payload"))
            attempts.append(self._compact(value))
        gates = []
        for row in conn.execute(
            "SELECT * FROM gate_results WHERE run_id=? AND gate_name=? ORDER BY created_at",
            (run_id, stage_id),
        ):
            value = dict(row); value["payload"] = _json(value.get("payload")); gates.append(value)
        decisions = []
        for row in conn.execute(
            "SELECT * FROM decisions WHERE run_id=? AND decision_type=? ORDER BY created_at",
            (run_id, stage_id),
        ):
            value = dict(row); value["payload"] = _json(value.get("payload")); decisions.append(value)
        task_value["dependencies"] = dependencies
        task_value["failure_payload"] = _json(task_value.get("failure_payload"))
        return self._compact({
            "schema_version": "logic-audit-dossier-v1",
            "scope": "stage", "job_id": job_id, "run_id": run_id,
            "stage_id": stage_id,
            "job": {
                "id": job["id"], "workflow_id": job["workflow_id"],
                "payload": _json(job["payload"]),
            },
            "run": {
                "run_id": run["run_id"], "workflow_ref": run["workflow_ref"],
            },
            "stage": task_value, "attempts": attempts,
            "premise_stages": parents, "dependent_stages": children,
            "output": self._artifact_projection(task_value.get("output_hash")),
            "persisted_gates": gates, "persisted_decisions": decisions,
            "review_contract": {
                "purpose": "只读复查行动、前提、内容和结论的逻辑合理性",
                "does_not_decide_progression": True,
                "does_not_mutate": True,
                "does_not_dispatch_repair": True,
                "questions_are_observations_not_failures": True,
            },
        })

    def _cross_stage_dossier(self, job_id: str, run_id: str) -> dict[str, Any]:
        conn = self.state._connection()
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        run = conn.execute(
            "SELECT * FROM orchestrator_runs WHERE run_id=? AND job_id=?",
            (run_id, job_id),
        ).fetchone()
        if job is None or run is None:
            raise KeyError(run_id)
        stages = []
        for row in conn.execute(
            "SELECT * FROM orchestrator_tasks WHERE run_id=? ORDER BY rowid", (run_id,)
        ):
            value = dict(row)
            value["dependencies"] = _json(value.get("dependencies"), [])
            value["failure_payload"] = _json(value.get("failure_payload"))
            output = self._artifact_projection(value.get("output_hash"), document_limit=8)
            stages.append({**value, "output": output})
        return self._compact({
            "schema_version": "logic-audit-dossier-v1", "scope": "cross_stage",
            "job_id": job_id, "run_id": run_id, "stage_id": "__cross_stage__",
            "job": {**dict(job), "payload": _json(job["payload"])},
            "run": dict(run),
            "stages": stages,
            "review_contract": {
                "purpose": "只读复查计划、执行、证据、内容、Gate 与发布之间的逻辑连续性",
                "does_not_decide_progression": True,
                "does_not_mutate": True,
                "does_not_dispatch_repair": True,
                "questions_are_observations_not_failures": True,
            },
        })

    def _queue(self, *, job_id: str, run_id: str, stage_id: str, scope: str,
               dossier: dict[str, Any]) -> dict[str, Any]:
        subject_hash = _digest(dossier)
        audit_run_id = "lau_" + _digest({
            "job_id": job_id, "run_id": run_id, "stage_id": stage_id,
            "scope": scope, "subject_hash": subject_hash,
            "policy": self.POLICY_VERSION,
        })[:32]
        now = utcnow()
        payload = {
            "schema_version": "logic-audit-run-v1", "audit_run_id": audit_run_id,
            "job_id": job_id, "run_id": run_id, "stage_id": stage_id,
            "scope": scope, "subject_hash": subject_hash,
            "policy_version": self.POLICY_VERSION, "dossier": dossier,
            "authority": {
                "pipeline_effect": "none", "mutation_authority": "none",
                "automatic_promotion": False,
            },
        }
        with self.state.transaction() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO logic_audit_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (audit_run_id, job_id, run_id, stage_id, scope, subject_hash,
                 self.POLICY_VERSION, "queued", self.MODEL, _canonical(payload),
                 None, now, now),
            ).rowcount == 1
        if inserted:
            self.control.events.append(
                "logic_audit", audit_run_id, "logic_audit.queued",
                {"job_id": job_id, "run_id": run_id, "stage_id": stage_id,
                 "scope": scope, "subject_hash": subject_hash},
                actor="logic-audit-scheduler",
            )
        return self.get(audit_run_id)

    def queue_stage(self, job_id: str, run_id: str, stage_id: str) -> dict[str, Any]:
        return self._queue(
            job_id=job_id, run_id=run_id, stage_id=stage_id, scope="stage",
            dossier=self._stage_dossier(job_id, run_id, stage_id),
        )

    def queue_cross_stage(self, job_id: str, run_id: str) -> dict[str, Any]:
        return self._queue(
            job_id=job_id, run_id=run_id, stage_id="__cross_stage__",
            scope="cross_stage", dossier=self._cross_stage_dossier(job_id, run_id),
        )

    def queue_ready_for_job(self, job_id: str, *, include_cross_stage: bool = True) -> list[dict[str, Any]]:
        conn = self.state._connection()
        run = conn.execute(
            "SELECT * FROM orchestrator_runs WHERE job_id=? ORDER BY created_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if run is None or job is None:
            return []
        run_id = str(run["run_id"])
        queued = [
            self.queue_stage(job_id, run_id, str(row["task_id"]))
            for row in conn.execute(
                "SELECT task_id,status FROM orchestrator_tasks WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ) if str(row["status"]) in self.TERMINAL_STAGE
        ]
        if include_cross_stage and (
            str(run["status"]) in {"succeeded", "failed", "quarantined"}
            or str(job["status"]) in self.TERMINAL_JOB
        ):
            queued.append(self.queue_cross_stage(job_id, run_id))
        return queued

    def get(self, audit_run_id: str) -> dict[str, Any]:
        row = self.state._connection().execute(
            "SELECT * FROM logic_audit_runs WHERE audit_run_id=?", (audit_run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(audit_run_id)
        result = dict(row); result["payload"] = _json(result.get("payload")); return result

    def rows(self, *, job_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM logic_audit_runs", []
        if job_id:
            query += " WHERE job_id=?"; params.append(job_id)
        query += " ORDER BY created_at DESC LIMIT ?"; params.append(min(max(limit, 1), 500))
        result = []
        for row in self.state._connection().execute(query, tuple(params)):
            value = dict(row); value["payload"] = _json(value.get("payload")); result.append(value)
        return result

    def findings(self, *, job_id: str | None = None, audit_run_id: str | None = None,
                 limit: int = 500) -> list[dict[str, Any]]:
        clauses, params = [], []
        if job_id:
            clauses.append("r.job_id=?"); params.append(job_id)
        if audit_run_id:
            clauses.append("f.audit_run_id=?"); params.append(audit_run_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = []
        for row in self.state._connection().execute(
            "SELECT f.*,r.job_id,r.run_id,r.stage_id,r.scope FROM logic_audit_findings f "
            "JOIN logic_audit_runs r ON r.audit_run_id=f.audit_run_id" + where
            + " ORDER BY f.created_at DESC LIMIT ?", (*params, min(max(limit, 1), 1000)),
        ):
            value = dict(row)
            for name in ("premise_refs", "conclusion_refs", "artifact_refs", "payload"):
                value[name] = _json(value.get(name), [] if name.endswith("refs") else {})
            rows.append(value)
        return rows

    @staticmethod
    def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
        if isinstance(value, dict):
            yield value
            for item in value.values():
                yield from LogicAuditAgent._walk_dicts(item)
        elif isinstance(value, list):
            for item in value:
                yield from LogicAuditAgent._walk_dicts(item)

    @staticmethod
    def _finding(finding_type: str, severity: str, title: str, observation: str,
                 question: str, *, premise_refs: list[str] | None = None,
                 conclusion_refs: list[str] | None = None,
                 artifact_refs: list[str] | None = None,
                 confidence: float = 1.0) -> dict[str, Any]:
        return {
            "finding_type": finding_type, "severity": severity,
            "confidence": confidence, "title_zh": title,
            "observation_zh": observation, "question_zh": question,
            "premise_refs": premise_refs or [], "conclusion_refs": conclusion_refs or [],
            "artifact_refs": artifact_refs or [], "source": "deterministic",
        }

    def _deterministic_findings(self, dossier: dict[str, Any]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        scope = str(dossier.get("scope") or "stage")
        stages = dossier.get("stages") if scope == "cross_stage" else [dossier.get("stage")]
        for stage in stages or []:
            if not isinstance(stage, dict):
                continue
            status, stage_id = str(stage.get("status") or ""), str(stage.get("task_id") or "")
            output = (stage.get("output") if scope == "cross_stage" else dossier.get("output")) or {}
            if status == "succeeded" and output.get("integrity") not in {"verified"}:
                findings.append(self._finding(
                    "insufficient_observability", "high", "成功阶段缺少可核验输出",
                    f"阶段 {stage_id} 被标记为成功，但没有可核验的不可变输出。",
                    "该阶段的成功结论依据了什么持久化产物？",
                    conclusion_refs=[f"task:{stage_id}:status=succeeded"],
                    artifact_refs=[str(stage.get("output_hash") or "")],
                ))
            if status in {"failed", "repairable", "manual_review", "quarantined"}:
                failure = stage.get("failure_payload") or {}
                if not stage.get("failure_code") and not failure.get("message"):
                    findings.append(self._finding(
                        "decision_reason_missing", "high", "失败阶段没有可审查原因",
                        f"阶段 {stage_id} 已失败，但没有错误代码或可读失败说明。",
                        "审查者如何判断本阶段的失败结论是否合理？",
                        conclusion_refs=[f"task:{stage_id}:status={status}"],
                    ))
        documents = []
        if scope == "stage":
            documents = (dossier.get("output") or {}).get("documents") or []
        else:
            for stage in dossier.get("stages") or []:
                documents.extend((stage.get("output") or {}).get("documents") or [])
        for document in documents:
            value = document.get("value") if isinstance(document, dict) else None
            for candidate in self._walk_dicts(value):
                questions = candidate.get("research_questions")
                if (isinstance(questions, list) and "identity_and_terminology" in questions
                        and not candidate.get("research_question_contracts")):
                    findings.append(self._finding(
                        "implicit_question_decomposition", "high",
                        "研究主题缺少可重放的子问题合同",
                        "研究计划声明了 identity_and_terminology，但没有保存稳定的 subquestion_id、前提、来源角色和验收条件。新的 Agent Session 可能产生不同拆解。",
                        "如何证明不同 Session 会研究同一组身份与术语子问题？",
                        premise_refs=["$.research_questions[?identity_and_terminology]"],
                        conclusion_refs=["$.research_question_contracts"],
                        artifact_refs=[str(document.get("digest") or "")],
                    ))
                    break
        # Stable deduplication within one dossier keeps nested plan snapshots
        # from manufacturing repeated observations.
        unique: dict[str, dict[str, Any]] = {}
        for finding in findings:
            key = _digest({name: finding[name] for name in (
                "finding_type", "title_zh", "observation_zh", "question_zh"
            )})
            unique[key] = finding
        return list(unique.values())

    def _validate_result(self, value: dict[str, Any]) -> dict[str, Any]:
        required = {
            "assessment", "summary_zh", "reviewed_actions",
            "reviewed_conclusions", "findings",
        }
        if not isinstance(value, dict) or required - set(value):
            raise LogicAuditError(
                f"logic audit result missing fields: {sorted(required - set(value or {}))}"
            )
        if value["assessment"] not in {
            "coherent", "questionable", "insufficient", "contradictory", "not_assessable"
        }:
            raise LogicAuditError("invalid logic audit assessment")
        if not isinstance(value["findings"], list) or len(value["findings"]) > 50:
            raise LogicAuditError("logic audit findings must be an array of at most 50 items")
        normalized = dict(value)
        normalized_findings = []
        for finding in value["findings"]:
            names = {
                "finding_type", "severity", "confidence", "title_zh", "observation_zh",
                "question_zh", "premise_refs", "conclusion_refs", "artifact_refs",
            }
            if not isinstance(finding, dict) or names != set(finding):
                raise LogicAuditError("logic audit finding has missing or authority-bearing fields")
            if finding["finding_type"] not in self.FINDING_TYPES:
                raise LogicAuditError("invalid logic audit finding type")
            if finding["severity"] not in self.SEVERITIES:
                raise LogicAuditError("invalid logic audit severity")
            confidence = float(finding["confidence"])
            if not 0 <= confidence <= 1:
                raise LogicAuditError("invalid logic audit confidence")
            if not all(str(finding[name]).strip() for name in (
                "title_zh", "observation_zh", "question_zh"
            )):
                raise LogicAuditError("logic audit observations and questions are required")
            normalized_findings.append({**finding, "confidence": confidence, "source": "semantic"})
        normalized["findings"] = normalized_findings
        return normalized

    def _persist_findings(self, audit_run_id: str, findings: list[dict[str, Any]]) -> list[str]:
        now, ids = utcnow(), []
        with self.state.transaction() as conn:
            for finding in findings:
                finding_id = "laf_" + _digest({
                    "audit_run_id": audit_run_id,
                    "finding_type": finding["finding_type"],
                    "observation": finding["observation_zh"],
                    "question": finding["question_zh"],
                })[:32]
                payload = {
                    "schema_version": "logic-audit-finding-v1",
                    "finding_id": finding_id, "audit_run_id": audit_run_id,
                    "pipeline_effect": "none", "mutation_authority": "none", **finding,
                }
                conn.execute(
                    "INSERT OR IGNORE INTO logic_audit_findings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (finding_id, audit_run_id, finding["finding_type"], finding["severity"],
                     float(finding["confidence"]), finding["title_zh"],
                     finding["observation_zh"], finding["question_zh"],
                     _canonical(finding.get("premise_refs") or []),
                     _canonical(finding.get("conclusion_refs") or []),
                     _canonical(finding.get("artifact_refs") or []), "open", None,
                     _canonical(payload), now, now),
                )
                ids.append(finding_id)
        return ids

    def _set(self, audit_run_id: str, status: str, payload: dict[str, Any], *,
             error: str | None = None, ownership: ExecutionOwnership | None = None) -> None:
        if ownership is not None:
            ownership.current()
        with self.state.transaction() as conn:
            changed = conn.execute(
                "UPDATE logic_audit_runs SET status=?,payload=?,last_error=?,updated_at=? "
                "WHERE audit_run_id=?",
                (status, _canonical(payload), error, utcnow(), audit_run_id),
            ).rowcount
        if changed != 1:
            raise LogicAuditError(f"logic audit row disappeared: {audit_run_id}")

    def execute(self, audit_run_id: str) -> dict[str, Any]:
        record = self.get(audit_run_id)
        if record["status"] in self.TERMINAL:
            return record
        prior = record["payload"].get("execution") or {}
        ownership = ExecutionOwnership.create(
            self.control, "logic-audit", audit_run_id,
            attempt=int(prior.get("attempt") or 0) + 1,
        )
        try:
            ownership.start()
        except LeaseLost:
            return record
        payload = dict(record["payload"])
        run_dir = self.root / "var/logic-audits" / audit_run_id
        dossier_path = run_dir / "dossier.json"
        run_dir.mkdir(parents=True, exist_ok=True)
        dossier_path.write_text(
            json.dumps(payload["dossier"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        payload["execution"] = {
            "attempt": ownership.attempt, "started_at": utcnow(),
            "owner_id": ownership.owner_id,
            "fencing_token": ownership.current().fencing_token,
        }
        try:
            self._set(audit_run_id, "reviewing", payload, ownership=ownership)
            deterministic = self._deterministic_findings(payload["dossier"])
            deterministic_ids = self._persist_findings(audit_run_id, deterministic)
            semantic_required = (
                record["scope"] == "cross_stage" or record["stage_id"] in self.SEMANTIC_STAGES
            )
            if semantic_required:
                semantic = self._validate_result(self.runner(self.root, {
                    "run_dir": str(run_dir), "dossier_path": str(dossier_path),
                    "dossier": payload["dossier"],
                }))
            else:
                semantic = {
                    "assessment": "questionable" if deterministic else "coherent",
                    "summary_zh": (
                        "确定性审查发现了需要关注的逻辑观察。" if deterministic
                        else "本阶段没有实质性语义结论，仅完成了确定性合同检查。"
                    ),
                    "reviewed_actions": [], "reviewed_conclusions": [], "findings": [],
                }
            semantic_ids = self._persist_findings(audit_run_id, semantic["findings"])
            payload.update({
                "result": {**semantic, "finding_ids": [*deterministic_ids, *semantic_ids]},
                "completed_at": utcnow(),
                "authority": {
                    "pipeline_effect": "none", "mutation_authority": "none",
                    "automatic_promotion": False,
                },
            })
            self._set(audit_run_id, "completed", payload, ownership=ownership)
            self.control.events.append(
                "logic_audit", audit_run_id, "logic_audit.completed",
                {"job_id": record["job_id"], "stage_id": record["stage_id"],
                 "scope": record["scope"], "assessment": semantic["assessment"],
                 "findings": len(deterministic_ids) + len(semantic_ids),
                 "pipeline_effect": "none"},
                actor="logic-audit-agent",
            )
        except LeaseLost:
            return self.get(audit_run_id)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError,
                json.JSONDecodeError) as exc:
            payload["failed_at"] = utcnow()
            self._set(audit_run_id, "failed", payload, error=str(exc), ownership=ownership)
            self.control.events.append(
                "logic_audit", audit_run_id, "logic_audit.failed",
                {"job_id": record["job_id"], "stage_id": record["stage_id"],
                 "error": type(exc).__name__, "message": str(exc),
                 "pipeline_effect": "none"},
                actor="logic-audit-agent",
            )
        finally:
            ownership.close()
        return self.get(audit_run_id)

    def promote(self, finding_id: str, *, actor: str = "dashboard-operator") -> dict[str, Any]:
        row = self.state._connection().execute(
            "SELECT f.*,r.job_id,r.run_id,r.stage_id FROM logic_audit_findings f "
            "JOIN logic_audit_runs r ON r.audit_run_id=f.audit_run_id WHERE f.finding_id=?",
            (finding_id,),
        ).fetchone()
        if row is None:
            raise KeyError(finding_id)
        if row["promoted_deviation_id"]:
            return {"finding_id": finding_id, "status": "promoted",
                    "deviation_id": row["promoted_deviation_id"]}
        from .goal_alignment.controller import GoalAlignmentController
        message = (
            f"逻辑审查观察：{row['title_zh']}\n"
            f"观察：{row['observation_zh']}\n"
            f"待调查问题：{row['question_zh']}\n"
            f"阶段：{row['stage_id']}；逻辑审查 Finding：{finding_id}"
        )
        report = GoalAlignmentController(
            self.root, self.control
        ).report_user_feedback(
            str(row["job_id"]), message, category="logic_audit_promotion"
        )
        deviation_id = str(report["deviation"]["deviation_id"])
        now = utcnow()
        payload = _json(row["payload"])
        payload["promotion"] = {
            "actor": actor, "promoted_at": now, "deviation_id": deviation_id,
            "automatic": False,
        }
        with self.state.transaction() as conn:
            conn.execute(
                "UPDATE logic_audit_findings SET status='promoted',promoted_deviation_id=?,"
                "payload=?,updated_at=? WHERE finding_id=? AND promoted_deviation_id IS NULL",
                (deviation_id, _canonical(payload), now, finding_id),
            )
        self.control.events.append(
            "logic_audit", str(row["audit_run_id"]), "logic_audit.finding_promoted",
            {"finding_id": finding_id, "deviation_id": deviation_id, "actor": actor,
             "automatic": False}, actor=actor,
        )
        return {"finding_id": finding_id, "status": "promoted",
                "deviation_id": deviation_id, "investigation": report}

    def _run_codex(self, _root: Path, request: dict[str, Any]) -> dict[str, Any]:
        run_dir = Path(request["run_dir"])
        output = run_dir / "logic-audit-result.json"
        output.unlink(missing_ok=True)
        schema = self.root / "contracts/logic-audit-result-v1.schema.json"
        prompt = (
            "You are an independent read-only Logic Review Agent. Review the persisted external "
            "action, content, premise, evidence, conclusion, and transition records in the frozen "
            "dossier. Do not reconstruct or reveal private chain-of-thought. Produce only concise "
            "auditable observations and questions in Chinese. Distinguish execution ordering from "
            "logical entailment: a successful parent task or matching hash does not by itself prove "
            "that its content is sufficient for the next conclusion. Look for missing premises, "
            "scope or quantifier expansion, incomplete identity joins, concept drift, circular "
            "support, unresolved facts presented as PASS, missing decision reasons, unexplored "
            "alternatives, and plan-to-execution coverage gaps. Every observation must cite the "
            "available premise/conclusion/artifact references. If the dossier is insufficient, "
            "ask what would be needed rather than asserting a defect. You are not a Gate, do not "
            "decide whether the pipeline may continue, and do not recommend repairs, retries, "
            "rewinds, code changes, Issues, PRs, or mutations. Findings are advisory only and have "
            "pipeline_effect=none outside this schema. Return only the required structured result. "
            f"Read the frozen dossier from {request['dossier_path']}. Treat that file as the "
            "complete review universe and do not inspect mutable workspace files."
        )
        command = [
            shutil.which("codex") or "codex", "exec", "--ephemeral",
            "--sandbox", "read-only", "--model", self.MODEL,
            "-c", 'model_reasoning_effort="high"', "--cd", str(self.root),
            "--output-schema", str(schema), "--output-last-message", str(output), prompt,
        ]
        completed = subprocess.run(
            command, cwd=self.root, text=True, capture_output=True,
            timeout=1200, check=False,
        )
        (run_dir / "logic-audit-stdout.log").write_text(
            completed.stdout[-200000:], encoding="utf-8"
        )
        (run_dir / "logic-audit-stderr.log").write_text(
            completed.stderr[-200000:], encoding="utf-8"
        )
        if completed.returncode != 0 or not output.is_file():
            raise LogicAuditError(
                f"logic audit Agent failed with exit {completed.returncode}: "
                f"{completed.stderr[-2000:]}"
            )
        value = json.loads(output.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise LogicAuditError("logic audit Agent result is not an object")
        return value
