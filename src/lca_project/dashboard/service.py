"""Read models and narrowly-scoped control actions for the dashboard.

The dashboard deliberately builds projections from persisted control-plane
facts.  It never infers success from files in a workspace and never updates
SQLite directly for control actions.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any
from urllib.parse import urlparse

from lca_project.control import ControlPlane
from lca_project.kernel.orchestrator import PersistentOrchestrator
from lca_project.kernel.registry import CapabilityRegistry
from lca_project.kernel.skills import SkillInvoker, SkillRegistry
from lca_project.kernel.workflow import WorkflowSpec, compile_workflow
from lca_project.kernel.worker import WorkerLoop
from lca_project.contracts import load_json


_STAGE_AUDIT_META: dict[str, tuple[str, str, str]] = {
    "plan": ("任务规划", "把 Job 目标转换为可执行的生产计划。", "解析目标、约束和发布模式，并生成阶段计划。"),
    "prepare": ("工作区准备", "建立受控工作区并准备后续任务所需的输入。", "校验请求、初始化目录并固化准备结果。"),
    "research_plan": ("研究计划", "确定需要回答的问题、查询词、字段和来源角色。", "生成研究问题、双语检索轨道和证据需求。"),
    "research_plan_gate": ("研究计划门禁", "确认研究计划足以驱动真实检索，同时区分阻断项和建议项。", "逐条检查研究问题、来源角色和检索轨道契约。"),
    "research_ready": ("检索候选生成", "把研究计划转化为可执行查询和候选来源。", "Agent 生成查询并调用配置的数据源发现候选。"),
    "search_execution_gate": ("检索执行门禁", "证明计划中的查询已经实际执行并到达终态。", "检查查询数量、执行状态和外部检索记录。"),
    "verify": ("证据核验", "逐条判断候选来源是否真正支持目标声明。", "审核 Agent 对来源、声明、对象一致性和支撑片段进行核验。"),
    "terminology_verify": ("术语核验", "确认节点身份和中英文术语不会因检索别名而发生漂移。", "核验规范术语、别名用途和双语等价关系。"),
    "source_diversity_gate": ("问题证据充分性门禁", "确认每个关键研究问题的前置要求都有直接证据；来源数量与语言多样性作为质量建议单独评估。", "逐问题核对绑定 requirement、确认声明和缺失前提，并附带非阻断的来源组合质量评估。"),
    "freeze": ("证据冻结", "把已核验输入冻结为后续生成不可变的因果输入。", "固化来源、声明、查询和内容哈希。"),
    "content_blueprint": ("内容蓝图", "定义内容结构、声明位置和证据绑定方式。", "生成章节、声明和证据槽位蓝图。"),
    "content_compose": ("内容生成", "基于冻结证据和蓝图生成正文。", "生成 Agent 编写内容并保留声明溯源。"),
    "content_closure_gate": ("内容闭合门禁", "确认正文覆盖蓝图且没有明显缺口、重复或无依据声明。", "检查语义闭合、引用覆盖和内容重复。"),
    "editorial_review": ("编辑审校", "独立检查准确性、可读性和术语一致性。", "审核 Agent 给出问题、结论和修改建议。"),
    "draft_content_gate": ("草稿内容门禁", "确认草稿达到可受控写入的最低质量。", "检查编辑问题、引用和草稿契约。"),
    "draft_apply": ("草稿应用", "把通过门禁的正文写入受控工作区。", "应用正文变更并记录不可变产物。"),
    "table_collect": ("表格数据检索", "为每个目标字段执行可审计的数据检索。", "Agent 执行字段查询、抓取候选并记录采用或拒绝原因。"),
    "table_search_execution_gate": ("表格检索门禁", "证明所有表格查询已真实执行。", "检查查询矩阵、Provider 尝试和终态。"),
    "table_verify": ("表格证据核验", "判断候选数据是否能支持具体字段。", "审核字段观察、单位、对象一致性和来源质量。"),
    "table_population_gate": ("表格填充门禁", "只允许有字段级证据的数据进入表格。", "逐字段检查候选、证据和空值原因。"),
    "table_apply": ("表格应用", "把通过门禁的数据写入最终表格。", "应用字段值并记录来源与缺口。"),
    "maturity_gate": ("成熟度门禁", "判断成果属于诊断预览、证据受限还是生产候选。", "汇总质量向量、数据就绪度和未关闭问题。"),
    "preview": ("生成预览", "生成供人工查看但不代表正式发布的成果。", "构建预览文件并校验可访问性。"),
    "release_gate": ("发布门禁", "确认正式发布所需的审核、质量和不可变证明完整。", "检查发布资格、审核结论和 Release 证据。"),
    "reviewed_apply": ("审核应用", "应用经过正式审核的最终变更。", "在受控边界内写入审核通过的内容。"),
    "publish": ("正式发布", "形成可验证、不可变的正式发布记录。", "发布成果并固化 Release Record。"),
}

_CAPABILITY_ACTORS = {
    "agent.propose": "生成 Agent",
    "agent.review": "审核 Agent",
    "wiki.batch": "确定性流程执行器",
    "release.apply": "受控发布执行器",
}

_STATUS_ZH = {
    "planned": "已规划", "ready": "就绪", "running": "执行中", "succeeded": "成功",
    "failed": "失败", "repairable": "可修复", "retryable": "可重试",
    "manual_review": "等待人工审核", "quarantined": "已隔离", "blocked": "已阻塞",
    "blocked_budget": "预算阻塞", "pending": "尚未执行", "skipped": "已跳过",
}

_STAGE_DIAGNOSTIC_FILES = {
    "research_plan_gate": "research-plan-gate.json",
    "search_execution_gate": "search-execution-gate.json",
    "terminology_verify": "terminology-verdict.json",
    "source_diversity_gate": "source-diversity-gate.json",
    "content_closure_gate": "content-closure-gate.json",
    "draft_content_gate": "draft-content-gate.json",
    "table_search_execution_gate": "table-data/search-execution-gate.json",
    "table_population_gate": "table-data/table-population-gate.json",
    "maturity_gate": "maturity-gate.json",
}

_JSON_VIEWER_MAX_BYTES = 10 * 1024 * 1024


def _json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return {} if default is None else default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {} if default is None else default


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in tuple(result):
        if key in {"metadata", "dependencies", "inputs", "input_hashes", "output_hashes", "summary"} or key.endswith("payload"):
            result[key] = _json(result[key], [] if key.endswith("hashes") or key == "dependencies" else {})
    return result


class DashboardService:
    """Query the platform through stable JSON-friendly dashboard projections."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.control = ControlPlane(self.root)
        self.state = self.control.state
        self._workers: dict[str, threading.Thread] = {}
        self._worker_lock = threading.Lock()
        self._autonomy_threads: dict[str, threading.Thread] = {}
        self._autonomy_lock = threading.Lock()
        self._goal_reconciler_thread: threading.Thread | None = None
        self._goal_reconciler_stop = threading.Event()

    @property
    def conn(self) -> sqlite3.Connection:
        return self.state._connection()

    def _has_table(self, name: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [_row(item) or {} for item in self.conn.execute(sql, params)]

    def _count(self, table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
        if not self._has_table(table):
            return 0
        suffix = f" WHERE {where}" if where else ""
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}{suffix}", params).fetchone()[0])

    def overview(self) -> dict[str, Any]:
        job_states = {row["status"]: row["n"] for row in self.conn.execute(
            "SELECT status,COUNT(*) AS n FROM jobs GROUP BY status ORDER BY status"
        )}
        task_states: dict[str, int] = {}
        if self._has_table("orchestrator_tasks"):
            task_states = {row["status"]: row["n"] for row in self.conn.execute(
                "SELECT status,COUNT(*) AS n FROM orchestrator_tasks GROUP BY status ORDER BY status"
            )}
        gates: dict[str, int] = {}
        if self._has_table("gate_results"):
            gates = {row["verdict"]: row["n"] for row in self.conn.execute(
                "SELECT verdict,COUNT(*) AS n FROM gate_results GROUP BY verdict ORDER BY verdict"
            )}
        recent_events = self.events(limit=12)["items"]
        recent_jobs = self.jobs(limit=8)["items"]
        active_leases = self._count("leases", "expires_at > ?", (datetime.now(timezone.utc).isoformat(),))
        total_budget = {"limit": 0, "reserved": 0, "consumed": 0}
        if self._has_table("budgets"):
            budget = self.conn.execute(
                "SELECT COALESCE(SUM(limit_units),0),COALESCE(SUM(reserved_units),0),"
                "COALESCE(SUM(consumed_units),0) FROM budgets"
            ).fetchone()
            total_budget = {"limit": budget[0], "reserved": budget[1], "consumed": budget[2]}
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "root": str(self.root),
            "counts": {
                "jobs": self._count("jobs"),
                "workflow_runs": self._count("orchestrator_runs"),
                "tasks": self._count("orchestrator_tasks"),
                "artifacts": self._count("artifacts"),
                "events": self._count("events"),
                "open_exceptions": self._count("exceptions", "status NOT IN ('resolved','closed')"),
                "releases": self._count("releases"),
                "active_leases": active_leases,
                "workers": self._count("worker_instances"),
                "open_deviations": self._count("deviation_reports", "status='open'"),
                "change_candidates": self._count("system_change_candidates"),
                "autonomous_campaigns": self._count("autonomous_campaigns"),
            },
            "job_states": job_states,
            "task_states": task_states,
            "gate_verdicts": gates,
            "budget": total_budget,
            "recent_jobs": recent_jobs,
            "recent_events": recent_events,
            "goal_alignment": self.goal_alignment(limit=8),
            "autonomy": self.autonomy(limit=8),
        }

    def skill_catalog(self) -> dict[str, Any]:
        """Expose registered Skill routes and their authoritative input schemas."""
        items: list[dict[str, Any]] = []
        for skill in SkillRegistry(self.root).all():
            schema = load_json(self.root / "contracts" / f"{skill.input_schema}.schema.json")
            items.append({
                "name": skill.name,
                "description": skill.description,
                "version": skill.version,
                "workflow": skill.workflow_ref,
                "policy": skill.policy_version,
                "input_schema": skill.input_schema,
                "schema": schema,
            })
        return {"items": items, "total": len(items)}

    def workers(self) -> dict[str, Any]:
        if not self._has_table("worker_instances"):
            return {"items": [], "total": 0}
        items = self._rows("SELECT * FROM worker_instances ORDER BY heartbeat_at DESC")
        return {"items": items, "total": len(items)}

    def create_job(self, skill_name: str, request: dict[str, Any], *,
                   idempotency_key: str | None = None, materialize: bool = False) -> dict[str, Any]:
        """Submit only through the registered Skill route and optionally materialise it."""
        if not isinstance(skill_name, str) or not skill_name.strip():
            raise ValueError("skill is required")
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key.strip()):
            raise ValueError("idempotency_key must be a non-empty string when provided")
        if not isinstance(materialize, bool):
            raise ValueError("materialize must be a boolean")
        accepted = SkillInvoker(self.root).invoke(
            skill_name.strip(), request, idempotency_key=idempotency_key.strip() if idempotency_key else None
        )
        result: dict[str, Any] = {**accepted, "materialized": False}
        if materialize:
            expanded = self.materialize(str(accepted["job_id"]))
            result.update({"materialized": True, "run_id": expanded["run_id"],
                           "tasks": len(expanded["tasks"])})
        return result

    def jobs(self, *, status: str = "", query: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("j.status=?")
            params.append(status)
        if query:
            clauses.append("(j.id LIKE ? OR j.workflow_id LIKE ? OR j.payload LIKE ?)")
            needle = f"%{query}%"
            params.extend((needle, needle, needle))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = int(self.conn.execute(f"SELECT COUNT(*) FROM jobs j{where}", tuple(params)).fetchone()[0])
        has_runs = self._has_table("orchestrator_runs")
        join = "LEFT JOIN orchestrator_runs o ON o.job_id=j.id" if has_runs else ""
        run_columns = "o.run_id,o.status AS run_status" if has_runs else "NULL AS run_id,NULL AS run_status"
        rows = self._rows(
            f"SELECT j.*,{run_columns} FROM jobs j {join}{where} "
            "ORDER BY j.updated_at DESC LIMIT ? OFFSET ?", (*params, limit, offset)
        )
        for item in rows:
            payload = item.get("payload") or {}
            scope = payload.get("scope") or {}
            item["target"] = payload.get("target") or scope.get("node_id") or scope.get("target") or "—"
            item["policy_version"] = payload.get("policy_version") or "—"
            item["risk"] = payload.get("risk") or "standard"
        states = [dict(row) for row in self.conn.execute(
            "SELECT status,COUNT(*) AS count FROM jobs GROUP BY status ORDER BY status"
        )]
        return {"items": rows, "total": total, "limit": limit, "offset": offset, "states": states}

    def job(self, job_id: str) -> dict[str, Any]:
        job = _row(self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        if job is None:
            raise KeyError(job_id)
        run = None
        tasks: list[dict[str, Any]] = []
        attempts: list[dict[str, Any]] = []
        if self._has_table("orchestrator_runs"):
            run = _row(self.conn.execute(
                "SELECT * FROM orchestrator_runs WHERE job_id=?", (job_id,)
            ).fetchone())
        if run and self._has_table("orchestrator_tasks"):
            tasks = self._rows(
                "SELECT * FROM orchestrator_tasks WHERE run_id=? ORDER BY rowid", (run["run_id"],)
            )
            attempts = self._rows(
                "SELECT * FROM orchestrator_attempts WHERE run_id=? ORDER BY started_at DESC", (run["run_id"],)
            )
        # Bound each indexed event stream before merging them.  A direct
        # ``OR ... ORDER BY`` makes SQLite materialise and sort the complete
        # history for both aggregates before applying LIMIT; a noisy Job can
        # therefore make every detail request take tens of seconds.
        events = self._rows(
            "WITH job_events AS ("
            " SELECT * FROM events WHERE aggregate_type='job' AND aggregate_id=?"
            " ORDER BY sequence DESC LIMIT 100"
            "), run_events AS ("
            " SELECT * FROM events WHERE aggregate_type='workflow_run' AND aggregate_id=?"
            " ORDER BY sequence DESC LIMIT 100"
            ") SELECT * FROM ("
            " SELECT * FROM job_events UNION ALL SELECT * FROM run_events"
            ") ORDER BY sequence DESC LIMIT 100",
            (job_id, run["run_id"] if run else ""),
        )
        run_id = run["run_id"] if run else ""
        gates = self._rows("SELECT * FROM gate_results WHERE run_id=? ORDER BY created_at", (run_id,))
        decisions = self._rows("SELECT * FROM decisions WHERE run_id=? ORDER BY created_at", (run_id,))
        exceptions = self._rows("SELECT * FROM exceptions WHERE run_id=? ORDER BY opened_at DESC", (run_id,))
        hashes: set[str] = set((job.get("payload") or {}).get("input_hashes") or [])
        for task in tasks:
            if task.get("output_hash"):
                hashes.add(str(task["output_hash"]))
        artifacts = []
        for digest in hashes:
            artifact = _row(self.conn.execute("SELECT * FROM artifacts WHERE digest=?", (digest,)).fetchone())
            if artifact:
                artifacts.append(artifact)
        workflow = self._workflow_detail(str(job.get("workflow_id") or ""))
        preview = self._preview_projection(job_id, run=run, tasks=tasks)
        goal_alignment = self.goal_alignment(job_id=job_id)
        logic_audit = self.logic_audit(job_id)
        execution_trace = self._execution_trace(
            job_id, job=job, run=run, tasks=tasks, attempts=attempts,
            goal_alignment=goal_alignment,
        )
        return {"job": job, "run": run, "tasks": tasks, "attempts": attempts,
                "events": events, "gates": gates, "decisions": decisions,
                "exceptions": exceptions, "artifacts": artifacts, "workflow": workflow,
                "goal_alignment": goal_alignment, "execution_trace": execution_trace,
                "logic_audit": logic_audit,
                "preview": preview}

    @staticmethod
    def _read_trace_json(batch: Path | None, relative: str) -> dict[str, Any]:
        """Read one known trace artifact without allowing paths outside the batch."""
        if batch is None:
            return {}
        try:
            path = (batch / relative).resolve()
            if (not path.is_relative_to(batch) or not path.is_file() or path.is_symlink()
                    or path.stat().st_size > 25 * 1024 * 1024):
                return {}
            raw = path.read_bytes()
        except OSError:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def _trace_batch(self, job_id: str, goal_alignment: dict[str, Any]) -> Path | None:
        workspace = (self.root / "var" / "workspaces" / "jobs" / job_id).resolve()
        observations = goal_alignment.get("quality_observations") or []
        for row in observations:
            payload = row.get("payload") or {}
            raw = ((payload.get("evidence") or {}).get("batch") or "")
            if not raw:
                continue
            batch = Path(str(raw)).resolve()
            if batch.is_relative_to(workspace) and batch.is_dir() and not batch.is_symlink():
                return batch
        return None

    @staticmethod
    def _trace_domain(url: Any) -> str:
        try:
            return urlparse(str(url or "")).netloc.lower().removeprefix("www.")
        except ValueError:
            return ""

    def _declared_completion_goal(self, job_id: str, job: dict[str, Any]) -> str:
        if self._has_table("autonomous_job_items") and self._has_table("autonomous_campaigns"):
            row = self.conn.execute(
                "SELECT c.payload FROM autonomous_job_items i "
                "JOIN autonomous_campaigns c ON c.campaign_id=i.campaign_id "
                "WHERE i.job_id=? ORDER BY i.updated_at DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if row:
                payload = _json(row["payload"])
                if payload.get("completion_goal"):
                    return str(payload["completion_goal"])
        request = (((job.get("payload") or {}).get("scope") or {}).get("request") or {})
        return (
            "reviewed_publication"
            if request.get("publication_mode") == "reviewed"
            else "lca_modeling_ready"
        )

    @staticmethod
    def _compact_audit_value(value: Any, *, depth: int = 0) -> Any:
        """Keep audit facts useful without returning unbounded artifact documents."""
        if depth >= 4:
            return "…"
        if isinstance(value, dict):
            return {
                str(key): DashboardService._compact_audit_value(item, depth=depth + 1)
                for key, item in list(value.items())[:40]
            }
        if isinstance(value, list):
            return [
                DashboardService._compact_audit_value(item, depth=depth + 1)
                for item in value[:30]
            ]
        if isinstance(value, str) and len(value) > 1200:
            return value[:1200] + "…"
        return value

    @classmethod
    def _audit_document_facts(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        keys = (
            "protocol", "schema_version", "decision", "verdict", "status", "reason",
            "summary", "message", "checks", "advisory_checks", "warnings", "failures",
            "metrics", "counts", "maturity_ceiling", "pipeline_continue",
            "candidate_eligible", "repair_target", "translation_policy", "quality_checks",
            "quality_assessment", "failed_requirement_ids", "question_contract_sha256",
            "strategy_hash", "research_question_contract_version",
            "research_questions",
            "node_id", "canonical_zh", "canonical_en",
        )
        facts = {
            key: cls._compact_audit_value(value[key])
            for key in keys if key in value and value[key] not in (None, "", [], {})
        }
        contracts = value.get("research_question_contracts")
        if isinstance(contracts, list):
            facts["research_question_contracts"] = [{
                "dimension": contract.get("dimension"),
                "criticality": contract.get("criticality"),
                "applicability": contract.get("applicability"),
                "required_question_ids": contract.get("required_question_ids") or [],
                "source_role_requirements": contract.get("source_role_requirements") or [],
                "preferred_source_classes": contract.get("preferred_source_classes") or [],
                "acceptance": contract.get("acceptance") or {},
                "subquestions": [{
                    "question_id": question.get("question_id"),
                    "question": question.get("question") or {},
                    "requirement_ids": question.get("requirement_ids") or [],
                    "closure_rule": question.get("closure_rule"),
                    "semantic_frame": question.get("semantic_frame") or {},
                    "query_intents": [{
                        "intent_id": intent.get("intent_id"),
                        "purpose": intent.get("purpose"),
                        "priority": intent.get("priority"),
                        "language_policy": intent.get("language_policy"),
                        "seed_terms": intent.get("seed_terms") or {},
                        "preferred_source_roles": intent.get("preferred_source_roles") or [],
                        "preferred_source_classes": intent.get("preferred_source_classes") or [],
                    } for intent in question.get("query_intents") or []
                    if isinstance(intent, dict)],
                } for question in contract.get("subquestions") or [] if isinstance(question, dict)],
            } for contract in contracts[:12] if isinstance(contract, dict)]
        ledger = value.get("question_evidence_ledger")
        if isinstance(ledger, dict):
            facts["question_evidence_ledger"] = {
                "question_contract_sha256": ledger.get("question_contract_sha256"),
                "critical_questions_closed": ledger.get("critical_questions_closed") is True,
                "critical_question_ids": ledger.get("critical_question_ids") or [],
                "critical_question_status": ledger.get("critical_question_status") or {},
                "metrics": ledger.get("metrics") or {},
                "questions": [{
                    "question_id": item.get("question_id"),
                    "dimension": item.get("dimension"),
                    "criticality": item.get("criticality"),
                    "question": item.get("question") or {},
                    "status": item.get("status"),
                    "closure_rule": item.get("closure_rule"),
                    "bound_requirement_ids": item.get("bound_requirement_ids") or [],
                    "confirmed_requirement_ids": item.get("confirmed_requirement_ids") or [],
                    "missing_requirement_ids": item.get("missing_requirement_ids") or [],
                    "evidence_count": len(item.get("evidence") or []),
                    "evidence": [{
                        "claim_id": evidence.get("claim_id"),
                        "requirement_id": evidence.get("requirement_id"),
                        "verdict": evidence.get("verdict"),
                        "claim_kind": evidence.get("claim_kind"),
                        "url": evidence.get("url"),
                        "support_type": evidence.get("support_type"),
                    } for evidence in item.get("evidence") or []
                    if isinstance(evidence, dict)][:30],
                    "source_role_requirements": item.get("source_role_requirements") or [],
                } for item in ledger.get("questions") or [] if isinstance(item, dict)],
                "unmapped_claims": ledger.get("unmapped_claims") or [],
            }
        return facts

    def _task_output_audit(self, digest: str | None) -> dict[str, Any]:
        """Project a task output and its hash-bound files into compact audit facts."""
        if not digest:
            return {"digest": None, "files": [], "documents": [], "integrity": "missing"}
        try:
            raw = self.control.artifacts.get_bytes(str(digest))
            document = json.loads(raw)
        except (KeyError, OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            return {
                "digest": str(digest), "files": [], "documents": [],
                "integrity": "unavailable", "error": str(exc),
            }
        if not isinstance(document, dict):
            return {"digest": str(digest), "files": [], "documents": [], "integrity": "ok"}
        if document.get("protocol") != "task-output-manifest-v1":
            return {
                "digest": str(digest), "protocol": document.get("protocol"),
                "files": [], "documents": [{
                    "path": None, "digest": str(digest), "role": "task_output",
                    "facts": self._audit_document_facts(document),
                }], "integrity": "ok",
            }
        try:
            manifest = self.control.artifacts.verify_task_output_manifest(str(digest))
        except (KeyError, OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            return {
                "digest": str(digest), "protocol": "task-output-manifest-v1",
                "files": [], "documents": [], "integrity": "invalid", "error": str(exc),
            }
        files: list[dict[str, Any]] = []
        documents: list[dict[str, Any]] = []
        for item in (manifest.get("files") or [])[:20]:
            entry = {
                "path": item.get("path"), "digest": item.get("sha256"),
                "role": item.get("role"), "media_type": item.get("media_type"),
                "size": item.get("size"),
            }
            files.append(entry)
            if item.get("media_type") != "application/json" or int(item.get("size") or 0) > 2_000_000:
                continue
            try:
                child = json.loads(self.control.artifacts.get_bytes(str(item.get("sha256") or "")))
            except (KeyError, OSError, ValueError, RuntimeError, json.JSONDecodeError):
                continue
            documents.append({**entry, "facts": self._audit_document_facts(child)})
        execution_facts: dict[str, Any] = {}
        try:
            execution = json.loads(self.control.artifacts.get_bytes(
                str(manifest.get("execution_result_hash") or "")
            ))
            execution_facts = self._audit_document_facts(execution)
        except (KeyError, OSError, ValueError, RuntimeError, json.JSONDecodeError):
            pass
        return {
            "digest": str(digest), "protocol": "task-output-manifest-v1",
            "attempt_id": manifest.get("attempt_id"), "files": files,
            "documents": documents, "execution_facts": execution_facts,
            "integrity": "verified",
        }

    def _verified_attempt_snapshot(
        self,
        job_id: str,
        task_id: str,
        attempt_id: str,
        relative: str,
    ) -> dict[str, Any]:
        """Read one JSON file only after its persisted attempt hash is verified."""
        job = self.conn.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            raise KeyError(job_id)
        attempt = self.conn.execute(
            "SELECT a.attempt_id,a.run_id FROM orchestrator_attempts a "
            "JOIN orchestrator_runs r ON r.run_id=a.run_id "
            "WHERE r.job_id=? AND a.task_id=? AND a.attempt_id=?",
            (job_id, task_id, attempt_id),
        ).fetchone()
        if attempt is None:
            raise KeyError(attempt_id)
        workspace = (self.root / "var" / "workspaces" / "jobs" / job_id).resolve()
        archive_root = (workspace / "runs" / "attempts" / task_id / attempt_id).resolve()
        archive = (archive_root / "manifest.json").resolve()
        try:
            if (not archive.is_relative_to(workspace) or not archive.is_file()
                    or archive.is_symlink() or archive.stat().st_size > 2_000_000):
                raise RuntimeError("attempt archive is missing or unsafe")
            manifest = json.loads(archive.read_bytes())
        except OSError as exc:
            raise RuntimeError("attempt archive cannot be read") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("attempt archive is not valid JSON") from exc
        if (not isinstance(manifest, dict)
                or manifest.get("protocol") != "task-attempt-archive-v1"
                or str(manifest.get("run_id") or "") != str(attempt["run_id"])
                or str(manifest.get("task_id") or "") != task_id
                or str(manifest.get("attempt_id") or "") != attempt_id):
            raise RuntimeError("attempt archive identity does not match the requested stage")
        logical = str(relative or "").strip()
        file_entry = next((
            item for item in manifest.get("files") or []
            if isinstance(item, dict) and str(item.get("path") or "") == logical
        ), None)
        if not file_entry:
            raise KeyError(logical)
        execution_root = Path(str(manifest.get("execution_root") or "")).resolve()
        path = (execution_root / logical).resolve()
        try:
            if (not execution_root.is_relative_to(workspace) or not execution_root.is_dir()
                    or execution_root.is_symlink() or not path.is_relative_to(execution_root)
                    or not path.is_file() or path.is_symlink()):
                raise RuntimeError("attempt snapshot path is missing or unsafe")
            size = path.stat().st_size
            if size > _JSON_VIEWER_MAX_BYTES:
                raise ValueError(
                    f"JSON document is larger than {_JSON_VIEWER_MAX_BYTES} bytes"
                )
            raw = path.read_bytes()
        except OSError as exc:
            raise RuntimeError("attempt snapshot cannot be read") from exc
        expected = str(file_entry.get("sha256") or "")
        actual = hashlib.sha256(raw).hexdigest()
        if not expected or actual != expected:
            raise RuntimeError("attempt snapshot hash does not match its archive")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("attempt snapshot is not valid JSON") from exc
        return {
            "value": value,
            "raw": raw,
            "digest": actual,
            "size": len(raw),
            "path": logical,
            "source": str(archive.relative_to(workspace)),
            "run_id": str(attempt["run_id"]),
            "_absolute_path": str(path),
        }

    def _task_attempt_diagnostic(
        self,
        job_id: str,
        task_id: str,
        history: list[dict[str, Any]],
        batch: Path | None,
    ) -> dict[str, Any] | None:
        """Recover failed-Gate facts only when the attempt archive binds their hash.

        Failed capabilities return a failure envelope instead of a normal output
        manifest. The attempt archive still records files changed before the
        exception. This projection verifies the current batch file against that
        archived hash and exposes it as diagnostic evidence, never as success.
        """
        relative = _STAGE_DIAGNOSTIC_FILES.get(task_id)
        if not relative or not history or batch is None:
            return None
        attempt_id = str(history[-1].get("attempt_id") or "")
        if not attempt_id:
            return None
        try:
            snapshot = self._verified_attempt_snapshot(
                job_id, task_id, attempt_id, relative,
            )
            if Path(str(snapshot["_absolute_path"])).resolve() != (batch / relative).resolve():
                return None
        except (KeyError, OSError, ValueError, RuntimeError, json.JSONDecodeError):
            return None
        value = snapshot["value"]
        if not isinstance(value, dict):
            return None
        return {
            "path": relative,
            "digest": snapshot["digest"],
            "role": "attempt_archive_diagnostic",
            "media_type": "application/json",
            "size": snapshot["size"],
            "facts": self._audit_document_facts(value),
            "integrity": "hash_verified_attempt_snapshot",
            "source": snapshot["source"],
            "attempt_id": attempt_id,
        }

    @staticmethod
    def _primary_audit_facts(output: dict[str, Any]) -> dict[str, Any]:
        documents = [
            item.get("facts") or {} for item in output.get("documents") or []
            if isinstance(item, dict)
        ]
        for facts in documents:
            if any(key in facts for key in ("decision", "verdict", "checks", "status")):
                return facts
        return documents[0] if documents else (output.get("execution_facts") or {})

    @staticmethod
    def _gate_projection(task_id: str, status: str, facts: dict[str, Any]) -> dict[str, Any] | None:
        is_gate = task_id.endswith("_gate") or bool(facts.get("checks"))
        if not is_gate:
            return None
        decision = str(facts.get("decision") or facts.get("verdict") or "")
        if not decision:
            decision = "PASS" if status == "succeeded" else "BLOCKED" if status in {
                "failed", "repairable", "manual_review", "quarantined", "blocked",
            } else "PENDING"
        advisory_value = facts.get("advisory_checks") or []
        if not isinstance(advisory_value, (list, tuple, set)):
            advisory_value = [advisory_value]
        advisory = {str(item) for item in advisory_value}
        checks_value = facts.get("checks") or {}
        if not isinstance(checks_value, dict):
            checks_value = {}
        checks = []
        for name, actual in checks_value.items():
            passed = actual is True or str(actual).lower() in {"pass", "passed", "ok", "true"}
            checks.append({
                "name": str(name), "actual": actual, "passed": passed,
                "advisory": str(name) in advisory, "blocking": str(name) not in advisory,
            })
        quality = facts.get("quality_assessment") or {}
        quality_checks = quality.get("checks") if isinstance(quality, dict) else {}
        if not isinstance(quality_checks, dict):
            quality_checks = {}
        for name, actual in quality_checks.items():
            if str(name) in {item["name"] for item in checks}:
                continue
            passed_check = actual is True or str(actual).lower() in {
                "pass", "passed", "ok", "true"
            }
            checks.append({
                "name": str(name), "actual": actual, "passed": passed_check,
                "advisory": True, "blocking": False,
                "constraint_class": "quality_target",
            })
        blocking_failures = [item["name"] for item in checks if item["blocking"] and not item["passed"]]
        advisory_failures = [item["name"] for item in checks if item["advisory"] and not item["passed"]]
        passed = facts.get("pipeline_continue") is True or decision.upper() in {
            "PASS", "PASS_WITH_DEBT", "PASSED", "OK", "SUCCEEDED", "APPROVED",
        }
        if passed:
            reason_zh = (
                "所有阻断性检查均已满足，因此 Gate 放行。"
                if decision.upper() in {"PASS", "PASSED", "OK", "SUCCEEDED", "APPROVED"}
                else "策略明确允许携带质量债或证据受限标记继续，因此 Gate 放行。"
            )
            if advisory_failures:
                reason_zh += " 未满足的建议项只限制成熟度，不阻止流程继续。"
        elif decision.upper() == "PENDING":
            reason_zh = "Gate 尚未执行，当前没有放行结论。"
        else:
            reason_zh = "存在未满足的阻断性检查，Gate 未放行。"
        return {
            "decision": decision, "passed": passed, "reason_zh": reason_zh,
            "checks": checks, "blocking_failures": blocking_failures,
            "advisory_failures": advisory_failures,
            "warnings": facts.get("warnings") or [], "failures": facts.get("failures") or [],
            "failed_requirement_ids": facts.get("failed_requirement_ids") or [],
            "question_contract_sha256": facts.get("question_contract_sha256"),
            "strategy_hash": facts.get("strategy_hash"),
            "question_evidence_ledger": facts.get("question_evidence_ledger") or {},
            "quality_assessment": quality,
            "maturity_ceiling": facts.get("maturity_ceiling"),
            "pipeline_continue": facts.get("pipeline_continue"),
        }

    @staticmethod
    def _research_question_projection(
        stages: list[dict[str, Any]],
        searches: list[dict[str, Any]],
        citations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Join the immutable question contract to execution and Gate evidence.

        The projection is deliberately read-only.  It never infers a missing v2
        contract for a legacy Job because doing so would make the Dashboard an
        unaudited source of research semantics.
        """
        plan_stage = next(
            (item for item in stages if item.get("task_id") == "research_plan"), None,
        )
        plan_documents = ((plan_stage or {}).get("output") or {}).get("documents") or []
        plan_document = next((
            item for item in plan_documents
            if isinstance(item, dict)
            and (item.get("facts") or {}).get("research_question_contracts")
        ), None) or next((
            item for item in plan_documents
            if isinstance(item, dict)
            and str(item.get("path") or "").endswith("research-plan.json")
        ), None)
        plan_facts = (plan_document or {}).get("facts") or {}
        contracts = plan_facts.get("research_question_contracts") or []

        gate_stage = next((
            item for item in reversed(stages)
            if ((item.get("gate") or {}).get("question_evidence_ledger") or {}).get("questions")
        ), None)
        gate = (gate_stage or {}).get("gate") or {}
        ledger = gate.get("question_evidence_ledger") or {}
        ledger_rows = {
            str(item.get("question_id") or ""): item
            for item in ledger.get("questions") or [] if isinstance(item, dict)
        }
        claim_question_ids: dict[str, str] = {}
        requirement_question_ids: dict[str, str] = {}
        for question_id, item in ledger_rows.items():
            for evidence in item.get("evidence") or []:
                if not isinstance(evidence, dict):
                    continue
                if evidence.get("claim_id"):
                    claim_question_ids[str(evidence["claim_id"])] = question_id
                if evidence.get("requirement_id"):
                    requirement_question_ids[str(evidence["requirement_id"])] = question_id
        gate_documents = ((gate_stage or {}).get("output") or {}).get("documents") or []
        gate_document = next((
            item for item in gate_documents
            if isinstance(item, dict)
            and (item.get("facts") or {}).get("question_evidence_ledger")
        ), None)

        if not contracts:
            legacy_questions = plan_facts.get("research_questions") or []
            return {
                "schema_version": "dashboard-research-question-governance-v1",
                "available": False,
                "reason": (
                    "legacy_research_plan_without_question_contract"
                    if legacy_questions else "question_contract_not_produced"
                ),
                "legacy_questions": legacy_questions,
                "questions": [],
                "artifacts": {
                    "plan": {
                        "digest": (plan_document or {}).get("digest"),
                        "path": (plan_document or {}).get("path"),
                    },
                    "gate": {
                        "digest": (gate_document or {}).get("digest"),
                        "path": (gate_document or {}).get("path"),
                    },
                },
            }

        searches_by_question: dict[str, list[dict[str, Any]]] = {}
        for search in searches:
            question_id = str(
                search.get("question_id")
                or claim_question_ids.get(str(search.get("field") or ""))
                or requirement_question_ids.get(str(search.get("requirement_id") or ""))
                or ""
            )
            if question_id:
                searches_by_question.setdefault(question_id, []).append(search)
        citations_by_question: dict[str, list[dict[str, Any]]] = {}
        for citation in citations:
            question_id = str(
                citation.get("question_id")
                or claim_question_ids.get(str(citation.get("claim_id") or ""))
                or requirement_question_ids.get(str(citation.get("requirement_id") or ""))
                or ""
            )
            if question_id:
                citations_by_question.setdefault(question_id, []).append(citation)

        questions: list[dict[str, Any]] = []
        for contract in contracts:
            required_ids = {str(value) for value in contract.get("required_question_ids") or []}
            for question in contract.get("subquestions") or []:
                if not isinstance(question, dict):
                    continue
                question_id = str(question.get("question_id") or "")
                evidence_state = ledger_rows.get(question_id) or {}
                question_searches = searches_by_question.get(question_id, [])
                question_citations = citations_by_question.get(question_id, [])
                results = [
                    result for search in question_searches
                    for result in search.get("results") or [] if isinstance(result, dict)
                ]
                status = str(evidence_state.get("status") or "planned")
                required = (
                    contract.get("criticality") == "required_for_model"
                    and question_id in required_ids
                )
                missing = evidence_state.get("missing_requirement_ids")
                if not isinstance(missing, list):
                    missing = list(question.get("requirement_ids") or [])
                if status == "confirmed":
                    conclusion_zh = "全部绑定 requirement 均已有确认性证据，问题已闭合。"
                elif status == "partially_supported":
                    conclusion_zh = "已有部分证据，但仍有绑定 requirement 未得到确认。"
                elif status == "contradicted":
                    conclusion_zh = "核验结果存在矛盾，不能把该问题视为闭合。"
                elif status == "explicit_gap":
                    conclusion_zh = "系统已明确记录证据缺口；该状态不是确认性证据。"
                elif status == "planned":
                    conclusion_zh = "研究问题已固化，但证据 Gate 尚未生成逐问题结论。"
                else:
                    conclusion_zh = "尚无足够证据满足该问题的闭合规则。"
                questions.append({
                    "question_id": question_id,
                    "dimension": contract.get("dimension"),
                    "criticality": contract.get("criticality"),
                    "required_for_model": required,
                    "question": question.get("question") or {},
                    "semantic_frame": question.get("semantic_frame") or {},
                    "closure_rule": question.get("closure_rule"),
                    "requirement_ids": question.get("requirement_ids") or [],
                    "source_role_requirements": contract.get("source_role_requirements") or [],
                    "preferred_source_classes": contract.get("preferred_source_classes") or [],
                    "query_intents": question.get("query_intents") or [],
                    "status": status,
                    "conclusion_zh": conclusion_zh,
                    "confirmed_requirement_ids": (
                        evidence_state.get("confirmed_requirement_ids") or []
                    ),
                    "missing_requirement_ids": missing,
                    "evidence": evidence_state.get("evidence") or [],
                    "execution": {
                        "queries": [{
                            "query_id": item.get("query_id"),
                            "intent_id": item.get("intent_id"),
                            "language": item.get("language"),
                            "query": item.get("query"),
                            "providers": item.get("providers") or [],
                            "candidate_count": len(item.get("results") or []),
                        } for item in question_searches],
                        "query_count": len(question_searches),
                        "candidate_count": len(results),
                        "accepted_count": sum(
                            item.get("outcome") == "accepted" for item in results
                        ),
                        "rejected_count": sum(
                            item.get("outcome") in {"rejected", "technical_failure"}
                            for item in results
                        ),
                        "verified_claim_count": len(question_citations),
                    },
                })

        required_questions = [item for item in questions if item["required_for_model"]]
        confirmed_required = [
            item for item in required_questions if item.get("status") == "confirmed"
        ]
        contract_hash = plan_facts.get("question_contract_sha256")
        ledger_hash = ledger.get("question_contract_sha256") or gate.get(
            "question_contract_sha256"
        )
        return {
            "schema_version": "dashboard-research-question-governance-v1",
            "available": True,
            "contract_version": plan_facts.get("research_question_contract_version"),
            "contract_sha256": contract_hash,
            "ledger_contract_sha256": ledger_hash,
            "contract_integrity": (
                None if not ledger_hash
                else bool(contract_hash and contract_hash == ledger_hash)
            ),
            "gate": {
                "task_id": (gate_stage or {}).get("task_id"),
                "decision": gate.get("decision"),
                "passed": gate.get("passed") is True,
                "reason_zh": gate.get("reason_zh"),
                "pipeline_continue": gate.get("pipeline_continue"),
                "maturity_ceiling": gate.get("maturity_ceiling"),
                "quality_assessment": gate.get("quality_assessment") or {},
            },
            "metrics": {
                "questions_total": len(questions),
                "required_questions_total": len(required_questions),
                "required_questions_confirmed": len(confirmed_required),
                "questions_with_queries": sum(
                    item["execution"]["query_count"] > 0 for item in questions
                ),
                "questions_with_evidence": sum(bool(item.get("evidence")) for item in questions),
            },
            "questions": questions,
            "unmapped_claims": ledger.get("unmapped_claims") or [],
            "artifacts": {
                "plan": {
                    "digest": (plan_document or {}).get("digest"),
                    "path": (plan_document or {}).get("path"),
                    "integrity": (plan_document or {}).get("integrity"),
                },
                "gate": {
                    "digest": (gate_document or {}).get("digest"),
                    "path": (gate_document or {}).get("path"),
                    "integrity": (gate_document or {}).get("integrity"),
                },
            },
        }

    def _execution_trace(
        self,
        job_id: str,
        *,
        job: dict[str, Any],
        run: dict[str, Any] | None,
        tasks: list[dict[str, Any]],
        attempts: list[dict[str, Any]],
        goal_alignment: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a compact audit projection from persisted workflow and evidence facts."""
        batch = self._trace_batch(job_id, goal_alignment)
        matrix = self._read_trace_json(batch, "table-data/search-matrix.executed.json")
        selection = self._read_trace_json(batch, "table-data/evidence-selection.json")
        source_evidence = self._read_trace_json(batch, "source-evidence.json")
        verified = self._read_trace_json(batch, "verify-output.json")
        frozen_search = self._read_trace_json(batch, "frozen-provider-search-results.json")
        diversity_repair = self._read_trace_json(batch, "research-scout-diversity-repair.json")
        frozen_queries = {
            str(item.get("search_hash") or ""): item
            for item in frozen_search.get("queries") or []
            if isinstance(item, dict) and item.get("search_hash")
        }
        frozen_attempts: dict[str, list[dict[str, Any]]] = {}
        for attempt in frozen_search.get("provider_attempts") or []:
            if isinstance(attempt, dict):
                frozen_attempts.setdefault(str(attempt.get("search_hash") or ""), []).append(attempt)
        excluded_urls = {
            str(url) for url in (
                (diversity_repair.get("diversity_repair") or {}).get("excluded_urls") or []
            ) if url
        }

        attempts_by_task: dict[str, list[dict[str, Any]]] = {}
        for attempt in attempts:
            attempts_by_task.setdefault(str(attempt.get("task_id") or ""), []).append(attempt)
        task_statuses = {
            str(item.get("task_id") or ""): str(item.get("status") or "") for item in tasks
        }
        stages = []
        for ordinal, task in enumerate(tasks, 1):
            task_id = str(task.get("task_id") or "")
            status = str(task.get("status") or "pending")
            history = sorted(
                attempts_by_task.get(task_id, []),
                key=lambda item: str(item.get("started_at") or ""),
            )
            failures = [
                item for item in history
                if item.get("status") in {
                    "failed", "repairable", "retryable", "manual_review",
                    "quarantined", "blocked", "blocked_budget",
                }
            ]
            output = self._task_output_audit(task.get("output_hash"))
            diagnostic = None
            if output.get("protocol") != "task-output-manifest-v1":
                diagnostic = self._task_attempt_diagnostic(
                    job_id, task_id, history, batch,
                )
            if diagnostic:
                output = {
                    **output,
                    "documents": [*(output.get("documents") or []), diagnostic],
                    "diagnostic_integrity": diagnostic["integrity"],
                }
            facts = self._primary_audit_facts(output)
            gate = self._gate_projection(task_id, status, facts)
            if gate:
                gate["evidence_source"] = (
                    "hash_verified_attempt_snapshot" if diagnostic
                    else "immutable_task_output"
                )
            latest_failure = (
                (failures[-1].get("failure_payload") or {}) if failures
                else (task.get("failure_payload") or {})
            )
            policy = latest_failure.get("policy_decision") or {}
            failure_code = task.get("failure_code") or (
                failures[-1].get("failure_code") if failures else None
            )
            dependencies = [str(item) for item in (task.get("dependencies") or [])]
            dependents = [
                str(item.get("task_id") or "") for item in tasks
                if task_id in {str(value) for value in (item.get("dependencies") or [])}
            ]
            blocked_dependencies = [
                item for item in dependencies if task_statuses.get(item) != "succeeded"
            ]
            meta = _STAGE_AUDIT_META.get(task_id, (
                task_id.replace("_", " "), "执行工作流声明的阶段目标。",
                f"调用 {task.get('capability_id') or '已注册能力'} 完成本阶段。",
            ))
            raw_reason = facts.get("reason") or facts.get("summary")
            if not raw_reason and status == "succeeded" and gate:
                raw_reason = f"Gate decision {gate['decision']}"
            if not raw_reason and status != "succeeded":
                raw_reason = latest_failure.get("message") or policy.get("reason")
            if status == "succeeded":
                if gate:
                    conclusion_zh = gate["reason_zh"]
                else:
                    conclusion_zh = "阶段执行成功，输出已经固化为不可变产物。"
                if dependents:
                    transition_zh = (
                        "本阶段已满足下游依赖条件，可继续进入：" + "、".join(dependents) + "。"
                    )
                else:
                    transition_zh = "本阶段成功结束；没有声明直接下游阶段。"
                transition_allowed = True
            elif status in {"failed", "repairable", "retryable", "manual_review", "quarantined", "blocked", "blocked_budget"}:
                conclusion_zh = (
                    f"阶段未成功：{failure_code or '未记录错误代码'}。"
                    + (f" {policy.get('reason')}" if policy.get("reason") else "")
                )
                transition_zh = "阶段没有放行，下游任务不会执行。"
                if policy.get("action") == "repair":
                    transition_zh += " 修复策略要求回卷并重新生成失效输入。"
                elif policy.get("action") == "manual_review":
                    transition_zh += " 相同失败重复出现，已停止盲目重试并等待人工审核。"
                elif policy.get("action") == "quarantine":
                    transition_zh += " 自动修复预算已耗尽，任务已隔离。"
                transition_allowed = False
            elif status == "skipped":
                conclusion_zh = "阶段按工作流策略跳过，没有执行 Agent 或写入产物。"
                transition_zh = "跳过是显式策略结果，不等同于成功执行。"
                transition_allowed = True
            else:
                conclusion_zh = "阶段尚未执行，没有形成 Agent 结论或 Gate 判定。"
                transition_zh = (
                    "等待以下上游阶段成功：" + "、".join(blocked_dependencies) + "。"
                    if blocked_dependencies else "等待调度器领取并执行。"
                )
                transition_allowed = False
            attempt_audit = []
            for item in history:
                attempt_failure = item.get("failure_payload") or {}
                attempt_policy = attempt_failure.get("policy_decision") or {}
                attempt_status = str(item.get("status") or "unknown")
                event_rows = [{
                    "event": "task.claimed", "event_zh": "Worker 领取阶段并绑定本次输入",
                    "at": item.get("started_at"),
                }]
                if attempt_status == "succeeded":
                    event_rows.append({
                        "event": "task.succeeded", "event_zh": "阶段成功并固化输出产物",
                        "at": item.get("finished_at"),
                    })
                elif item.get("finished_at"):
                    event_rows.append({
                        "event": "task.failed", "event_zh": (
                            "阶段失败；修复策略决定：" + str(attempt_policy.get("action") or "记录失败")
                        ), "at": item.get("finished_at"),
                    })
                attempt_audit.append({
                    "attempt": item.get("attempt"), "attempt_id": item.get("attempt_id"),
                    "status": attempt_status, "status_zh": _STATUS_ZH.get(attempt_status, attempt_status),
                    "worker_id": item.get("worker_id"), "started_at": item.get("started_at"),
                    "finished_at": item.get("finished_at"),
                    "input_hashes": item.get("input_hashes") or [],
                    "output_hash": item.get("output_hash"),
                    "failure_code": item.get("failure_code"),
                    "failure_message": attempt_failure.get("message"),
                    "failure_fingerprint": attempt_failure.get("failure_fingerprint"),
                    "repair_action": attempt_policy.get("action"),
                    "repair_reason": attempt_policy.get("reason"),
                    "invalidates": attempt_policy.get("invalidates") or [],
                    "preserves": attempt_policy.get("preserves") or [],
                    "events": event_rows,
                })
            inputs = task.get("inputs") or {}
            workers = list(dict.fromkeys(
                str(item.get("worker_id")) for item in history if item.get("worker_id")
            ))
            stages.append({
                "ordinal": ordinal,
                "task_id": task_id,
                "name_zh": meta[0], "purpose_zh": meta[1], "action_zh": meta[2],
                "capability_id": task.get("capability_id"),
                "status": status, "status_zh": _STATUS_ZH.get(status, status),
                "attempt_count": len(history) or int(task.get("attempt") or 0),
                "failed_attempts": len(failures),
                "started_at": history[0].get("started_at") if history else None,
                "finished_at": history[-1].get("finished_at") if history else None,
                "updated_at": task.get("updated_at"),
                "failure_code": failure_code,
                "output_hash": task.get("output_hash"),
                "dependencies": dependencies, "blocked_dependencies": blocked_dependencies,
                "agent": {
                    "logical_actor_zh": _CAPABILITY_ACTORS.get(
                        str(task.get("capability_id") or ""), "已注册能力执行器"
                    ),
                    "capability_id": task.get("capability_id"),
                    "runtime_profile": inputs.get("runtime_profile"),
                    "worker_ids": workers,
                },
                "inputs": {
                    "action": inputs.get("action"), "dependencies": dependencies,
                    "latest_input_hashes": history[-1].get("input_hashes") if history else [],
                },
                "attempts": attempt_audit,
                "output": output,
                "conclusion": {
                    "summary_zh": conclusion_zh, "raw_reason": raw_reason,
                    "decision": facts.get("decision") or facts.get("verdict") or facts.get("status"),
                    "metrics": facts.get("metrics") or facts.get("counts") or {},
                },
                "gate": gate,
                "transition": {
                    "allowed": transition_allowed, "reason_zh": transition_zh,
                    "next_tasks": dependents,
                },
            })

        audits: dict[tuple[str, str], dict[str, Any]] = {}
        for audit in selection.get("candidate_audits") or []:
            key = (str(audit.get("query_hash") or ""), str(audit.get("url") or ""))
            audits[key] = audit
        accepted_urls = {
            str(item.get("url") or item.get("source_url") or "")
            for item in (selection.get("accepted_evidence") or [])
            if isinstance(item, dict) and (item.get("url") or item.get("source_url"))
        }

        searches: list[dict[str, Any]] = []
        for item in matrix.get("queries") or []:
            if not isinstance(item, dict):
                continue
            query_hash = str(item.get("query_hash") or "")
            results = []
            for result in item.get("results") or []:
                if not isinstance(result, dict):
                    continue
                url = str(result.get("url") or "")
                audit = audits.get((query_hash, url), {})
                decision = str(audit.get("decision") or result.get("current_job_status") or "candidate")
                selected = decision in {"accepted", "selected", "confirmed"} or url in accepted_urls
                fetch_status = str(result.get("fetch_status") or "") or None
                raw_error = result.get("error")
                if isinstance(raw_error, dict):
                    technical_error = {
                        "code": raw_error.get("code") or "fetch_error",
                        "message": raw_error.get("message") or json.dumps(raw_error, ensure_ascii=False),
                    }
                elif raw_error:
                    technical_error = {"code": "fetch_error", "message": str(raw_error)}
                else:
                    technical_error = None
                technical_failure = bool(technical_error) or fetch_status in {"error", "failed"}
                if selected:
                    outcome = "accepted"
                    decision_stage = "evidence_selection"
                elif technical_failure:
                    outcome = "technical_failure"
                    decision_stage = "fetch_or_extraction"
                elif decision == "rejected":
                    outcome = "rejected"
                    decision_stage = "evidence_selection"
                else:
                    outcome = "pending"
                    decision_stage = "discovery"
                results.append({
                    "title": result.get("title") or url,
                    "url": url,
                    "domain": self._trace_domain(url),
                    "status": result.get("status") or result.get("current_job_status"),
                    "candidate_status": result.get("current_job_status"),
                    "provider": result.get("provider"),
                    "fetch_status": fetch_status,
                    "content_type": result.get("content_type"),
                    "source_class": result.get("source_class"),
                    "snippet": result.get("snippet"),
                    "decision": decision,
                    "outcome": outcome,
                    "decision_stage": decision_stage,
                    "evaluation_completed": outcome in {"accepted", "rejected"},
                    "selected": selected,
                    "reasons": audit.get("reasons") or [],
                    "technical_error": technical_error,
                    "observation_count": len(audit.get("observations") or []),
                    "observations": (audit.get("observations") or [])[:8],
                    "extraction_support": audit.get("extraction_support"),
                    "public_extractability": audit.get("public_extractability"),
                    "document_route": audit.get("document_route") or result.get("document_route"),
                    "document_type": audit.get("document_type") or result.get("document_type"),
                    "verifications": [],
                })
            providers = [
                {"provider": value.get("provider"), "status": value.get("status"),
                 "results": value.get("results"), "cache_hit": value.get("cache_hit") is True}
                for value in (item.get("provider_attempts") or []) if isinstance(value, dict)
            ]
            searches.append({
                "query_id": query_hash or f"table-query-{len(searches) + 1}",
                "kind": "table_field",
                "field": item.get("field"),
                "table": item.get("table"),
                "language": item.get("language"),
                "strategy": item.get("query_strategy"),
                "query": item.get("query"),
                "providers": providers,
                "results": results,
            })

        claim_search_ids: set[str] = set()
        for item in source_evidence.get("claims") or []:
            if not isinstance(item, dict) or not isinstance(item.get("query"), dict):
                continue
            query = item["query"]
            research_tracks = [
                value for value in item.get("research_tracks") or []
                if isinstance(value, dict)
            ]
            primary_track = next((
                value for value in research_tracks if value.get("question_id")
            ), {})
            query_id = str(query.get("query_id") or query.get("search_hash") or "")
            if not query_id or query_id in claim_search_ids:
                continue
            claim_search_ids.add(query_id)
            candidates = []
            providers: dict[str, int] = {}
            candidate_urls: set[str] = set()
            for result in item.get("candidates") or []:
                if not isinstance(result, dict):
                    continue
                provider = str(result.get("search_provider") or "research_scout")
                providers[provider] = providers.get(provider, 0) + 1
                url = str(result.get("url") or "")
                candidate_urls.add(url)
                raw_error = result.get("error")
                technical_error = (
                    {"code": raw_error.get("code") or "fetch_error",
                     "message": raw_error.get("message") or json.dumps(raw_error, ensure_ascii=False)}
                    if isinstance(raw_error, dict)
                    else ({"code": "fetch_error", "message": str(raw_error)} if raw_error else None)
                )
                fetch_status = str(result.get("status") or item.get("search_status") or "") or None
                technical_failure = bool(technical_error) or fetch_status in {"error", "failed"}
                candidates.append({
                    "title": result.get("title") or url,
                    "url": url,
                    "domain": self._trace_domain(url),
                    "status": fetch_status,
                    "candidate_status": item.get("disposition"),
                    "provider": provider,
                    "fetch_status": fetch_status,
                    "content_type": result.get("content_type"),
                    "source_class": result.get("source_class"),
                    "snippet": result.get("excerpt"),
                    "decision": item.get("disposition") or "sent_to_verification",
                    "outcome": "technical_failure" if technical_failure else "pending",
                    "decision_stage": "fetch_or_extraction" if technical_failure else "verification",
                    "evaluation_completed": False,
                    "selected": False,
                    "reasons": [],
                    "technical_error": technical_error,
                    "observation_count": 0,
                    "observations": [],
                    "extraction_support": None,
                    "public_extractability": None,
                    "document_route": None,
                    "document_type": result.get("content_type"),
                    "claim_id": (item.get("claim") or {}).get("claim_id"),
                    "verifications": [],
                })
            frozen = frozen_queries.get(str(query.get("search_hash") or query_id), {})
            for result in frozen.get("results") or []:
                if not isinstance(result, dict):
                    continue
                url = str(result.get("url") or "")
                if not url or url in candidate_urls:
                    continue
                provider = str(result.get("provider") or "research_scout")
                providers[provider] = providers.get(provider, 0) + 1
                excluded = url in excluded_urls
                candidates.append({
                    "title": result.get("title") or url,
                    "url": url,
                    "domain": self._trace_domain(url),
                    "status": frozen.get("status") or "found",
                    "candidate_status": "excluded" if excluded else "not_selected_for_fetch",
                    "provider": provider,
                    "fetch_status": "not_fetched",
                    "content_type": None,
                    "source_class": None,
                    "snippet": result.get("snippet"),
                    "decision": "rejected",
                    "outcome": "rejected",
                    "decision_stage": "diversity_repair" if excluded else "candidate_binding",
                    "evaluation_completed": True,
                    "selected": False,
                    "reasons": [
                        "excluded_by_diversity_repair" if excluded
                        else "not_selected_for_fetch"
                    ],
                    "technical_error": None,
                    "observation_count": 0,
                    "observations": [],
                    "extraction_support": None,
                    "public_extractability": None,
                    "document_route": None,
                    "document_type": None,
                    "claim_id": (item.get("claim") or {}).get("claim_id"),
                    "verifications": [],
                })
            attempt_rows = frozen_attempts.get(str(query.get("search_hash") or query_id), [])
            provider_rows = [
                {"provider": value.get("provider"), "status": value.get("status"),
                 "results": value.get("results"), "cache_hit": value.get("cache_hit") is True,
                 "error": value.get("error")}
                for value in attempt_rows
            ]
            if not provider_rows:
                provider_rows = [
                    {"provider": provider, "status": "ok", "results": count,
                     "cache_hit": False, "error": None}
                    for provider, count in providers.items()
                ]
            searches.append({
                "query_id": query_id,
                "kind": "claim_evidence",
                "field": (item.get("claim") or {}).get("claim_id"),
                "requirement_id": (item.get("claim") or {}).get("requirement_id"),
                "question_id": primary_track.get("question_id"),
                "research_question": primary_track.get("research_question"),
                "intent_id": primary_track.get("intent_id"),
                "research_tracks": research_tracks,
                "table": None,
                "language": primary_track.get("language"),
                "strategy": "source_first" if query.get("source_first") else "claim_search",
                "query": query.get("text"),
                "providers": provider_rows,
                "results": candidates,
            })

        citations = []
        for item in verified.get("claims") or []:
            if not isinstance(item, dict):
                continue
            claim = item.get("claim") or {}
            check = item.get("verify") or {}
            fetched = item.get("fetchResult") or {}
            verdict = str(check.get("verdict") or "NOT_REVIEWED")
            url = str(fetched.get("url") or "")
            selected = verdict == "CONFIRMED"
            citations.append({
                "claim_id": claim.get("claim_id"),
                "requirement_id": claim.get("requirement_id"),
                "question_id": claim.get("question_id"),
                "section": claim.get("section"),
                "claim_kind": claim.get("claim_kind"),
                "claim_text": claim.get("claim_text"),
                "verdict": verdict,
                "node_alignment": check.get("node_alignment"),
                "reasoning": check.get("reasoning"),
                "supporting_quote": check.get("supporting_quote"),
                "url": url,
                "domain": self._trace_domain(url),
                "selected": selected,
            })
        citation_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for citation in citations:
            key = (str(citation.get("claim_id") or ""), str(citation.get("url") or ""))
            if key[0] and key[1]:
                citation_index.setdefault(key, []).append(citation)
        for search in searches:
            if search.get("kind") != "claim_evidence":
                continue
            for result in search["results"]:
                matches = citation_index.get(
                    (str(result.get("claim_id") or ""), str(result.get("url") or "")), []
                )
                if not matches:
                    continue
                result["verifications"] = matches
                result["reasons"] = [
                    item.get("reasoning") for item in matches if item.get("reasoning")
                ]
                verdicts = {str(item.get("verdict") or "") for item in matches}
                if "CONFIRMED" in verdicts:
                    result["selected"] = True
                    result["decision"] = "confirmed_citation"
                    result["outcome"] = "accepted"
                    result["decision_stage"] = "claim_verification"
                    result["evaluation_completed"] = True
                elif verdicts & {"INSUFFICIENT", "NOT_FOUND"}:
                    result["decision"] = sorted(verdicts)[0]
                    result["outcome"] = "rejected"
                    result["decision_stage"] = "claim_verification"
                    result["evaluation_completed"] = True

        table_fields = []
        for item in selection.get("fields") or []:
            if not isinstance(item, dict):
                continue
            gap = item.get("gap_evidence") or {}
            table_fields.append({
                "table": item.get("table"),
                "field": item.get("field"),
                "decision": item.get("decision"),
                "candidate_count": item.get("candidate_count", 0),
                "reason": item.get("reason") or gap.get("reason"),
                "rejected_urls": gap.get("rejected_candidate_urls") or [],
                "query_hashes": gap.get("query_hashes") or [],
            })

        severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        issues_by_key: dict[str, dict[str, Any]] = {}
        deviations = goal_alignment.get("deviations") or []
        for deviation in deviations:
            payload = deviation.get("payload") or {}
            summary = str(payload.get("summary") or deviation.get("fingerprint") or "未分类偏离")
            key = f"{deviation.get('deviation_type')}|{summary}"
            current = issues_by_key.setdefault(key, {
                "deviation_type": deviation.get("deviation_type"),
                "severity": deviation.get("severity"), "summary": summary,
                "count": 0, "statuses": set(), "ids": [],
                "first_seen": deviation.get("created_at"), "last_seen": deviation.get("updated_at"),
                "evidence": payload.get("evidence") or {},
            })
            current["count"] += 1
            current["statuses"].add(str(deviation.get("status") or "unknown"))
            current["ids"].append(deviation.get("deviation_id"))
            if severity_rank.get(str(deviation.get("severity")), 0) > severity_rank.get(str(current["severity"]), 0):
                current["severity"] = deviation.get("severity")
            current["first_seen"] = min(filter(None, [current["first_seen"], deviation.get("created_at")]), default=None)
            current["last_seen"] = max(filter(None, [current["last_seen"], deviation.get("updated_at")]), default=None)
        issues = []
        for item in issues_by_key.values():
            item["statuses"] = sorted(item["statuses"])
            issues.append(item)
        issues.sort(key=lambda item: (severity_rank.get(str(item.get("severity")), 0), str(item.get("last_seen") or "")), reverse=True)

        deviation_ids = {
            str(item.get("deviation_id") or "") for item in deviations
            if item.get("deviation_id")
        }
        repair_plans = [
            item for item in (goal_alignment.get("repair_plans") or [])
            if str(item.get("deviation_id") or "") in deviation_ids
        ]
        change_candidates = []
        for item in goal_alignment.get("change_candidates") or []:
            payload = item.get("payload") or {}
            if (str(item.get("source_deviation_id") or "") in deviation_ids
                    or payload.get("source_job_id") == job_id):
                change_candidates.append(item)

        repair_rows = goal_alignment.get("system_repair_runs") or []
        active_repair_statuses = {
            "queued", "coding", "validating", "awaiting_scm_publication",
            "awaiting_approval", "promoted", "awaiting_outcome_validation",
        }
        actions: list[dict[str, Any]] = []
        action_specs = (
            ("triage", goal_alignment.get("failure_triage_runs") or []),
            ("repair_plan", repair_plans),
            ("system_change", change_candidates),
            ("code_repair", repair_rows),
        )
        for kind, rows in action_specs:
            for row in rows:
                payload = row.get("payload") or {}
                result = payload.get("result") or {}
                request = payload.get("request") or {}
                triage = request.get("triage") or {}
                title = (
                    result.get("cause_code") or request.get("cause_code")
                    or triage.get("cause_code") or row.get("action") or row.get("target")
                    or payload.get("action") or row.get("model") or kind
                )
                summary = (
                    result.get("summary") or request.get("explanation")
                    or triage.get("summary") or payload.get("summary")
                    or row.get("last_error") or payload.get("explanation")
                    or payload.get("status") or ""
                )
                actions.append({
                    "kind": kind, "status": row.get("status"), "title": title,
                    "summary": summary, "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "id": (row.get("repair_run_id") if kind == "code_repair" else None)
                    or row.get("triage_run_id") or row.get("repair_plan_id")
                    or row.get("candidate_id"),
                    "risk": row.get("risk") or result.get("risk") or triage.get("risk"),
                    "details": {
                        key: value for key, value in {
                            "cause_code": result.get("cause_code") or request.get("cause_code"),
                            "failed_task": request.get("failed_task"),
                            "recovery_task": result.get("recovery_task")
                            or request.get("recovery_task"),
                            "implementation_targets": result.get("implementation_targets")
                            or request.get("implementation_targets"),
                            "causal_input_changes": result.get("causal_input_changes")
                            or request.get("causal_input_changes"),
                            "proof_contract": result.get("proof_contract")
                            or request.get("proof_contract"),
                            "patch_hash": row.get("patch_hash"),
                            "failure_fingerprint": payload.get("failure_fingerprint")
                            or request.get("source_failure_fingerprint"),
                            "causal_plan_hash": payload.get("causal_plan_hash"),
                            "outcome_validation": payload.get("outcome_validation"),
                            "scm": payload.get("scm"),
                            "action": row.get("action") or payload.get("action"),
                        }.items() if value not in (None, "", [], {})
                    },
                })
        actions.sort(key=lambda item: str(item.get("created_at") or ""))

        repair_activity: dict[str, Any] = {
            "available": bool(repair_rows), "active": False,
            "history_count": len(repair_rows), "latest": None,
        }
        if repair_rows:
            latest_repair = next(
                (item for item in repair_rows
                 if str(item.get("status") or "") in active_repair_statuses),
                repair_rows[0],
            )
            repair_payload = latest_repair.get("payload") or {}
            repair_request = repair_payload.get("request") or {}
            repair_triage = repair_request.get("triage") or {}
            repair_scm = repair_payload.get("scm") or {}
            repair_status = str(latest_repair.get("status") or "unknown")
            validations = list(repair_payload.get("validations") or [])
            validation_passed = bool(validations) and all(
                item.get("passed") is True for item in validations
            )
            coding_done = bool(
                repair_payload.get("agent_result")
                or repair_payload.get("changed_files")
                or latest_repair.get("patch_hash")
                or repair_status in {
                    "validating", "awaiting_scm_publication", "awaiting_approval",
                    "promoted", "awaiting_outcome_validation", "effective",
                    "partially_effective", "ineffective",
                }
            )
            scm_published = bool(
                repair_scm.get("pr_url")
                or repair_scm.get("status") == "published"
            )
            promoted = repair_status in {
                "promoted", "awaiting_outcome_validation", "effective",
                "partially_effective", "ineffective",
            }
            outcome_done = repair_status in {"effective", "partially_effective"}

            def repair_step(
                step_id: str, label: str, *, done: bool = False,
                active: bool = False, failed: bool = False,
            ) -> dict[str, Any]:
                state = "failed" if failed else "active" if active else "done" if done else "pending"
                return {"id": step_id, "label": label, "state": state}

            repair_activity = {
                "available": True,
                "active": repair_status in active_repair_statuses,
                "history_count": len(repair_rows),
                "latest": {
                    "repair_run_id": latest_repair.get("repair_run_id"),
                    "candidate_id": latest_repair.get("candidate_id"),
                    "status": repair_status,
                    "cause_code": repair_request.get("cause_code")
                    or repair_triage.get("cause_code"),
                    "summary": repair_request.get("explanation")
                    or repair_triage.get("summary") or latest_repair.get("last_error"),
                    "failed_task": repair_request.get("failed_task"),
                    "recovery_task": repair_request.get("recovery_task"),
                    "created_at": latest_repair.get("created_at"),
                    "updated_at": latest_repair.get("updated_at"),
                    "last_error": latest_repair.get("last_error"),
                    "attempt": (repair_payload.get("execution") or {}).get("attempt"),
                    "owner_id": (repair_payload.get("execution") or {}).get("owner_id"),
                    "started_at": (repair_payload.get("execution") or {}).get("started_at"),
                    "patch_hash": latest_repair.get("patch_hash"),
                    "changed_files": repair_payload.get("changed_files") or [],
                    "validations": validations,
                    "scm": {
                        "status": repair_scm.get("status"),
                        "issue_number": repair_scm.get("issue_number"),
                        "issue_url": repair_scm.get("issue_url"),
                        "pr_number": repair_scm.get("pr_number"),
                        "pr_url": repair_scm.get("pr_url"),
                        "head_branch": repair_scm.get("head_branch"),
                        "commit_sha": repair_scm.get("commit_sha"),
                        "last_error": repair_scm.get("last_error"),
                    },
                    "steps": [
                        repair_step("diagnosis", "根因诊断", done=True),
                        repair_step(
                            "issue", "Issue 建档", done=bool(repair_scm.get("issue_url")),
                            active=repair_status == "queued" and not repair_scm.get("issue_url"),
                        ),
                        repair_step(
                            "coding", "代码修复", done=coding_done,
                            active=repair_status == "coding",
                            failed=repair_status == "failed" and not coding_done,
                        ),
                        repair_step(
                            "validation", "测试与 Canary", done=validation_passed,
                            active=repair_status == "validating",
                            failed=repair_status == "failed" and coding_done,
                        ),
                        repair_step(
                            "scm", "PR 与合并", done=scm_published,
                            active=repair_status == "awaiting_scm_publication",
                        ),
                        repair_step(
                            "promotion", "部署与受控回卷", done=promoted,
                            active=repair_status in {"awaiting_approval", "promoted"},
                        ),
                        repair_step(
                            "outcome", "正式运行验证", done=outcome_done,
                            active=repair_status == "awaiting_outcome_validation",
                            failed=repair_status == "ineffective",
                        ),
                    ],
                },
            }

        quality = (goal_alignment.get("quality_observations") or [{}])[0].get("payload") or {}
        research = ((quality.get("evidence") or {}).get("research_outcome") or {})
        maturity = ((quality.get("evidence") or {}).get("maturity") or {})
        total_results = sum(len(item.get("results") or []) for item in searches)
        result_outcomes = [
            str(result.get("outcome") or "pending")
            for search in searches for result in search.get("results") or []
        ]
        providers = sorted({
            str(provider.get("provider")) for item in searches for provider in item.get("providers") or []
            if provider.get("provider")
        })
        source_domains = sorted({
            str(result.get("domain")) for item in searches for result in item.get("results") or []
            if result.get("domain")
        })
        populated_fields = int((selection.get("counts") or {}).get("populated") or 0)
        accepted_evidence = len(selection.get("accepted_evidence") or [])
        open_issue_summaries = [
            str(item.get("summary") or item.get("deviation_type") or "目标偏离")
            for item in issues if "open" in item.get("statuses", [])
        ]
        blockers = list(dict.fromkeys([
            *[str(value) for value in maturity.get("reason_codes") or []],
            *[str(value) for value in research.get("reason_codes") or []],
            *open_issue_summaries,
        ]))
        modeling_ready = bool(
            maturity.get("candidate_eligible") is True
            and maturity.get("data_readiness") == "data_ready"
            and accepted_evidence > 0 and populated_fields > 0
        )
        completion_goal = self._declared_completion_goal(job_id, job)
        publication_proof_valid = False
        publication_proof_error = None
        if completion_goal == "reviewed_publication":
            from lca_project.kernel.goal_alignment.autonomous_supervisor import (
                verify_reviewed_publication,
            )
            publication_proof_valid, publication_proof_error = verify_reviewed_publication(
                self.control, job_id, str(run["run_id"]) if run else None,
            )
            if not publication_proof_valid:
                blockers.append("governed_reviewed_publication_not_proven")
        workflow_complete = bool(run and run.get("status") == "succeeded")
        goal_complete = (
            workflow_complete if completion_goal == "workflow_delivery"
            else modeling_ready and publication_proof_valid
            if completion_goal == "reviewed_publication"
            else modeling_ready
        )
        autonomy_active = str(job.get("status") or "") in {
            "planned", "ready", "leased", "running", "stalled", "retryable",
            "repairable", "manual_review", "blocked_budget", "candidate", "gated", "applied",
        }
        pipeline_continue = bool(
            maturity.get("pipeline_continue") is True
            or (
                completion_goal == "reviewed_publication"
                and not publication_proof_valid and autonomy_active
            )
        )
        if goal_complete:
            next_action = "目标已完成"
        elif completion_goal == "reviewed_publication" and modeling_ready:
            next_action = "继续审核与受控发布" if autonomy_active else "等待恢复审核发布尾链"
        elif pipeline_continue:
            next_action = "继续自治修复" if autonomy_active else "存在修复路径，等待恢复"
        else:
            next_action = "无自动路径或等待授权"
        research_question_governance = self._research_question_projection(
            stages, searches, citations,
        )
        return {
            "schema_version": "dashboard-execution-trace-v2",
            "job_id": job_id,
            "batch": str(batch) if batch else None,
            "summary": {
                "tasks": len(tasks),
                "tasks_succeeded": sum(item.get("status") == "succeeded" for item in tasks),
                "attempts": len(attempts),
                "failed_attempts": sum(
                    item.get("status") in {
                        "failed", "repairable", "retryable", "manual_review",
                        "quarantined", "blocked", "blocked_budget",
                    }
                    for item in attempts
                ),
                "queries": len(searches),
                "candidate_results": total_results,
                "candidate_accepted": result_outcomes.count("accepted"),
                "candidate_rejected": result_outcomes.count("rejected"),
                "candidate_technical_failures": result_outcomes.count("technical_failure"),
                "candidate_pending": result_outcomes.count("pending"),
                "providers": len(providers),
                "source_domains": len(source_domains),
                "confirmed_citations": sum(item.get("selected") is True for item in citations),
                "table_fields": len(table_fields),
                "populated_fields": populated_fields,
                "open_issues": sum("open" in item.get("statuses", []) for item in issues),
                "repair_actions": len(actions),
            },
            "providers": providers,
            "source_domains": source_domains,
            "stages": stages,
            "searches": searches,
            "citations": citations,
            "table_fields": table_fields,
            "issues": issues,
            "actions": actions,
            "repair_activity": repair_activity,
            "research_question_governance": research_question_governance,
            "quality": {"score": quality.get("score"), "dimensions": quality.get("dimensions") or {}},
            "research_outcome": research,
            "goal_status": {
                "goal_id": completion_goal,
                "workflow_complete": workflow_complete,
                "goal_complete": goal_complete,
                "modeling_ready": modeling_ready,
                "candidate_eligible": maturity.get("candidate_eligible") is True,
                "maturity": maturity.get("maturity"),
                "data_readiness": maturity.get("data_readiness"),
                "accepted_evidence": accepted_evidence,
                "populated_fields": populated_fields,
                "publication_proof_valid": publication_proof_valid,
                "publication_proof_error": publication_proof_error,
                "pipeline_continue": pipeline_continue,
                "autonomy_active": autonomy_active,
                "next_action": next_action,
                "blockers": list(dict.fromkeys(blockers)),
            },
            "run_status": run.get("status") if run else None,
        }

    def _preview_projection(
        self,
        job_id: str,
        *,
        run: dict[str, Any] | None = None,
        tasks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Return a verified, workspace-confined preview projection for a Job."""
        if not self._has_table("orchestrator_runs") or not self._has_table("orchestrator_tasks"):
            return None
        if run is None:
            run = _row(self.conn.execute(
                "SELECT * FROM orchestrator_runs WHERE job_id=?", (job_id,)
            ).fetchone())
        if not run:
            return None
        if tasks is None:
            tasks = self._rows(
                "SELECT * FROM orchestrator_tasks WHERE run_id=? ORDER BY rowid",
                (run["run_id"],),
            )
        preview_task = next((item for item in tasks if item.get("task_id") == "preview"), None)
        if not preview_task or preview_task.get("status") != "succeeded":
            return None
        output_hash = str(preview_task.get("output_hash") or "")
        if not output_hash:
            return None
        try:
            manifest = self.control.artifacts.verify_task_output_manifest(output_hash)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            return None
        report_entry = next(
            (item for item in manifest.get("files") or []
             if str(item.get("path") or "").endswith("/preview-report.json")),
            None,
        )
        if not report_entry:
            return None
        workspace = (self.root / "var" / "workspaces" / "jobs" / job_id).resolve()
        report_path = (workspace / str(report_entry["path"])).resolve()
        if (not report_path.is_relative_to(workspace) or not report_path.is_file()
                or report_path.is_symlink()):
            return None
        report_bytes = report_path.read_bytes()
        if hashlib.sha256(report_bytes).hexdigest() != str(report_entry.get("sha256") or ""):
            return None
        try:
            report = json.loads(report_bytes)
        except json.JSONDecodeError:
            return None
        docs = (workspace / "docs").resolve()
        assets: dict[str, dict[str, str]] = {}
        for role, value in (report.get("artifacts") or {}).items():
            if not isinstance(value, dict):
                continue
            path = Path(str(value.get("path") or "")).resolve()
            digest = str(value.get("sha256") or "")
            if (not path.is_relative_to(docs) or not path.is_file() or path.is_symlink()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
                return None
            assets[str(role)] = {"filename": path.name, "sha256": digest}
        viewer_value = assets.get("viewer") or {}
        viewer_name = str(viewer_value.get("filename") or "")
        if not viewer_name:
            return None
        return {
            "url": f"/preview/{job_id}/{viewer_name}",
            "viewer": viewer_name,
            "assets": assets,
            "maturity": report.get("maturity"),
            "mode": report.get("mode"),
            "start_node": report.get("start_node"),
            "all_passed": report.get("all_passed") is True,
            "candidate_eligible": report.get("candidate_eligible") is True,
            "report_sha256": str(report_entry.get("sha256") or ""),
        }

    def preview_asset(self, job_id: str, filename: str) -> Path:
        """Resolve one generated preview asset without exposing the Job workspace."""
        if not filename or Path(filename).name != filename:
            raise KeyError(job_id)
        projection = self._preview_projection(job_id)
        if not projection:
            raise KeyError(job_id)
        expected = next(
            (str(value.get("sha256") or "") for value in projection.get("assets", {}).values()
             if value.get("filename") == filename),
            "",
        )
        if not expected:
            raise KeyError(filename)
        docs = (self.root / "var" / "workspaces" / "jobs" / job_id / "docs").resolve()
        path = (docs / filename).resolve()
        if (not path.is_relative_to(docs) or not path.is_file() or path.is_symlink()):
            raise KeyError(filename)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise KeyError(filename)
        return path

    def goal_alignment(self, *, job_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        from lca_project.kernel.goal_alignment import GoalAlignmentController
        try:
            status = GoalAlignmentController(self.root, self.control).status(job_id=job_id)
        except OSError:
            status = {"goal_contracts": [], "quality_observations": [], "deviations": [],
                      "repair_plans": [], "change_candidates": [],
                      "failure_triage_runs": [],
                      "system_repair_runs": [],
                      "validation_certificates": [], "promotion_receipts": []}
        if limit:
            for key, value in status.items():
                if isinstance(value, list):
                    status[key] = value[:limit]
        return status

    def audit_goal(self, job_id: str, *, auto_repair: bool = False) -> dict[str, Any]:
        from lca_project.kernel.goal_alignment import GoalAlignmentController
        return GoalAlignmentController(self.root, self.control).audit_job(
            job_id, auto_repair=auto_repair, trigger="dashboard"
        )

    def logic_audit(self, job_id: str) -> dict[str, Any]:
        """Project advisory logic reviews without exposing the frozen dossier.

        The projection deliberately repeats the authority boundary so clients
        cannot mistake a finding for a Gate verdict or repair instruction.
        """
        if self.conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is None:
            raise KeyError(job_id)
        runs = []
        run_rows = self.conn.execute(
            "SELECT audit_run_id,job_id,run_id,stage_id,scope,subject_hash,"
            "policy_version,status,model,last_error,created_at,updated_at,"
            "json_extract(payload,'$.completed_at') AS completed_at,"
            "json_extract(payload,'$.result') AS result_json,"
            "json_extract(payload,'$.authority') AS authority_json "
            "FROM logic_audit_runs WHERE job_id=? ORDER BY created_at DESC LIMIT 200",
            (job_id,),
        )
        for raw in run_rows:
            row = dict(raw)
            runs.append({
                "audit_run_id": row["audit_run_id"],
                "run_id": row.get("run_id"),
                "stage_id": row.get("stage_id"),
                "scope": row.get("scope"),
                "subject_hash": row.get("subject_hash"),
                "policy_version": row.get("policy_version"),
                "status": row.get("status"),
                "model": row.get("model"),
                "last_error": row.get("last_error"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "completed_at": row.get("completed_at"),
                "result": _json(row.get("result_json")),
                "authority": _json(row.get("authority_json")) or {
                    "pipeline_effect": "none",
                    "mutation_authority": "none",
                    "automatic_promotion": False,
                },
            })
        findings = []
        finding_rows = self.conn.execute(
            "SELECT f.*,r.job_id,r.run_id,r.stage_id,r.scope "
            "FROM logic_audit_findings f JOIN logic_audit_runs r "
            "ON r.audit_run_id=f.audit_run_id WHERE r.job_id=? "
            "ORDER BY f.created_at DESC LIMIT 500",
            (job_id,),
        )
        for raw in finding_rows:
            row = dict(raw)
            for name in ("premise_refs", "conclusion_refs", "artifact_refs"):
                row[name] = _json(row.get(name), [])
            payload = _json(row.get("payload"))
            findings.append({
                **{key: row.get(key) for key in (
                    "finding_id", "audit_run_id", "run_id", "stage_id", "scope",
                    "finding_type", "severity", "confidence", "title_zh",
                    "observation_zh", "question_zh", "premise_refs",
                    "conclusion_refs", "artifact_refs", "status",
                    "promoted_deviation_id", "created_at", "updated_at",
                )},
                "source": payload.get("source") or "unknown",
                "authority": {
                    "pipeline_effect": "none",
                    "mutation_authority": "none",
                    "automatic_promotion": False,
                },
            })
        by_severity: dict[str, int] = {}
        for finding in findings:
            severity = str(finding.get("severity") or "unknown")
            by_severity[severity] = by_severity.get(severity, 0) + 1
        return {
            "schema_version": "logic-audit-dashboard-v1",
            "job_id": job_id,
            "authority": {
                "pipeline_effect": "none",
                "mutation_authority": "none",
                "automatic_promotion": False,
                "promotion_requires_explicit_operator_action": True,
            },
            "summary": {
                "runs": len(runs),
                "completed": sum(item["status"] == "completed" for item in runs),
                "queued": sum(item["status"] == "queued" for item in runs),
                "reviewing": sum(item["status"] == "reviewing" for item in runs),
                "failed": sum(item["status"] == "failed" for item in runs),
                "open_findings": sum(item["status"] == "open" for item in findings),
                "promoted_findings": sum(item["status"] == "promoted" for item in findings),
                "by_severity": by_severity,
            },
            "runs": runs,
            "findings": findings,
        }

    def start_logic_audit(self, job_id: str) -> dict[str, Any]:
        """Take immutable snapshots and dispatch advisory review asynchronously."""
        from lca_project.kernel.goal_alignment.work_dispatcher import dispatch_logic_audit
        from lca_project.kernel.logic_audit import LogicAuditAgent

        agent = LogicAuditAgent(self.root, self.control)
        rows = agent.queue_ready_for_job(job_id)
        dispatched = [
            str(row["audit_run_id"])
            for row in rows
            if row.get("status") == "queued"
            and dispatch_logic_audit(self.root, str(row["audit_run_id"]))
        ]
        return {
            "status": "dispatched" if dispatched else "current",
            "queued_or_existing": [str(row["audit_run_id"]) for row in rows],
            "dispatched": dispatched,
            "logic_audit": self.logic_audit(job_id),
        }

    def promote_logic_finding(self, finding_id: str) -> dict[str, Any]:
        """Cross the investigation boundary only after explicit UI confirmation."""
        from lca_project.kernel.logic_audit import LogicAuditAgent
        return LogicAuditAgent(self.root, self.control).promote(
            finding_id, actor="dashboard-operator"
        )

    def goal_feedback(self, job_id: str, message: str, category: str) -> dict[str, Any]:
        from lca_project.kernel.goal_alignment import GoalAlignmentController
        return GoalAlignmentController(self.root, self.control).report_user_feedback(
            job_id, message, category=category
        )

    def autonomy(self, *, campaign_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        from lca_project.kernel.goal_alignment.autonomous_supervisor import AutonomousJobSupervisor
        supervisor = AutonomousJobSupervisor(self.root, control=self.control)
        return supervisor.campaign(campaign_id) if campaign_id else supervisor.campaigns(limit=limit)

    def create_autonomy(self, spec: dict[str, Any], *, start: bool = True) -> dict[str, Any]:
        from lca_project.kernel.goal_alignment.autonomous_supervisor import AutonomousJobSupervisor
        supervisor = AutonomousJobSupervisor(self.root, control=self.control)
        created = supervisor.create_campaign(spec)
        campaign_id = str(created["campaign"]["campaign_id"])
        first_tick = supervisor.tick(campaign_id, execute_task=False)
        started = self.start_autonomy(campaign_id) if start else {"status": "not_started"}
        return {**supervisor.campaign(campaign_id), "first_tick": first_tick,
                "background": started}

    def start_autonomy(self, campaign_id: str) -> dict[str, Any]:
        from lca_project.kernel.goal_alignment.autonomous_supervisor import AutonomousJobSupervisor
        # Resolve before spawning so a bad ID is returned synchronously.
        AutonomousJobSupervisor(self.root, control=self.control).campaign(campaign_id)
        with self._autonomy_lock:
            current = self._autonomy_threads.get(campaign_id)
            if current and current.is_alive():
                return {"status": "already_running", "campaign_id": campaign_id,
                        "supervisor_id": current.name}
            supervisor_id = f"dashboard-autonomy:{campaign_id[-10:]}"
            thread = threading.Thread(
                target=AutonomousJobSupervisor(
                    self.root, supervisor_id=supervisor_id
                ).run,
                args=(campaign_id,), name=supervisor_id, daemon=True,
            )
            self._autonomy_threads[campaign_id] = thread
            thread.start()
        return {"status": "started", "campaign_id": campaign_id,
                "supervisor_id": supervisor_id}

    def reconcile_goal_wakeups_once(self) -> dict[str, Any]:
        """Restart campaign supervisors that have durable, unconsumed goal work."""
        if not self._has_table("goal_supervisor_wakeups"):
            return {"status": "idle", "campaigns": []}
        rows = self.conn.execute(
            "SELECT DISTINCT c.campaign_id FROM goal_supervisor_wakeups w "
            "JOIN autonomous_job_items i ON i.job_id=w.job_id "
            "JOIN autonomous_campaigns c ON c.campaign_id=i.campaign_id "
            "LEFT JOIN autonomous_supervisor_heartbeats h ON h.campaign_id=c.campaign_id "
            "WHERE w.status='pending' AND c.status IN ('running','completed','needs_attention') "
            "AND (c.status!='needs_attention' OR COALESCE(h.last_error,'')='') "
            "ORDER BY c.created_at"
        )
        started: list[dict[str, Any]] = []
        for row in rows:
            started.append(self.start_autonomy(str(row["campaign_id"])))
        return {"status": "started" if started else "idle", "campaigns": started}

    def reconcile_nonterminal_work_once(self) -> dict[str, Any]:
        """Recover durable work independently of wakeup delivery.

        Wakeups are notifications, not the work queue.  On process restart we
        must also resume campaigns and Agent runs that were already claimed
        when the old process disappeared.
        """
        from lca_project.kernel.goal_alignment.execution_ownership import execution_is_fresh
        from lca_project.kernel.goal_alignment.work_dispatcher import (
            dispatch_failure_triage,
            dispatch_logic_audit,
            dispatch_scm_publication,
            dispatch_system_repair,
        )
        from lca_project.kernel.goal_alignment.autonomous_supervisor import (
            _scm_publication_retry_due,
        )

        recovered: dict[str, list[str]] = {
            "campaigns": [], "triage": [], "repairs": [], "scm": [],
            "logic_audits": [],
        }
        if self._has_table("autonomous_campaigns"):
            for row in self.conn.execute(
                "SELECT campaign_id FROM autonomous_campaigns WHERE status='running' "
                "ORDER BY created_at"
            ):
                campaign_id = str(row["campaign_id"])
                result = self.start_autonomy(campaign_id)
                if result["status"] == "started":
                    recovered["campaigns"].append(campaign_id)
        if self._has_table("failure_triage_runs"):
            for row in self.conn.execute(
                "SELECT triage_run_id,status FROM failure_triage_runs "
                "WHERE status IN ('queued','investigating') ORDER BY created_at"
            ):
                triage_run_id = str(row["triage_run_id"])
                if (row["status"] == "queued" or not execution_is_fresh(
                        self.control, "failure-triage", triage_run_id)):
                    if dispatch_failure_triage(self.root, triage_run_id):
                        recovered["triage"].append(triage_run_id)
        if self._has_table("system_repair_runs"):
            for row in self.conn.execute(
                "SELECT r.repair_run_id,r.status,r.updated_at "
                "FROM system_repair_runs r JOIN jobs j ON j.id=r.source_job_id "
                "WHERE j.status NOT IN "
                "('published','failed','superseded','quarantined',"
                "'diagnostic_preview','evidence_limited') "
                "AND (r.status IN "
                "('queued','coding','validating','awaiting_scm_publication') "
                "OR (r.status='failed' AND r.last_error='canary validation failed' "
                "AND json_extract(r.payload,'$.validation_replan') IS NULL "
                "AND json_extract(r.payload,'$.validation_replan_exhausted') IS NULL) "
                "OR (r.status='failed' AND r.last_error="
                "'coding Agent may not edit generated integrity manifests directly' "
                "AND json_extract(r.payload,'$.coding_retry_exhausted') IS NULL)) "
                "AND NOT EXISTS (SELECT 1 FROM system_repair_runs newer "
                "WHERE newer.source_job_id=r.source_job_id "
                "AND newer.created_at>r.created_at) "
                "ORDER BY r.created_at"
            ):
                repair_run_id = str(row["repair_run_id"])
                status = str(row["status"])
                if status == "awaiting_scm_publication":
                    if (_scm_publication_retry_due(str(row["updated_at"]))
                            and dispatch_scm_publication(self.root, repair_run_id)):
                        recovered["scm"].append(repair_run_id)
                elif (status == "queued" or not execution_is_fresh(
                        self.control, "system-repair", repair_run_id)):
                    if dispatch_system_repair(self.root, repair_run_id):
                        recovered["repairs"].append(repair_run_id)
        if self._has_table("logic_audit_runs"):
            for row in self.conn.execute(
                "SELECT audit_run_id,status FROM logic_audit_runs "
                "WHERE status IN ('queued','reviewing') ORDER BY created_at"
            ):
                audit_run_id = str(row["audit_run_id"])
                if (row["status"] == "queued" or not execution_is_fresh(
                        self.control, "logic-audit", audit_run_id)):
                    if dispatch_logic_audit(self.root, audit_run_id):
                        recovered["logic_audits"].append(audit_run_id)
        total = sum(len(value) for value in recovered.values())
        return {"status": "recovered" if total else "idle", **recovered}

    def reconcile_system_meta_once(self) -> dict[str, Any]:
        """Advance the outer repair loop before ordinary Campaign projection."""
        from lca_project.kernel.goal_alignment.meta_supervisor import SystemMetaSupervisor
        return SystemMetaSupervisor(
            self.root, control=self.control, supervisor_id="dashboard-system-meta"
        ).reconcile()

    def start_goal_reconciler(self, *, poll_seconds: float = 2.0) -> None:
        if self._goal_reconciler_thread and self._goal_reconciler_thread.is_alive():
            return
        self._goal_reconciler_stop.clear()

        def loop() -> None:
            from lca_project.kernel.goal_alignment.work_dispatcher import dispatch_system_meta
            while not self._goal_reconciler_stop.is_set():
                try:
                    self.reconcile_nonterminal_work_once()
                    dispatch_system_meta(self.root)
                    self.reconcile_goal_wakeups_once()
                except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error):
                    # Individual campaign/audit events retain the detailed
                    # failure.  The service loop remains alive for later work.
                    pass
                self._goal_reconciler_stop.wait(max(0.1, poll_seconds))

        self._goal_reconciler_thread = threading.Thread(
            target=loop, name="dashboard-goal-reconciler", daemon=True,
        )
        self._goal_reconciler_thread.start()

    def stop_goal_reconciler(self) -> None:
        self._goal_reconciler_stop.set()
        if self._goal_reconciler_thread:
            self._goal_reconciler_thread.join(timeout=3)

    def pause_autonomy(self, campaign_id: str) -> dict[str, Any]:
        from lca_project.kernel.goal_alignment.autonomous_supervisor import AutonomousJobSupervisor
        return AutonomousJobSupervisor(self.root, control=self.control).pause(campaign_id)

    def resume_autonomy(self, campaign_id: str, *, start: bool = True) -> dict[str, Any]:
        from lca_project.kernel.goal_alignment.autonomous_supervisor import AutonomousJobSupervisor
        result = AutonomousJobSupervisor(self.root, control=self.control).resume(campaign_id)
        return {**result, "background": self.start_autonomy(campaign_id) if start else None}

    def workflow_runs(self, *, status: str = "", limit: int = 100) -> dict[str, Any]:
        if not self._has_table("orchestrator_runs"):
            return {"items": [], "total": 0}
        params: list[Any] = []
        where = ""
        if status:
            where, params = "WHERE o.status=?", [status]
        rows = self._rows(
            "SELECT o.*,j.status AS job_status,j.payload AS job_payload,"
            "COUNT(t.task_id) AS task_count,"
            "SUM(CASE WHEN t.status='succeeded' THEN 1 ELSE 0 END) AS succeeded_count,"
            "SUM(CASE WHEN t.status='ready' THEN 1 ELSE 0 END) AS ready_count "
            "FROM orchestrator_runs o JOIN jobs j ON j.id=o.job_id "
            "LEFT JOIN orchestrator_tasks t ON t.run_id=o.run_id "
            f"{where} GROUP BY o.run_id ORDER BY o.updated_at DESC LIMIT ?", (*params, min(max(limit, 1), 200))
        )
        for item in rows:
            payload = item.pop("job_payload", {}) or {}
            item["target"] = payload.get("target", "—")
        return {"items": rows, "total": self._count("orchestrator_runs")}

    def artifacts(self, *, query: str = "", media_type: str = "", limit: int = 60,
                  offset: int = 0) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if query:
            clauses.append("(digest LIKE ? OR metadata LIKE ?)")
            params.extend((f"%{query}%", f"%{query}%"))
        if media_type:
            clauses.append("media_type=?")
            params.append(media_type)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = int(self.conn.execute(f"SELECT COUNT(*) FROM artifacts{where}", tuple(params)).fetchone()[0])
        rows = self._rows(
            f"SELECT * FROM artifacts{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, min(max(limit, 1), 200), max(offset, 0)),
        )
        types = [dict(row) for row in self.conn.execute(
            "SELECT media_type,COUNT(*) AS count FROM artifacts GROUP BY media_type ORDER BY count DESC"
        )]
        return {"items": rows, "total": total, "types": types}

    def artifact(self, digest: str) -> dict[str, Any]:
        artifact = _row(self.conn.execute("SELECT * FROM artifacts WHERE digest=?", (digest,)).fetchone())
        if artifact is None:
            raise KeyError(digest)
        edges = self._rows(
            "SELECT * FROM artifact_edges WHERE parent_digest=? OR child_digest=? ORDER BY created_at",
            (digest, digest),
        )
        preview: Any = None
        preview_type = "none"
        path = Path(str(artifact["uri"]))
        if path.is_file() and path.stat().st_size <= 512_000:
            raw = path.read_bytes()
            if artifact["media_type"] == "application/json":
                preview, preview_type = _json(raw.decode("utf-8", "replace")), "json"
            elif artifact["media_type"].startswith("text/") or artifact["media_type"] in {
                "application/xml", "application/javascript"
            }:
                preview, preview_type = raw.decode("utf-8", "replace"), "text"
        return {"artifact": artifact, "edges": edges, "preview": preview, "preview_type": preview_type}

    def json_artifact(self, digest: str) -> dict[str, Any]:
        """Return one CAS JSON document after content-address verification."""
        artifact = _row(self.conn.execute(
            "SELECT * FROM artifacts WHERE digest=?", (digest,)
        ).fetchone())
        if artifact is None:
            raise KeyError(digest)
        if artifact.get("media_type") != "application/json":
            raise ValueError("artifact is not an application/json document")
        if int(artifact.get("size") or 0) > _JSON_VIEWER_MAX_BYTES:
            raise ValueError(
                f"JSON document is larger than {_JSON_VIEWER_MAX_BYTES} bytes"
            )
        raw = self.control.artifacts.get_bytes(digest)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("artifact is not valid JSON") from exc
        metadata = artifact.get("metadata") or {}
        logical_path = str(metadata.get("logical_path") or "")
        filename = Path(logical_path).name if logical_path else (
            str(metadata.get("schema") or "artifact") + ".json"
        )
        return {
            "schema_version": "dashboard-json-document-v1",
            "source_kind": "immutable_artifact",
            "source_label_zh": "CAS 不可变产物",
            "verification_zh": "内容已重新计算 SHA-256，并与 Artifact Digest 一致。",
            "verified": True,
            "digest": digest,
            "size": len(raw),
            "filename": filename,
            "logical_path": logical_path or None,
            "media_type": artifact.get("media_type"),
            "metadata": metadata,
            "value": value,
        }

    def json_attempt_snapshot(
        self,
        job_id: str,
        task_id: str,
        attempt_id: str,
        relative: str,
    ) -> dict[str, Any]:
        """Return one failed-attempt JSON snapshot after archive-hash verification."""
        snapshot = self._verified_attempt_snapshot(
            job_id, task_id, attempt_id, relative,
        )
        return {
            "schema_version": "dashboard-json-document-v1",
            "source_kind": "attempt_archive_snapshot",
            "source_label_zh": "失败尝试归档快照",
            "verification_zh": "文件已重新计算 SHA-256，并与 Attempt 归档清单一致。",
            "verified": True,
            "digest": snapshot["digest"],
            "size": snapshot["size"],
            "filename": Path(str(snapshot["path"])).name,
            "logical_path": snapshot["path"],
            "media_type": "application/json",
            "metadata": {
                "job_id": job_id,
                "run_id": snapshot["run_id"],
                "task_id": task_id,
                "attempt_id": attempt_id,
                "archive_manifest": snapshot["source"],
            },
            "value": snapshot["value"],
        }

    def events(self, *, query: str = "", event_type: str = "", limit: int = 100,
               after: int = 0) -> dict[str, Any]:
        clauses = ["sequence>?"]
        params: list[Any] = [max(after, 0)]
        if query:
            clauses.append("(aggregate_id LIKE ? OR event_type LIKE ? OR actor LIKE ? OR payload LIKE ?)")
            needle = f"%{query}%"
            params.extend((needle, needle, needle, needle))
        if event_type:
            clauses.append("event_type=?")
            params.append(event_type)
        rows = self._rows(
            "SELECT * FROM events WHERE " + " AND ".join(clauses) +
            " ORDER BY sequence DESC LIMIT ?", (*params, min(max(limit, 1), 500))
        )
        types = [dict(row) for row in self.conn.execute(
            "SELECT event_type,COUNT(*) AS count FROM events GROUP BY event_type ORDER BY count DESC,event_type"
        )]
        return {"items": rows, "total": self._count("events"), "types": types}

    def exceptions(self, *, status: str = "", limit: int = 100) -> dict[str, Any]:
        where, params = (" WHERE status=?", (status,)) if status else ("", ())
        items = self._rows(
            f"SELECT * FROM exceptions{where} ORDER BY opened_at DESC LIMIT ?",
            (*params, min(max(limit, 1), 200)),
        )
        faults = self._rows(
            "SELECT * FROM wiki_runtime_faults ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 200),)
        ) if self._has_table("wiki_runtime_faults") else []
        return {"items": items, "faults": faults, "total": self._count("exceptions") + len(faults)}

    def system(self) -> dict[str, Any]:
        capabilities = []
        try:
            registry = CapabilityRegistry.load_directory(self.root / "capabilities")
            raw_by_id = {}
            for path in (self.root / "capabilities").glob("*.json"):
                raw = load_json(path)
                raw_by_id[str(raw.get("capability_id", raw.get("id", "")))] = raw
            capabilities = [{"id": item.id, "version": item.version,
                             "executor": raw_by_id.get(item.id, {}).get("executor", "unknown"),
                             "side_effects": item.side_effects,
                             "production_ready": raw_by_id.get(item.id, {}).get("production_ready", False),
                             "tags": raw_by_id.get(item.id, {}).get("tags", [])}
                            for item in registry.all()]
        except (OSError, ValueError):
            pass
        skills = []
        try:
            skills = [{"name": item.name, "version": item.version, "workflow": item.workflow_ref,
                       "policy": item.policy_version} for item in SkillRegistry(self.root).all()]
        except (OSError, ValueError):
            pass
        workflows = []
        known = {item["id"] for item in capabilities}
        for path in sorted((self.root / "workflows").glob("*.json")):
            try:
                spec = WorkflowSpec.from_mapping(load_json(path))
                compiled = compile_workflow(spec, known)
                workflows.append({"ref": path.stem, "id": spec.id, "version": spec.version,
                                  "tasks": len(spec.tasks), "status": "valid", "order": list(compiled.order)})
            except (OSError, ValueError) as exc:
                workflows.append({"ref": path.stem, "status": "invalid", "error": str(exc)})
        budgets = self._rows("SELECT * FROM budgets ORDER BY scope") if self._has_table("budgets") else []
        leases = self._rows("SELECT * FROM leases ORDER BY updated_at DESC") if self._has_table("leases") else []
        stages = self._rows(
            "SELECT * FROM stage_executions ORDER BY updated_at DESC LIMIT 50"
        ) if self._has_table("stage_executions") else []
        proofs = self._rows(
            "SELECT proof_id,kind,subject,producer,artifact_digest,created_at FROM proof_receipts "
            "ORDER BY created_at DESC LIMIT 50"
        ) if self._has_table("proof_receipts") else []
        return {"root": str(self.root), "database": str(self.state.path), "capabilities": capabilities,
                "skills": skills, "workflows": workflows, "budgets": budgets, "leases": leases,
                "stages": stages, "proofs": proofs}

    def materialize(self, job_id: str) -> dict[str, Any]:
        orchestrator = PersistentOrchestrator(self.root)
        run_id = orchestrator.materialize(job_id)
        return {"status": "ok", "run_id": run_id,
                "tasks": [item.__dict__ for item in orchestrator.tasks(run_id)]}

    def recover(self, run_id: str, task_id: str) -> dict[str, Any]:
        orchestrator = PersistentOrchestrator(self.root)
        orchestrator.recover(run_id, task_id)
        return {"status": "ok", "run_id": run_id, "task_id": task_id}

    def pause_job(self, job_id: str) -> dict[str, Any]:
        return self.control.pause_job(job_id, reason="Dashboard operator requested pause")

    def resume_job(self, job_id: str) -> dict[str, Any]:
        return self.control.resume_job(job_id, reason="Dashboard operator resumed Job")

    def start_worker(self, job_id: str) -> dict[str, Any]:
        """Start one Job-scoped background loop without blocking the HTTP request."""
        if self.state.get("jobs", job_id) is None:
            raise KeyError(job_id)
        if self.state.get("jobs", job_id)["status"] == "paused":
            raise ValueError("Job is paused; resume it before starting a worker")
        with self._worker_lock:
            current = self._workers.get(job_id)
            if current and current.is_alive():
                return {"status": "already_running", "job_id": job_id,
                        "worker_id": current.name}
            worker_id = f"dashboard:{job_id[-12:]}"
            thread = threading.Thread(
                target=WorkerLoop(self.root, worker_id=worker_id).run,
                kwargs={"job_id": job_id, "poll_seconds": 1.0},
                name=worker_id, daemon=True,
            )
            self._workers[job_id] = thread
            thread.start()
        return {"status": "started", "job_id": job_id, "worker_id": worker_id}

    def _workflow_detail(self, workflow_ref: str) -> dict[str, Any] | None:
        if not workflow_ref:
            return None
        path = self.root / "workflows" / f"{workflow_ref}.json"
        if not path.is_file():
            return None
        raw = load_json(path)
        spec = WorkflowSpec.from_mapping(raw)
        return {"ref": workflow_ref, "description": raw.get("description", ""),
                "policy_version": raw.get("policy_version"), "steps": raw.get("steps", raw.get("tasks", [])),
                "task_count": len(spec.tasks)}
