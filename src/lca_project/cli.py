"""Unified command-line interface for the local autonomous control plane."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import hashlib
from typing import Any

from .contracts import Job, load_json
from .control import ControlPlane
from .kernel.registry import CapabilityRegistry
from .kernel.workflow import WorkflowSpec, compile_workflow
from .kernel.reconcile import reconcile
from .kernel.skills import SkillInvoker, SkillRegistry
from .kernel.orchestrator import PersistentOrchestrator
from .kernel.conformance import check_conformance


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _dump(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def validate_project(root: Path) -> dict[str, Any]:
    required = ("contracts", "policies", "capabilities", "workflows", "agents", "skills", "vendor")
    missing = [name for name in required if not (root / name).is_dir()]
    if missing:
        raise ValueError(f"missing project directories: {', '.join(missing)}")
    registry = CapabilityRegistry.load_directory(root / "capabilities")
    skills = SkillRegistry(root)
    known = {item.id for item in registry.all()}
    workflows = []
    for path in sorted((root / "workflows").glob("*.json")):
        raw = load_json(path)
        compiled = compile_workflow(WorkflowSpec.from_mapping(raw), known)
        workflows.append({"id": compiled.spec.id, "version": compiled.spec.version, "steps": len(compiled.order)})
    json_files = list((root / "contracts").glob("*.json")) + list((root / "policies").glob("*.json"))
    for path in json_files:
        load_json(path)
    manifest_path = root / "docs" / "migration-manifest.json"
    migrated = 0
    wiki_phase2_assets = 0
    if manifest_path.exists():
        manifest = load_json(manifest_path)
        migrated = len(manifest.get("assets", manifest.get("files", [])))
    phase2_manifest_path = root / "docs" / "wiki-phase2-migration-manifest.json"
    if phase2_manifest_path.exists():
        phase2 = load_json(phase2_manifest_path)
        anchors = phase2.get("anchor_hashes", {})
        if not isinstance(anchors, dict) or not anchors:
            raise ValueError("Wiki Phase 2 migration manifest has no anchor hashes")
        for relative, expected in anchors.items():
            target = root / "vendor" / "lca_cornerstone" / relative
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ValueError(f"Wiki Phase 2 asset hash mismatch: {relative}")
        wiki_phase2_assets = len(anchors)
    return {"status": "pass", "capabilities": len(known), "skills": len(skills.all()), "workflows": workflows,
            "contracts_and_policies": len(json_files), "migrated_assets": migrated,
            "wiki_phase2_anchor_assets": wiki_phase2_assets}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="lca-platform")
    result.add_argument("--root", type=Path, default=PROJECT_ROOT)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("validate")
    commands.add_parser("status")
    start = commands.add_parser("start", help="invoke a machine-readable Skill and create a persistent Job")
    start.add_argument("skill")
    start.add_argument("--request", required=True, type=Path)
    start.add_argument("--idempotency-key")
    materialize = commands.add_parser("materialize", help="materialize one Job into persistent Workflow tasks")
    materialize.add_argument("job_id")
    workflow_status = commands.add_parser("workflow-status")
    workflow_status.add_argument("run_id")
    conformance = commands.add_parser("conformance")
    conformance.add_argument("--workflow", help="workflow ref, for example wiki-node-production@5")
    wiki_sync = commands.add_parser("wiki-sync", help="admit frozen Wiki artifacts into a Workflow run")
    wiki_sync.add_argument("run_id")
    wiki_sync.add_argument("batch_dir", type=Path)
    supervise = commands.add_parser("supervise", help="run one frozen stage plan without external polling")
    supervise.add_argument("plan", type=Path)
    supervise.add_argument("--compactions-observed", type=int, default=0)
    reconcile_cmd = commands.add_parser("reconcile")
    reconcile_cmd.add_argument("--once", action="store_true", help="run one deterministic reconciliation cycle")
    reconcile_cmd.add_argument("--desired", type=Path, help="DesiredState.v1 JSON; omitted uses desired-state.json when present")
    job = commands.add_parser("job")
    job.add_argument("target")
    job.add_argument("--workflow", required=True)
    job.add_argument("--policy", required=True)
    job.add_argument("--input-hash", action="append", required=True)
    job.add_argument("--idempotency-key")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("kind", choices=("job", "artifact", "release"))
    inspect.add_argument("id")
    commands.add_parser("exceptions")
    wiki_rehearse = commands.add_parser("wiki-rehearse")
    wiki_rehearse.add_argument("--workspace", type=Path, help="empty isolated workspace; defaults under var/workspaces")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "validate":
            _dump(validate_project(root)); return 0
        plane = ControlPlane(root)
        if args.command == "init":
            (root / "var" / "workspaces").mkdir(parents=True, exist_ok=True)
            (root / "var" / "releases").mkdir(parents=True, exist_ok=True)
            _dump({"status": "initialized", **plane.status()}); return 0
        if args.command == "status":
            _dump(plane.status()); return 0
        if args.command == "start":
            request = load_json(args.request)
            _dump(SkillInvoker(root).invoke(args.skill, request, idempotency_key=args.idempotency_key)); return 0
        if args.command == "materialize":
            orchestrator = PersistentOrchestrator(root)
            run_id = orchestrator.materialize(args.job_id)
            _dump({"run_id": run_id, "tasks": [asdict(item) for item in orchestrator.tasks(run_id)]}); return 0
        if args.command == "workflow-status":
            orchestrator = PersistentOrchestrator(root)
            _dump({"run_id": args.run_id, "tasks": [asdict(item) for item in orchestrator.tasks(args.run_id)]}); return 0
        if args.command == "conformance":
            report = check_conformance(root, workflow_ref=args.workflow)
            _dump(report); return 0 if report["status"] == "pass" else 2
        if args.command == "wiki-sync":
            from lca_project.domains.wiki_reconcile import reconcile_wiki_run
            _dump(reconcile_wiki_run(root, args.run_id, args.batch_dir)); return 0
        if args.command == "supervise":
            from .kernel.stage_supervisor import StageSupervisor
            report = StageSupervisor(root).run(args.plan, compactions_observed=args.compactions_observed)
            _dump(report); return 0 if report["status"] == "succeeded" else 2
        if args.command == "job":
            job = Job(target=args.target, workflow=args.workflow, scope={"target": args.target},
                      policy_version=args.policy, input_hashes=tuple(args.input_hash))
            job_id, duplicate = plane.submit_job(job, idempotency_key=args.idempotency_key)
            _dump({"job_id": job_id, "deduplicated": duplicate}); return 0
        if args.command == "inspect":
            table = {"job": "jobs", "artifact": "artifacts", "release": "releases"}[args.kind]
            value = plane.state.get(table, args.id)
            if value is None:
                raise KeyError(args.id)
            _dump(value); return 0
        if args.command == "exceptions":
            rows = [dict(row) for row in plane.state._connection().execute("SELECT * FROM exceptions ORDER BY opened_at")]
            _dump({"exceptions": rows}); return 0
        if args.command == "wiki-rehearse":
            from .wiki_runtime.rehearsal import WikiPhase2Rehearsal
            _dump(WikiPhase2Rehearsal(root).run(workspace=args.workspace)); return 0
        if args.command == "reconcile":
            desired_path = args.desired or (root / "desired-state.json" if (root / "desired-state.json").exists() else None)
            desired: list[str] = []
            created: list[str] = []
            if desired_path:
                raw = json.loads(desired_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):  # legacy stable-ID projection
                    if not all(isinstance(item, str) for item in raw):
                        raise ValueError("desired target list must contain strings")
                    desired = raw
                elif isinstance(raw, dict) and raw.get("schema_version") == "desired-state-v1":
                    for item in raw.get("jobs", []):
                        required = {"target", "workflow", "policy_version", "input_hashes"}
                        if not isinstance(item, dict) or required - item.keys():
                            raise ValueError("each desired Job requires target, workflow, policy_version and input_hashes")
                        stable = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
                        job = Job(target=item["target"], workflow=item["workflow"], scope=item.get("scope", {}),
                                  policy_version=item["policy_version"], input_hashes=tuple(item["input_hashes"]),
                                  budget=item.get("budget", {}), risk=item.get("risk", "standard"))
                        job_id, duplicate = plane.submit_job(job, idempotency_key=f"desired:{stable}")
                        desired.append(job_id)
                        if not duplicate:
                            created.append(job_id)
                else:
                    raise ValueError("--desired must be DesiredState.v1 or a JSON array of IDs")
            observed = [row["id"] for row in plane.state._connection().execute("SELECT id FROM jobs")]
            report = reconcile(desired, observed)
            plane.events.append("program", "default", "reconcile.completed", {**asdict(report), "created": created}, actor="reconciler")
            _dump({**asdict(report), "created": created, "ok": report.ok}); return 0
    except (ValueError, KeyError, RuntimeError) as exc:
        _dump({"status": "error", "error": type(exc).__name__, "message": str(exc)})
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
