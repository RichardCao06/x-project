"""Read-only Agent investigation for failures not explained by deterministic rules."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable

from ...control import ControlPlane
from ..state import utcnow
from .store import canonical, digest


TriageRunner = Callable[[Path, dict[str, Any]], dict[str, Any]]


class FailureTriageError(RuntimeError):
    """The investigation did not produce a safe, evidence-backed route."""


class FailureTriageAgent:
    """Investigate an unknown failure without granting mutation authority."""

    MODEL = "gpt-5.6-sol"
    TERMINAL = {"completed", "rejected"}
    IGNORED_NAMES = {
        ".git", ".idea", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        "__pycache__", "node_modules", "var", ".venv", "venv",
    }
    EVIDENCE_FILES = (
        "research-plan.json", "research-plan-gate.json", "search-execution-gate.json",
        "source-diversity-gate.json", "source-evidence.json", "source-queue.json",
        "research-ready.json", "content-blueprint.json", "verify-output.json",
        "table-data/search-matrix.json", "table-data/search-matrix.executed.json",
        "table-data/evidence-selection.json", "table-data/collection.json",
        "maturity-gate.json", "table-data/source-verdict.json",
        "table-data/table-population-gate.json",
    )
    ROUTES = {
        "retry_task", "rewind_task", "expand_research", "propose_code_change",
        "propose_gate_change", "propose_policy_change", "record_evidence_limited",
        "request_operator",
    }

    def __init__(self, root: str | Path, control: ControlPlane | None = None, *,
                 runner: TriageRunner | None = None) -> None:
        self.root = Path(root).resolve()
        self.control = control or ControlPlane(self.root)
        self.state = self.control.state
        self.runner = runner or self._run_codex

    @classmethod
    def _ignore(cls, _directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in cls.IGNORED_NAMES or name.endswith(".pyc")}

    @classmethod
    def _compact(cls, value: Any, *, depth: int = 0) -> Any:
        if depth >= 5:
            return "<depth-limited>"
        if isinstance(value, dict):
            return {str(key): cls._compact(item, depth=depth + 1)
                    for key, item in list(value.items())[:80]}
        if isinstance(value, list):
            return [cls._compact(item, depth=depth + 1) for item in value[:30]]
        if isinstance(value, str) and len(value) > 4000:
            return value[:4000] + "…<truncated>"
        return value

    def build_dossier(self, request: dict[str, Any]) -> dict[str, Any]:
        dossier = {key: value for key, value in request.items() if key != "batch_path"}
        batch_value = request.get("batch_path")
        artifacts: dict[str, Any] = {}
        if batch_value:
            batch = Path(str(batch_value)).resolve()
            try:
                batch.relative_to(self.root / "var/workspaces/jobs")
            except ValueError:
                batch = Path("/__invalid_batch__")
            for relative in self.EVIDENCE_FILES:
                path = batch / relative
                if not path.is_file() or path.stat().st_size > 2_000_000:
                    continue
                try:
                    artifacts[relative] = self._compact(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
        dossier["artifact_evidence"] = artifacts
        dossier["investigation_policy"] = {
            "fingerprints_are_deduplication_hints_not_diagnosis",
            "must_distinguish_absent_evidence_from_broken_evidence_flow",
            "must_trace_preconditions_and_downstream_producers_for_deadlocks",
            "must_assess_goal_progress_not_only_task_completion",
            "must_name_the_next_causal_input_change_and_measurable_proof",
        }
        # Sets are not JSON serializable; use a stable list in the persisted dossier.
        dossier["investigation_policy"] = sorted(dossier["investigation_policy"])
        return self._compact(dossier)

    def queue(self, *, deviation_id: str, source_job_id: str,
              source_run_id: str | None, task_id: str | None,
              request: dict[str, Any]) -> dict[str, Any]:
        dossier = self.build_dossier(request)
        dossier_hash = digest(dossier)
        triage_run_id = "tri_" + digest({
            "deviation_id": deviation_id, "dossier_hash": dossier_hash,
        })[:32]
        now = utcnow()
        payload = {
            "schema_version": "failure-triage-run-v1",
            "triage_run_id": triage_run_id, "deviation_id": deviation_id,
            "source_job_id": source_job_id, "source_run_id": source_run_id,
            "task_id": task_id, "dossier": dossier,
        }
        inserted = False
        with self.state.transaction() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO failure_triage_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (triage_run_id, deviation_id, source_job_id, source_run_id, task_id,
                 "queued", self.MODEL, None, dossier_hash, canonical(payload), None, now, now),
            ).rowcount == 1
            # ``deviation_id`` is the durable identity in the production
            # schema.  A later observation can produce a different dossier
            # hash (and therefore a different proposed triage_run_id) for the
            # same deviation.  Resolve that uniqueness conflict to the
            # canonical persisted row instead of emitting a phantom queued
            # event and then reading an ID that was never inserted.
            persisted = conn.execute(
                "SELECT triage_run_id FROM failure_triage_runs WHERE deviation_id=?",
                (deviation_id,),
            ).fetchone()
        if persisted is None:
            raise FailureTriageError("triage queue did not persist a canonical row")
        persisted_id = str(persisted["triage_run_id"])
        if inserted:
            self.control.events.append(
                "failure_triage", persisted_id, "failure_triage.queued",
                {"deviation_id": deviation_id, "job_id": source_job_id,
                 "task_id": task_id, "dossier_hash": dossier_hash},
                actor="goal-alignment-controller",
            )
        elif persisted_id != triage_run_id:
            duplicate_payload = {
                "deviation_id": deviation_id, "job_id": source_job_id,
                "task_id": task_id, "suppressed_triage_run_id": triage_run_id,
                "suppressed_dossier_hash": dossier_hash,
            }
            self.control.events.append(
                "failure_triage", persisted_id, "failure_triage.duplicate_suppressed",
                duplicate_payload,
                actor="goal-alignment-controller",
                # Repeated Supervisor cycles over the same stale observation
                # are idempotent at the event ledger too.  This turns the
                # production 22k-event amplification into one truthful audit
                # record per distinct suppressed dossier.
                event_id="evt_" + digest({
                    "event": "failure_triage.duplicate_suppressed",
                    "canonical": persisted_id, "payload": duplicate_payload,
                })[:32],
            )
        return self.get(persisted_id)

    def get(self, triage_run_id: str) -> dict[str, Any]:
        row = self.state._connection().execute(
            "SELECT * FROM failure_triage_runs WHERE triage_run_id=?", (triage_run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(triage_run_id)
        result = dict(row); result["payload"] = json.loads(result["payload"])
        return result

    def rows(self, *, job_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM failure_triage_runs", []
        if job_id:
            query += " WHERE source_job_id=?"; params.append(job_id)
        query += " ORDER BY created_at DESC LIMIT ?"; params.append(min(max(limit, 1), 200))
        rows = []
        for row in self.state._connection().execute(query, tuple(params)):
            item = dict(row); item["payload"] = json.loads(item["payload"]); rows.append(item)
        return rows

    def _set(self, triage_run_id: str, status: str, *, payload: dict[str, Any],
             sandbox: Path | None = None, error: str | None = None) -> None:
        with self.state.transaction() as conn:
            conn.execute(
                "UPDATE failure_triage_runs SET status=?,sandbox_path=?,payload=?,last_error=?,"
                "updated_at=? WHERE triage_run_id=?",
                (status, str(sandbox) if sandbox else None, canonical(payload), error,
                 utcnow(), triage_run_id),
            )

    @classmethod
    def _validate_result(cls, value: dict[str, Any], *, failed_task: str,
                         allowed_tasks: set[str] | None = None) -> dict[str, Any]:
        required = {
            "problem_class", "cause_code", "summary", "causal_chain", "evidence",
            "repair_level", "repair_action", "recovery_task", "risk", "confidence",
            "requires_external_authority", "implementation_targets", "validation_tests",
            "goal_assessment", "causal_input_changes", "proof_contract",
            "actions", "safe_autonomous_actions_remaining",
        }
        if required - set(value):
            raise FailureTriageError(f"triage result missing fields: {sorted(required - set(value))}")
        action, level = str(value["repair_action"]), str(value["repair_level"])
        if action not in cls.ROUTES:
            raise FailureTriageError(f"unsupported triage route: {action}")
        if action.startswith("propose_") and level != "L2":
            raise FailureTriageError("system changes must use L2")
        if action == "propose_code_change" and (
            not value.get("implementation_targets") or not value.get("validation_tests")
        ):
            raise FailureTriageError("coding route requires targets and focused tests")
        assessment = value.get("goal_assessment") or {}
        if (not isinstance(assessment, dict)
                or not isinstance(assessment.get("closer_to_goal"), bool)
                or not assessment.get("why_not_closer")):
            raise FailureTriageError(
                "triage must explain whether and why the result missed the goal"
            )
        if not value.get("causal_input_changes") or not value.get("proof_contract"):
            raise FailureTriageError(
                "triage must specify causal input changes and measurable proof"
            )
        confidence = float(value["confidence"])
        if not 0 <= confidence <= 1 or not value.get("evidence"):
            raise FailureTriageError("triage confidence/evidence is invalid")
        result = dict(value)
        result["recovery_task"] = str(value.get("recovery_task") or failed_task)
        if result["recovery_task"] and not re.fullmatch(
            r"[a-z][a-z0-9_]{0,79}", result["recovery_task"]
        ):
            raise FailureTriageError("recovery_task must be a concrete Workflow task id")
        if (allowed_tasks is not None and result["recovery_task"] not in allowed_tasks):
            if failed_task and failed_task in allowed_tasks:
                result["recovery_task"] = failed_task
            else:
                raise FailureTriageError(
                    "recovery_task is not present in the frozen Workflow DAG"
                )
        if confidence < 0.65:
            result.update({"repair_level": "manual", "repair_action": "request_operator",
                           "requires_external_authority": True, "risk": "high"})
        if result["requires_external_authority"] and action not in {
            "request_operator", "record_evidence_limited"
        }:
            result.update({"repair_level": "manual", "repair_action": "request_operator"})
        return result

    def execute(self, triage_run_id: str) -> dict[str, Any]:
        record = self.get(triage_run_id)
        if record["status"] in self.TERMINAL:
            return record
        if record["status"] == "investigating":
            # A supervisor tick may overlap a long-running Codex investigation.
            # Never delete or restart its frozen sandbox from a second caller.
            return record
        run_dir = self.root / "var/failure-triage" / triage_run_id
        sandbox = run_dir / "repository"
        if sandbox.exists():
            shutil.rmtree(sandbox)
        run_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.root, sandbox, ignore=self._ignore)
        payload = dict(record["payload"])
        dossier_path = sandbox / ".triage/dossier.json"
        dossier_path.parent.mkdir(parents=True, exist_ok=True)
        dossier_path.write_text(
            json.dumps(payload["dossier"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        prior = payload.get("execution") or {}
        payload["execution"] = {
            "attempt": int(prior.get("attempt") or 0) + 1, "started_at": utcnow(),
        }
        self._set(triage_run_id, "investigating", payload=payload, sandbox=sandbox)
        try:
            result = self.runner(sandbox, {
                "run_dir": str(run_dir), "dossier_path": str(dossier_path),
                "dossier": payload["dossier"],
            })
            allowed_tasks = {
                str(item.get("task_id") or "")
                for item in (payload.get("dossier") or {}).get("task_graph") or []
                if item.get("task_id")
            }
            result = self._validate_result(
                result, failed_task=str(record.get("task_id") or ""),
                allowed_tasks=allowed_tasks or None,
            )
            payload.update({"result": result, "completed_at": utcnow()})
            self._set(triage_run_id, "completed", payload=payload, sandbox=sandbox)
            self.control.events.append(
                "failure_triage", triage_run_id, "failure_triage.completed",
                {"problem_class": result["problem_class"],
                 "cause_code": result["cause_code"], "repair_action": result["repair_action"],
                 "risk": result["risk"], "confidence": result["confidence"]},
                actor="failure-triage-agent",
            )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError,
                json.JSONDecodeError) as exc:
            payload["execution"] = {**payload.get("execution", {}), "failed_at": utcnow()}
            self._set(triage_run_id, "failed", payload=payload, sandbox=sandbox,
                      error=str(exc))
            self.control.events.append(
                "failure_triage", triage_run_id, "failure_triage.failed",
                {"error": str(exc)}, actor="failure-triage-agent",
            )
        return self.get(triage_run_id)

    def _run_codex(self, sandbox: Path, request: dict[str, Any]) -> dict[str, Any]:
        run_dir = Path(request["run_dir"])
        output = run_dir / "triage-result.json"
        schema = self.root / "contracts/failure-triage-result-v1.schema.json"
        prompt = (
            "You are the read-only Failure Triage Agent in a governed self-healing system. "
            "Investigate the actual problem from evidence, workflow preconditions, downstream "
            "producers, and source code. A known fingerprint is only a deduplication hint and "
            "must never substitute for causal analysis. Distinguish true evidence scarcity from "
            "a broken evidence path, circular prerequisite, contract mismatch, implementation "
            "defect, transient infrastructure, or a decision requiring external authority. "
            "First answer two separate questions: did the workflow finish, and did its result "
            "move the declared goal forward? For every deviation, explain why it did not, name "
            "the exact causal input to change on the next run, and define baseline/target metrics "
            "plus the artifact that will prove the change worked. A repeated execution with the "
            "same causal inputs is not a repair. "
            "Do not edit files. Do not relax Goal Contracts or invent evidence. Recommend the "
            "smallest route that changes the causal input. Use risk=low only for an invariant-"
            "preserving implementation fix with regression tests; otherwise choose medium or "
            "higher. External authority must stop only the smallest mutating boundary: an "
            "internal implementation defect should still propose autonomous code analysis, "
            "patching, and validation even when promotion or runtime recovery needs approval. "
            "Use actions to decompose compound code, materialization, rewind, and resume work, "
            "and report whether any safe autonomous action remains. Return only the required "
            "structured result. The frozen dossier is at "
            f"{request['dossier_path']}.\n\n"
            + json.dumps(request["dossier"], ensure_ascii=False, indent=2)
        )
        command = [
            shutil.which("codex") or "codex", "exec", "--ephemeral",
            "--sandbox", "read-only", "--model", self.MODEL,
            "-c", 'model_reasoning_effort="high"', "--cd", str(sandbox),
            "--output-schema", str(schema), "--output-last-message", str(output), prompt,
        ]
        completed = subprocess.run(
            command, cwd=sandbox, text=True, capture_output=True, timeout=1200, check=False
        )
        (run_dir / "triage-stdout.log").write_text(
            completed.stdout[-200000:], encoding="utf-8"
        )
        (run_dir / "triage-stderr.log").write_text(
            completed.stderr[-200000:], encoding="utf-8"
        )
        if completed.returncode != 0 or not output.is_file():
            raise FailureTriageError(
                f"triage Agent failed with exit {completed.returncode}: "
                f"{completed.stderr[-2000:]}"
            )
        value = json.loads(output.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise FailureTriageError("triage Agent result is not an object")
        return value
