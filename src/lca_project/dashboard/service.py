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
        events = self._rows(
            "SELECT * FROM events WHERE (aggregate_type='job' AND aggregate_id=?) "
            "OR (aggregate_type='workflow_run' AND aggregate_id=?) ORDER BY sequence DESC LIMIT 100",
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
        execution_trace = self._execution_trace(
            job_id, job=job, run=run, tasks=tasks, attempts=attempts,
            goal_alignment=goal_alignment,
        )
        return {"job": job, "run": run, "tasks": tasks, "attempts": attempts,
                "events": events, "gates": gates, "decisions": decisions,
                "exceptions": exceptions, "artifacts": artifacts, "workflow": workflow,
                "goal_alignment": goal_alignment, "execution_trace": execution_trace,
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

        attempts_by_task: dict[str, list[dict[str, Any]]] = {}
        for attempt in attempts:
            attempts_by_task.setdefault(str(attempt.get("task_id") or ""), []).append(attempt)
        stages = []
        for ordinal, task in enumerate(tasks, 1):
            history = sorted(
                attempts_by_task.get(str(task.get("task_id") or ""), []),
                key=lambda item: str(item.get("started_at") or ""),
            )
            failures = [
                item for item in history
                if item.get("status") in {
                    "failed", "repairable", "retryable", "manual_review",
                    "quarantined", "blocked", "blocked_budget",
                }
            ]
            stages.append({
                "ordinal": ordinal,
                "task_id": task.get("task_id"),
                "capability_id": task.get("capability_id"),
                "status": task.get("status"),
                "attempt_count": len(history) or int(task.get("attempt") or 0),
                "failed_attempts": len(failures),
                "started_at": history[0].get("started_at") if history else None,
                "finished_at": history[-1].get("finished_at") if history else None,
                "updated_at": task.get("updated_at"),
                "failure_code": task.get("failure_code") or (
                    failures[-1].get("failure_code") if failures else None
                ),
                "output_hash": task.get("output_hash"),
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
            query_id = str(query.get("query_id") or query.get("search_hash") or "")
            if not query_id or query_id in claim_search_ids:
                continue
            claim_search_ids.add(query_id)
            candidates = []
            providers: dict[str, int] = {}
            for result in item.get("candidates") or []:
                if not isinstance(result, dict):
                    continue
                provider = str(result.get("search_provider") or "research_scout")
                providers[provider] = providers.get(provider, 0) + 1
                url = str(result.get("url") or "")
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
            searches.append({
                "query_id": query_id,
                "kind": "claim_evidence",
                "field": (item.get("claim") or {}).get("claim_id"),
                "table": None,
                "language": None,
                "strategy": "source_first" if query.get("source_first") else "claim_search",
                "query": query.get("text"),
                "providers": [
                    {"provider": provider, "status": "ok", "results": count, "cache_hit": False}
                    for provider, count in providers.items()
                ],
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

        actions: list[dict[str, Any]] = []
        action_specs = (
            ("triage", goal_alignment.get("failure_triage_runs") or []),
            ("repair_plan", repair_plans),
            ("system_change", change_candidates),
            ("code_repair", goal_alignment.get("system_repair_runs") or []),
        )
        for kind, rows in action_specs:
            for row in rows:
                payload = row.get("payload") or {}
                result = payload.get("result") or {}
                title = (
                    result.get("cause_code") or row.get("action") or row.get("target")
                    or payload.get("action") or row.get("model") or kind
                )
                summary = (
                    result.get("summary") or payload.get("summary") or row.get("last_error")
                    or payload.get("explanation") or payload.get("status") or ""
                )
                actions.append({
                    "kind": kind, "status": row.get("status"), "title": title,
                    "summary": summary, "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "id": row.get("triage_run_id") or row.get("repair_plan_id")
                    or row.get("candidate_id") or row.get("repair_run_id"),
                    "risk": row.get("risk") or result.get("risk"),
                    "details": {
                        key: value for key, value in {
                            "cause_code": result.get("cause_code"),
                            "recovery_task": result.get("recovery_task"),
                            "implementation_targets": result.get("implementation_targets"),
                            "causal_input_changes": result.get("causal_input_changes"),
                            "proof_contract": result.get("proof_contract"),
                            "patch_hash": row.get("patch_hash"),
                            "failure_fingerprint": payload.get("failure_fingerprint")
                            or (payload.get("request") or {}).get("source_failure_fingerprint"),
                            "causal_plan_hash": payload.get("causal_plan_hash"),
                            "outcome_validation": payload.get("outcome_validation"),
                            "scm": payload.get("scm"),
                            "action": row.get("action") or payload.get("action"),
                        }.items() if value not in (None, "", [], {})
                    },
                })
        actions.sort(key=lambda item: str(item.get("created_at") or ""))

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
        return {
            "schema_version": "dashboard-execution-trace-v1",
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
            while not self._goal_reconciler_stop.is_set():
                try:
                    self.reconcile_system_meta_once()
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
