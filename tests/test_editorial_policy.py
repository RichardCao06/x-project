from __future__ import annotations

import json
from pathlib import Path

import pytest

from lca_project import capability_runtime
from lca_project.domains.editorial_policy import (
    apply_editorial_policy,
    classify_editorial_review,
)
from wiki_content_enrich import validate_editorial_policy


def review(issue_type: str) -> dict:
    return {
        "protocol": "wiki-editorial-review-v1", "node_id": "A040",
        "verdict": "NO_GO", "reviewed_sections": ["定义"],
        "checks": {"paragraph_focus": False, "redundancy_control": False},
        "issues": [{"section": "定义", "paragraph_index": 1,
                    "issue_type": issue_type, "explanation": "具体问题说明",
                    "repair_instruction": "具体修复指令"}],
    }


def test_preview_blocks_evidence_risk_but_not_style_advisory() -> None:
    assert classify_editorial_review(review("identity_drift"), "preview")["decision"] == "block"
    assert classify_editorial_review(review("citation_intrusion"), "preview")["decision"] == "block"
    assert classify_editorial_review(review("redundant"), "preview")["decision"] == "accept_with_advisories"
    assert classify_editorial_review(review("unsupported_fusion"), "reviewed")["decision"] == "block"


def test_preview_advisories_preserve_raw_review_and_are_reusable(tmp_path: Path) -> None:
    review_path = tmp_path / "editorial-review.json"
    content_path = tmp_path / "content.json"
    usage_path = tmp_path / "editorial-review-usage.json"
    review_path.write_text(json.dumps(review("redundant"), ensure_ascii=False), encoding="utf-8")
    content_path.write_text('{"protocol":"wiki-content-draft-v2"}', encoding="utf-8")
    usage_path.write_text(json.dumps({"exit_code": 2, "verdict": "NO_GO", "artifacts": {}}),
                          encoding="utf-8")

    result = apply_editorial_policy(review_path, content_path, "preview", usage_path=usage_path)

    assert result["decision"] == "accept_with_advisories"
    normalized = json.loads(review_path.read_text(encoding="utf-8"))
    assert normalized["verdict"] == "NO_GO"
    assert normalized["issues"][0]["issue_type"] == "redundant"
    advisories = json.loads((tmp_path / "editorial-advisories.json").read_text(encoding="utf-8"))
    assert advisories["issues"][0]["issue_type"] == "redundant"
    decision = json.loads((tmp_path / "editorial-policy-decision.json").read_text(encoding="utf-8"))
    assert decision["decision"] == "accept_with_advisories"
    usage = json.loads(usage_path.read_text(encoding="utf-8"))
    assert usage["exit_code"] == 2
    assert usage["advisory_count"] == 1
    assert usage["policy_decision"] == "accept_with_advisories"


def _bound_policy(tmp_path: Path, issue_type: str = "redundant") -> tuple[Path, Path, Path]:
    review_path = tmp_path / "editorial-review.json"
    content_path = tmp_path / "content.json"
    review_path.write_text(json.dumps(review(issue_type), ensure_ascii=False), encoding="utf-8")
    content_path.write_text('{"protocol":"wiki-content-draft-v2"}', encoding="utf-8")
    apply_editorial_policy(review_path, content_path, "preview")
    return content_path, review_path, tmp_path / "editorial-policy-decision.json"


def test_downstream_accepts_hash_bound_preview_advisories(tmp_path: Path) -> None:
    content_path, review_path, policy_path = _bound_policy(tmp_path)

    raw_review, decision = validate_editorial_policy(
        content_path, {"node_id": "A040"}, review_path, policy_path, "preview",
    )

    assert raw_review["verdict"] == "NO_GO"
    assert decision["decision"] == "accept_with_advisories"


@pytest.mark.parametrize("tampered_input", ["content", "review"])
def test_downstream_rejects_stale_editorial_policy_hashes(
    tmp_path: Path, tampered_input: str,
) -> None:
    content_path, review_path, policy_path = _bound_policy(tmp_path)
    target = content_path if tampered_input == "content" else review_path
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="哈希绑定"):
        validate_editorial_policy(
            content_path, {"node_id": "A040"}, review_path, policy_path, "preview",
        )


def test_reviewed_mode_rejects_preview_advisory_decision(tmp_path: Path) -> None:
    content_path, review_path, policy_path = _bound_policy(tmp_path)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["publication_mode"] = "reviewed"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")

    with pytest.raises(ValueError, match="哈希绑定"):
        validate_editorial_policy(
            content_path, {"node_id": "A040"}, review_path, policy_path, "reviewed",
        )


def test_reviewed_mode_still_requires_clean_raw_go(tmp_path: Path) -> None:
    review_path = tmp_path / "editorial-review.json"
    content_path = tmp_path / "content.json"
    raw_review = review("redundant")
    raw_review.update({"verdict": "GO", "issues": []})
    content_path.write_text('{"protocol":"wiki-content-draft-v2"}', encoding="utf-8")
    review_path.write_text(json.dumps(raw_review), encoding="utf-8")
    apply_editorial_policy(review_path, content_path, "reviewed")
    policy_path = tmp_path / "editorial-policy-decision.json"

    with pytest.raises(ValueError, match="哈希绑定"):
        validate_editorial_policy(
            content_path, {"node_id": "A040"}, review_path, policy_path, "reviewed",
        )

    raw_review["checks"] = {"paragraph_focus": True, "redundancy_control": True}
    review_path.write_text(json.dumps(raw_review), encoding="utf-8")
    apply_editorial_policy(review_path, content_path, "reviewed")
    _, decision = validate_editorial_policy(
        content_path, {"node_id": "A040"}, review_path, policy_path, "reviewed",
    )
    assert decision["decision"] == "accept"


def test_draft_pipeline_passes_policy_contract_and_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[list[str]] = []

    def fake_pipeline(commands: list[list[str]], **_: object) -> dict:
        captured.extend(commands)
        return {"status": "ok"}

    monkeypatch.setattr(capability_runtime, "_pipeline", fake_pipeline)
    workspace = tmp_path / "workspace"
    batch = workspace / "runs" / "batch"

    capability_runtime.wiki_batch({
        "operation": "draft-content-pipeline",
        "workspace": str(workspace),
        "batch": str(batch),
        "publication_mode": "reviewed",
    })

    enrich_command = captured[0]
    assert enrich_command[6] == str(batch / "editorial-loop/editorial-policy-decision.json")
    assert enrich_command[7] == "reviewed"


def test_draft_pipeline_preserves_blocked_gate_as_business_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    batch = workspace / "runs" / "batch"
    output = batch / "draft-content-gate.json"
    output.parent.mkdir(parents=True)
    output.write_text(
        '{"protocol":"wiki-draft-content-gate-v1","decision":"REPAIR",'
        '"failed_requirement_ids":["source_diversity"]}',
        encoding="utf-8",
    )

    def fake_pipeline(commands: list[list[str]], **kwargs: object) -> dict:
        assert kwargs["blocked_codes"] == {2: "CONTENT_LOCAL_ISSUES"}
        return {
            "status": "blocked",
            "failure": {
                "code": "CONTENT_LOCAL_ISSUES",
                "category": "business_validation",
                "scope": "task",
                "message": "gate returned blocked (2)",
            },
            "steps": [],
        }

    monkeypatch.setattr(capability_runtime, "_pipeline", fake_pipeline)
    result = capability_runtime.wiki_batch({
        "operation": "draft-content-pipeline",
        "workspace": str(workspace),
        "batch": str(batch),
        "publication_mode": "reviewed",
    })

    assert result["status"] == "blocked"
    assert result["failure"]["code"] == "CONTENT_LOCAL_ISSUES"
    assert result["failure"]["gate_decision"] == "REPAIR"
    assert result["failure"]["failed_requirement_ids"] == ["source_diversity"]
    assert result["gate_result"]["decision"] == "REPAIR"
