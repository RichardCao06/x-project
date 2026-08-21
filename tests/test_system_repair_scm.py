from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
from types import SimpleNamespace

from lca_project.control import ControlPlane
from lca_project.kernel.goal_alignment import (
    ChangeController, SystemRepairAgent, SystemRepairScmPublisher,
)


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=True)


def git_project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    remote = tmp_path / "remote.git"
    (root / "config").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src/example.py").write_text("VALUE = 'before'\n", encoding="utf-8")
    (root / "config/system-repair-scm.json").write_text(json.dumps({
        "schema_version": "system-repair-scm-v1",
        "enabled": True,
        "provider": "github",
        "repository": "example/project",
        "remote": "origin",
        "base_branch": "main",
        "branch_prefix": "autofix/system-repair",
        "create_issues": True,
        "create_draft_prs": True,
        "required_for_promotion": False,
        "require_base_head_match": True,
    }), encoding="utf-8")
    run(["git", "init", "-b", "main"], root)
    run(["git", "add", "."], root)
    run([
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "initial",
    ], root)
    run(["git", "init", "--bare", str(remote)], tmp_path)
    run(["git", "remote", "add", "origin", str(remote)], root)
    run(["git", "push", "-u", "origin", "main"], root)
    return root, remote


def test_validated_repair_gets_deduplicated_issue_commit_and_draft_pr(
    tmp_path: Path,
) -> None:
    root, remote = git_project(tmp_path)
    github_commands: list[list[str]] = []

    def command_runner(
        command: list[str], cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        if command[0] != "gh":
            return subprocess.run(
                command, cwd=cwd, text=True, capture_output=True, check=False,
            )
        github_commands.append(command)
        if command[1:3] == ["issue", "list"]:
            stdout = "[]"
        elif command[1:3] == ["issue", "create"]:
            stdout = "https://github.com/example/project/issues/17\n"
        elif command[1:3] == ["pr", "list"]:
            stdout = "[]"
        elif command[1:3] == ["pr", "create"]:
            if len([item for item in github_commands
                    if item[1:3] == ["pr", "create"]]) == 1:
                return subprocess.CompletedProcess(
                    command, 1, "", "temporary GitHub failure"
                )
            stdout = "https://github.com/example/project/pull/23\n"
        else:  # pragma: no cover - makes unexpected provider calls obvious
            return subprocess.CompletedProcess(command, 2, "", "unexpected gh command")
        return subprocess.CompletedProcess(command, 0, stdout, "")

    control = ControlPlane(root)
    candidate = ChangeController(root, control).propose(
        source_deviation_id="dev_scm", target="propose_code_change", risk="low",
        change={"diagnosis": "SEARCH_SELECTION_AUDIT_MISSING"},
        rollback={"strategy": "restore"},
    )
    publisher = SystemRepairScmPublisher(root, control, runner=command_runner)
    agent = SystemRepairAgent(root, control, scm_publisher=publisher)
    repair = agent.queue(
        candidate_id=candidate["candidate_id"], source_job_id="job_scm",
        source_run_id="run_scm", request={"source_failure_fingerprint": "fp_scm"},
    )

    assert repair["payload"]["scm"]["issue_number"] == 17
    assert len([item for item in github_commands if item[1:3] == ["issue", "create"]]) == 1
    publisher.publish_issue(repair, candidate)
    assert len([item for item in github_commands if item[1:3] == ["issue", "create"]]) == 1

    sandbox = tmp_path / "sandbox"
    (sandbox / "src").mkdir(parents=True)
    (sandbox / "src/example.py").write_text("VALUE = 'after'\n", encoding="utf-8")
    deferred = publisher.publish_patch(
        repair, candidate, sandbox=sandbox, changed_files=["src/example.py"],
        patch_hash="a" * 64,
        validations=[{"phase": "sandbox", "passed": True},
                     {"phase": "shadow", "passed": True},
                     {"phase": "canary", "passed": True}],
        base_hashes={"src/example.py":
                     "33c9f262adca46337e8cb12e17a5b3eb17573d2ef47389d4a5a89de8a4104796"},
    )
    assert deferred["status"] == "publication_deferred"
    assert len(str(deferred["commit_sha"])) == 40

    published = publisher.publish_patch(
        repair, candidate, sandbox=sandbox, changed_files=["src/example.py"],
        patch_hash="a" * 64,
        validations=[{"phase": "sandbox", "passed": True},
                     {"phase": "shadow", "passed": True},
                     {"phase": "canary", "passed": True}],
        base_hashes={"src/example.py":
                     "33c9f262adca46337e8cb12e17a5b3eb17573d2ef47389d4a5a89de8a4104796"},
    )

    assert published["status"] == "published"
    assert published["issue_url"].endswith("/issues/17")
    assert published["pr_url"].endswith("/pull/23")
    assert len(str(published["commit_sha"])) == 40
    assert (root / "src/example.py").read_text(encoding="utf-8") == "VALUE = 'before'\n"
    branch = str(published["head_branch"])
    remote_value = run(
        ["git", "--git-dir", str(remote), "show", f"{branch}:src/example.py"], tmp_path
    ).stdout
    assert remote_value == "VALUE = 'after'\n"
    message = run(
        ["git", "--git-dir", str(remote), "show", "-s", "--format=%B", branch], tmp_path
    ).stdout
    assert f"System-Repair-Run: {repair['repair_run_id']}" in message
    assert "Patch-Hash: " + "a" * 64 in message
    pr_create = next(item for item in github_commands if item[1:3] == ["pr", "create"])
    assert "--draft" in pr_create
    assert "Closes #17" in pr_create[pr_create.index("--body") + 1]


def test_required_scm_failure_stops_before_live_promotion(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    control = ControlPlane(root)
    candidate = ChangeController(root, control).propose(
        source_deviation_id="dev_required", target="propose_code_change", risk="low",
        change={"diagnosis": "EXAMPLE"}, rollback={"strategy": "restore"},
    )

    class DeferredPublisher:
        enabled = False
        policy = SimpleNamespace(required_for_promotion=True)

        @staticmethod
        def publish_patch(*_args: object, **_kwargs: object) -> dict[str, str]:
            return {"status": "publication_deferred", "last_error": "remote unavailable"}

    def fake_agent(sandbox: Path, _: dict) -> dict:
        implementation = sandbox / "src/lca_project/repair.py"
        regression = sandbox / "tests/test_repair.py"
        implementation.parent.mkdir(parents=True, exist_ok=True)
        regression.parent.mkdir(parents=True, exist_ok=True)
        implementation.write_text("FIXED = True\n", encoding="utf-8")
        regression.write_text("def test_fixed(): assert True\n", encoding="utf-8")
        return {"summary": "fix", "changed_files": [
            "src/lca_project/repair.py", "tests/test_repair.py",
        ], "tests_added": ["tests/test_repair.py"], "risk_notes": []}

    validator = lambda _root, phase, tests: {
        "phase": phase, "passed": True, "tests": list(tests),
    }
    agent = SystemRepairAgent(
        root, control, agent_runner=fake_agent, validator=validator,
        scm_publisher=DeferredPublisher(),  # type: ignore[arg-type]
    )
    queued = agent.queue(
        candidate_id=candidate["candidate_id"], source_job_id="job_required",
        source_run_id=None, request={"recovery_task": ""},
    )
    result = agent.execute(queued["repair_run_id"])

    assert result["status"] == "awaiting_scm_publication"
    assert result["last_error"] == "remote unavailable"
    assert not (root / "src/lca_project/repair.py").exists()
    assert ChangeController(root, control).get(candidate["candidate_id"])["status"] == "canary_passed"


def test_scm_publishes_pr_to_main_when_running_head_is_ahead_of_remote_base(
    tmp_path: Path,
) -> None:
    root, remote = git_project(tmp_path)
    github_commands: list[list[str]] = []

    def command_runner(
        command: list[str], cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        if command[0] != "gh":
            return subprocess.run(
                command, cwd=cwd, text=True, capture_output=True, check=False,
            )
        github_commands.append(command)
        if command[1:3] == ["issue", "list"]:
            return subprocess.CompletedProcess(command, 0, "[]", "")
        if command[1:3] == ["issue", "create"]:
            return subprocess.CompletedProcess(
                command, 0, "https://github.com/example/project/issues/31\n", ""
            )
        if command[1:3] == ["pr", "list"]:
            return subprocess.CompletedProcess(command, 0, "[]", "")
        if command[1:3] == ["pr", "create"]:
            return subprocess.CompletedProcess(
                command, 0, "https://github.com/example/project/pull/32\n", ""
            )
        return subprocess.CompletedProcess(command, 2, "", "unexpected GitHub operation")

    (root / "local-only.txt").write_text("not on remote\n", encoding="utf-8")
    run(["git", "add", "local-only.txt"], root)
    run([
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "local only",
    ], root)
    control = ControlPlane(root)
    candidate = ChangeController(root, control).propose(
        source_deviation_id="dev_head", target="propose_code_change", risk="low",
        change={"diagnosis": "HEAD_MISMATCH"}, rollback={"strategy": "restore"},
    )
    publisher = SystemRepairScmPublisher(root, control, runner=command_runner)
    agent = SystemRepairAgent(root, control, scm_publisher=publisher)
    repair = agent.queue(
        candidate_id=candidate["candidate_id"], source_job_id="job_head",
        source_run_id=None, request={},
    )
    sandbox = tmp_path / "sandbox-head"
    (sandbox / "src").mkdir(parents=True)
    (sandbox / "src/example.py").write_text("VALUE = 'after'\n", encoding="utf-8")

    result = publisher.publish_patch(
        repair, candidate, sandbox=sandbox, changed_files=["src/example.py"],
        patch_hash="b" * 64, validations=[],
        base_hashes={"src/example.py":
                     "33c9f262adca46337e8cb12e17a5b3eb17573d2ef47389d4a5a89de8a4104796"},
    )

    assert result["status"] == "published"
    assert result["pr_url"].endswith("/pull/32")
    patch = result["payload"]["patch"]
    assert patch["source_head"] != patch["base_head"]
    assert patch["base_is_ancestor"] is True
    assert patch["pr_base"] == "main"
    branch = str(result["head_branch"])
    remote_value = run(
        ["git", "--git-dir", str(remote), "show", f"{branch}:src/example.py"],
        tmp_path,
    ).stdout
    assert remote_value == "VALUE = 'after'\n"
    pr_create = next(item for item in github_commands if item[1:3] == ["pr", "create"])
    assert pr_create[pr_create.index("--base") + 1] == "main"


def test_scm_three_way_applies_only_repair_delta_to_main(tmp_path: Path) -> None:
    root, remote = git_project(tmp_path)
    main_lines = [f"LINE_{index} = {index}\n" for index in range(20)]
    (root / "src/example.py").write_text("".join(main_lines), encoding="utf-8")
    run(["git", "add", "src/example.py"], root)
    run([
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "expand fixture",
    ], root)
    run(["git", "push", "origin", "main"], root)

    branch_lines = list(main_lines)
    branch_lines[1] = "LINE_1 = 'branch-only'\n"
    (root / "src/example.py").write_text("".join(branch_lines), encoding="utf-8")
    run(["git", "add", "src/example.py"], root)
    run([
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "local branch behavior",
    ], root)

    def command_runner(
        command: list[str], cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        if command[0] != "gh":
            return subprocess.run(
                command, cwd=cwd, text=True, capture_output=True, check=False,
            )
        if command[1:3] == ["issue", "list"]:
            return subprocess.CompletedProcess(command, 0, "[]", "")
        if command[1:3] == ["issue", "create"]:
            return subprocess.CompletedProcess(
                command, 0, "https://github.com/example/project/issues/41\n", ""
            )
        if command[1:3] == ["pr", "list"]:
            return subprocess.CompletedProcess(command, 0, "[]", "")
        if command[1:3] == ["pr", "create"]:
            return subprocess.CompletedProcess(
                command, 0, "https://github.com/example/project/pull/42\n", ""
            )
        return subprocess.CompletedProcess(command, 2, "", "unexpected GitHub operation")

    control = ControlPlane(root)
    candidate = ChangeController(root, control).propose(
        source_deviation_id="dev_three_way", target="propose_code_change", risk="low",
        change={"diagnosis": "THREE_WAY"}, rollback={"strategy": "restore"},
    )
    publisher = SystemRepairScmPublisher(root, control, runner=command_runner)
    agent = SystemRepairAgent(root, control, scm_publisher=publisher)
    repair = agent.queue(
        candidate_id=candidate["candidate_id"], source_job_id="job_three_way",
        source_run_id=None, request={},
    )
    repaired_lines = list(branch_lines)
    repaired_lines[18] = "LINE_18 = 'repaired'\n"
    sandbox = tmp_path / "sandbox-three-way"
    (sandbox / "src").mkdir(parents=True)
    (sandbox / "src/example.py").write_text(
        "".join(repaired_lines), encoding="utf-8"
    )
    baseline_hash = hashlib.sha256("".join(branch_lines).encode()).hexdigest()

    result = publisher.publish_patch(
        repair, candidate, sandbox=sandbox, changed_files=["src/example.py"],
        patch_hash="c" * 64, validations=[],
        base_hashes={"src/example.py": baseline_hash},
    )

    assert result["status"] == "published"
    assert result["payload"]["patch"]["merge_evidence"] == [
        {"mode": "three_way", "path": "src/example.py"}
    ]
    branch = str(result["head_branch"])
    remote_value = run(
        ["git", "--git-dir", str(remote), "show", f"{branch}:src/example.py"],
        tmp_path,
    ).stdout.splitlines()
    assert remote_value[1] == "LINE_1 = 1"
    assert remote_value[18] == "LINE_18 = 'repaired'"


def test_required_scm_configuration_requires_draft_prs(tmp_path: Path) -> None:
    root, _remote = git_project(tmp_path)
    config = root / "config/system-repair-scm.json"
    value = json.loads(config.read_text(encoding="utf-8"))
    value["create_draft_prs"] = False
    value["required_for_promotion"] = True
    config.write_text(json.dumps(value), encoding="utf-8")

    control = ControlPlane(root)
    try:
        SystemRepairScmPublisher(root, control)
    except ValueError as exc:
        assert "must create Draft PRs" in str(exc)
    else:  # pragma: no cover - explicit failure reads better than pytest.raises here
        raise AssertionError("required SCM promotion accepted a no-PR policy")
