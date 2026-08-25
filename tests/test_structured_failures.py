from __future__ import annotations

from pathlib import Path
import hashlib
import json
import subprocess
import sys

import pytest

from lca_project.kernel.failures import FailureEnvelope
from lca_project.kernel.repair import RepairAction, RepairPolicyRegistry
from lca_project import capability_runtime


ROOT = Path(__file__).resolve().parents[1]


def test_capability_failure_is_validated_but_cannot_grant_repair_authority() -> None:
    envelope = FailureEnvelope.from_capability({
        "code": "EDITORIAL_LOCAL_ISSUES",
        "category": "content_validation",
        "scope": "section:system_boundary",
        "message": "editorial review returned NO_GO",
        "retryable": False,
        "automatic_repair": "publish_directly",
        "invalidates": ["everything"],
    })
    policy = RepairPolicyRegistry(ROOT / "policies/wiki-repair-policy-v1.json")
    decision = policy.decide(envelope.code, attempt=1, max_attempts=10)
    assert decision.action == RepairAction.REPAIR
    assert decision.repairer_capability == "wiki.content.patch"
    assert "content_compose" in decision.invalidates
    assert "everything" not in decision.invalidates


def test_editorial_no_go_is_a_business_block_not_process_failure(
    tmp_path: Path, monkeypatch
) -> None:
    launcher = tmp_path / "scripts/run_wiki_editorial_review_capture.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("# frozen launcher\n", encoding="utf-8")
    output_dir = tmp_path / "editorial-loop"
    output_dir.mkdir()
    (output_dir / "editorial-review.json").write_text(json.dumps({
        "verdict": "NO_GO", "issues": [{"section": "节点特定采集字段",
        "paragraph_index": 2, "issue_type": "identity_drift"}],
    }), encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 2, stdout='{"verdict":"NO_GO"}', stderr=""
    ))

    result = capability_runtime.agent({
        "phase": "editorial_review", "workspace": str(tmp_path),
        "argv": ["verify", "content", "blueprint", "schema", str(output_dir)],
    })

    assert result["status"] == "blocked"
    assert result["failure"]["code"] == "EDITORIAL_LOCAL_ISSUES"
    assert result["failure"]["category"] == "business_validation"
    assert "identity_drift" in result["failure"]["message"]


def test_content_validation_is_a_business_block_not_process_failure(
    tmp_path: Path, monkeypatch
) -> None:
    launcher = tmp_path / "scripts/run_wiki_content_capture.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("# frozen launcher\n", encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 2, stdout='{"validation_error":"paragraph contract"}', stderr=""
    ))

    result = capability_runtime.agent({
        "phase": "content", "workspace": str(tmp_path), "argv": [],
    })

    assert result["status"] == "blocked"
    assert result["failure"]["code"] == "CONTENT_LOCAL_ISSUES"


def test_nomination_cache_is_bound_to_launcher_code(tmp_path: Path) -> None:
    nomination = tmp_path / "nomination-runtime"
    nomination.mkdir()
    scout = tmp_path / "research-scout.json"
    scout.write_text('{"protocol":"wiki-research-scout-v1"}\n', encoding="utf-8")
    launcher = tmp_path / "run_wiki_nomination_capture.py"
    launcher.write_text("# canonicalizer v1\n", encoding="utf-8")
    (nomination / "nomination-result.json").write_text(json.dumps({
        "claims": [
            {"claim_kind": "external_fact", "believed_source": f"Source {index}"}
            for index in range(3)
        ],
    }), encoding="utf-8")
    (nomination / "wiki-usage-v1.json").write_text("{}\n", encoding="utf-8")
    (nomination / "nomination-usage.json").write_text(json.dumps({
        "exit_code": 0, "validation_error": None,
    }), encoding="utf-8")
    (nomination / "nomination-invocation.json").write_text(json.dumps({
        "research_scout": {"sha256": capability_runtime._sha256(scout)},
        "launcher_sha256": capability_runtime._sha256(launcher),
        "nomination_policy_version": "research-scout-source-specific-v9",
    }), encoding="utf-8")

    assert capability_runtime._nomination_cache_is_current(
        nomination, scout, launcher,
    ) is True

    launcher.write_text("# canonicalizer v2\n", encoding="utf-8")

    assert capability_runtime._nomination_cache_is_current(
        nomination, scout, launcher,
    ) is False


def test_capability_cannot_spoof_adapter_infrastructure_failure() -> None:
    with pytest.raises(ValueError, match="adapter-owned"):
        FailureEnvelope.from_capability({"code": "PROCESS_EXIT", "message": "gate failed"})


def test_unknown_failure_and_exhausted_retry_fail_closed() -> None:
    policy = RepairPolicyRegistry(ROOT / "policies/wiki-repair-policy-v1.json")
    unknown = policy.decide("UNREGISTERED_BUSINESS_FAILURE", attempt=1, max_attempts=10)
    exhausted = policy.decide("TIMEOUT", attempt=2, max_attempts=10)
    assert unknown.action == RepairAction.QUARANTINE
    assert exhausted.action == RepairAction.QUARANTINE
    assert len(policy.policy_hash) == 64


def test_editorial_repairs_are_bounded_before_manual_review() -> None:
    policy = RepairPolicyRegistry(ROOT / "policies/wiki-repair-policy-v1.json")
    repair = policy.decide("EDITORIAL_LOCAL_ISSUES", attempt=2, max_attempts=100)
    exhausted = policy.decide("EDITORIAL_LOCAL_ISSUES", attempt=3, max_attempts=100)

    assert repair.action == RepairAction.REPAIR
    assert repair.invalidates[:2] == ("content_compose", "editorial_review")
    assert exhausted.action == RepairAction.MANUAL_REVIEW


def test_gate_block_is_not_reported_as_reserved_process_exit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 2, stdout="", stderr="source diversity below threshold"
    ))
    result = capability_runtime._run(
        ["gate"], cwd=tmp_path, timeout=1, blocked_code="SOURCE_DIVERSITY_BLOCKED"
    )
    assert result["status"] == "blocked"
    assert result["failure"]["code"] == "SOURCE_DIVERSITY_BLOCKED"
    FailureEnvelope.from_capability(result["failure"])


def test_diversity_repair_scout_excludes_failed_and_oversize_pdf_candidates(tmp_path: Path) -> None:
    batch = tmp_path / "batch"
    cache = batch / "search-cache/fetch"
    cache.mkdir(parents=True)
    (batch / "source-diversity-gate.json").write_text(
        json.dumps({"decision": "BLOCKED"}), encoding="utf-8"
    )
    (cache / "failed.json").write_text(json.dumps({"record": {
        "status": "error", "url": "https://failed.example/manual.pdf"
    }}), encoding="utf-8")
    scout = batch / "research-scout.json"
    scout.write_text(json.dumps({
        "protocol": "wiki-research-scout-v1", "node_id": "A015", "candidates": [
            {"url": "https://failed.example/manual.pdf"},
            {"url": "https://other-pdf.example/guide.pdf"},
            {"url": "https://one.example/page"}, {"url": "https://two.example/page"},
            {"url": "https://three.example/page"},
        ],
    }), encoding="utf-8")
    repaired = capability_runtime._diversity_repair_scout(batch, scout)
    value = json.loads(repaired.read_text(encoding="utf-8"))
    assert repaired.name == "research-scout-diversity-repair.json"
    assert {row["url"] for row in value["candidates"]} == {
        "https://one.example/page", "https://two.example/page", "https://three.example/page",
    }


def test_repair_scout_regeneration_does_not_append_failed_candidates(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "research-plan.json"
    plan.write_text(json.dumps({
        "node_id": "A019",
        "question_contract_sha256": "a" * 64,
        "research_questions": ["identity.activity_definition"],
        "terminology": {"canonical_zh": "服务器", "canonical_en": "server"},
    }), encoding="utf-8")
    config = tmp_path / "search-providers.json"
    config.write_text(json.dumps({"providers": {}, "routing": {}}), encoding="utf-8")
    gate = tmp_path / "source-diversity-gate.json"
    gate.write_text(json.dumps({
        "failed_requirement_ids": ["identity.activity_definition"],
        "attempt": 1, "strategy_hash": "b" * 64,
    }), encoding="utf-8")
    previous = tmp_path / "research-scout.json"
    previous.write_text(json.dumps({
        "protocol": "wiki-research-scout-v1", "node_id": "A019",
        "candidates": [
            {"url": "https://failed.example/page", "title": "Failed",
             "question_id": "identity.activity_definition"},
            {"url": "https://keep.example/page", "title": "Keep",
             "question_id": "unaffected.question"},
        ],
    }), encoding="utf-8")
    fetch_dir = tmp_path / "search-cache/fetch"
    fetch_dir.mkdir(parents=True)
    (fetch_dir / "failed.json").write_text(json.dumps({"record": {
        "status": "empty", "url": "https://failed.example/page",
    }}), encoding="utf-8")
    output = tmp_path / "research-scout-diversity-repair.json"

    completed = subprocess.run([
        sys.executable, str(ROOT / "scripts/scout_wiki_research_plan.py"),
        str(plan), str(config), str(output),
        "--repair-gate", str(gate), "--previous-scout", str(previous),
        "--failed-fetch-dir", str(fetch_dir),
    ], capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr
    repaired = json.loads(output.read_text(encoding="utf-8"))
    urls = {row["url"] for row in repaired["candidates"]}
    assert urls == {"https://keep.example/page"}
    assert repaired["diversity_repair"]["excluded_urls"] == [
        "https://failed.example/page"
    ]
    assert repaired["diversity_repair"]["excluded_url_hashes"] == [
        hashlib.sha256(b"https://failed.example/page").hexdigest()
    ]
