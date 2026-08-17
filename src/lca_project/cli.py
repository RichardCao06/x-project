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
    dashboard = commands.add_parser("dashboard", help="serve the local observability and control dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--allow-remote", action="store_true", help="allow binding beyond the local machine")
    worker = commands.add_parser("worker", help="execute ready Workflow tasks through registered runtimes")
    worker.add_argument("--run-id")
    worker.add_argument("--job-id")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--poll-seconds", type=float, default=2.0)
    worker.add_argument("--worker-id")
    worker.add_argument("--lease-seconds", type=int, default=30)
    worker.add_argument("--heartbeat-seconds", type=float, default=5.0)
    worker_daemon = commands.add_parser(
        "worker-daemon", help="continuously execute tasks with fenced lease heartbeats")
    worker_daemon.add_argument("--run-id")
    worker_daemon.add_argument("--job-id")
    worker_daemon.add_argument("--poll-seconds", type=float, default=2.0)
    worker_daemon.add_argument("--worker-id")
    worker_daemon.add_argument("--lease-seconds", type=int, default=30)
    worker_daemon.add_argument("--heartbeat-seconds", type=float, default=5.0)
    watchdog = commands.add_parser(
        "worker-watchdog", help="requeue attempts whose worker or fenced lease was lost")
    watchdog.add_argument("--stale-seconds", type=float, default=30.0)
    baseline = commands.add_parser(
        "optimization-baseline", help="freeze an immutable optimization baseline artifact")
    baseline.add_argument("--job-id")
    verify_optimization = commands.add_parser(
        "verify-optimization", help="calculate executable reliability and audit metrics")
    verify_optimization.add_argument("--job-id")
    diagnose = commands.add_parser("diagnose-job", help="return one structured Job diagnosis")
    diagnose.add_argument("job_id")
    repair_job = commands.add_parser("repair-job", help="validate and schedule a bounded Job repair")
    repair_job.add_argument("job_id")
    repair_job.add_argument("--repair-plan", type=Path, required=True)
    worker_repair = commands.add_parser("worker-repair", help="reopen a failed task after its binding is fixed")
    worker_repair.add_argument("run_id")
    worker_repair.add_argument("task_id")
    worker_repair.add_argument("--repair-plan", type=Path, required=True)
    table_repair = commands.add_parser(
        "repair-skipped-tables", help="reopen the historical preview-skipped Wiki table branch")
    table_repair.add_argument("run_id")
    wiki_rehearse = commands.add_parser("wiki-rehearse")
    wiki_rehearse.add_argument("--workspace", type=Path, help="empty isolated workspace; defaults under var/workspaces")
    goal_audit = commands.add_parser("goal-audit", help="detect goal deviation and schedule bounded L0/L1 repair")
    goal_audit.add_argument("job_id")
    goal_audit.add_argument("--auto-repair", action="store_true")
    goal_status = commands.add_parser("goal-status", help="inspect the persisted self-healing audit chain")
    goal_status.add_argument("--job-id")
    goal_feedback = commands.add_parser("goal-feedback", help="record a user-discovered metric escape")
    goal_feedback.add_argument("job_id")
    goal_feedback.add_argument("message")
    goal_feedback.add_argument("--category", default="user_feedback")
    change = commands.add_parser("system-change", help="govern sandbox/shadow/canary policy changes")
    change.add_argument("action", choices=("status", "certify", "promote", "rollback"))
    change.add_argument("candidate_id")
    change.add_argument("--phase", choices=("sandbox", "shadow", "canary", "post_promotion"))
    change.add_argument("--suites", type=Path, help="JSON object of suite-name to pass/fail")
    change.add_argument("--operator", action="store_true")
    change.add_argument("--reason", default="operator requested rollback")
    system_repair = commands.add_parser(
        "system-repair", help="inspect or execute a governed coding-Agent repair")
    system_repair.add_argument("action", choices=("status", "execute", "approve", "reject"))
    system_repair.add_argument("repair_run_id", nargs="?")
    system_repair.add_argument("--job-id")
    system_repair.add_argument("--reason", default="operator rejected validated repair")
    failure_triage = commands.add_parser(
        "failure-triage", help="inspect or execute a read-only unknown-failure investigation")
    failure_triage.add_argument("action", choices=("status", "execute"))
    failure_triage.add_argument("triage_run_id", nargs="?")
    failure_triage.add_argument("--job-id")
    meta_supervisor = commands.add_parser(
        "meta-supervisor", help="audit and advance the outer control-plane repair loop")
    meta_supervisor.add_argument("--job-id")
    meta_supervisor.add_argument("--audit-only", action="store_true")
    meta_supervisor.add_argument(
        "--approve", metavar="META_REPAIR_ID",
        help="approve the validated operator branch of one compound repair graph",
    )
    autonomy_create = commands.add_parser("autonomy-create", help="register a finite autonomous Job campaign")
    autonomy_create.add_argument("spec", type=Path)
    autonomy_create.add_argument("--run", action="store_true", help="run until completion or attention is required")
    autonomy_run = commands.add_parser("autonomy-supervisor", help="advance an autonomous campaign")
    autonomy_run.add_argument("campaign_id")
    autonomy_run.add_argument("--once", action="store_true")
    autonomy_run.add_argument("--create-only", action="store_true")
    autonomy_status = commands.add_parser("autonomy-status")
    autonomy_status.add_argument("campaign_id", nargs="?")
    autonomy_pause = commands.add_parser("autonomy-pause")
    autonomy_pause.add_argument("campaign_id")
    autonomy_resume = commands.add_parser("autonomy-resume")
    autonomy_resume.add_argument("campaign_id")
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
        if args.command == "dashboard":
            if not 1 <= args.port <= 65535:
                raise ValueError("dashboard port must be 1..65535")
            if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_remote:
                raise ValueError("remote dashboard binding requires --allow-remote")
            from .dashboard.server import serve
            serve(root, host=args.host, port=args.port); return 0
        if args.command in {"worker", "worker-daemon"}:
            if args.poll_seconds <= 0:
                raise ValueError("worker poll interval must be positive")
            if args.lease_seconds <= 0 or args.heartbeat_seconds <= 0:
                raise ValueError("worker lease and heartbeat intervals must be positive")
            from .kernel.worker import WorkerLoop
            result = WorkerLoop(
                root, worker_id=args.worker_id, lease_seconds=args.lease_seconds,
                heartbeat_seconds=args.heartbeat_seconds,
            ).run(
                run_id=args.run_id, job_id=args.job_id,
                once=(args.once if args.command == "worker" else False),
                poll_seconds=args.poll_seconds,
            )
            _dump(asdict(result)); return 0 if result.status != "failed" else 2
        if args.command == "worker-watchdog":
            from .kernel.workers import WorkerWatchdog
            orchestrator = PersistentOrchestrator(root)
            report = WorkerWatchdog(
                orchestrator.control.state, orchestrator.control.events
            ).sweep(stale_after_seconds=args.stale_seconds)
            _dump(asdict(report)); return 0
        if args.command in {"optimization-baseline", "verify-optimization"}:
            from .kernel.verification import OptimizationVerifier
            verifier = OptimizationVerifier(root)
            report, digest = (verifier.freeze_baseline(job_id=args.job_id)
                              if args.command == "optimization-baseline"
                              else verifier.verify(job_id=args.job_id))
            _dump({**report, "artifact_hash": digest})
            return 0 if report.get("status", "pass") == "pass" else 2
        if args.command == "diagnose-job":
            from .kernel.verification import OptimizationVerifier
            _dump(OptimizationVerifier(root).diagnose_job(args.job_id)); return 0
        if args.command == "repair-job":
            from .kernel.worker import repair_failed_attempt
            orchestrator = PersistentOrchestrator(root)
            run = orchestrator.control.state._connection().execute(
                "SELECT run_id FROM orchestrator_runs WHERE job_id=?", (args.job_id,)
            ).fetchone()
            if run is None:
                raise KeyError(args.job_id)
            plan = load_json(args.repair_plan)
            task_id = str(plan.get("task_id") or "")
            receipt = repair_failed_attempt(
                root, str(run["run_id"]), task_id, repair_plan=args.repair_plan
            )
            _dump({"status": "scheduled", "job_id": args.job_id,
                   "run_id": run["run_id"], "task_id": task_id,
                   "repair_receipt_hash": receipt}); return 0
        if args.command == "worker-repair":
            from .kernel.worker import repair_failed_attempt
            receipt = repair_failed_attempt(root, args.run_id, args.task_id, repair_plan=args.repair_plan)
            _dump({"status": "ready", "run_id": args.run_id, "task_id": args.task_id,
                   "repair_receipt_hash":receipt}); return 0
        if args.command == "repair-skipped-tables":
            PersistentOrchestrator(root).reopen_skipped_table_branch(args.run_id)
            _dump({"status": "table_branch_reopened", "run_id": args.run_id}); return 0
        if args.command == "wiki-rehearse":
            from .wiki_runtime.rehearsal import WikiPhase2Rehearsal
            _dump(WikiPhase2Rehearsal(root).run(workspace=args.workspace)); return 0
        if args.command in {"goal-audit", "goal-status", "goal-feedback"}:
            from .kernel.goal_alignment import GoalAlignmentController
            controller = GoalAlignmentController(root, plane)
            if args.command == "goal-audit":
                _dump(controller.audit_job(args.job_id, auto_repair=args.auto_repair,
                                           trigger="cli")); return 0
            if args.command == "goal-feedback":
                _dump(controller.report_user_feedback(args.job_id, args.message,
                                                       category=args.category)); return 0
            _dump(controller.status(job_id=args.job_id)); return 0
        if args.command == "system-change":
            from .kernel.goal_alignment import ChangeController
            controller = ChangeController(root, plane)
            if args.action == "status":
                _dump(controller.get(args.candidate_id)); return 0
            if args.action == "certify":
                if not args.phase or not args.suites:
                    raise ValueError("certify requires --phase and --suites")
                suites = load_json(args.suites)
                if not isinstance(suites, dict) or not all(isinstance(v, bool) for v in suites.values()):
                    raise ValueError("--suites must be a JSON object of boolean results")
                _dump(controller.certify(args.candidate_id, phase=args.phase, suites=suites)); return 0
            if args.action == "promote":
                _dump(controller.promote(args.candidate_id, operator=args.operator)); return 0
            _dump(controller.rollback(args.candidate_id, reason=args.reason)); return 0
        if args.command == "system-repair":
            from .kernel.goal_alignment import SystemRepairAgent
            agent = SystemRepairAgent(root, plane)
            if args.action == "status":
                _dump(agent.get(args.repair_run_id) if args.repair_run_id
                      else agent.rows(job_id=args.job_id)); return 0
            if not args.repair_run_id:
                raise ValueError(f"system-repair {args.action} requires repair_run_id")
            if args.action == "approve":
                _dump(agent.approve(args.repair_run_id)); return 0
            if args.action == "reject":
                _dump(agent.reject(args.repair_run_id, reason=args.reason)); return 0
            _dump(agent.execute(args.repair_run_id)); return 0
        if args.command == "failure-triage":
            from .kernel.goal_alignment import FailureTriageAgent
            agent = FailureTriageAgent(root, plane)
            if args.action == "status":
                _dump(agent.get(args.triage_run_id) if args.triage_run_id
                      else agent.rows(job_id=args.job_id)); return 0
            if not args.triage_run_id:
                raise ValueError("failure-triage execute requires triage_run_id")
            _dump(agent.execute(args.triage_run_id)); return 0
        if args.command == "meta-supervisor":
            from .kernel.goal_alignment import SystemMetaSupervisor
            supervisor = SystemMetaSupervisor(root, control=plane)
            if args.approve:
                _dump(supervisor.approve(args.approve)); return 0
            _dump({"status": "audited", "deviations": supervisor.audit(job_id=args.job_id)}
                  if args.audit_only else supervisor.reconcile(job_id=args.job_id))
            return 0
        if args.command in {"autonomy-create", "autonomy-supervisor", "autonomy-status",
                            "autonomy-pause", "autonomy-resume"}:
            from .kernel.goal_alignment.autonomous_supervisor import AutonomousJobSupervisor
            supervisor = AutonomousJobSupervisor(root, control=plane)
            if args.command == "autonomy-create":
                created = supervisor.create_campaign(load_json(args.spec))
                campaign_id = created["campaign"]["campaign_id"]
                _dump(supervisor.run(campaign_id) if args.run else
                      {**created, "first_tick": supervisor.tick(campaign_id, execute_task=False)})
                return 0
            if args.command == "autonomy-supervisor":
                _dump(supervisor.tick(args.campaign_id, execute_task=not args.create_only)
                      if args.once or args.create_only else supervisor.run(args.campaign_id)); return 0
            if args.command == "autonomy-status":
                _dump(supervisor.campaign(args.campaign_id) if args.campaign_id
                      else supervisor.campaigns()); return 0
            if args.command == "autonomy-pause":
                _dump(supervisor.pause(args.campaign_id)); return 0
            _dump(supervisor.resume(args.campaign_id)); return 0
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
