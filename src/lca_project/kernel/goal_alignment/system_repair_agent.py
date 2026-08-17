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
from ..orchestrator import PersistentOrchestrator
from ..state import utcnow
from .change_controller import ChangeController
from .store import canonical, digest


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
    TERMINAL = {"promoted", "awaiting_outcome_validation", "effective",
                "partially_effective", "ineffective", "awaiting_approval",
                "rejected", "rolled_back"}
    ALLOWED_PREFIXES = (
        "src/lca_project/", "scripts/", "vendor/lca_cornerstone/scripts/",
        "tests/", "policies/", "contracts/", "workflows/", "capabilities/",
        "config/wiki-table-document-routes.json",
        "docs/wiki-phase2-migration-manifest.json",
    )
    PROTECTED_PATHS = {
        "policies/wiki-goal-contract-v1.json",
        "policies/governance-v1.json",
        "capabilities/release.apply@1.json",
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
                 validator: Validator | None = None) -> None:
        self.root = Path(root).resolve()
        self.control = control or ControlPlane(self.root)
        self.state = self.control.state
        self.agent_runner = agent_runner or self._run_codex
        self.validator = validator or self._validate
        self.changes = ChangeController(self.root, self.control)

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

    def queue(self, *, candidate_id: str, source_job_id: str,
              source_run_id: str | None, request: dict[str, Any]) -> dict[str, Any]:
        triage_run_id = str(request.get("triage_run_id") or "")
        repair_fingerprint = self._repair_fingerprint(request)
        if triage_run_id or repair_fingerprint:
            rows = self.state._connection().execute(
                "SELECT repair_run_id,status,payload FROM system_repair_runs "
                "WHERE source_job_id=? AND COALESCE(source_run_id,'')=COALESCE(?, '') "
                "ORDER BY created_at DESC", (source_job_id, source_run_id),
            )
            active = {
                "queued", "coding", "validating", "awaiting_approval", "promoted",
                "awaiting_outcome_validation", "effective", "partially_effective",
            }
            for row in rows:
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
        """Update only pre-existing immutable-asset anchors changed by the Agent."""
        manifest_path = sandbox / "docs/wiki-phase2-migration-manifest.json"
        if not manifest_path.is_file():
            return []
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        anchors = manifest.get("anchor_hashes")
        if not isinstance(anchors, dict):
            return []
        updated: list[str] = []
        changed_set = set(agent_changed)
        for relative in sorted(anchors):
            repository_path = f"vendor/lca_cornerstone/{relative}"
            target = sandbox / repository_path
            if (repository_path not in changed_set or repository_path not in before
                    or not target.is_file()):
                continue
            anchors[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
            updated.append(relative)
        if updated:
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        return updated

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
        focused = tuple(dict.fromkeys(requested)) or self.VALIDATION_COMMANDS["sandbox"]
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
             error: str | None = None) -> None:
        with self.state.transaction() as conn:
            conn.execute(
                "UPDATE system_repair_runs SET status=?,sandbox_path=?,patch_hash=?,payload=?,"
                "last_error=?,updated_at=? WHERE repair_run_id=?",
                (status, str(sandbox) if sandbox else None, patch_hash, canonical(payload),
                error, utcnow(), repair_run_id),
            )

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
        record = self.get(repair_run_id)
        if record["status"] in self.TERMINAL:
            return record
        candidate = self.changes.get(str(record["candidate_id"]))
        if record["status"] == "failed" and candidate["status"] != "proposed":
            return record
        run_dir = self.root / "var/system-repairs" / repair_run_id
        sandbox = run_dir / "sandbox"
        if sandbox.exists():
            shutil.rmtree(sandbox)
        run_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.root, sandbox, ignore=self._ignore)
        before = self._snapshot(sandbox)
        payload = dict(record["payload"])
        prior_execution = payload.get("execution") or {}
        payload["execution"] = {
            "attempt": int(prior_execution.get("attempt") or 0) + 1,
            "started_at": utcnow(),
        }
        self._set(repair_run_id, "coding", payload=payload, sandbox=sandbox)
        patch_hash: str | None = None
        try:
            self._validate_repair_request(payload["request"])
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
            if "docs/wiki-phase2-migration-manifest.json" in agent_changed:
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
                            "generated_integrity_anchors": refreshed_anchors,
                            "patch_hash": patch_hash, "validations": validations})
            self._set(repair_run_id, "validating", payload=payload,
                      sandbox=sandbox, patch_hash=patch_hash)
            for phase, tests in self._validation_commands(
                sandbox, payload["request"]
            ).items():
                result = self.validator(sandbox, phase, tests)
                validations.append(result)
                self._set(repair_run_id, "validating", payload=payload,
                          sandbox=sandbox, patch_hash=patch_hash)
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
            if candidate["risk"] != "low":
                self._set(repair_run_id, "awaiting_approval", payload=payload,
                          sandbox=sandbox, patch_hash=patch_hash)
                return self.get(repair_run_id)
            self._promote(record, candidate, sandbox, changed, payload, patch_hash)
            return self.get(repair_run_id)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError,
                json.JSONDecodeError) as exc:
            payload["execution"] = {**payload.get("execution", {}), "failed_at": utcnow()}
            self._set(repair_run_id, "failed", payload=payload, sandbox=sandbox,
                      patch_hash=patch_hash, error=str(exc))
            self.control.events.append("system_repair", repair_run_id,
                                       "system_repair.failed", {"error": str(exc)},
                                       actor="system-repair-agent")
            return self.get(repair_run_id)

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
                 recovery_task_override: str | None = None) -> None:
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
                      sandbox=sandbox, patch_hash=patch_hash)
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
