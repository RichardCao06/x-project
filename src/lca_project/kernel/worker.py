"""Persistent worker loop connecting orchestration to capability runtimes.

The orchestrator owns task state.  This module owns only the short-lived act
of leasing one ready task, deriving its versioned execution envelope, invoking
the registered capability, and committing the immutable result or failure.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import asdict as dataclass_asdict
import hashlib
import json
from pathlib import Path
import shutil
import socket
import time
from typing import Any
from urllib.parse import urlsplit
import uuid

from lca_project.contracts import JobState, load_json
from lca_project.domains.wiki_workspace import WikiWorkspaceBuilder
from .executor import ExecutionError, SandboxedExecutor
from .failures import FailureEnvelope, INFRASTRUCTURE_CODES
from .leases import LeaseLost
from .orchestrator import PersistentOrchestrator, TaskRecord
from .repair import RepairAction, RepairDecision, RepairPolicyRegistry
from .workers import LeaseHeartbeat, WorkerRegistry, WorkerWatchdog


class WorkerError(RuntimeError):
    """A task cannot be bound to an executable, auditable envelope."""


@dataclass(frozen=True)
class WorkerCycle:
    status: str
    worker_id: str
    run_id: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    output_hash: str | None = None
    failure_code: str | None = None


class GraphTaskBinding:
    """Build auditable envelopes for the migrated name-graph SOP."""

    SEEDS = ("seed_lca", "seed_products", "seed_activities", "seed_engineering")
    MAPPINGS = ("mapping_products", "mapping_activities", "mapping_technology")
    REVIEWS = ("review_engineer", "review_curator", "review_classifier", "review_adversarial")
    SUPPORTED = frozenset({"plan", "conventions", *SEEDS, "build", "closure", *MAPPINGS,
                           *REVIEWS, "consolidate", "scorecard", "materialize", "gate", "release"})

    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def _request(job: dict[str, Any]) -> dict[str, Any]:
        request = ((job.get("payload") or {}).get("scope") or {}).get("request")
        if not isinstance(request, dict) or not isinstance(request.get("industry"), str):
            raise WorkerError("Graph Job has no frozen Skill request")
        return request

    def context(self, run_id: str, job: dict[str, Any]) -> dict[str, Any]:
        request = self._request(job)
        workspace = self.root / "var/workspaces/jobs" / str(job["id"])
        return {"request": request, "slug": str(request["industry"]), "workspace": workspace,
                "run_id": run_id}

    def _ensure_workspace(self, workspace: Path) -> None:
        sources = {
            "scripts/materialize.py": self.root / "vendor/lca_cornerstone/scripts/materialize.py",
            "scripts/reconcile.py": self.root / "vendor/lca_cornerstone/scripts/reconcile.py",
            "scripts/validate_graph.py": self.root / "vendor/lca_cornerstone/scripts/validate_graph.py",
            "scripts/run_graph_phase_capture.py": self.root / "vendor/lca_cornerstone/scripts/run_graph_phase_capture.py",
            "profiles/graph-production-profile-v1.json": self.root / "skills/industry-graph/production-profile-v1.json",
        }
        manifest = workspace / "workspace-manifest.json"
        if not manifest.is_file():
            workspace.mkdir(parents=True, exist_ok=True)
            records = []
            for logical, source in sources.items():
                target = workspace / logical
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                records.append({"path": logical, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
            manifest.write_text(json.dumps({"protocol": "graph-workspace-v1", "files": records},
                                           ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return
        document = load_json(manifest)
        expected = {row["path"]: row["sha256"] for row in document.get("files", [])}
        if set(expected) != set(sources):
            raise WorkerError("graph workspace manifest tree drift")
        for logical, source in sources.items():
            target = workspace / logical
            digest = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else ""
            if digest != expected[logical] or digest != hashlib.sha256(source.read_bytes()).hexdigest():
                raise WorkerError(f"graph workspace asset drift: {logical}")

    @staticmethod
    def _phase_name(task_id: str) -> str:
        if task_id.startswith("seed_"): return "seed"
        if task_id.startswith("mapping_"): return "mapping"
        if task_id.startswith("review_"): return "review"
        return task_id

    @classmethod
    def _phase_result(cls, workspace: Path, task_id: str) -> Path:
        return workspace / "phases" / task_id / f"{cls._phase_name(task_id)}.json"

    def envelope(self, run_id: str, task: TaskRecord, job: dict[str, Any]) -> dict[str, Any]:
        if task.task_id not in self.SUPPORTED:
            raise WorkerError(f"graph task has no runtime binding: {task.task_id}")
        ctx = self.context(run_id, job); workspace = ctx["workspace"]
        if task.task_id == "plan":
            self._ensure_workspace(workspace)
            return {"operation": "plan", "workspace": str(workspace), "request": ctx["request"],
                    "profile": str(workspace / "profiles/graph-production-profile-v1.json"),
                    "output": str(workspace / "plan.json")}
        if not (workspace / "plan.json").is_file():
            raise WorkerError("graph plan is missing")
        dependencies = {"conventions": [],
                        **{name: ["conventions"] for name in self.SEEDS},
                        "build": ["conventions", *self.SEEDS],
                        "closure": ["conventions", "build"],
                        **{name: ["conventions", "closure"] for name in (*self.MAPPINGS, *self.REVIEWS)},
                        "consolidate": ["conventions", "closure", *self.MAPPINGS, *self.REVIEWS],
                        "scorecard": ["conventions", "consolidate"]}
        if task.task_id in dependencies:
            phase = self._phase_name(task.task_id)
            reason_task = task.task_id in {"conventions", "build", "closure", "consolidate"} or task.task_id in self.REVIEWS
            return {"phase": f"graph_{phase}", "scope": task.inputs.get("scope"),
                    "workspace": str(workspace),
                    "plan": str(workspace / "plan.json"),
                    "inputs": [str(self._phase_result(workspace, name))
                               for name in dependencies[task.task_id]],
                    "output_dir": str(workspace / "phases" / task.task_id),
                    "model": "gpt-5.6-sol" if reason_task else "gpt-5.6-terra",
                    "reasoning_effort": "high" if reason_task else "medium"}
        candidate = workspace / f"staging/{ctx['slug']}-name-graph.json"
        if task.task_id == "materialize":
            return {"operation": "materialize_reconcile", "workspace": str(workspace),
                    "plan": str(workspace / "plan.json"),
                    "conventions": str(self._phase_result(workspace, "conventions")),
                    "final_graph": str(self._phase_result(workspace, "consolidate")),
                    "mappings": [str(self._phase_result(workspace, name)) for name in self.MAPPINGS],
                    "scorecard": str(self._phase_result(workspace, "scorecard")),
                    "journal": str(workspace / "journal.jsonl"), "candidate": str(candidate)}
        if task.task_id == "gate":
            return {"operation": "validate_11", "workspace": str(workspace),
                    "candidate": str(candidate), "report": str(workspace / "graph-gate-report.json")}
        return {"operation": "graph_publish", "workspace": str(workspace),
                "candidate": str(candidate), "gate_report": str(workspace / "graph-gate-report.json"),
                "destination": str(self.root / f"var/publications/graphs/{ctx['slug']}-name-graph.json"),
                "record": str(workspace / "release-record.json")}

    def evidence(self, run_id: str, task: TaskRecord, job: dict[str, Any]) -> dict[str, Any]:
        ctx = self.context(run_id, job); workspace = ctx["workspace"]
        if task.task_id == "plan": names = ("plan.json", "workspace-manifest.json")
        elif task.task_id in {"conventions", *self.SEEDS, "build", "closure", *self.MAPPINGS,
                             *self.REVIEWS, "consolidate", "scorecard"}:
            names = tuple(f"phases/{task.task_id}/{name}" for name in
                          (f"{self._phase_name(task.task_id)}.json", "invocation.json", "events.jsonl", "usage.json"))
        elif task.task_id == "materialize":
            names = ("journal.jsonl", f"staging/{ctx['slug']}-name-graph.json")
        elif task.task_id == "gate": names = ("graph-gate-report.json",)
        else: names = ("release-record.json",)
        artifacts = [{"path": name, "size": (workspace / name).stat().st_size}
                     for name in names if (workspace / name).is_file()]
        if not artifacts:
            raise WorkerError(f"{task.task_id} produced no durable graph artifacts")
        return {"binding": "graph-industry-production@1", "action": task.task_id,
                "industry": ctx["slug"], "workspace": str(workspace), "artifacts": artifacts}


class WikiTaskBinding:
    """Build execution envelopes for versioned Wiki production tasks."""

    SUPPORTED = frozenset({"plan", "prepare", "research_plan", "research_plan_gate", "research_ready",
                           "search_execution_gate", "verify", "terminology_verify",
                           "source_diversity_gate", "freeze",
                           "content_blueprint", "content_compose", "content_closure_gate", "editorial_review",
                           "draft_content_gate", "draft_apply", "table_collect",
                           "table_search_execution_gate", "table_verify",
                           "table_population_gate", "table_apply", "maturity_gate", "preview",
                           "release_gate", "reviewed_apply", "publish"})

    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def _request(job: dict[str, Any]) -> dict[str, Any]:
        request = ((job.get("payload") or {}).get("scope") or {}).get("request")
        if not isinstance(request, dict):
            raise WorkerError("Wiki Job has no frozen Skill request")
        return request

    def _industry_slug(self, value: Any) -> str:
        requested = str(value or "").strip()
        fixture = self.root / "vendor/lca_cornerstone/fixtures/wiki-phase2"
        if (fixture / "wiki" / requested).is_dir():
            return requested
        matches: list[str] = []
        for graph in sorted((fixture / "docs").glob("*-name-graph.json")):
            document = load_json(graph)
            meta = document.get("_meta") or {}
            title = " ".join(str(meta.get(key, "")) for key in ("industry", "title"))
            if requested and requested in title:
                matches.append(graph.name.removesuffix("-name-graph.json"))
        if len(matches) != 1:
            raise WorkerError(f"cannot resolve industry {requested!r} to one frozen slug")
        return matches[0]

    def context(self, run_id: str, job: dict[str, Any]) -> dict[str, Any]:
        request = self._request(job)
        slug = self._industry_slug(request.get("industry"))
        nodes = request.get("nodes")
        if not isinstance(nodes, list) or len(nodes) != 1:
            raise WorkerError("Wiki worker requires exactly one node")
        node = str(nodes[0])
        batch_id = str(request.get("batch_id") or f"{node.lower()}-{run_id.removeprefix('run_')[:12]}")
        workspace = self.root / "var/workspaces/jobs" / str(job["id"])
        batch = workspace / "runs/wiki-batches" / slug / batch_id
        return {"request": request, "slug": slug, "node": node, "batch_id": batch_id,
                "workspace": workspace, "batch": batch}

    @staticmethod
    def _domains(value: Any) -> set[str]:
        result: set[str] = set()
        if isinstance(value, dict):
            for item in value.values():
                result.update(WikiTaskBinding._domains(item))
        elif isinstance(value, list):
            for item in value:
                result.update(WikiTaskBinding._domains(item))
        elif isinstance(value, str):
            for part in value.replace("；", " ").split():
                if part.startswith(("http://", "https://")):
                    hostname = (urlsplit(part).hostname or "").lower()
                    if hostname:
                        result.add(hostname)
        return result

    def allowed_domains(self, workspace: Path, slug: str) -> list[str]:
        domains: set[str] = set()
        for path in (workspace / f"sources/{slug}/registry.json",
                     workspace / "registry/lca_source_catalog.json"):
            if path.is_file():
                domains.update(self._domains(load_json(path)))
        if not domains:
            raise WorkerError(f"no frozen source domains available for {slug}")
        return sorted(domains)

    def envelope(self, run_id: str, task: TaskRecord, job: dict[str, Any]) -> dict[str, Any]:
        if task.task_id not in self.SUPPORTED:
            raise WorkerError(
                f"wiki task {task.task_id!r} has no complete runtime binding; refusing synthetic success"
            )
        ctx = self.context(run_id, job)
        workspace, batch = ctx["workspace"], ctx["batch"]
        if task.task_id == "plan":
            if not workspace.exists():
                workspace.parent.mkdir(parents=True, exist_ok=True)
                WikiWorkspaceBuilder().build(workspace)
            else:
                WikiWorkspaceBuilder().verify(workspace)
            return {"operation": "plan", "workspace": str(workspace), "argv": [
                ctx["slug"], "--nodes", ctx["node"], "--batch-id", ctx["batch_id"],
                "--output", str(batch),
            ]}
        if not (batch / "manifest.json").is_file():
            raise WorkerError("plan output manifest is missing")
        if task.task_id == "prepare":
            return {"operation": "prepare", "workspace": str(workspace),
                    "argv": [str(batch / "manifest.json")]}
        if task.task_id == "research_plan":
            hints = self.root / "skills/generate-node-wiki/source-hints" / f"{ctx['node']}.json"
            return {"operation":"research-plan", "workspace":str(workspace),
                    "workflow":str(batch / "nomination.workflow.run.js"),
                    "output":str(batch / "research-plan.json"),
                    "source_hints":str(hints) if hints.is_file() else None,
                    "registry":str(workspace / f"sources/{ctx['slug']}/registry.json")}
        if task.task_id == "research_plan_gate":
            return {"operation": "research-plan-gate", "workspace": str(workspace),
                    "plan": str(batch / "research-plan.json"),
                    "output": str(batch / "research-plan-gate.json")}
        if not (batch / "prepared.json").is_file():
            raise WorkerError("prepare output is missing")
        if task.task_id == "verify":
            if not (batch / "research-ready.json").is_file():
                raise WorkerError("research-ready gate output is missing")
            return {"phase": "verify_pipeline", "workspace": str(workspace), "batch": str(batch)}
        if task.task_id == "search_execution_gate":
            return {"operation":"search-execution-gate", "workspace":str(workspace),
                    "evidence":str(batch / "source-evidence.json"),
                    "output":str(batch / "search-execution-gate.json"),
                    "allow_partial":ctx["request"].get("publication_mode")=="preview"}
        if task.task_id == "source_diversity_gate":
            return {"operation":"source-diversity-gate", "workspace":str(workspace),
                    "verified":str(batch / "verify-output.json"),
                    "plan":str(batch / "research-plan.json"),
                    "output":str(batch / "source-diversity-gate.json"),
                    "reviewed":ctx["request"].get("publication_mode")=="reviewed",
                    "attempt": task.attempt, "repair_budget": 2}
        if task.task_id == "terminology_verify":
            return {"operation":"terminology-verify", "workspace":str(workspace),
                    "plan":str(batch / "research-plan.json"),
                    "verified":str(batch / "verify-output.json"),
                    "output":str(batch / "terminology-verdict.json")}
        if task.task_id == "freeze":
            return {"operation": "finalize", "workspace": str(workspace),
                    "argv": [str(batch / "prepared.json"), "--allow-partial", "--resume"]}
        graph = workspace / f"docs/{ctx['slug']}-name-graph.json"
        if task.task_id == "content_blueprint":
            return {"operation": "content-blueprint", "workspace": str(workspace),
                    "argv": [str(graph), ctx["node"], str(batch / "content-blueprint.json")]}
        if task.task_id == "content_compose":
            usage_path = batch / "content-runtime/content-usage.json"
            content_path = batch / "content-runtime/content-result.json"
            editorial_feedback_path = batch / "editorial-loop/editorial-review.json"
            editorial_repair_marker = batch / "content-runtime/frozen-editorial-repair.json"
            editorial_feedback_pending = False
            if editorial_feedback_path.is_file():
                try:
                    marker = load_json(editorial_repair_marker) if editorial_repair_marker.is_file() else {}
                    current_review_hash = hashlib.sha256(editorial_feedback_path.read_bytes()).hexdigest()
                    editorial_feedback_pending = (
                        load_json(editorial_feedback_path).get("verdict") == "NO_GO"
                        and marker.get("review_sha256") != current_review_hash
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    editorial_feedback_pending = False
            content_result_available = False
            deterministic_repair_available = False
            if content_path.is_file():
                try:
                    content_doc = load_json(content_path)
                    content_result_available = bool(content_doc.get("sections"))
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    content_result_available = False
            if usage_path.is_file():
                try:
                    validation_error = str(load_json(usage_path).get("validation_error") or "")
                    deterministic_repair_available = any(marker in validation_error for marker in (
                        "九节标题或顺序漂移", "单段外部事实锚点过多",
                        "claim 类型映射漂移", "overused=",
                    ))
                except (TypeError, ValueError, json.JSONDecodeError):
                    deterministic_repair_available = False
            if editorial_feedback_pending and content_result_available:
                return {"phase": "editorial_patch", "workspace": str(workspace),
                        "batch": str(batch)}
            # Once one model draft exists, send structural-only defects to the
            # bounded deterministic normalizer before spending a second model
            # attempt.  This also keeps the repeated-failure fuse from moving a
            # locally repairable draft to manual review before normalization.
            if (task.attempt >= 1 and content_result_available
                    and deterministic_repair_available):
                return {"phase": "content_normalize", "workspace": str(workspace),
                        "batch": str(batch)}
            argv = [
                str(batch / "verify-output.json"), str(batch / "content-blueprint.json"),
                str(workspace / "schemas/wiki-content-draft.schema.json"),
                str(batch / "content-runtime"), "--cost-usd", "0"]
            if editorial_feedback_pending:
                combined_feedback = batch / "content-runtime/combined-editorial-feedback.json"
                combined_feedback.write_text(json.dumps({
                    "protocol": "wiki-content-combined-repair-feedback-v1",
                    "editorial_review": load_json(editorial_feedback_path),
                    "required_repairs": [
                        "逐项保留并完成独立编辑审查提出的修复，不得恢复编号堆叠、重复段落或引用侵入。",
                        "在不新增未核实外部事实、不重复既有句子的前提下修复审查指出的内容问题。",
                        "需要补充时应围绕对象、记录字段、判定理由、适用条件和失效条件展开，不能用空话填充。",
                    ],
                }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                argv.extend(["--editorial-feedback", str(combined_feedback)])
            elif usage_path.is_file():
                usage = load_json(usage_path)
                error = str(usage.get("validation_error") or "")
                if error:
                    feedback = batch / "content-runtime/repair-feedback.json"
                    feedback.write_text(json.dumps({
                        "protocol": "wiki-content-repair-feedback-v1",
                        "failure_code": "DETERMINISTIC_CONTENT_GATE",
                        "validation_error": error,
                        "required_repairs": [
                            "在不重复、不填充空话且不增加未核实外部事实的前提下修复确定性门禁问题。",
                            "需要补充时，逐节围绕目标对象、理由、记录字段、适用条件与失效条件展开。",
                            "若 validation_error 列出 overused claim，每个该 claim 只能保留至多三次 evidence_claim_ids 映射；不要删除正文，也不要改写冻结研究 claim。",
                        ],
                    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    argv.extend(["--editorial-feedback", str(feedback)])
            return {"phase": "content", "workspace": str(workspace), "argv": argv}
        if task.task_id == "editorial_review":
            content_path = batch / "content-runtime/content-result.json"
            review_path = batch / "editorial-loop/editorial-review.json"
            policy_path = batch / "editorial-loop/editorial-policy-decision.json"
            if content_path.is_file() and review_path.is_file() and policy_path.is_file():
                try:
                    policy = load_json(policy_path)
                    reusable = (
                        policy.get("decision") in {"accept", "accept_with_advisories"}
                        and policy.get("content_sha256") == hashlib.sha256(content_path.read_bytes()).hexdigest()
                        and policy.get("review_sha256") == hashlib.sha256(review_path.read_bytes()).hexdigest()
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    reusable = False
                if reusable:
                    return {"phase": "editorial_policy_reuse", "workspace": str(workspace),
                            "batch": str(batch)}
            return {"phase": "editorial_review", "workspace": str(workspace),
                    "publication_mode": ctx["request"].get("publication_mode", "preview"), "argv": [
                str(batch / "verify-output.json"), str(batch / "content-runtime/content-result.json"),
                str(batch / "content-blueprint.json"),
                str(workspace / "schemas/wiki-editorial-review.schema.json"),
                str(batch / "editorial-loop"), "--cost-usd", "0"]}
        if task.task_id == "content_closure_gate":
            return {"operation": "content-closure-gate", "workspace": str(workspace),
                    "batch": str(batch)}
        if task.task_id == "draft_content_gate":
            return {"operation": "draft-content-pipeline", "workspace": str(workspace),
                    "batch": str(batch),
                    "publication_mode": ctx["request"].get("publication_mode", "preview")}
        if task.task_id == "draft_apply":
            return {"operation": "draft_apply", "workspace": str(workspace),
                    "batch": str(batch)}
        if task.task_id == "table_collect":
            hints = self.root / "skills/generate-node-wiki/source-hints" / f"{ctx['node']}.json"
            return {"phase": "table_collect", "workspace": str(workspace), "batch": str(batch),
                    "source_hints": str(hints) if hints.is_file() else None,
                    "research_plan":str(batch / "research-plan.json") if (batch / "research-plan.json").is_file() else None}
        if task.task_id == "table_verify":
            return {"phase": "table_verify", "workspace": str(workspace), "batch": str(batch)}
        if task.task_id == "table_search_execution_gate":
            return {"operation":"table-search-execution-gate", "workspace":str(workspace),
                    "matrix":str(batch / "table-data/search-matrix.executed.json"),
                    "output":str(batch / "table-data/search-execution-gate.json"),
                    "allow_partial":ctx["request"].get("publication_mode")=="preview"}
        page_matches = sorted((workspace / f"wiki/{ctx['slug']}").glob(
            f"{'products' if ctx['node'].startswith('P') else 'activities'}/{ctx['node']}--*.md"
        ))
        if task.task_id in {"table_population_gate", "table_apply"}:
            if len(page_matches) != 1:
                raise WorkerError(f"expected one Wiki page for {ctx['node']}, found {len(page_matches)}")
            registry = workspace / f"sources/{ctx['slug']}/registry.json"
            if not registry.is_file():
                raise WorkerError("Wiki source registry is missing")
            return {"operation": task.task_id, "workspace": str(workspace), "batch": str(batch),
                    "page": str(page_matches[0]), "registry": str(registry)}
        if task.task_id == "preview":
            return {"operation": "node-preview", "workspace": str(workspace),
                    "argv": [ctx["slug"], str(ctx["request"].get("industry")), ctx["node"],
                             str(batch / "preview-report.json")]}
        if task.task_id == "maturity_gate":
            return {"operation": "maturity-gate", "workspace": str(workspace),
                    "batch": str(batch)}
        if task.task_id == "release_gate":
            return {"operation": "release-gate", "workspace": str(workspace),
                    "batch": str(batch)}
        if task.task_id == "reviewed_apply":
            return {"operation": "reviewed_apply", "workspace": str(workspace),
                    "batch": str(batch)}
        if task.task_id == "publish":
            return {"operation": "wiki_publish_candidate", "workspace": str(workspace),
                    "batch": str(batch)}
        prepared = load_json(batch / "prepared.json")
        workflows = prepared.get("workflows") or []
        nomination = next((item for item in workflows if item.get("mode") == "nomination"), None)
        if not isinstance(nomination, dict):
            raise WorkerError("prepared batch has no nomination workflow")
        workflow = workspace / str(nomination.get("path", ""))
        output_dir = batch / "nomination-runtime"
        return {"phase": "research_ready", "workspace": str(workspace), "batch": str(batch),
                "allowed_domains": self.allowed_domains(workspace, ctx["slug"]),
                "open_discovery": (batch / "research-plan.json").is_file(),
                "research_plan": str(batch / "research-plan.json")
                if (batch / "research-plan.json").is_file() else None,
                "source_hints": str(self.root / "skills/generate-node-wiki/source-hints"
                                    / f"{ctx['node']}.json")
                if (self.root / "skills/generate-node-wiki/source-hints"
                    / f"{ctx['node']}.json").is_file() else None}

    def evidence(self, run_id: str, task: TaskRecord, job: dict[str, Any]) -> dict[str, Any]:
        ctx = self.context(run_id, job)
        batch = ctx["batch"]
        names = {
            "plan": ("manifest.json", "journal.json"),
            "prepare": ("prepared.json", "journal.json", "nomination.workflow.run.js"),
            "research_plan": ("research-plan.json",),
            "research_plan_gate": ("research-plan-gate.json",),
            "research_ready": (
                "nomination-runtime/nomination-result.json",
                "nomination-runtime/nomination-invocation.json",
                "nomination-runtime/nomination-events.jsonl",
                "nomination-runtime/wiki-usage-v1.json",
                "source-queue.json", "source-evidence.json", "verify-only.workflow.run.js",
                "research-ready.json",
            ),
            "search_execution_gate": ("search-execution-gate.json",),
            "verify": (
                "verify-runtime/verify-verdicts.runtime.json",
                "verify-runtime/verify-invocation.json",
                "verify-runtime/verify-events.jsonl",
                "verify-runtime/wiki-usage-v1.json",
                "verify-output.json", "verified.json",
            ),
            "source_diversity_gate": ("source-diversity-gate.json",),
            "terminology_verify": ("terminology-verdict.json",),
            "freeze": ("frozen.json",),
            "content_blueprint": ("content-blueprint.json",),
            "content_compose": ("content-runtime/content-result.json",
                                "content-runtime/wiki-usage-v1.json"),
            "content_closure_gate": ("content-closure-gate.json",),
            "editorial_review": ("editorial-loop/editorial-review.json",
                                 "editorial-loop/editorial-policy-decision.json",
                                 "editorial-loop/editorial-advisories.json"),
            "draft_content_gate": ("content-enriched.json", "draft-content-gate.json"),
            "draft_apply": ("content-apply-report.json",),
            "table_collect": ("table-data/search-matrix.json", "table-data/search-matrix.executed.json",
                              "table-data/search-execution-manifest.json", "table-data/collection.json",
                              "table-data/evidence-selection.json"),
            "table_search_execution_gate": ("table-data/search-execution-gate.json",),
            "table_verify": ("table-data/source-verdict.json",),
            "table_population_gate": ("table-data/table-stage/stage-report.json",
                                      "table-data/table-population-gate.json"),
            "table_apply": ("table-data/table-apply-report.json",),
            "maturity_gate": ("maturity-gate.json",),
            "preview": ("preview-report.json",),
            "release_gate": (
                "coverage.json", "go-no-go.json", "quality-gate.json", "gate-report.json",
            ),
            "reviewed_apply": ("reviewed-apply-report.json",),
            "publish": ("publish-report.json", "release-record.json"),
        }[task.task_id]
        artifacts = []
        for name in names:
            path = batch / name
            if path.is_file():
                artifacts.append({"path": str(path.relative_to(ctx["workspace"])),
                                  "size": path.stat().st_size})
        if task.task_id in {"draft_apply", "table_apply"}:
            page_matches = sorted((ctx["workspace"] / f"wiki/{ctx['slug']}").glob(
                f"{'products' if ctx['node'].startswith('P') else 'activities'}/"
                f"{ctx['node']}--*.md"
            ))
            registry = ctx["workspace"] / f"sources/{ctx['slug']}/registry.json"
            if len(page_matches) != 1 or not registry.is_file():
                raise WorkerError(
                    f"{task.task_id} cannot bind its materialized page/registry outputs"
                )
            for path in (*page_matches, registry):
                artifacts.append({
                    "path": str(path.relative_to(ctx["workspace"])),
                    "size": path.stat().st_size,
                    "role": "materialized_output",
                })
        if not artifacts:
            raise WorkerError(f"{task.task_id} produced no durable protocol artifacts")
        # The frozen Job owns the workflow binding.  WikiTaskBinding is a
        # stateless adapter and deliberately has no orchestrator dependency.
        binding = str(job.get("workflow_id") or "wiki-node-production@9")
        return {"binding": binding, "action": task.task_id,
                "industry": ctx["slug"], "node": ctx["node"],
                "workspace": str(ctx["workspace"]), "artifacts": artifacts}


class WorkerLoop:
    """Lease and execute ready orchestrator tasks, one task per cycle."""

    def __init__(self, root: str | Path, *, worker_id: str | None = None,
                 lease_seconds: int = 30, heartbeat_seconds: float = 5.0) -> None:
        self.root = Path(root).resolve()
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:10]}"
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = min(max(0.1, heartbeat_seconds), max(0.1, lease_seconds / 3))
        self.orchestrator = PersistentOrchestrator(self.root)
        self.control = self.orchestrator.control
        self.wiki = WikiTaskBinding(self.root)
        self.graph = GraphTaskBinding(self.root)
        self.workers = WorkerRegistry(self.control.state)
        self.workers.register(self.worker_id)
        self.watchdog = WorkerWatchdog(self.control.state, self.control.events)
        self.repair_policy = RepairPolicyRegistry(
            self.root / "policies/wiki-repair-policy-v1.json"
        )

    def _audit_goal_alignment(self, job_id: str, *, trigger: str) -> None:
        """Keep supervision observable without making it a task failure source."""
        try:
            from .goal_alignment import GoalAlignmentController
            GoalAlignmentController(self.root, self.control).audit_job(
                job_id, auto_repair=False, trigger=trigger
            )
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            self.control.events.append("job", job_id, "goal_alignment.audit_failed", {
                "trigger": trigger, "error": type(exc).__name__, "message": str(exc),
            }, actor="worker")

    def _reconcile_goal_supervision(self, job_id: str) -> None:
        """Let a live Worker recover supervision abandoned by a dead campaign loop."""
        try:
            pending = self.control.state._connection().execute(
                "SELECT 1 FROM goal_supervisor_wakeups WHERE job_id=? AND status='pending' "
                "LIMIT 1", (job_id,),
            ).fetchone()
            if not pending:
                return
            rows = self.control.state._connection().execute(
                "SELECT DISTINCT c.campaign_id FROM autonomous_campaigns c "
                "JOIN autonomous_job_items i ON i.campaign_id=c.campaign_id "
                "WHERE i.job_id=? AND c.status!='paused'", (job_id,),
            )
            from .goal_alignment.autonomous_supervisor import AutonomousJobSupervisor
            for row in rows:
                try:
                    AutonomousJobSupervisor(
                        self.root, supervisor_id=f"{self.worker_id}:goal-wakeup",
                    ).tick(str(row["campaign_id"]), execute_task=False)
                except LeaseLost:
                    # An active Supervisor owns the campaign and will consume
                    # the same durable wakeup on its next cycle.
                    continue
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            self.control.events.append(
                "job", job_id, "goal_alignment.reconcile_failed",
                {"error": type(exc).__name__, "message": str(exc)}, actor="worker",
            )

    def _run_rows(self, run_id: str | None, job_id: str | None) -> list[dict[str, Any]]:
        # A run-level manual review freezes the failing branch, not every
        # independent ready branch.  Orchestrator readiness still enforces all
        # task dependencies, so this cannot bypass a failed prerequisite.
        clauses, params = ["o.status IN ('ready','manual_review')", "j.status!='paused'"], []
        if run_id:
            clauses.append("o.run_id=?"); params.append(run_id)
        if job_id:
            clauses.append("o.job_id=?"); params.append(job_id)
        return [dict(row) for row in self.control.state._connection().execute(
            "SELECT o.*,j.status AS job_status,j.payload AS job_payload FROM orchestrator_runs o "
            "JOIN jobs j ON j.id=o.job_id WHERE " + " AND ".join(clauses) + " ORDER BY o.created_at",
            tuple(params),
        )]

    def _start_job(self, job_id: str, token: int) -> None:
        self.control.governance.admit_execution(job_id)
        row = self.control.state.get("jobs", job_id)
        if row is None:
            raise KeyError(job_id)
        state = JobState(row["status"])
        if state == JobState.STALLED:
            self.control.transition_job(job_id, JobState.LEASED, reason="replacement worker lease acquired",
                                        fencing_token=token)
            self.control.transition_job(job_id, JobState.RUNNING, reason="requeued task execution started",
                                        fencing_token=token)
        elif state == JobState.READY:
            self.control.transition_job(job_id, JobState.LEASED, reason="worker lease acquired",
                                        fencing_token=token)
            self.control.transition_job(job_id, JobState.RUNNING, reason="first task execution started",
                                        fencing_token=token)

    def _capability_manifest(self, capability_id: str) -> Path:
        for path in sorted((self.root / "capabilities").glob("*.json")):
            raw = load_json(path)
            if raw.get("capability_id", raw.get("id")) == capability_id:
                return path
        raise WorkerError(f"capability manifest disappeared: {capability_id}")

    def _executor_for(self, run: dict[str, Any], job: dict[str, Any],
                      capability_id: str, workspace: Path, skill: str) -> SandboxedExecutor:
        """Protect this task's frozen bindings without racing unrelated Jobs."""
        payload = job.get("payload") or {}
        protected = [
            self._capability_manifest(capability_id),
            self.root / "workflows" / f"{run['workflow_ref']}.json",
            self.root / "policies" / f"{payload['policy_version']}.json",
            workspace / "workspace-manifest.json",
        ]
        if skill == "industry-graph":
            protected.append(
                self.root / "vendor/lca_cornerstone/profiles/graph-production-profile-v1.json"
            )
        else:
            protected.extend([
                self.root / "policies/wiki-repair-policy-v1.json",
                self.root / "vendor/lca_cornerstone/profiles/wiki-node-production-profile-v1.json",
                self.root / "skills/generate-node-wiki/SKILL.md",
            ])
        return SandboxedExecutor(
            self.root / "var/scratch",
            protected_roots=tuple(path for path in protected if path.is_file()),
            project_root=self.root,
        )

    @staticmethod
    def _attempt_snapshot(root: Path) -> dict[str, dict[str, Any]]:
        if not root.is_dir():
            return {}
        snapshot: dict[str, dict[str, Any]] = {}
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if relative.startswith("runs/attempts/") or "/attempts/" in relative:
                continue
            content = path.read_bytes()
            snapshot[relative] = {"sha256": hashlib.sha256(content).hexdigest(),
                                  "size": len(content)}
        return snapshot

    def _archive_attempt(self, *, workspace: Path, execution_root: Path,
                         run_id: str, task_id: str, attempt_id: str,
                         before: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Freeze every file changed by one attempt under an append-only directory."""
        after = self._attempt_snapshot(execution_root)
        changed = sorted(path for path in set(before) | set(after)
                         if before.get(path) != after.get(path))
        entries = []
        for relative in changed:
            current = execution_root / relative
            if not current.is_file():
                entries.append({"path": relative, "change": "deleted",
                                "before": before.get(relative)})
                continue
            frozen = self.control.artifacts.put_file(current, metadata={
                "schema": "attempt-output-file-v1", "run_id": run_id,
                "task_id": task_id, "attempt_id": attempt_id,
                "logical_path": relative,
            })
            entries.append({"path": relative,
                            "change": "created" if relative not in before else "modified",
                            "sha256": frozen.digest, "size": frozen.size,
                            "before": before.get(relative)})
        archive_dir = workspace / "runs/attempts" / task_id / attempt_id
        archive_dir.mkdir(parents=True, exist_ok=False)
        manifest_value = {
            "protocol": "task-attempt-archive-v1", "run_id": run_id,
            "task_id": task_id, "attempt_id": attempt_id,
            "execution_root": str(execution_root), "files": entries,
        }
        manifest_path = archive_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest_value, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        frozen_manifest = self.control.artifacts.put_file(manifest_path, metadata={
            "schema": "task-attempt-archive-v1", "run_id": run_id,
            "task_id": task_id, "attempt_id": attempt_id,
        })
        for entry in entries:
            if entry.get("sha256"):
                self.control.artifacts.link(str(entry["sha256"]), frozen_manifest.digest,
                                            "attempt_output_file")
        return {"path": str(manifest_path), "sha256": frozen_manifest.digest,
                "changed_files": len(entries)}

    @staticmethod
    def _failure_fingerprint(task_id: str, code: str, envelope: dict[str, Any]) -> str:
        signal = {
            "task_id": task_id, "code": code,
            "category": envelope.get("category"), "scope": envelope.get("scope"),
            "message": str(envelope.get("message") or "").strip(),
        }
        return hashlib.sha256(json.dumps(
            signal, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()

    def _previous_failure_fingerprint(self, run_id: str, task_id: str) -> str | None:
        row = self.control.state._connection().execute(
            "SELECT failure_payload FROM orchestrator_attempts "
            "WHERE run_id=? AND task_id=? AND failure_payload IS NOT NULL "
            "ORDER BY attempt DESC LIMIT 1", (run_id, task_id),
        ).fetchone()
        if row is None:
            return None
        try:
            return str(json.loads(row["failure_payload"]).get("failure_fingerprint") or "") or None
        except (TypeError, json.JSONDecodeError):
            return None

    def run_once(self, *, run_id: str | None = None, job_id: str | None = None) -> WorkerCycle:
        for run in self._run_rows(run_id, job_id):
            ready = self.orchestrator.ready(str(run["run_id"]))
            if not ready:
                continue
            task = ready[0]
            reused = self.orchestrator.try_reuse(task.run_id, task.task_id)
            if reused is not None:
                attempt_id, output_hash, receipt_hash = reused
                self.control.events.append("workflow_run", task.run_id, "task.reused", {
                    "task_id": task.task_id, "attempt_id": attempt_id,
                    "output_hash": output_hash, "reuse_receipt_hash": receipt_hash,
                }, actor="worker")
                return WorkerCycle("succeeded", self.worker_id, task.run_id, task.task_id,
                                   attempt_id, output_hash)
            resource = f"workflow-task:{task.run_id}:{task.task_id}"
            try:
                lease = self.control.leases.acquire(resource, self.worker_id, self.lease_seconds)
            except LeaseLost:
                continue
            attempt_id: str | None = None
            heartbeat: LeaseHeartbeat | None = None
            attempt_archive: dict[str, Any] | None = None
            archive_before: dict[str, dict[str, Any]] | None = None
            archive_workspace: Path | None = None
            archive_root: Path | None = None
            job_id_value = str(run["job_id"])
            try:
                job = self.control.state.get("jobs", job_id_value)
                if job is None:
                    raise WorkerError("workflow Job disappeared")
                self.workers.heartbeat(
                    self.worker_id, status="running", job_id=job_id_value,
                    run_id=task.run_id, task_id=task.task_id, progress=True,
                )
                heartbeat = LeaseHeartbeat(
                    self.control.leases, self.workers, lease, self.worker_id,
                    self.lease_seconds, self.heartbeat_seconds,
                ).start()
                self._start_job(job_id_value, lease.fencing_token)
                attempt_id, input_hashes = self.orchestrator.claim(
                    task.run_id, task.task_id, worker_id=self.worker_id,
                    lease_resource=resource, fencing_token=lease.fencing_token,
                )
                skill = str((((job.get("payload") or {}).get("scope") or {}).get("skill") or ""))
                binding = self.graph if skill == "industry-graph" else self.wiki
                binding_context = binding.context(task.run_id, job)
                request = binding_context["request"]
                archive_workspace = Path(binding_context["workspace"])
                archive_root = Path(binding_context.get("batch") or archive_workspace)
                archive_before = self._attempt_snapshot(archive_root)
                envelope = binding.envelope(task.run_id, task, job)
                self.orchestrator.refresh_attempt_binding(attempt_id)
                self.control.events.append("workflow_run", task.run_id, "task.claimed", {
                    "task_id": task.task_id, "attempt_id": attempt_id, "worker_id": self.worker_id,
                    "fencing_token": lease.fencing_token, "input_hashes": list(input_hashes),
                }, actor="worker")
                capability = self.orchestrator.registry.get(task.capability_id)
                task_executor = self._executor_for(
                    run, job, task.capability_id,
                    Path(binding.context(task.run_id, job)["workspace"]), skill,
                )
                result = task_executor.execute(
                    capability, envelope, run_id=task.run_id, task_id=task.task_id
                )
                release_payload: dict[str, Any] = {}
                if result.status == "ok" and skill != "industry-graph" and task.task_id == "publish":
                    release_payload = self.control.governance.publish_wiki_release(
                        job_id=job_id_value,
                        workspace=binding_context["workspace"],
                        batch=binding_context["batch"],
                        industry=str(binding_context["slug"]),
                        node=str(binding_context["node"]),
                        risk="low",
                    )
                attempt_archive = self._archive_attempt(
                    workspace=archive_workspace, execution_root=archive_root,
                    run_id=task.run_id, task_id=task.task_id,
                    attempt_id=attempt_id, before=archive_before,
                )
                active_lease = heartbeat.current()
                if result.status != "ok":
                    failure = result.payload.get("failure") or {}
                    try:
                        envelope = FailureEnvelope.from_capability(failure)
                    except ValueError as invalid:
                        raise ExecutionError(
                            "OUTPUT_PROTOCOL", str(invalid), stdout=result.stdout,
                            stderr=result.stderr,
                            failure=FailureEnvelope.infrastructure(
                                "OUTPUT_PROTOCOL", str(invalid)
                            ).asdict(),
                        ) from invalid
                    raise ExecutionError(
                        envelope.code, envelope.message, stdout=result.stdout,
                        stderr=result.stderr, failure=envelope.asdict(),
                    )
                evidence = binding.evidence(task.run_id, task, job)
                payload = {**result.payload, **release_payload, **evidence,
                           "capability": task.capability_id, "attempt_id": attempt_id,
                           "attempt_archive": attempt_archive}
                workspace = Path(evidence["workspace"])
                lineage_files = {
                    "capability_manifest": self._capability_manifest(task.capability_id),
                    "workflow_binding": self.root / "workflows" / f"{run['workflow_ref']}.json",
                    "production_policy": self.root / "policies" / f"{job['payload']['policy_version']}.json",
                    "workspace_manifest": workspace / "workspace-manifest.json",
                }
                if skill == "industry-graph":
                    lineage_files["production_profile"] = workspace / "profiles/graph-production-profile-v1.json"
                else:
                    lineage_files.update({
                        "repair_policy": self.root / "policies/wiki-repair-policy-v1.json",
                        "node_profile": workspace / "profiles/wiki-node-production-profile-v1.json",
                    })
                lineage_files["attempt_archive"] = Path(attempt_archive["path"])
                manifest = self.control.artifacts.put_task_output_manifest(
                    evidence["workspace"], evidence["artifacts"], payload,
                    run_id=task.run_id, task_id=task.task_id, attempt_id=attempt_id,
                    input_hashes=input_hashes,
                    lineage_files=lineage_files,
                )
                digest = self.orchestrator.complete(
                    attempt_id, payload, output_manifest_hash=manifest.digest,
                    worker_id=self.worker_id, lease_resource=resource,
                    fencing_token=active_lease.fencing_token,
                )
                if skill == "industry-graph":
                    current = self.control.state.get("jobs", job_id_value)
                    state = JobState(current["status"]) if current else None
                    if task.task_id == "materialize" and state == JobState.RUNNING:
                        self.control.transition_job(job_id_value, JobState.CANDIDATE,
                                                    reason="graph candidate materialized and reconciled")
                    elif task.task_id == "gate" and state == JobState.CANDIDATE:
                        self.control.transition_job(job_id_value, JobState.GATED,
                                                    reason="candidate-bound graph gate passed 11/11")
                    elif task.task_id == "release" and state == JobState.GATED:
                        self.control.transition_job(job_id_value, JobState.APPLIED,
                                                    reason="hash-locked graph apply completed")
                        self.control.transition_job(job_id_value, JobState.PUBLISHED,
                                                    reason="graph release record persisted")
                if skill != "industry-graph":
                    current = self.control.state.get("jobs", job_id_value)
                    state = JobState(current["status"]) if current else None
                    if task.task_id == "release_gate" and state == JobState.RUNNING:
                        self.control.transition_job(
                            job_id_value, JobState.CANDIDATE,
                            reason="reviewed Wiki candidate passed the release gate",
                        )
                        self.control.transition_job(
                            job_id_value, JobState.GATED,
                            reason="candidate-bound G10 release proofs passed",
                        )
                    elif task.task_id == "reviewed_apply" and state == JobState.GATED:
                        self.control.transition_job(
                            job_id_value, JobState.APPLIED,
                            reason="reviewed Wiki frontmatter applied in the frozen workspace",
                        )
                    elif task.task_id == "publish" and state == JobState.APPLIED:
                        self.control.transition_job(
                            job_id_value, JobState.PUBLISHED,
                            reason="governed hash-locked Wiki release applied",
                        )
                if skill != "industry-graph" and task.task_id == "preview" and request.get("publication_mode") == "preview":
                    reason = "preview_unpublished branch does not grant release or publication authority"
                    for skipped in ("release_gate", "reviewed_apply", "publish"):
                        self.orchestrator.skip(task.run_id, skipped, reason)
                    current = self.control.state.get("jobs", job_id_value)
                    if current and JobState(current["status"]) == JobState.RUNNING:
                        maturity_path = self.wiki.context(task.run_id, job)["batch"] / "maturity-gate.json"
                        maturity = load_json(maturity_path) if maturity_path.is_file() else {}
                        maturity_name = str(maturity.get("maturity") or "diagnostic_preview")
                        target = (JobState.CANDIDATE if maturity.get("candidate_eligible") is True
                                  else JobState.REPAIRABLE
                                  if maturity.get("pipeline_continue") is True
                                  else JobState.EVIDENCE_LIMITED
                                  if maturity_name == "evidence_limited"
                                  else JobState.DIAGNOSTIC_PREVIEW)
                        self.control.transition_job(
                            job_id_value, target,
                            reason=(f"preview artifact completed with maturity={maturity_name}; "
                                    f"goal_complete={maturity.get('candidate_eligible') is True}; "
                                    f"pipeline_continue={maturity.get('pipeline_continue') is True}"),
                        )
                self.control.events.append("workflow_run", task.run_id, "task.succeeded", {
                    "task_id": task.task_id, "attempt_id": attempt_id, "output_hash": digest,
                    "worker_id": self.worker_id,
                }, actor="worker")
                self._audit_goal_alignment(
                    job_id_value, trigger=f"task_succeeded:{task.task_id}"
                )
                return WorkerCycle("succeeded", self.worker_id, task.run_id, task.task_id,
                                   attempt_id, digest)
            except (ExecutionError, WorkerError, OSError, ValueError, RuntimeError) as exc:
                code = (exc.code if isinstance(exc, ExecutionError) else
                        getattr(exc, "code", type(exc).__name__.upper()))
                if isinstance(exc, ExecutionError) and exc.failure:
                    envelope = dict(exc.failure)
                elif code in INFRASTRUCTURE_CODES:
                    envelope = FailureEnvelope.infrastructure(code, str(exc)).asdict()
                elif isinstance(exc, OSError):
                    code = "TEMPORARY_IO"
                    envelope = FailureEnvelope.infrastructure(code, str(exc)).asdict()
                else:
                    envelope = FailureEnvelope(
                        code, "worker_runtime", f"task:{task.task_id}", str(exc)
                    ).asdict()
                if (attempt_id is not None and attempt_archive is None
                        and archive_workspace is not None and archive_root is not None
                        and archive_before is not None):
                    try:
                        attempt_archive = self._archive_attempt(
                            workspace=archive_workspace, execution_root=archive_root,
                            run_id=task.run_id, task_id=task.task_id,
                            attempt_id=attempt_id, before=archive_before,
                        )
                    except (OSError, ValueError, RuntimeError) as archive_error:
                        attempt_archive = {"error": str(archive_error)}
                fingerprint = self._failure_fingerprint(task.task_id, code, envelope)
                repeated = self._previous_failure_fingerprint(task.run_id, task.task_id) == fingerprint
                epoch_attempt = self.orchestrator.repair_epoch_attempt(
                    task.run_id, task.task_id, task.attempt
                )
                decision = self.repair_policy.decide(
                    code, attempt=epoch_attempt, max_attempts=100, actor="worker"
                )
                configured_rule = self.repair_policy.rules.get(code) or {}
                configured_action = RepairAction(str(configured_rule.get("action", "quarantine")))
                if repeated and code != "SOURCE_DIVERSITY_BLOCKED" and configured_action in {
                    RepairAction.RETRY, RepairAction.RECOVER, RepairAction.REPAIR,
                }:
                    decision = RepairDecision(
                        RepairAction.MANUAL_REVIEW,
                        "identical failure fingerprint repeated; blind retry stopped",
                        policy_version=decision.policy_version,
                        policy_hash=decision.policy_hash,
                    )
                detail = {**envelope, "protocol": "failure-envelope-v1",
                          "worker_id": self.worker_id, "run_id": task.run_id,
                          "task_id": task.task_id,
                          "failure_fingerprint": fingerprint,
                          "identical_failure_repeated": repeated,
                          "attempt_archive": attempt_archive,
                          "policy_decision": dataclass_asdict(decision)}
                if isinstance(exc, ExecutionError):
                    detail.update({"diagnostics": {
                        "stdout_tail": exc.stdout[-8000:], "stderr_tail": exc.stderr[-8000:]
                    }})
                active_lease = None
                try:
                    active_lease = heartbeat.current() if heartbeat else lease
                except LeaseLost:
                    code = "WORKER_LOST"
                if attempt_id is None and code != "WORKER_LOST":
                    attempt_id, _ = self.orchestrator.claim(
                        task.run_id, task.task_id, worker_id=self.worker_id,
                        lease_resource=resource, fencing_token=active_lease.fencing_token,
                    )
                repairable = decision.action in {
                    RepairAction.RETRY, RepairAction.RECOVER, RepairAction.REPAIR,
                    RepairAction.MANUAL_REVIEW,
                }
                rewound = False
                if attempt_id is not None and code != "WORKER_LOST" and active_lease is not None:
                    self.orchestrator.fail(
                        attempt_id, code, detail, repairable=repairable,
                        worker_id=self.worker_id, lease_resource=resource,
                        fencing_token=active_lease.fencing_token,
                        status_override=("manual_review"
                                         if decision.action == RepairAction.MANUAL_REVIEW else None),
                    )
                    current = self.control.state.get("jobs", job_id_value)
                    if current and JobState(current["status"]) in {
                        JobState.RUNNING, JobState.CANDIDATE, JobState.GATED, JobState.APPLIED,
                    }:
                        current_state = JobState(current["status"])
                        target_state = (JobState.MANUAL_REVIEW
                                        if decision.action == RepairAction.MANUAL_REVIEW
                                        and current_state == JobState.RUNNING
                                        else JobState.REPAIRABLE if repairable else JobState.FAILED)
                        self.control.transition_job(
                            job_id_value, target_state,
                            reason=f"task {task.task_id} failed: {code}"
                        )
                    # Observe the persisted failed shape before a bounded
                    # rewind clears it.  This is what lets the supervisor learn
                    # from auto-recovered false blocks instead of hiding them.
                    self._audit_goal_alignment(
                        job_id_value, trigger=f"task_failed:{task.task_id}:{code}"
                    )
                    if decision.action in {RepairAction.RETRY, RepairAction.RECOVER}:
                        self.orchestrator.recover(task.run_id, task.task_id)
                    elif (code in {"CONTENT_LOCAL_ISSUES", "EDITORIAL_LOCAL_ISSUES"}
                          and decision.action == RepairAction.REPAIR):
                        self.orchestrator.rewind_from(
                            task.run_id, "content_compose",
                            reason=("editorial review requested a bounded content repair"
                                    if code == "EDITORIAL_LOCAL_ISSUES"
                                    else "deterministic content validation requested a bounded repair"),
                            actor="worker",
                        )
                        rewound = True
                    elif (code == "SOURCE_DIVERSITY_BLOCKED"
                          and decision.action == RepairAction.REPAIR):
                        self.orchestrator.rewind_from(
                            task.run_id, "research_ready",
                            reason="source diversity repair selected alternate frozen candidates",
                            actor="worker",
                        )
                        rewound = True
                    elif (code == "RESEARCH_PLAN_INVALID"
                          and decision.action == RepairAction.REPAIR):
                        self.orchestrator.rewind_from(
                            task.run_id, "research_plan",
                            reason="research plan gate requested audited bilingual terminology repair",
                            actor="worker",
                        )
                        rewound = True
                self.workers.heartbeat(
                    self.worker_id, status="degraded", job_id=job_id_value,
                    run_id=task.run_id, task_id=task.task_id, last_error=str(exc),
                )
                event_type = "task.ownership_lost" if code == "WORKER_LOST" else "task.failed"
                self.control.events.append("workflow_run", task.run_id, event_type, {
                    "task_id": task.task_id, "attempt_id": attempt_id, "failure_code": code,
                    "worker_id": self.worker_id, "repair_action": str(decision.action),
                    "repair_policy_hash": decision.policy_hash,
                }, actor="worker")
                cycle_status = ("retry_scheduled" if rewound or decision.action in {
                    RepairAction.RETRY, RepairAction.RECOVER
                } and code != "WORKER_LOST" else "failed")
                return WorkerCycle(cycle_status, self.worker_id, task.run_id, task.task_id,
                                   attempt_id, failure_code=code)
            finally:
                release_lease = heartbeat.stop() if heartbeat else lease
                self.control.leases.release(release_lease)
                self.workers.idle(self.worker_id)
        return WorkerCycle("idle", self.worker_id)

    def run(self, *, run_id: str | None = None, job_id: str | None = None,
            once: bool = False, poll_seconds: float = 2.0) -> WorkerCycle:
        try:
            while True:
                if job_id:
                    job = self.control.state.get("jobs", job_id)
                    if job and JobState(job["status"]) == JobState.PAUSED:
                        return WorkerCycle("paused", self.worker_id)
                self.watchdog.sweep(stale_after_seconds=max(
                    self.heartbeat_seconds * 3, self.lease_seconds * 1.5
                ))
                cycle = self.run_once(run_id=run_id, job_id=job_id)
                cycle_job_id = job_id
                if not cycle_job_id and cycle.run_id:
                    row = self.control.state._connection().execute(
                        "SELECT job_id FROM orchestrator_runs WHERE run_id=?", (cycle.run_id,),
                    ).fetchone()
                    cycle_job_id = str(row["job_id"]) if row else None
                if cycle_job_id:
                    self._reconcile_goal_supervision(cycle_job_id)
                if once or cycle.status == "failed":
                    return cycle
                if cycle.status == "idle":
                    self.workers.heartbeat(self.worker_id, status="idle")
                    time.sleep(max(0.1, poll_seconds))
        finally:
            self.workers.stop(self.worker_id)


def repair_failed_attempt(root: str | Path, run_id: str, task_id: str, *,
                          repair_plan: str | Path | None = None) -> str:
    """Rebind a task after its executable envelope changes, invalidating descendants."""
    orchestrator = PersistentOrchestrator(root)
    row = orchestrator.control.state._connection().execute(
        "SELECT status,output_hash,failure_code FROM orchestrator_tasks WHERE run_id=? AND task_id=?", (run_id, task_id)
    ).fetchone()
    if row is None or row["status"] not in {"quarantined", "succeeded", "repairable", "manual_review"}:
        raise WorkerError(f"task cannot be rebound from status: {task_id}")
    if repair_plan is None:
        raise WorkerError("worker repair requires a persisted wiki-repair-plan-v1")
    plan_path = Path(repair_plan).resolve(); plan = load_json(plan_path)
    required = {"protocol","run_id","task_id","failure_hash","failure_class","repair_action",
                "affected_requirements","allowed_scope","forbidden_scope","created_by","reviewed_by",
                "expires_after_run"}
    if (required - plan.keys() or plan.get("protocol") != "wiki-repair-plan-v1"
            or plan.get("run_id") != run_id or plan.get("task_id") != task_id
            or plan.get("failure_hash") != row["output_hash"] or plan.get("expires_after_run") is not True
            or not str(plan.get("reviewed_by", "")).strip()
            or plan.get("reviewed_by") == plan.get("created_by")):
        raise WorkerError("repair plan does not bind the current failed task and failure hash")
    plan_artifact = orchestrator.control.artifacts.put_json(plan, metadata={"schema":"wiki-repair-plan-v1"})
    repair_policy = RepairPolicyRegistry(Path(root) / "policies/wiki-repair-policy-v1.json")
    decision = repair_policy.decide(str(row["failure_code"]), attempt=0, max_attempts=100,
                                    actor="worker")
    dry_run = orchestrator.repair_dry_run(
        run_id, task_id, policy_invalidates=decision.invalidates,
        policy_preserves=decision.preserves,
    )
    requested_scope = set(map(str, plan.get("allowed_scope", [])))
    if requested_scope and not set(dry_run["will_invalidate"]) <= requested_scope:
        raise WorkerError("repair invalidation exceeds the plan allowed_scope")
    dry_run_artifact = orchestrator.control.artifacts.put_json(
        {**dry_run, "repair_plan_hash": plan_artifact.digest,
         "repair_policy_hash": decision.policy_hash},
        metadata={"schema": "wiki-repair-dry-run-v1"},
    )
    rows = list(orchestrator.control.state._connection().execute(
        "SELECT task_id,dependencies FROM orchestrator_tasks WHERE run_id=?", (run_id,)
    ))
    descendants: set[str] = set()
    changed = True
    while changed:
        changed = False
        for item in rows:
            dependencies = set(json.loads(item["dependencies"]))
            if item["task_id"] != task_id and dependencies & ({task_id} | descendants) \
                    and item["task_id"] not in descendants:
                descendants.add(str(item["task_id"])); changed = True
    invalidated = set(dry_run["will_invalidate"])
    for binding_task in sorted(invalidated):
        orchestrator.create_binding_generation(
            run_id, binding_task, reason=f"repair plan {plan_artifact.digest}"
        )
    with orchestrator.control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_tasks SET status='repairable',failure_code='BINDING_UPDATED' "
                     "WHERE run_id=? AND task_id=?",
                     (run_id, task_id))
        for descendant in descendants & invalidated:
            conn.execute("UPDATE orchestrator_tasks SET status='pending',output_hash=NULL,"
                         "failure_code=NULL WHERE run_id=? AND task_id=?", (run_id, descendant))
        conn.execute("UPDATE orchestrator_runs SET status='repairable' WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM leases WHERE resource=?", (f"workflow-task:{run_id}:{task_id}",))
    run = orchestrator.control.state._connection().execute(
        "SELECT job_id FROM orchestrator_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if run is None:
        raise WorkerError(f"run not found: {run_id}")
    job = orchestrator.control.state.get("jobs", str(run["job_id"]))
    if job and JobState(job["status"]) in {JobState.FAILED, JobState.MANUAL_REVIEW}:
        orchestrator.control.transition_job(str(run["job_id"]), JobState.REPAIRABLE,
                                            reason=f"operator repaired binding for {task_id}")
    with orchestrator.control.state.transaction() as conn:
        conn.execute("UPDATE orchestrator_tasks SET status='ready' WHERE run_id=? AND task_id=?",
                     (run_id, task_id))
        conn.execute("UPDATE orchestrator_runs SET status='ready' WHERE run_id=?", (run_id,))
    orchestrator.control.events.append("workflow_run", run_id, "task.binding_updated", {
        "task_id": task_id, "invalidated_descendants": sorted(descendants & invalidated),
        "repair_plan_hash": plan_artifact.digest, "repair_dry_run_hash": dry_run_artifact.digest,
    }, actor="operator")
    current = orchestrator.control.state.get("jobs", str(run["job_id"]))
    if current and JobState(current["status"]) == JobState.REPAIRABLE:
        orchestrator.control.transition_job(str(run["job_id"]), JobState.READY,
                                            reason=f"retry scheduled for {task_id}")
    receipt = orchestrator.control.artifacts.put_json({"protocol":"wiki-repair-receipt-v1",
        "run_id":run_id,"task_id":task_id,"failure_hash":row["output_hash"],
        "repair_plan_hash":plan_artifact.digest,"repair_dry_run_hash":dry_run_artifact.digest,
        "repair_policy_hash":decision.policy_hash,"status":"scheduled","created_by":plan["created_by"]},
        metadata={"schema":"wiki-repair-receipt-v1"})
    return receipt.digest
