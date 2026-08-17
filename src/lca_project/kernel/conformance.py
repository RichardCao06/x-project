"""Executable architecture conformance checks.

Compilation is necessary but insufficient.  A workflow is production-ready
only when every referenced capability binds the input/output protocol and an
actual probe can pass through the same sandboxed executor used in production.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lca_project.contracts import load_json
from .executor import ExecutionError, SandboxedExecutor
from .registry import CapabilityRegistry
from .skills import SkillRegistry
from .workflow import WorkflowSpec, compile_workflow


WIKI_ACTIONS = {
    "wiki.batch": {"plan", "prepare", "research_plan", "research_plan_gate", "search_execution_gate",
                   "terminology_verify", "source_diversity_gate", "freeze", "content_blueprint", "draft_content_gate",
                   "content_closure_gate", "table_search_execution_gate", "table_population_gate",
                   "maturity_gate", "preview", "release_gate"},
    "agent.propose": {"nomination", "content_compose", "table_collect"},
    "agent.review": {"verify", "editorial_review", "table_verify"},
    "release.apply": {"draft_apply", "table_apply", "reviewed_apply", "publish"},
}

WIKI_RUNTIME_PROFILES = {
    "nomination": "terra-worker", "content_compose": "terra-worker",
    "table_collect": "terra-worker", "verify": "sol-verifier",
    "editorial_review": "sol-checker", "table_verify": "sol-verifier",
}

GRAPH_ACTIONS = {
    "graph.batch": {"plan", "materialize_reconcile"},
    "graph.gate": {"validate_11"},
    "agent.propose": {"graph_conventions", "graph_seed", "graph_build", "graph_closure",
                      "graph_mapping", "graph_consolidate"},
    "agent.review": {"graph_review", "graph_scorecard"},
    "release.apply": {"graph_publish"},
}

GRAPH_RUNTIME_PROFILES = {
    "graph_conventions": "sol-integrator", "graph_seed": "terra-worker",
    "graph_build": "sol-integrator", "graph_closure": "sol-integrator",
    "graph_mapping": "terra-worker", "graph_review": "sol-checker",
    "graph_consolidate": "sol-integrator", "graph_scorecard": "terra-checker",
}


def check_conformance(root: str | Path, *, workflow_ref: str | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    skills = SkillRegistry(root)
    registry = CapabilityRegistry.load_directory(root / "capabilities")
    paths = [root / "workflows" / f"{workflow_ref}.json"] if workflow_ref else sorted((root / "workflows").glob("*.json"))
    reports: list[dict[str, Any]] = []
    protected = root / "policies"
    executor = SandboxedExecutor(root / "var" / "conformance", protected_roots=(protected,), project_root=root)
    for path in paths:
        if not path.is_file():
            reports.append({"workflow": workflow_ref, "ready": False, "errors": ["workflow not found"]})
            continue
        raw = load_json(path)
        spec = WorkflowSpec.from_mapping(raw)
        compiled = compile_workflow(spec, {item.id for item in registry.all()})
        capabilities = sorted({task.capability for task in spec.tasks})
        probes: list[dict[str, Any]] = []
        errors: list[str] = []
        if spec.id == "wiki-node-production":
            for task in spec.tasks:
                action = task.inputs.get("action")
                if action not in WIKI_ACTIONS.get(task.capability, set()):
                    errors.append(
                        f"{task.id}: missing or invalid executable action for {task.capability}: {action!r}"
                    )
                expected_profile = WIKI_RUNTIME_PROFILES.get(str(action))
                if expected_profile and task.inputs.get("runtime_profile") != expected_profile:
                    errors.append(
                        f"{task.id}: runtime_profile must be {expected_profile!r}, got {task.inputs.get('runtime_profile')!r}"
                    )
        if spec.id == "graph-industry-production":
            for task in spec.tasks:
                action = task.inputs.get("action")
                if action not in GRAPH_ACTIONS.get(task.capability, set()):
                    errors.append(
                        f"{task.id}: missing or invalid executable action for {task.capability}: {action!r}"
                    )
                expected_profile = GRAPH_RUNTIME_PROFILES.get(str(action))
                if expected_profile and task.inputs.get("runtime_profile") != expected_profile:
                    errors.append(
                        f"{task.id}: runtime_profile must be {expected_profile!r}, got {task.inputs.get('runtime_profile')!r}"
                    )
        for capability_id in capabilities:
            capability = registry.get(capability_id)
            if not ({"{input}", "{output}"} <= set(capability.command)):
                errors.append(f"{capability_id}: no Capability.v1 input/output binding")
                probes.append({"capability": capability_id, "status": "not_protocol_ready"})
                continue
            try:
                result = executor.execute(capability, {"operation": "probe"},
                                          run_id="conformance", task_id=capability_id.replace(".", "_"))
                probes.append({"capability": capability_id, "status": result.status,
                               "adapter": result.payload.get("adapter")})
                if result.status != "ok":
                    errors.append(f"{capability_id}: probe returned {result.status}")
            except ExecutionError as exc:
                errors.append(f"{capability_id}: {exc.code}: {exc}")
                probes.append({"capability": capability_id, "status": "failed", "code": exc.code})
        reports.append({"workflow": f"{spec.id}@{spec.version}", "tasks": list(compiled.order),
                        "capabilities": probes, "ready": not errors, "errors": errors})
    return {"status": "pass" if reports and all(item["ready"] for item in reports) else "fail",
            "skills": [skill.name for skill in skills.all()], "workflows": reports}
