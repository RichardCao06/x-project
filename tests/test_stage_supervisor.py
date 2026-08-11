from __future__ import annotations

import json
from pathlib import Path
import sys

from lca_project.kernel.stage_supervisor import StageSupervisor


def write_plan(path: Path, *, stage_id: str, workspace: Path, commands: list[dict], limits: dict | None = None) -> Path:
    path.write_text(json.dumps({"stage_id": stage_id, "workspace": str(workspace),
                                "limits": limits or {}, "commands": commands}), encoding="utf-8")
    return path


def test_supervisor_runs_a_batch_in_one_call_and_skips_hash_valid_outputs(tmp_path: Path) -> None:
    root, workspace = tmp_path / "project", tmp_path / "workspace"
    root.mkdir(); workspace.mkdir()
    output = workspace / "done.txt"
    plan = write_plan(tmp_path / "plan.json", stage_id="stage-ok", workspace=workspace, commands=[{
        "id": "one", "argv": [sys.executable, "-c", f"from pathlib import Path; Path({str(output)!r}).write_text('ok')"],
        "expected_outputs": [str(output)], "kind": "process",
    }])
    first = StageSupervisor(root).run(plan)
    second = StageSupervisor(root).run(plan)
    assert first["status"] == second["status"] == "succeeded"
    assert first["counters"] == {"model_calls": 0, "process_calls": 1, "compactions": 0}
    assert second["counters"]["process_calls"] == 0
    assert second["completed_commands"][0]["status"] == "skipped_existing"
    assert Path(first["checkpoint_path"]).is_file()


def test_supervisor_hard_stops_before_101st_model_call(tmp_path: Path) -> None:
    root, workspace = tmp_path / "project", tmp_path / "workspace"
    root.mkdir(); workspace.mkdir()
    command = {"argv": [sys.executable, "-c", "pass"], "kind": "model"}
    commands = [{"id": f"call-{number}", **command} for number in range(101)]
    plan = write_plan(tmp_path / "plan.json", stage_id="stage-budget", workspace=workspace,
                      commands=commands, limits={"max_model_calls": 100, "max_processes": 200})
    report = StageSupervisor(root).run(plan)
    assert report["status"] == "checkpointed"
    assert report["reason"] == "model-call budget exceeded"
    assert report["counters"]["model_calls"] == 100
    assert report["next_command"] == "call-100"


def test_compaction_signal_checkpoints_without_launching_process(tmp_path: Path) -> None:
    root, workspace = tmp_path / "project", tmp_path / "workspace"
    root.mkdir(); workspace.mkdir()
    plan = write_plan(tmp_path / "plan.json", stage_id="stage-compact", workspace=workspace,
                      commands=[{"id": "never", "argv": [sys.executable, "-c", "raise SystemExit(99)"]}])
    report = StageSupervisor(root).run(plan, compactions_observed=1)
    assert report["status"] == "checkpointed"
    assert report["reason"] == "compaction budget exceeded"
    assert report["counters"]["process_calls"] == 0


def test_retry_is_internal_and_bounded(tmp_path: Path) -> None:
    root, workspace = tmp_path / "project", tmp_path / "workspace"
    root.mkdir(); workspace.mkdir()
    plan = write_plan(tmp_path / "plan.json", stage_id="stage-fail", workspace=workspace,
                      commands=[{"id": "bad", "argv": [sys.executable, "-c", "raise SystemExit(3)"],
                                 "max_attempts": 2}])
    report = StageSupervisor(root).run(plan)
    assert report["status"] == "failed"
    assert report["counters"]["process_calls"] == 2
    assert len(report["completed_commands"]) == 2


def test_batched_worker_reserves_its_worst_case_internal_model_calls(tmp_path: Path) -> None:
    root, workspace = tmp_path / "project", tmp_path / "workspace"
    root.mkdir(); workspace.mkdir()
    plan = write_plan(tmp_path / "plan.json", stage_id="stage-units", workspace=workspace,
                      commands=[{"id": "bounded-loop", "argv": [sys.executable, "-c", "pass"],
                                 "kind": "model", "model_call_units": 4}],
                      limits={"max_model_calls": 3})
    report = StageSupervisor(root).run(plan)
    assert report["status"] == "checkpointed"
    assert report["counters"]["process_calls"] == 0


def test_frozen_input_drift_fails_before_process_launch(tmp_path: Path) -> None:
    root, workspace = tmp_path / "project", tmp_path / "workspace"
    root.mkdir(); workspace.mkdir()
    frozen = tmp_path / "input.json"; frozen.write_text("original")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"stage_id": "stage-drift", "workspace": str(workspace),
        "frozen_inputs": {str(frozen): "0" * 64},
        "commands": [{"id": "never", "argv": [sys.executable, "-c", "pass"]}]}), encoding="utf-8")
    try:
        StageSupervisor(root).run(plan_path)
    except ValueError as exc:
        assert "frozen input hash drift" in str(exc)
    else:
        raise AssertionError("hash drift must fail closed")
