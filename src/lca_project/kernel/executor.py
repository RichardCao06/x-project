"""Allow-listed capability execution in a per-run scratch directory."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys
from typing import Any

from .registry import Capability


class ExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, stdout: str = "", stderr: str = "",
                 failure: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code, self.stdout, self.stderr = code, stdout, stderr
        self.failure = failure


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    payload: dict[str, Any]
    stdout: str
    stderr: str
    workspace: Path


def _validate(schema: dict[str, Any], value: Any, label: str) -> None:
    if not schema:
        return
    required = schema.get("required", [])
    if not isinstance(value, dict) or any(key not in value for key in required):
        raise ExecutionError("OUTPUT_PROTOCOL", f"{label} misses required fields")
    for key, spec in schema.get("properties", {}).items():
        if key in value and spec.get("type") == "object" and not isinstance(value[key], dict):
            raise ExecutionError("OUTPUT_PROTOCOL", f"{label}.{key} must be object")
        if key in value and spec.get("type") == "array" and not isinstance(value[key], list):
            raise ExecutionError("OUTPUT_PROTOCOL", f"{label}.{key} must be array")


class SandboxedExecutor:
    def __init__(self, scratch_root: str | Path, *, protected_roots: tuple[str | Path, ...] = (),
                 project_root: str | Path | None = None,
                 coordination_locks: tuple[str | Path, ...] = ()) -> None:
        self.scratch_root = Path(scratch_root)
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        self.protected_roots = tuple(Path(item).resolve() for item in protected_roots)
        self.project_root = Path(project_root).resolve() if project_root else self.scratch_root.resolve().parent
        self.coordination_locks = tuple(Path(item).resolve() for item in coordination_locks)

    @contextmanager
    def _binding_boundary(self):
        """Hold shared binding locks for the complete snapshot/execute/check cycle."""
        handles = []
        try:
            for lock in sorted(self.coordination_locks):
                lock.parent.mkdir(parents=True, exist_ok=True)
                handle = lock.open("a+b")
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                handles.append(handle)
            yield
        finally:
            for handle in reversed(handles):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    @staticmethod
    def _snapshot(roots: tuple[Path, ...], exclude: Path | None = None) -> dict[Path, bytes]:
        snapshot: dict[Path, bytes] = {}
        for root in roots:
            if root.is_file():
                snapshot[root] = root.read_bytes()
            elif root.is_dir():
                for path in root.rglob("*"):
                    if exclude is not None and path.resolve().is_relative_to(exclude):
                        continue
                    if path.is_file() and not path.is_symlink():
                        snapshot[path] = path.read_bytes()
        return snapshot

    @staticmethod
    def _restore(roots: tuple[Path, ...], before: dict[Path, bytes], exclude: Path | None = None) -> None:
        current = SandboxedExecutor._snapshot(roots, exclude)
        for path in current.keys() - before.keys():
            path.unlink(missing_ok=True)
        for path, content in before.items():
            if current.get(path) != content:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

    def execute(self, capability: Capability, inputs: dict[str, Any], *, run_id: str, task_id: str) -> ExecutionResult:
        with self._binding_boundary():
            return self._execute_bound(
                capability, inputs, run_id=run_id, task_id=task_id
            )

    def _execute_bound(self, capability: Capability, inputs: dict[str, Any], *,
                       run_id: str, task_id: str) -> ExecutionResult:
        _validate(capability.input_schema, inputs, "input")
        if capability.side_effects == "none" and not self.protected_roots:
            raise ExecutionError("POLICY", "side_effects=none requires an explicit protected_roots boundary")
        with tempfile.TemporaryDirectory(prefix=f"{run_id}-{task_id}-", dir=self.scratch_root) as temp:
            workspace = Path(temp)
            input_path, output_path = workspace / "input.json", workspace / "output.json"
            input_path.write_text(json.dumps(inputs, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            replacements = {"{input}": str(input_path), "{output}": str(output_path), "{workspace}": str(workspace),
                            "{python}": sys.executable}
            command = [replacements.get(part, part) for part in capability.command]
            if command and command[0] == "{agent_runtime}":
                raise ExecutionError("POLICY", "agent capability requires the attested agent runtime")
            if len(command) > 1 and not Path(command[1]).is_absolute() and (self.project_root / command[1]).is_file():
                command[1] = str((self.project_root / command[1]).resolve())
            before = self._snapshot(self.protected_roots, self.scratch_root.resolve()) if capability.side_effects == "none" else {}
            try:
                env = os.environ.copy()
                # The conformance fixture copies only declarative project assets;
                # bind the runtime package that owns this executor, not the fixture.
                source_root = str(Path(__file__).resolve().parents[2])
                env["PYTHONPATH"] = source_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
                # Read-only capabilities may import validators from protected
                # protocol roots. Bytecode caches are still filesystem writes
                # and would correctly trip the side-effect guard.
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                proc = subprocess.run(command, cwd=workspace, text=True, capture_output=True,
                                      timeout=capability.timeout_seconds, check=False, env=env)
            except subprocess.TimeoutExpired as exc:
                raise ExecutionError("TIMEOUT", f"{capability.id} timed out", stdout=exc.stdout or "", stderr=exc.stderr or "") from exc
            after = self._snapshot(self.protected_roots, self.scratch_root.resolve()) if capability.side_effects == "none" else before
            if after != before:
                changed = sorted(
                    str(path) for path in set(before) | set(after)
                    if before.get(path) != after.get(path)
                )
                self._restore(self.protected_roots, before, self.scratch_root.resolve())
                detail = ", ".join(changed[:5])
                raise ExecutionError(
                    "SIDE_EFFECT",
                    f"{capability.id} modified a protected root: {detail}",
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                )
            if proc.returncode != 0:
                raise ExecutionError("PROCESS_EXIT", f"{capability.id} exited {proc.returncode}", stdout=proc.stdout, stderr=proc.stderr)
            if not output_path.is_file():
                raise ExecutionError("OUTPUT_PROTOCOL", "capability did not produce output.json", stdout=proc.stdout, stderr=proc.stderr)
            try:
                payload = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ExecutionError("OUTPUT_PROTOCOL", "output.json is not valid JSON", stdout=proc.stdout, stderr=proc.stderr) from exc
            _validate(capability.output_schema, payload, "output")
            if payload.get("status") not in {"ok", "blocked", "failed"}:
                raise ExecutionError("OUTPUT_PROTOCOL", "output.status must be ok, blocked or failed", stdout=proc.stdout, stderr=proc.stderr)
            # Persisting artifacts is deliberately a separate CAS transaction; this temp
            # directory disappears after return and cannot mutate the release workspace.
            return ExecutionResult(str(payload["status"]), payload, proc.stdout, proc.stderr, workspace)
