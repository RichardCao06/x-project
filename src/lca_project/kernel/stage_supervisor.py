"""Bounded, non-polling execution of an autonomous workflow stage."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess
import time
import re
from typing import Any

from lca_project.control import ControlPlane
from .state import utcnow


@dataclass(frozen=True)
class StageLimits:
    max_model_calls: int = 100
    max_processes: int = 100
    max_elapsed_seconds: int = 7200
    max_compactions: int = 0

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "StageLimits":
        raw = value or {}
        defaults = cls()
        result = cls(**{key: int(raw.get(key, getattr(defaults, key))) for key in cls.__dataclass_fields__})
        if any(getattr(result, key) < 0 for key in cls.__dataclass_fields__):
            raise ValueError("stage limits must be non-negative")
        return result


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StageSupervisor:
    """Own all child waits; callers receive one terminal summary, never a poll handle."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.control = ControlPlane(self.root)
        self.state = self.control.state
        self.state._connection().executescript("""
        CREATE TABLE IF NOT EXISTS stage_executions(
          stage_id TEXT PRIMARY KEY, run_id TEXT, status TEXT NOT NULL,
          plan_hash TEXT NOT NULL, model_calls INTEGER NOT NULL DEFAULT 0,
          process_calls INTEGER NOT NULL DEFAULT 0, compactions INTEGER NOT NULL DEFAULT 0,
          started_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
          checkpoint_hash TEXT, summary TEXT NOT NULL DEFAULT '{}');
        """)

    def _finish(self, *, stage_id: str, run_id: str | None, status: str, reason: str,
                plan_hash: str, workspace: Path, records: list[dict[str, Any]],
                counters: dict[str, int], next_command: str | None) -> dict[str, Any]:
        payload = {
            "protocol": "stage-checkpoint-v1", "stage_id": stage_id, "run_id": run_id,
            "status": status, "reason": reason, "plan_hash": plan_hash,
            "workspace": str(workspace), "completed_commands": records,
            "counters": counters, "next_command": next_command, "created_at": utcnow(),
            "handoff": "open a fresh Codex task and rerun this frozen plan; hash-valid outputs are skipped",
        }
        artifact = self.control.artifacts.put_json(payload, metadata={"schema": "stage-checkpoint-v1"})
        safe_stage_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", stage_id).strip("-") or "stage"
        checkpoint_path = workspace / f"stage-checkpoint-{safe_stage_id}.json"
        checkpoint_path.write_text(json.dumps({**payload, "checkpoint_hash": artifact.digest}, ensure_ascii=False,
                                              indent=2, sort_keys=True) + "\n", encoding="utf-8")
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute("""UPDATE stage_executions SET status=?,model_calls=?,process_calls=?,compactions=?,
                         updated_at=?,finished_at=?,checkpoint_hash=?,summary=? WHERE stage_id=?""",
                         (status, counters["model_calls"], counters["process_calls"], counters["compactions"],
                          now, now, artifact.digest, json.dumps(payload, ensure_ascii=False, sort_keys=True), stage_id))
        self.control.events.append("stage", stage_id, "stage.finished", {
            "status": status, "reason": reason, "checkpoint_hash": artifact.digest,
        }, actor="stage-supervisor")
        return {**payload, "checkpoint_hash": artifact.digest, "checkpoint_path": str(checkpoint_path)}

    def run(self, plan_path: str | Path, *, compactions_observed: int = 0) -> dict[str, Any]:
        plan_path = Path(plan_path).resolve()
        plan_bytes = plan_path.read_bytes()
        plan = json.loads(plan_bytes)
        if not isinstance(plan, dict) or not isinstance(plan.get("commands"), list):
            raise ValueError("stage plan requires a commands array")
        stage_id = str(plan.get("stage_id", "")).strip()
        if not stage_id:
            raise ValueError("stage plan requires stage_id")
        run_id = str(plan["run_id"]) if plan.get("run_id") else None
        workspace = Path(plan.get("workspace", plan_path.parent)).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        frozen_inputs = plan.get("frozen_inputs", {})
        if not isinstance(frozen_inputs, dict):
            raise ValueError("frozen_inputs must be a path-to-sha256 object")
        for raw_path, expected_hash in frozen_inputs.items():
            frozen_path = Path(raw_path).resolve()
            if not frozen_path.is_file() or _hash_file(frozen_path) != expected_hash:
                raise ValueError(f"frozen input hash drift: {frozen_path}")
        limits = StageLimits.from_mapping(plan.get("limits"))
        plan_hash = hashlib.sha256(plan_bytes).hexdigest()
        existing = self.state._connection().execute(
            "SELECT * FROM stage_executions WHERE stage_id=?", (stage_id,)).fetchone()
        if existing and existing["plan_hash"] != plan_hash:
            raise ValueError("stage_id already exists with a different frozen plan")
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute("""INSERT INTO stage_executions
              (stage_id,run_id,status,plan_hash,model_calls,process_calls,compactions,started_at,updated_at,summary)
              VALUES(?,?,?,?,0,0,?,?,?,'{}')
              ON CONFLICT(stage_id) DO UPDATE SET status='running',updated_at=excluded.updated_at""",
                         (stage_id, run_id, "running", plan_hash, compactions_observed, now, now))
        records: list[dict[str, Any]] = []
        counters = {"model_calls": 0, "process_calls": 0, "compactions": compactions_observed}
        started = time.monotonic()
        commands = plan["commands"]

        def stop(status: str, reason: str, index: int) -> dict[str, Any]:
            next_id = str(commands[index].get("id")) if index < len(commands) else None
            return self._finish(stage_id=stage_id, run_id=run_id, status=status, reason=reason,
                                plan_hash=plan_hash, workspace=workspace, records=records,
                                counters=counters, next_command=next_id)

        if compactions_observed > limits.max_compactions:
            return stop("checkpointed", "compaction budget exceeded", 0)
        for index, item in enumerate(commands):
            if not isinstance(item, dict) or not isinstance(item.get("argv"), list) or not item.get("id"):
                raise ValueError(f"invalid command at index {index}")
            command_id = str(item["id"])
            expected = [Path(value).resolve() for value in item.get("expected_outputs", [])]
            if expected and all(path.is_file() for path in expected):
                records.append({"id": command_id, "status": "skipped_existing",
                                "outputs": {str(path): _hash_file(path) for path in expected}})
                continue
            kind = str(item.get("kind", "process"))
            model_units = int(item.get("model_call_units", 1 if kind == "model" else 0))
            if model_units < 0:
                raise ValueError(f"negative model_call_units for {command_id}")
            attempts = max(1, int(item.get("max_attempts", 1)))
            for attempt in range(1, attempts + 1):
                elapsed = int(time.monotonic() - started)
                proposed_models = counters["model_calls"] + model_units
                if proposed_models > limits.max_model_calls:
                    return stop("checkpointed", "model-call budget exceeded", index)
                if counters["process_calls"] + 1 > limits.max_processes:
                    return stop("checkpointed", "process-call budget exceeded", index)
                if elapsed >= limits.max_elapsed_seconds:
                    return stop("checkpointed", "elapsed-time budget exceeded", index)
                counters["model_calls"], counters["process_calls"] = proposed_models, counters["process_calls"] + 1
                timeout = min(int(item.get("timeout_seconds", 1800)), limits.max_elapsed_seconds - elapsed)
                command_started = time.monotonic()
                try:
                    completed = subprocess.run([str(value) for value in item["argv"]], cwd=workspace,
                                               stdin=subprocess.DEVNULL, text=True, capture_output=True,
                                               check=False, timeout=max(1, timeout))
                    returncode, timeout_hit = completed.returncode, False
                    stdout, stderr = completed.stdout[-8000:], completed.stderr[-8000:]
                except subprocess.TimeoutExpired as exc:
                    returncode, timeout_hit = 124, True
                    stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                    stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                    stdout, stderr = stdout[-8000:], stderr[-8000:]
                record = {"id": command_id, "kind": kind, "attempt": attempt,
                          "returncode": returncode, "timeout": timeout_hit,
                          "elapsed_seconds": round(time.monotonic() - command_started, 3),
                          "stdout_tail": stdout, "stderr_tail": stderr}
                records.append(record)
                with self.state.transaction() as conn:
                    conn.execute("""UPDATE stage_executions SET model_calls=?,process_calls=?,compactions=?,
                                 updated_at=?,summary=? WHERE stage_id=?""",
                                 (counters["model_calls"], counters["process_calls"], counters["compactions"],
                                  utcnow(), json.dumps({"records": records}, ensure_ascii=False), stage_id))
                if returncode == 0 and (not expected or all(path.is_file() for path in expected)):
                    if expected:
                        record["outputs"] = {str(path): _hash_file(path) for path in expected}
                    break
                if attempt == attempts:
                    return stop("failed", f"command failed: {command_id}", index)
        return stop("succeeded", "all commands completed", len(commands))
