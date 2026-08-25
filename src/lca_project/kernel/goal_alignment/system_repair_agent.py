"""Governed coding-Agent execution for system-level self repair."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable

from ...control import ControlPlane
from ...domains.wiki_workspace import WikiWorkspaceBuilder
from ..leases import LeaseLost
from ..orchestrator import PersistentOrchestrator
from ..state import utcnow
from .change_controller import ChangeController
from .execution_ownership import ExecutionOwnership
from .store import canonical, digest
from .system_repair_scm import SystemRepairScmPublisher


AgentRunner = Callable[[Path, dict[str, Any]], dict[str, Any]]
Validator = Callable[[Path, str, tuple[str, ...]], dict[str, Any]]


class SystemRepairError(RuntimeError):
    """The coding repair could not be proven safe enough to promote."""


class SystemRepairAgent:
    """Run a coding Agent in a disposable snapshot, then govern promotion."""

    MODEL = "gpt-5.6-sol"
    # These states are non-executable.  ``awaiting_outcome_validation`` is not
    # a successful repair result; it only means the validated patch is live and
    # the official recovery branch must now prove goal improvement.
    TERMINAL = {"promoted", "awaiting_scm_publication", "replan_required",
                "awaiting_outcome_validation", "effective",
                "partially_effective", "ineffective", "awaiting_approval",
                "rejected", "rolled_back"}
    ALLOWED_PREFIXES = (
        "src/lca_project/", "scripts/", "vendor/lca_cornerstone/scripts/",
        "tests/", "policies/", "contracts/", "workflows/", "capabilities/",
        "config/wiki-table-document-routes.json",
        "docs/migration-manifest.json",
        "docs/wiki-phase2-migration-manifest.json",
    )
    PROTECTED_PATHS = {
        "policies/wiki-goal-contract-v1.json",
        "policies/governance-v1.json",
        "capabilities/release.apply@1.json",
        "config/system-repair-replay-corpus.json",
    }
    IGNORED_NAMES = {
        ".git", ".idea", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        "__pycache__", "node_modules", "var", ".venv", "venv",
    }
    VALIDATION_COMMANDS = {
        "sandbox": (
            "tests/test_editorial_policy.py", "tests/test_wiki_draft_gate_policy.py",
            "tests/test_structured_failures.py",
        ),
        "shadow": (
            "tests/test_goal_alignment.py", "tests/test_worker_loop.py",
            "tests/test_autonomous_supervisor.py",
        ),
        "canary": (),
    }

    def __init__(self, root: str | Path, control: ControlPlane | None = None, *,
                 agent_runner: AgentRunner | None = None,
                 validator: Validator | None = None,
                 scm_publisher: SystemRepairScmPublisher | None = None) -> None:
        self.root = Path(root).resolve()
        self.control = control or ControlPlane(self.root)
        self.state = self.control.state
        self.agent_runner = agent_runner or self._run_codex
        self.validator = validator or self._validate
        self.changes = ChangeController(self.root, self.control)
        self.scm = scm_publisher or SystemRepairScmPublisher(self.root, self.control)

    @staticmethod
    def _repair_fingerprint(request: dict[str, Any]) -> str:
        evidence = request.get("evidence") or {}
        failure = evidence.get("failure") or {} if isinstance(evidence, dict) else {}
        return str(
            request.get("source_failure_fingerprint")
            or (evidence.get("failure_fingerprint") if isinstance(evidence, dict) else "")
            or (failure.get("failure_fingerprint") if isinstance(failure, dict) else "")
            or ""
        )

    @staticmethod
    def _causal_plan_hash(request: dict[str, Any]) -> str:
        """Hash the proposed causal intervention, independent of prose noise."""
        changes = [
            {
                "causal_input": str(item.get("causal_input") or item.get("target") or ""),
                "change": str(item.get("change") or item.get("implementation") or ""),
                "expected_effect": str(item.get("expected_effect") or ""),
            }
            for item in request.get("causal_input_changes") or []
            if isinstance(item, dict)
        ]
        return digest({"causal_input_changes": changes}) if changes else ""

    def queue(self, *, candidate_id: str, source_job_id: str,
              source_run_id: str | None, request: dict[str, Any]) -> dict[str, Any]:
        triage_run_id = str(request.get("triage_run_id") or "")
        repair_fingerprint = self._repair_fingerprint(request)
        causal_plan_hash = self._causal_plan_hash(request)
        if triage_run_id or repair_fingerprint:
            rows = self.state._connection().execute(
                "SELECT repair_run_id,status,payload FROM system_repair_runs "
                "WHERE source_job_id=? AND COALESCE(source_run_id,'')=COALESCE(?, '') "
                "ORDER BY created_at DESC", (source_job_id, source_run_id),
            )
            active = {
                "queued", "coding", "validating", "awaiting_scm_publication",
                "awaiting_approval", "promoted",
                "awaiting_outcome_validation", "effective", "partially_effective",
            }
            for row in rows:
                if str(request.get("supersedes_repair_run_id") or "") == str(
                        row["repair_run_id"]):
                    continue
                payload = json.loads(row["payload"])
                prior_request = payload.get("request") or {}
                same_triage = bool(
                    triage_run_id
                    and str(prior_request.get("triage_run_id") or "") == triage_run_id
                )
                same_failure = bool(
                    repair_fingerprint
                    and self._repair_fingerprint(prior_request) == repair_fingerprint
                )
                same_causal_plan = bool(
                    causal_plan_hash
                    and self._causal_plan_hash(prior_request) == causal_plan_hash
                )
                if row["status"] in active and (same_triage or same_failure):
                    self.control.events.append(
                        "system_repair", str(row["repair_run_id"]),
                        "system_repair.duplicate_suppressed", {
                            "triage_run_id": triage_run_id,
                            "failure_fingerprint": repair_fingerprint,
                            "suppressed_candidate_id": candidate_id,
                        }, actor="goal-alignment-controller",
                    )
                    return self.get(str(row["repair_run_id"]))
                if (row["status"] in {"ineffective", "failed", "rejected", "rolled_back"}
                        and same_failure and same_causal_plan):
                    self.control.events.append(
                        "system_repair", str(row["repair_run_id"]),
                        "system_repair.causal_replan_required", {
                            "failure_fingerprint": repair_fingerprint,
                            "causal_plan_hash": causal_plan_hash,
                            "suppressed_candidate_id": candidate_id,
                            "prior_status": row["status"],
                        }, actor="goal-alignment-controller",
                    )
                    return self.get(str(row["repair_run_id"]))
        request_hash = digest(request)
        repair_run_id = "srr_" + digest({
            "candidate_id": candidate_id, "job_id": source_job_id,
            "run_id": source_run_id, "request_hash": request_hash,
        })[:32]
        now = utcnow()
        payload = {
            "schema_version": "system-repair-run-v1",
            "repair_run_id": repair_run_id, "candidate_id": candidate_id,
            "source_job_id": source_job_id, "source_run_id": source_run_id,
            "request": request,
            "failure_fingerprint": repair_fingerprint or None,
            "causal_plan_hash": causal_plan_hash or None,
        }
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO system_repair_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (repair_run_id, candidate_id, source_job_id, source_run_id, "queued",
                 self.MODEL, None, request_hash, None, canonical(payload), None, now, now),
            )
        self.control.events.append("system_repair", repair_run_id,
                                   "system_repair.queued", {
                                       "candidate_id": candidate_id,
                                       "source_job_id": source_job_id,
                                   }, actor="goal-alignment-controller")
        if self.scm.enabled:
            repair = self.get(repair_run_id)
            scm = self.scm.publish_issue(repair, self.changes.get(candidate_id))
            payload = dict(repair["payload"])
            payload["scm"] = scm
            self._set(repair_run_id, "queued", payload=payload)
        return self.get(repair_run_id)

    def supersede_duplicate(self, repair_run_id: str, *, canonical_repair_run_id: str) -> dict[str, Any]:
        """Reject an unexecuted duplicate while retaining its audit chain."""
        record = self.get(repair_run_id)
        if record["status"] != "queued":
            raise ValueError("only a queued duplicate can be superseded")
        candidate = self.changes.get(str(record["candidate_id"]))
        reason = f"duplicate of active repair {canonical_repair_run_id}"
        if candidate["status"] == "proposed":
            self.changes.reject(str(record["candidate_id"]), reason=reason)
        payload = dict(record["payload"])
        payload["duplicate"] = {
            "canonical_repair_run_id": canonical_repair_run_id,
            "superseded_at": utcnow(),
        }
        self._set(repair_run_id, "rejected", payload=payload, error=reason)
        self.control.events.append(
            "system_repair", repair_run_id, "system_repair.duplicate_suppressed",
            payload["duplicate"], actor="system-meta-supervisor",
        )
        return self.get(repair_run_id)

    def get(self, repair_run_id: str) -> dict[str, Any]:
        row = self.state._connection().execute(
            "SELECT * FROM system_repair_runs WHERE repair_run_id=?", (repair_run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(repair_run_id)
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def rows(self, *, job_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM system_repair_runs", []
        if job_id:
            query += " WHERE source_job_id=?"; params.append(job_id)
        query += " ORDER BY created_at DESC LIMIT ?"; params.append(min(max(limit, 1), 200))
        result = []
        for row in self.state._connection().execute(query, tuple(params)):
            item = dict(row); item["payload"] = json.loads(item["payload"]); result.append(item)
        return result

    @classmethod
    def _ignore(cls, _directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in cls.IGNORED_NAMES or name.endswith(".pyc")}

    @classmethod
    def _is_allowed_path(cls, path: str) -> bool:
        """Treat directory entries as prefixes and file entries as exact grants."""
        return any(
            path.startswith(entry) if entry.endswith("/") else path == entry
            for entry in cls.ALLOWED_PREFIXES
        )

    @classmethod
    def _snapshot(cls, root: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        for prefix in cls.ALLOWED_PREFIXES:
            base = root / prefix
            if not base.exists():
                continue
            paths = [base] if base.is_file() else base.rglob("*")
            for path in paths:
                relative = path.relative_to(root)
                if path.is_file() and not path.is_symlink() and not any(
                    part in cls.IGNORED_NAMES for part in relative.parts
                ):
                    result[relative.as_posix()] = hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
        return result

    @classmethod
    def _changed_files(cls, before: dict[str, str], after: dict[str, str]) -> list[str]:
        deleted = sorted(set(before) - set(after))
        if deleted:
            raise SystemRepairError(f"coding Agent may not delete files: {deleted}")
        changed = sorted(path for path, sha in after.items() if before.get(path) != sha)
        if not changed:
            raise SystemRepairError("coding Agent produced no patch")
        if not all(cls._is_allowed_path(path) for path in changed):
            raise SystemRepairError(f"coding Agent changed forbidden paths: {changed}")
        protected = sorted(set(changed) & cls.PROTECTED_PATHS)
        if protected:
            raise SystemRepairError(
                f"coding Agent may not change governance authority: {protected}"
            )
        if not any(path.startswith("tests/") for path in changed):
            raise SystemRepairError("coding repair must add or update regression tests")
        if not any(not path.startswith("tests/") for path in changed):
            raise SystemRepairError("coding repair changed tests but no implementation")
        return changed

    @staticmethod
    def _refresh_integrity_anchors(
        sandbox: Path, before: dict[str, str], agent_changed: list[str],
    ) -> list[str]:
        """Atomically refresh every declared anchor for changed vendored assets.

        Both migration manifests are generated governance evidence.  The Agent
        may not edit either directly; this deterministic adapter updates only
        rows already bound to an asset that the Agent actually changed.
        """
        updated: list[str] = []
        changed_set = set(agent_changed)
        phase2_path = sandbox / "docs/wiki-phase2-migration-manifest.json"
        if phase2_path.is_file():
            manifest = json.loads(phase2_path.read_text(encoding="utf-8"))
            anchors = manifest.get("anchor_hashes")
            changed_anchors: list[str] = []
            if isinstance(anchors, dict):
                for relative in sorted(anchors):
                    repository_path = f"vendor/lca_cornerstone/{relative}"
                    target = sandbox / repository_path
                    if (repository_path not in changed_set
                            or repository_path not in before or not target.is_file()):
                        continue
                    anchors[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
                    changed_anchors.append(relative)
                    updated.append(relative)
            if changed_anchors:
                phase2_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

        migration_path = sandbox / "docs/migration-manifest.json"
        if migration_path.is_file():
            migration = json.loads(migration_path.read_text(encoding="utf-8"))
            changed_assets: list[str] = []
            for asset in migration.get("assets") or []:
                if not isinstance(asset, dict):
                    continue
                repository_path = str(asset.get("target_path") or "")
                target = sandbox / repository_path
                if (repository_path not in changed_set
                        or repository_path not in before or not target.is_file()):
                    continue
                asset["target_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
                changed_assets.append(repository_path)
                updated.append(f"migration:{repository_path}")
            if changed_assets:
                migration_path.write_text(
                    json.dumps(migration, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
        return updated

    def _repair_contract_targets(self, request: dict[str, Any]) -> list[str]:
        targets: list[str] = []
        for value in request.get("implementation_targets") or []:
            raw = str(value).strip()
            relative = raw.split("::", 1)[0].split(":", 1)[0]
            if "/" in relative or relative.endswith((".py", ".json", ".js")):
                targets.append(relative)
        return sorted(set(targets))

    def _validate_repair_contract(self, request: dict[str, Any]) -> None:
        forbidden = [
            path for path in self._repair_contract_targets(request)
            if not self._is_allowed_path(path)
        ]
        if forbidden:
            raise SystemRepairError(
                "REPAIR_CONTRACT_UNSATISFIABLE: implementation targets are outside "
                f"the governed mutation scope: {forbidden}"
            )
        migration_path = self.root / "docs/migration-manifest.json"
        if not migration_path.is_file():
            return
        migration = json.loads(migration_path.read_text(encoding="utf-8"))
        frozen = {
            str(asset.get("target_path") or "")
            for asset in migration.get("assets") or []
            if isinstance(asset, dict) and asset.get("frozen") is True
        }
        anchored_targets = frozen & set(self._repair_contract_targets(request))
        if anchored_targets and not self._is_allowed_path("docs/migration-manifest.json"):
            raise SystemRepairError(
                "REPAIR_CONTRACT_UNSATISFIABLE: frozen implementation targets require "
                "an integrity-manifest migration outside the governed scope"
            )

    def _run_codex(self, sandbox: Path, request: dict[str, Any]) -> dict[str, Any]:
        run_dir = Path(request["run_dir"])
        schema = self.root / "contracts/system-code-repair-result-v1.schema.json"
        output = run_dir / "agent-result.json"
        prompt = (
            "You are the governed System Repair coding agent. Work only in the supplied isolated "
            "repository snapshot. Diagnose the structured failure below, implement the smallest "
            "goal-preserving fix, and add regression tests. Do not change Goal Contracts, repair "
            "authority, release permissions, secrets, generated var data, or git state. Do not "
            "commit. Preserve fail-closed behavior and hash bindings. Run focused tests before "
            "finishing. When causal_input_changes and proof_contract are supplied, the result "
            "must declare causal_input_changes_applied with actual changed files and "
            "proof_instrumentation for every requested metric; tests alone are patch evidence, "
            "while the replayed source Job will provide outcome evidence. Your final response "
            "must match the provided JSON schema.\n\n"
            + json.dumps(request["repair_request"], ensure_ascii=False, indent=2)
        )
        command = [
            shutil.which("codex") or "codex", "exec", "--ephemeral",
            "--approve-for-me",
            "--model", self.MODEL, "-c", 'model_reasoning_effort="high"',
            "--cd", str(sandbox), "--output-schema", str(schema),
            "--output-last-message", str(output), prompt,
        ]
        completed = subprocess.run(command, cwd=sandbox, text=True, capture_output=True,
                                   timeout=1800, check=False)
        (run_dir / "agent-stdout.log").write_text(completed.stdout[-200000:], encoding="utf-8")
        (run_dir / "agent-stderr.log").write_text(completed.stderr[-200000:], encoding="utf-8")
        if completed.returncode != 0 or not output.is_file():
            raise SystemRepairError(
                f"coding Agent failed with exit {completed.returncode}: {completed.stderr[-2000:]}"
            )
        value = json.loads(output.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise SystemRepairError("coding Agent result is not an object")
        return value

    @staticmethod
    def _validate_repair_request(request: dict[str, Any]) -> None:
        """Reject constrained repair requests before granting mutation authority."""
        constraints = request.get("goal_constraints") or {}
        named_changes = {
            str(item.get("target") or item.get("causal_input") or "").strip()
            for item in request.get("causal_input_changes") or []
            if isinstance(item, dict)
        } - {""}
        if (constraints.get("repair_must_change_a_named_causal_input") is True
                and not named_changes):
            raise SystemRepairError(
                "repair request requires a named causal input change"
            )
        proof_metrics = {
            str(item.get("metric") or "").strip()
            for item in request.get("proof_contract") or []
            if isinstance(item, dict)
        } - {""}
        if (constraints.get("promotion_requires_proof_metric_improvement") is True
                and not proof_metrics):
            raise SystemRepairError(
                "repair request requires a measurable Proof Contract"
            )

    @staticmethod
    def _validate_goal_repair_claims(
        request: dict[str, Any], result: dict[str, Any], changed: list[str],
    ) -> None:
        required_changes = request.get("causal_input_changes") or []
        required_proof = request.get("proof_contract") or []
        if not required_changes and not required_proof:
            return
        applied = result.get("causal_input_changes_applied") or []
        instrumentation = result.get("proof_instrumentation") or []
        required_targets = {str(item.get("target") or item.get("causal_input") or "")
                            for item in required_changes if isinstance(item, dict)}
        applied_targets = {str(item.get("target") or "") for item in applied
                           if isinstance(item, dict)}
        if not required_targets or not required_targets <= applied_targets:
            raise SystemRepairError(
                "coding repair did not declare every named causal input change"
            )
        claimed_files = {
            str(path) for item in applied if isinstance(item, dict)
            for path in item.get("changed_files") or []
        }
        if not claimed_files or not claimed_files <= set(changed):
            raise SystemRepairError(
                "causal input change claims are not bound to the actual snapshot diff"
            )
        required_metrics = {str(item.get("metric") or "") for item in required_proof
                            if isinstance(item, dict)}
        instrumented_metrics = {str(item.get("metric") or "") for item in instrumentation
                                if isinstance(item, dict)}
        if not required_metrics or not required_metrics <= instrumented_metrics:
            raise SystemRepairError(
                "coding repair does not instrument every Proof Contract metric"
            )

    def _validation_commands(
        self, sandbox: Path, request: dict[str, Any]
    ) -> dict[str, tuple[str, ...]]:
        requested = []
        for value in request.get("validation_tests") or []:
            relative = str(value)
            if (relative.startswith("tests/") and ".." not in Path(relative).parts
                    and (sandbox / relative).is_file()):
                requested.append(relative)
        evidence = request.get("evidence") or {}
        mechanism_family = str(
            request.get("mechanism_family")
            or (evidence.get("mechanism_family") if isinstance(evidence, dict) else "")
            or ""
        )
        corpus_tests: list[str] = []
        corpus_path = self.root / "config/system-repair-replay-corpus.json"
        if mechanism_family and corpus_path.is_file():
            try:
                corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
                if corpus.get("schema_version") == "system-repair-replay-corpus-v1":
                    corpus_tests = [
                        str(value) for value in
                        (corpus.get("families") or {}).get(mechanism_family, [])
                        if str(value).startswith("tests/")
                        and ".." not in Path(str(value)).parts
                        and (sandbox / str(value)).is_file()
                    ]
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                corpus_tests = []
        focused = tuple(dict.fromkeys([*requested, *corpus_tests]))
        if not focused:
            focused = self.VALIDATION_COMMANDS["sandbox"]
        return {
            "sandbox": focused,
            "shadow": self.VALIDATION_COMMANDS["shadow"],
            "canary": (),
        }

    @staticmethod
    def _validate(root: Path, phase: str, tests: tuple[str, ...]) -> dict[str, Any]:
        command = [shutil.which("pytest") or "pytest", "-q", *tests]
        env = dict(os.environ); env["PYTHONPATH"] = str(root / "src")
        completed = subprocess.run(command, cwd=root, env=env, text=True,
                                   capture_output=True, timeout=1800, check=False)
        return {"phase": phase, "passed": completed.returncode == 0,
                "command": command, "exit_code": completed.returncode,
                "stdout_tail": completed.stdout[-12000:],
                "stderr_tail": completed.stderr[-12000:]}

    def _set(self, repair_run_id: str, status: str, *, payload: dict[str, Any],
             sandbox: Path | None = None, patch_hash: str | None = None,
             error: str | None = None,
             ownership: ExecutionOwnership | None = None) -> None:
        lease = None
        if ownership is not None:
            lease = ownership.current()
        with self.state.transaction() as conn:
            sql = (
                "UPDATE system_repair_runs SET status=?,sandbox_path=?,patch_hash=?,payload=?,"
                "last_error=?,updated_at=? WHERE repair_run_id=?"
            )
            params: list[Any] = [
                status, str(sandbox) if sandbox else None, patch_hash,
                canonical(payload), error, utcnow(), repair_run_id,
            ]
            if lease is not None:
                sql += (
                    " AND EXISTS(SELECT 1 FROM goal_execution_owners o JOIN leases l "
                    "ON l.resource=o.resource AND l.holder=o.owner_id "
                    "AND l.fencing_token=o.fencing_token WHERE o.execution_type=? "
                    "AND o.execution_id=? AND o.owner_id=? AND o.fencing_token=? "
                    "AND o.status='running' AND l.expires_at>?)"
                )
                params.extend([
                    ownership.execution_type, ownership.execution_id,
                    ownership.owner_id, lease.fencing_token, utcnow(),
                ])
            changed = conn.execute(sql, tuple(params)).rowcount
        if changed != 1:
            raise LeaseLost(f"repair row disappeared during fenced update: {repair_run_id}")

    def _materialized_state(self, run_id: str | None, workspace: Path) -> dict[str, str]:
        """Return the latest task-owned physical hashes for a running workflow."""
        if not run_id:
            return {}
        latest: dict[str, str] = {}
        rows = self.state._connection().execute(
            "SELECT output_hash FROM orchestrator_tasks WHERE run_id=? "
            "AND status='succeeded' AND output_hash IS NOT NULL ORDER BY updated_at",
            (run_id,),
        )
        for row in rows:
            manifest = self.control.artifacts.verify_task_output_manifest(
                str(row["output_hash"])
            )
            for item in manifest.get("files") or []:
                if item.get("role") == "materialized_output":
                    latest[str(item["path"])] = str(item["sha256"])
        for logical, expected in latest.items():
            target = (workspace / logical).resolve()
            if (not target.is_relative_to(workspace.resolve()) or not target.is_file()
                    or target.is_symlink()
                    or hashlib.sha256(target.read_bytes()).hexdigest() != expected):
                raise SystemRepairError(
                    f"preserved materialized output is already inconsistent: {logical}"
                )
        return latest

    def execute(self, repair_run_id: str) -> dict[str, Any]:
        """Execute a durable repair under a fenced single-consumer lease."""
        record = self.get(repair_run_id)
        prior = record["payload"].get("execution") or {}
        ownership = ExecutionOwnership.create(
            self.control, "system-repair", repair_run_id,
            attempt=int(prior.get("attempt") or 0) + 1,
        )
        try:
            ownership.start()
        except LeaseLost:
            return self.get(repair_run_id)
        try:
            return self._execute_owned(repair_run_id, ownership=ownership)
        finally:
            ownership.close()

    def _execute_owned(
        self, repair_run_id: str, *, ownership: ExecutionOwnership,
    ) -> dict[str, Any]:
        record = self.get(repair_run_id)
        if record["status"] in self.TERMINAL:
            return record
        candidate = self.changes.get(str(record["candidate_id"]))
        if (record["status"] in {"coding", "validating"}
                and candidate["status"] != "proposed"):
            self._queue_execution_recovery_replan(
                record, candidate, ownership=ownership,
            )
            return self.get(repair_run_id)
        if record["status"] == "failed" and candidate["status"] != "proposed":
            return record
        run_dir = self.root / "var/system-repairs" / repair_run_id
        sandbox = run_dir / "sandbox"
        if sandbox.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(sandbox)],
                cwd=self.root, text=True, capture_output=True, check=False,
            )
            if sandbox.exists():
                shutil.rmtree(sandbox)
        run_dir.mkdir(parents=True, exist_ok=True)
        request_payload = record["payload"].get("request") or {}
        if request_payload.get("scm_replan"):
            subprocess.run(
                ["git", "fetch", self.scm.policy.remote, self.scm.policy.base_branch],
                cwd=self.root, text=True, capture_output=True, check=True,
            )
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(sandbox),
                 f"{self.scm.policy.remote}/{self.scm.policy.base_branch}"],
                cwd=self.root, text=True, capture_output=True, check=True,
            )
        else:
            shutil.copytree(self.root, sandbox, ignore=self._ignore)
        before = self._snapshot(sandbox)
        payload = dict(record["payload"])
        prior_execution = payload.get("execution") or {}
        payload["execution"] = {
            "attempt": ownership.attempt,
            "started_at": utcnow(),
            "owner_id": ownership.owner_id,
            "fencing_token": ownership.current().fencing_token,
        }
        self._set(
            repair_run_id, "coding", payload=payload, sandbox=sandbox,
            ownership=ownership,
        )
        patch_hash: str | None = None
        try:
            self._validate_repair_request(payload["request"])
            self._validate_repair_contract(payload["request"])
            request = {"run_dir": str(run_dir), "repair_request": {
                **payload["request"], "candidate": candidate["payload"],
                "allowed_paths": list(self.ALLOWED_PREFIXES),
            }}
            agent_result = self.agent_runner(sandbox, request)
            agent_after = self._snapshot(sandbox)
            agent_changed = self._changed_files(before, agent_after)
            self._validate_goal_repair_claims(
                payload["request"], agent_result, agent_changed
            )
            if set(agent_changed) & {
                "docs/migration-manifest.json",
                "docs/wiki-phase2-migration-manifest.json",
            }:
                raise SystemRepairError(
                    "coding Agent may not edit generated integrity manifests directly"
                )
            declared = sorted(str(path) for path in agent_result.get("changed_files") or [])
            if declared and declared != agent_changed:
                raise SystemRepairError(
                    "Agent declaration does not match snapshot diff: "
                    f"declared={declared} actual={agent_changed}"
                )
            refreshed_anchors = self._refresh_integrity_anchors(
                sandbox, before, agent_changed
            )
            after = self._snapshot(sandbox)
            changed = self._changed_files(before, after)
            patch_hash = digest({path: after[path] for path in changed})
            validations: list[dict[str, Any]] = []
            payload.update({"agent_result": agent_result, "changed_files": changed,
                            "scm_base_hashes": {path: before.get(path) for path in changed},
                            "generated_integrity_anchors": refreshed_anchors,
                            "patch_hash": patch_hash, "validations": validations})
            self._set(repair_run_id, "validating", payload=payload,
                      sandbox=sandbox, patch_hash=patch_hash, ownership=ownership)
            for phase, tests in self._validation_commands(
                sandbox, payload["request"]
            ).items():
                result = self.validator(sandbox, phase, tests)
                validations.append(result)
                self._set(repair_run_id, "validating", payload=payload,
                          sandbox=sandbox, patch_hash=patch_hash, ownership=ownership)
                certificate = self.changes.certify(
                    str(record["candidate_id"]), phase=phase,
                    suites={"golden" if phase == "sandbox" else
                            "mutation" if phase == "shadow" else "regression":
                            bool(result.get("passed"))},
                    evidence={"repair_run_id": repair_run_id, "patch_hash": patch_hash,
                              "result": result},
                )
                if certificate["verdict"] != "pass":
                    raise SystemRepairError(f"{phase} validation failed")
            scm = self.scm.publish_patch(
                {**record, "payload": payload}, candidate,
                sandbox=sandbox, changed_files=changed, patch_hash=patch_hash,
                validations=validations,
                base_hashes=payload["scm_base_hashes"],
            )
            payload["scm"] = scm
            self._set(repair_run_id, "validating", payload=payload,
                      sandbox=sandbox, patch_hash=patch_hash, ownership=ownership)
            if (self.scm.policy.required_for_promotion
                    and scm.get("status") != "published"):
                if self._queue_scm_replan(
                        record, candidate, payload, scm, ownership=ownership):
                    return self.get(repair_run_id)
                self._set(repair_run_id, "awaiting_scm_publication", payload=payload,
                          sandbox=sandbox, patch_hash=patch_hash,
                          error=str(scm.get("last_error") or "SCM publication is pending"),
                          ownership=ownership)
                return self.get(repair_run_id)
            if candidate["risk"] != "low":
                self._set(repair_run_id, "awaiting_approval", payload=payload,
                          sandbox=sandbox, patch_hash=patch_hash, ownership=ownership)
                return self.get(repair_run_id)
            self._promote(
                record, candidate, sandbox, changed, payload, patch_hash,
                ownership=ownership,
            )
            return self.get(repair_run_id)

        except LeaseLost:
            return self.get(repair_run_id)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError,
                json.JSONDecodeError) as exc:
            payload["execution"] = {**payload.get("execution", {}), "failed_at": utcnow()}
            self._set(repair_run_id, "failed", payload=payload, sandbox=sandbox,
                      patch_hash=patch_hash, error=str(exc), ownership=ownership)
            self.control.events.append("system_repair", repair_run_id,
                                       "system_repair.failed", {"error": str(exc)},
                                       actor="system-repair-agent")
            return self.get(repair_run_id)

    def publish_scm(self, repair_run_id: str) -> dict[str, Any]:
        """Retry a required Issue/commit/PR publication before promotion."""
        record = self.get(repair_run_id)
        if record["status"] != "awaiting_scm_publication":
            raise ValueError("only an awaiting-SCM-publication repair can be published")
        candidate = self.changes.get(str(record["candidate_id"]))
        if candidate["status"] != "canary_passed":
            raise ValueError("repair candidate is not canary validated")
        payload = dict(record["payload"])
        sandbox = Path(str(record.get("sandbox_path") or "")).resolve()
        if not sandbox.is_dir() or not sandbox.is_relative_to(self.root / "var/system-repairs"):
            raise ValueError("repair sandbox is unavailable or unsafe")
        patch_hash = str(record.get("patch_hash") or "")
        changed = [str(item) for item in payload.get("changed_files") or []]
        if not patch_hash or not changed:
            raise ValueError("validated repair has no bound patch")
        scm = self.scm.publish_patch(
            record, candidate, sandbox=sandbox, changed_files=changed,
            patch_hash=patch_hash, validations=list(payload.get("validations") or []),
            base_hashes=dict(payload.get("scm_base_hashes") or {}),
        )
        payload["scm"] = scm
        if scm.get("status") != "published":
            if self._queue_scm_replan(record, candidate, payload, scm):
                return self.get(repair_run_id)
            self._set(repair_run_id, "awaiting_scm_publication", payload=payload,
                      sandbox=sandbox, patch_hash=patch_hash,
                      error=str(scm.get("last_error") or "SCM publication is pending"))
            return self.get(repair_run_id)
        if candidate["risk"] != "low":
            self._set(repair_run_id, "awaiting_approval", payload=payload,
                      sandbox=sandbox, patch_hash=patch_hash)
        else:
            self._promote(record, candidate, sandbox, changed, payload, patch_hash)
        return self.get(repair_run_id)

    @staticmethod
    def _scm_failure_kind(scm: dict[str, Any]) -> str:
        patch = (scm.get("payload") or {}).get("patch") or {}
        return str(patch.get("failure_kind") or "")

    def _queue_scm_replan(
        self, record: dict[str, Any], candidate: dict[str, Any],
        payload: dict[str, Any], scm: dict[str, Any], *,
        ownership: ExecutionOwnership | None = None,
    ) -> bool:
        """Replace an obsolete-base patch with a causally different repair run."""
        if self._scm_failure_kind(scm) != "base_revision_conflict":
            return False
        request = dict(payload.get("request") or {})
        revision = int(request.get("scm_replan_revision") or 0) + 1
        if revision > 3:
            return False
        error = str(scm.get("last_error") or "SCM base revision conflict")
        successor = self.changes.propose(
            source_deviation_id=candidate.get("source_deviation_id"),
            target="rebase_and_regenerate_system_repair",
            risk=str(candidate.get("risk") or "high"),
            change={
                "action": "rebase_and_regenerate_system_repair",
                "supersedes_candidate_id": candidate["candidate_id"],
                "supersedes_repair_run_id": record["repair_run_id"],
                "scm_replan_revision": revision,
                "publication_error": error,
            },
            rollback={
                "strategy": "discard_successor_repair_branch",
                "trigger": "validation regression or repeated base conflict",
            },
        )
        causal_changes = list(request.get("causal_input_changes") or [])
        causal_changes.append({
            "causal_input": "repository_base_revision",
            "change": "regenerate the validated patch from the current configured main head",
            "expected_effect": "produce a source-bound delta that applies without replaying stale hashes",
        })
        successor_run = self.queue(
            candidate_id=str(successor["candidate_id"]),
            source_job_id=str(record["source_job_id"]),
            source_run_id=record.get("source_run_id"),
            request={
                **request,
                "triage_run_id": None,
                "scm_replan_revision": revision,
                "supersedes_repair_run_id": record["repair_run_id"],
                "causal_input_changes": causal_changes,
                "scm_replan": {
                    "failure_kind": "base_revision_conflict",
                    "error": error,
                    "replan_at": utcnow(),
                },
            },
        )
        payload["scm_replan"] = {
            "revision": revision,
            "successor_candidate_id": successor["candidate_id"],
            "successor_repair_run_id": successor_run["repair_run_id"],
            "reason": error,
        }
        self._set(
            str(record["repair_run_id"]), "replan_required", payload=payload,
            sandbox=Path(str(record.get("sandbox_path") or "")),
            patch_hash=str(record.get("patch_hash") or ""), error=error,
            ownership=ownership,
        )
        from .work_dispatcher import dispatch_system_repair
        dispatch_system_repair(self.root, str(successor_run["repair_run_id"]))
        self.control.events.append(
            "system_repair", str(record["repair_run_id"]),
            "system_repair.scm_causal_replan_queued", payload["scm_replan"],
            actor="system-repair-agent",
        )
        return True

    def _queue_execution_recovery_replan(
        self, record: dict[str, Any], candidate: dict[str, Any], *,
        ownership: ExecutionOwnership,
    ) -> None:
        """Restart a partially certified repair without reusing stale proofs."""
        payload = dict(record["payload"])
        request = dict(payload.get("request") or {})
        revision = int(request.get("execution_recovery_revision") or 0) + 1
        successor = self.changes.propose(
            source_deviation_id=candidate.get("source_deviation_id"),
            target=str(candidate.get("target") or "recover_system_repair_execution"),
            risk=str(candidate.get("risk") or "high"),
            change={
                **((candidate.get("payload") or {}).get("change") or {}),
                "execution_recovery_revision": revision,
                "supersedes_candidate_id": candidate["candidate_id"],
                "supersedes_repair_run_id": record["repair_run_id"],
                "reason": "executor disappeared after candidate certification advanced",
            },
            rollback=((candidate.get("payload") or {}).get("rollback") or {
                "strategy": "discard_recovery_candidate",
            }),
        )
        causal_changes = list(request.get("causal_input_changes") or [])
        causal_changes.append({
            "causal_input": "repair_execution_snapshot",
            "change": "regenerate and revalidate the repair in a new fenced execution epoch",
            "expected_effect": "bind every certificate to one complete successor patch",
        })
        successor_run = self.queue(
            candidate_id=str(successor["candidate_id"]),
            source_job_id=str(record["source_job_id"]),
            source_run_id=record.get("source_run_id"),
            request={
                **request,
                "triage_run_id": None,
                "execution_recovery_revision": revision,
                "supersedes_repair_run_id": record["repair_run_id"],
                "causal_input_changes": causal_changes,
            },
        )
        payload["execution_replan"] = {
            "revision": revision,
            "successor_candidate_id": successor["candidate_id"],
            "successor_repair_run_id": successor_run["repair_run_id"],
            "queued_at": utcnow(),
        }
        self._set(
            str(record["repair_run_id"]), "replan_required", payload=payload,
            sandbox=Path(str(record.get("sandbox_path") or "")),
            patch_hash=str(record.get("patch_hash") or ""),
            error="stale executor left partially bound validation certificates",
            ownership=ownership,
        )
        from .work_dispatcher import dispatch_system_repair
        dispatch_system_repair(self.root, str(successor_run["repair_run_id"]))

    def approve(self, repair_run_id: str, *, recovery_task: str | None = None) -> dict[str, Any]:
        """Apply explicit promotion authority and its separately authorized rewind."""
        record = self.get(repair_run_id)
        if record["status"] != "awaiting_approval":
            raise ValueError("only an awaiting-approval repair can be approved")
        candidate = self.changes.get(str(record["candidate_id"]))
        if candidate["status"] != "canary_passed":
            raise ValueError("repair candidate is not canary validated")
        payload = dict(record["payload"])
        sandbox = Path(str(record.get("sandbox_path") or "")).resolve()
        if not sandbox.is_dir() or not sandbox.is_relative_to(self.root / "var/system-repairs"):
            raise ValueError("repair sandbox is unavailable or unsafe")
        patch_hash = str(record.get("patch_hash") or "")
        changed = [str(item) for item in payload.get("changed_files") or []]
        if not patch_hash or not changed:
            raise ValueError("validated repair has no bound patch")
        self._promote(
            record, candidate, sandbox, changed, payload, patch_hash, operator=True,
            recovery_task_override=recovery_task,
        )
        return self.get(repair_run_id)

    def reject(self, repair_run_id: str, *, reason: str) -> dict[str, Any]:
        """Reject a validated but unpromoted patch without touching live files."""
        record = self.get(repair_run_id)
        if record["status"] != "awaiting_approval":
            raise ValueError("only an awaiting-approval repair can be rejected")
        candidate = self.changes.get(str(record["candidate_id"]))
        if candidate["status"] != "canary_passed":
            raise ValueError("repair candidate is not awaiting a promotion decision")
        rejected = self.changes.reject(str(record["candidate_id"]), reason=reason)
        payload = dict(record["payload"])
        payload.update({"rejection": rejected, "rejected_at": utcnow(),
                        "rejection_reason": reason})
        self._set(
            repair_run_id, "rejected", payload=payload,
            sandbox=Path(str(record.get("sandbox_path") or "")),
            patch_hash=str(record.get("patch_hash") or "") or None,
        )
        self.control.events.append(
            "system_repair", repair_run_id, "system_repair.rejected",
            {"candidate_id": candidate["candidate_id"], "reason": reason},
            actor="system-repair-agent",
        )
        return self.get(repair_run_id)

    def authorize_rewind(self, repair_run_id: str, recovery_task: str) -> dict[str, Any]:
        """Idempotently correct or complete the operator-authorized recovery point."""
        record = self.get(repair_run_id)
        if record["status"] != "awaiting_outcome_validation":
            raise ValueError("rewind correction requires an outcome-validation repair")
        source_run_id = str(record.get("source_run_id") or "")
        if not source_run_id or not recovery_task:
            raise ValueError("rewind correction requires a run and recovery task")
        payload = dict(record["payload"])
        prior_invalidated = [str(item) for item in payload.get("invalidated") or []]
        if recovery_task in prior_invalidated:
            return record
        invalidated = list(PersistentOrchestrator(self.root).rewind_from(
            source_run_id, recovery_task,
            reason=f"corrected authorized rewind for {repair_run_id}",
            actor="system-meta-supervisor",
            reset_attempts=True,
        ))
        payload["invalidated"] = list(dict.fromkeys([*prior_invalidated, *invalidated]))
        authorization = dict(payload.get("operator_authorization") or {})
        authorization.update({
            "approved": True,
            "previous_authorized_recovery_task": authorization.get(
                "authorized_recovery_task"
            ),
            "authorized_recovery_task": recovery_task,
            "corrected_at": utcnow(),
        })
        payload["operator_authorization"] = authorization
        self._set(
            repair_run_id, "awaiting_outcome_validation", payload=payload,
            sandbox=Path(str(record.get("sandbox_path") or "")),
            patch_hash=str(record.get("patch_hash") or "") or None,
        )
        self.control.events.append(
            "system_repair", repair_run_id, "system_repair.rewind_corrected", {
                "source_run_id": source_run_id,
                "recovery_task": recovery_task,
                "invalidated": invalidated,
            }, actor="system-meta-supervisor",
        )
        return self.get(repair_run_id)

    def _promote(self, record: dict[str, Any], candidate: dict[str, Any], sandbox: Path,
                 changed: list[str], payload: dict[str, Any], patch_hash: str, *,
                 operator: bool = False,
                 recovery_task_override: str | None = None,
                 ownership: ExecutionOwnership | None = None) -> None:
        repair_run_id = str(record["repair_run_id"])
        backup = self.root / "var/system-repairs" / repair_run_id / "backup"
        copied: list[str] = []
        originally_present: set[str] = set()
        try:
            source_run_id = record.get("source_run_id")
            source_job_id = str(record["source_job_id"])
            workspace = self.root / "var/workspaces/jobs" / source_job_id
            materialized_before = self._materialized_state(
                str(source_run_id) if source_run_id else None, workspace
            ) if workspace.is_dir() else {}
            for relative in changed:
                source, target = sandbox / relative, self.root / relative
                saved = backup / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                if target.is_file():
                    originally_present.add(relative)
                    shutil.copy2(target, saved)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target); copied.append(relative)
            post = self.validator(self.root, "post_promotion", ())
            if not post.get("passed"):
                raise SystemRepairError("post-promotion regression suite failed")
            promoted = self.changes.promote(
                str(candidate["candidate_id"]), operator=operator
            )
            if (workspace / "workspace-manifest.json").is_file():
                # Project-level scripts run from the live project and need no
                # workspace projection.  Only explicitly changed vendored
                # assets may be copied, and the workspace builder will still
                # reject task-owned bootstrap state such as Wiki pages.
                vendor_changes = [
                    relative for relative in changed
                    if relative.startswith("vendor/lca_cornerstone/")
                ]
                WikiWorkspaceBuilder().refresh(
                    workspace, vendor_paths=vendor_changes
                )
            for logical, expected in materialized_before.items():
                actual = hashlib.sha256((workspace / logical).read_bytes()).hexdigest()
                if actual != expected:
                    raise SystemRepairError(
                        f"promotion changed preserved materialized output: {logical}"
                    )
            requested_recovery_task = str(
                payload["request"].get("recovery_task") or ""
            )
            recovery_task = str(recovery_task_override or requested_recovery_task)
            invalidated: list[str] = []
            if source_run_id and recovery_task:
                invalidated = list(PersistentOrchestrator(self.root).rewind_from(
                    str(source_run_id), recovery_task,
                    reason=f"promoted system repair {repair_run_id}",
                    actor="system-repair-agent",
                    reset_attempts=True,
                ))
            promoted_at = utcnow()
            baseline_outcome = ((payload.get("request") or {}).get("evidence") or {}).get(
                "research_outcome"
            ) or {}
            payload.update({"promotion": promoted, "post_promotion": post,
                            "operator_authorization": {
                                "approved": bool(operator),
                                "requested_recovery_task": requested_recovery_task,
                                "authorized_recovery_task": recovery_task,
                            },
                            "invalidated": invalidated, "promoted_at": promoted_at,
                            "outcome_validation": {
                                "status": "pending", "official_replay_required": True,
                                "baseline_metrics": baseline_outcome.get("metrics") or {},
                                "proof_contract": baseline_outcome.get("proof_contract") or
                                (payload.get("request") or {}).get("proof_contract") or [],
                            }})
            self._set(repair_run_id, "awaiting_outcome_validation", payload=payload,
                      sandbox=sandbox, patch_hash=patch_hash, ownership=ownership)
            self.control.events.append("system_repair", repair_run_id,
                                       "system_repair.promoted", {
                                           "candidate_id": candidate["candidate_id"],
                                           "patch_hash": patch_hash,
                                           "source_job_id": source_job_id,
                                           "invalidated": invalidated,
                                       }, actor="system-repair-agent")
            self.control.events.append(
                "system_repair", repair_run_id,
                "system_repair.awaiting_outcome_validation", {
                    "candidate_id": candidate["candidate_id"],
                    "source_job_id": source_job_id,
                    "proof_contract": payload["outcome_validation"]["proof_contract"],
                }, actor="system-repair-agent",
            )
        except Exception as exc:
            for relative in reversed(copied):
                saved, target = backup / relative, self.root / relative
                if saved.is_file():
                    shutil.copy2(saved, target)
                elif relative not in originally_present and target.is_file():
                    target.unlink()
            source_job_id = str(record["source_job_id"])
            workspace = self.root / "var/workspaces/jobs" / source_job_id
            if (workspace / "workspace-manifest.json").is_file():
                try:
                    vendor_changes = [
                        relative for relative in copied
                        if relative.startswith("vendor/lca_cornerstone/")
                    ]
                    WikiWorkspaceBuilder().refresh(
                        workspace, vendor_paths=vendor_changes
                    )
                except (OSError, ValueError):
                    pass
            current = self.changes.get(str(candidate["candidate_id"]))
            if current["status"] in {"promoted", "monitored"}:
                self.changes.rollback(
                    str(candidate["candidate_id"]),
                    reason=f"promotion side effect failed: {type(exc).__name__}",
                )
            elif current["status"] not in {"rejected", "rolled_back"}:
                self.changes.reject(
                    str(candidate["candidate_id"]),
                    reason=f"post-validation promotion failed: {type(exc).__name__}",
                )
            raise
