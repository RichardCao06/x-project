from __future__ import annotations

import sys
from pathlib import Path

import pytest

from lca_project.kernel.executor import ExecutionError, SandboxedExecutor
from lca_project.kernel.registry import Capability, CapabilityRegistry, RegistryError
from lca_project.kernel.release import ReleaseError, ReleaseManager
from lca_project.kernel.repair import RepairAction, RepairRouter
from lca_project.kernel.workflow import TaskState, WorkflowError, WorkflowRun, WorkflowSpec, compile_workflow


def capability(command: list[str], **changes: object) -> Capability:
    raw = {"id": "test", "version": "1", "command": command, "timeout_seconds": 1,
           "input_schema": {}, "output_schema": {"required": ["status"]}}
    raw.update(changes)
    return Capability.from_mapping(raw)


def test_cap_001_unknown_capability_fails_workflow_compile() -> None:
    spec = WorkflowSpec.from_mapping({"id": "wf", "version": "1", "tasks": [{"id": "a", "capability": "absent"}]})
    with pytest.raises(WorkflowError, match="unknown capabilities"):
        compile_workflow(spec, {"registered"})


def test_cap_003_bad_output_is_not_accepted(project_root: Path) -> None:
    script_path = project_root / "bad_output.py"
    script_path.write_text("import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('[]')")
    runner = SandboxedExecutor(project_root / "scratch", protected_roots=(project_root,))
    with pytest.raises(ExecutionError, match="misses required"):
        runner.execute(capability([sys.executable, str(script_path), "{output}"]), {}, run_id="run", task_id="task")


def test_cap_005_timeout_is_reported_as_failure(project_root: Path) -> None:
    runner = SandboxedExecutor(project_root / "scratch", protected_roots=(project_root,))
    with pytest.raises(ExecutionError) as failure:
        runner.execute(capability([sys.executable, "-c", "import time; time.sleep(2)"]), {}, run_id="run", task_id="task")
    assert failure.value.code == "TIMEOUT"


def test_cap_006_sandbox_has_no_release_workspace(project_root: Path) -> None:
    script_path = project_root / "ok_output.py"
    script_path.write_text("import json,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(status='ok',cwd=str(pathlib.Path.cwd()))))")
    result = SandboxedExecutor(project_root / "scratch", protected_roots=(project_root,)).execute(
        capability([sys.executable, str(script_path), "{output}"]), {}, run_id="run", task_id="task")
    assert str(project_root) in result.payload["cwd"]
    assert not (project_root / "release-target").exists()


def test_cap_008_registry_loads_all_declared_capabilities() -> None:
    root = Path(__file__).resolve().parents[1]
    registry = CapabilityRegistry.load_directory(root / "capabilities")
    assert len(registry.all()) >= 6
    assert {item.side_effects for item in registry.all()} <= {"none", "staged"}


def test_wf_001_cycle_and_unknown_dependency_are_rejected() -> None:
    circular = WorkflowSpec.from_mapping({"id": "wf", "version": "1", "tasks": [
        {"id": "a", "capability": "x", "depends_on": ["b"]}, {"id": "b", "capability": "x", "depends_on": ["a"]}]})
    with pytest.raises(WorkflowError, match="cycle"):
        compile_workflow(circular, {"x"})
    dangling = WorkflowSpec.from_mapping({"id": "wf", "version": "1", "tasks": [{"id": "a", "capability": "x", "depends_on": ["b"]}]})
    with pytest.raises(WorkflowError, match="unknown task dependencies"):
        compile_workflow(dangling, {"x"})


def test_wf_004_failing_task_does_not_mutate_sibling_state() -> None:
    spec = WorkflowSpec.from_mapping({"id": "wf", "version": "1", "tasks": [
        {"id": "left", "capability": "x"}, {"id": "right", "capability": "x"}]})
    run = WorkflowRun(compile_workflow(spec, {"x"}))
    assert run.claim_ready() == ("left", "right")
    run.transition("left", TaskState.RUNNING); run.transition("left", TaskState.FAILED)
    assert run.states["right"] == TaskState.READY


def test_wf_006_repair_router_quarantines_after_retry_limit() -> None:
    decision = RepairRouter().decide("TIMEOUT", attempt=3, max_attempts=3)
    assert decision.action == RepairAction.QUARANTINE


def test_wf_007_deterministic_compilation_replays_same_order() -> None:
    spec = WorkflowSpec.from_mapping({"id": "wf", "version": "1", "tasks": [
        {"id": "b", "capability": "x", "depends_on": ["a"]}, {"id": "a", "capability": "x"}]})
    assert compile_workflow(spec, {"x"}).order == compile_workflow(spec, {"x"}).order == ("a", "b")


def test_rel_002_stage_hash_lock_rejects_tampering(project_root: Path) -> None:
    manager = ReleaseManager(project_root / "releases", required_gates=set())
    staged = manager.stage({"page.md": b"version one"}, expected_current={"page.md": None})
    (staged.root / "page.md").write_bytes(b"altered")
    with pytest.raises(ReleaseError, match="hash lock"):
        manager.apply(staged, project_root / "destination")


def test_rel_004_stage_does_not_touch_destination(project_root: Path) -> None:
    manager = ReleaseManager(project_root / "releases")
    staged = manager.stage({"preview/index.html": b"preview"})
    destination = project_root / "production"
    assert not destination.exists()
    assert (staged.root / "preview/index.html").read_bytes() == b"preview"


def test_rel_007_rejects_path_traversal(project_root: Path) -> None:
    with pytest.raises(ReleaseError, match="relative"):
        ReleaseManager(project_root / "releases").stage({"../escape": b"x"})
