"""Uniform Capability.v1 adapter for frozen production tools.

Every invocation consumes one input JSON and produces one output JSON.  The
adapter never guesses script order; the Workflow task supplies an operation
and only allow-listed operation/argument shapes are accepted.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


class CapabilityAdapterError(RuntimeError):
    pass


WIKI_OPERATIONS = {
    "plan", "prepare", "validate", "research-ready", "verify", "finalize",
    "apply", "preview", "go-no-go", "gate", "publish", "content-blueprint",
}


def _run(command: list[str], *, cwd: Path, timeout: int) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True,
                               timeout=timeout, check=False)
    if completed.returncode:
        return {"status": "failed", "failure": {"code": "PROCESS_EXIT", "returncode": completed.returncode,
                "stderr": completed.stderr[-8000:]}, "stdout": completed.stdout[-8000:]}
    return {"status": "ok", "stdout": completed.stdout[-8000:], "stderr": completed.stderr[-8000:]}


def _path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CapabilityAdapterError(f"{label} must be a path string")
    return Path(value).resolve()


def wiki_batch(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("operation") == "probe":
        return {"status": "ok", "adapter": "wiki.batch", "operations": sorted(WIKI_OPERATIONS)}
    operation = str(value.get("operation", ""))
    if operation not in WIKI_OPERATIONS:
        raise CapabilityAdapterError(f"unsupported wiki.batch operation: {operation}")
    workspace = _path(value.get("workspace"), "workspace")
    if operation == "content-blueprint":
        argv = value.get("argv")
        if not isinstance(argv, list) or len(argv) != 3 or not all(isinstance(item, str) for item in argv):
            raise CapabilityAdapterError("content-blueprint argv must be [graph,node_id,output]")
        if any(item.startswith("/") and not Path(item).resolve().is_relative_to(workspace) for item in argv):
            raise CapabilityAdapterError("content-blueprint argv escapes workspace")
        builder = Path(__file__).resolve().parents[2] / "scripts" / "build_wiki_content_blueprint.py"
        return _run([sys.executable, str(builder), *argv], cwd=workspace,
                    timeout=int(value.get("timeout_seconds", 1800)))
    script = workspace / "scripts" / "wiki_batch.py"
    if not script.is_file():
        raise CapabilityAdapterError("workspace has no frozen wiki_batch.py")
    argv = value.get("argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise CapabilityAdapterError("argv must be a string list")
    if any(item.startswith("/") and not Path(item).resolve().is_relative_to(workspace) for item in argv):
        raise CapabilityAdapterError("wiki.batch argv escapes workspace")
    return _run([sys.executable, str(script), operation, *argv], cwd=workspace,
                timeout=int(value.get("timeout_seconds", 1800)))


AGENT_LAUNCHERS = {
    "nomination": "run_wiki_nomination_capture.py",
    "verify": "run_wiki_verify_capture.py",
    "content": "run_wiki_content_capture.py",
    "editorial_review": "run_wiki_editorial_review_capture.py",
}


def agent(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("operation") == "probe":
        return {"status": "ok", "adapter": "agent-runtime", "launchers": sorted(AGENT_LAUNCHERS)}
    phase = str(value.get("phase", ""))
    launcher = AGENT_LAUNCHERS.get(phase)
    if launcher is None:
        raise CapabilityAdapterError(f"unsupported agent phase: {phase}")
    workspace = _path(value.get("workspace"), "workspace")
    script = workspace / "scripts" / launcher
    argv = value.get("argv")
    if not script.is_file() or not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise CapabilityAdapterError("invalid frozen agent launcher or argv")
    if any(item.startswith("/") and not Path(item).resolve().is_relative_to(workspace) for item in argv):
        raise CapabilityAdapterError("agent argv escapes workspace")
    result = _run([sys.executable, str(script), *argv], cwd=workspace,
                  timeout=int(value.get("timeout_seconds", 1800)))
    if result["status"] == "ok":
        result["attestation_required"] = True
    return result


def release(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("operation") == "probe":
        return {"status": "ok", "adapter": "release.apply", "authority": "ReleaseManager"}
    # Production release is deliberately not a shell escape.  A caller must
    # provide a persisted eligibility receipt; the job-driven release service
    # consumes it.  Until then fail closed instead of calling a legacy publish.
    if not isinstance(value.get("eligibility_receipt"), dict):
        return {"status": "blocked", "failure": {"code": "RELEASE_ELIGIBILITY_REQUIRED"}}
    return {"status": "blocked", "failure": {"code": "RELEASE_SERVICE_REQUIRED",
            "message": "use the job-driven ReleaseManager service"}}


HANDLERS = {"wiki.batch": wiki_batch, "agent.propose": agent, "agent.review": agent,
            "release.apply": release}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("capability", choices=sorted(HANDLERS))
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        value = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise CapabilityAdapterError("input must be an object")
        output = HANDLERS[args.capability](value)
    except (OSError, ValueError, CapabilityAdapterError, subprocess.TimeoutExpired) as exc:
        output = {"status": "failed", "failure": {"code": type(exc).__name__, "message": str(exc)}}
    args.output.write_text(json.dumps(output, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
