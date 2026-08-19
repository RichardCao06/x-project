from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from lca_project.domains.editorial_patch import prepare_legacy_patch_review


ROOT = Path(__file__).resolve().parents[1]


def load_runtime():
    path = ROOT / "scripts/run_wiki_editorial_patch.py"
    spec = importlib.util.spec_from_file_location("run_wiki_editorial_patch", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def patch_review() -> dict:
    return {"issues": [
        {"issue_id": "E001", "section_id": "定义", "paragraph_id": "p1",
         "target_hash": "sha256:first", "operation": "replace"},
        {"issue_id": "E002", "section_id": "边界", "paragraph_id": "p2",
         "target_hash": "sha256:second", "operation": "replace"},
        {"issue_id": "E003", "section_id": "数据", "paragraph_id": "p3",
         "target_hash": "sha256:third", "operation": "split_replace"},
    ]}


def repair_for(issue: dict, count: int) -> dict:
    return {
        **{field: issue[field]
           for field in ("issue_id", "section_id", "paragraph_id", "target_hash")},
        "preserved_claim_ids": [],
        "replacements": [{} for _ in range(count)],
    }


def schema_allows_replacement_count(schema: dict, issue_id: str, count: int) -> bool:
    branches = schema["properties"]["repairs"]["items"]["anyOf"]
    branch = next(item for item in branches
                  if item["properties"]["issue_id"]["enum"] == [issue_id])
    replacements = branch["properties"]["replacements"]
    return replacements["minItems"] <= count <= replacements["maxItems"]


def test_schema_binds_each_target_to_operation_specific_cardinality() -> None:
    runtime = load_runtime()
    review = patch_review()
    schema = runtime.build_output_schema(review)
    repairs_schema = schema["properties"]["repairs"]
    branches = {
        branch["properties"]["issue_id"]["enum"][0]: branch
        for branch in repairs_schema["items"]["anyOf"]
    }

    assert repairs_schema["minItems"] == repairs_schema["maxItems"] == 3
    for issue in review["issues"]:
        branch = branches[issue["issue_id"]]
        properties = branch["properties"]
        assert {field: properties[field]["enum"][0] for field in (
            "issue_id", "section_id", "paragraph_id", "target_hash"
        )} == {field: issue[field] for field in (
            "issue_id", "section_id", "paragraph_id", "target_hash"
        )}
    assert branches["E001"]["properties"]["replacements"]["minItems"] == 1
    assert branches["E001"]["properties"]["replacements"]["maxItems"] == 1
    assert branches["E002"]["properties"]["replacements"]["minItems"] == 1
    assert branches["E002"]["properties"]["replacements"]["maxItems"] == 1
    assert branches["E003"]["properties"]["replacements"]["minItems"] == 2
    assert branches["E003"]["properties"]["replacements"]["maxItems"] == 4


@pytest.mark.parametrize(("issue_id", "count", "accepted"), [
    ("E002", 0, False), ("E002", 1, True), ("E002", 2, False),
    ("E003", 1, False), ("E003", 2, True), ("E003", 3, True),
    ("E003", 4, True), ("E003", 5, False),
])
def test_schema_accepts_only_operation_valid_replacement_counts(
    issue_id: str, count: int, accepted: bool,
) -> None:
    schema = load_runtime().build_output_schema(patch_review())
    assert schema_allows_replacement_count(schema, issue_id, count) is accepted


def test_cache_reuse_rejects_invalid_cardinality_and_runtime_revision() -> None:
    runtime = load_runtime()
    review = patch_review()
    valid = {"repairs": [
        repair_for(review["issues"][0], 1),
        repair_for(review["issues"][1], 1),
        repair_for(review["issues"][2], 2),
    ]}
    invocation = {
        "content_sha256": "content", "review_sha256": "review",
        "patch_review_sha256": "patch-review", "output_schema_sha256": "schema",
        "patch_runtime_revision_sha256": runtime.PATCH_RUNTIME_REVISION_SHA256,
    }
    inputs = {
        "content_sha256": "content", "review_sha256": "review",
        "patch_review_sha256": "patch-review", "output_schema_sha256": "schema",
    }

    assert runtime.can_reuse_repairs(invocation, valid, review, **inputs) is True
    invalid_e002 = json.loads(json.dumps(valid))
    invalid_e002["repairs"][1]["replacements"].append({})
    assert runtime.can_reuse_repairs(invocation, invalid_e002, review, **inputs) is False
    stale_invocation = {**invocation, "patch_runtime_revision_sha256": "old-runtime"}
    assert runtime.can_reuse_repairs(stale_invocation, valid, review, **inputs) is False


def test_generic_prompt_uses_current_node_without_foreign_node_policy() -> None:
    runtime = load_runtime()
    prompt = runtime.build_prompt(
        {"protocol": "wiki-content-draft-v2", "node_id": "A013"},
        [{"issue": {"issue_id": "E001", "tokens_must_preserve": ["P022"]},
          "paragraph": _paragraph("当前节点问题")}],
        [],
    )

    assert "NODE_ID=A013" in prompt
    assert "A039" not in prompt
    assert "P057" not in prompt
    assert "tokens_must_preserve" in prompt


def _paragraph(focus: str) -> dict:
    return {"focus": focus, "sentences": [{
        "text": f"{focus}的原始说明保持单一中心并可被确定性检查。",
        "claim_kind": "modeling_judgment", "rhetorical_role": "thesis",
        "evidence_claim_ids": [],
    }]}


def _replacement(label: str) -> dict:
    return {"focus": f"{label}后的独立段落中心", "sentences": [
        {"text": f"{label}后的第一句陈述独立段落的唯一中心。",
         "claim_kind": "modeling_judgment", "rhetorical_role": "thesis",
         "evidence_claim_ids": []},
        {"text": f"{label}后的第二句只解释该中心的建模边界。",
         "claim_kind": "modeling_judgment", "rhetorical_role": "boundary",
         "evidence_claim_ids": []},
    ]}


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_a013_runtime_rematerializes_and_applies_four_hash_bound_targets(
    tmp_path: Path, monkeypatch,
) -> None:
    runtime = load_runtime()
    content = {
        "protocol": "wiki-content-draft-v2", "node_id": "A013", "sections": [{
            "heading": "核算边界", "paragraphs": [
                _paragraph("未点名段落"), _paragraph("第一处问题"),
                _paragraph("第二处问题"), _paragraph("第三处混合问题"),
                {**_paragraph("第四处节点问题"), "sentences": [{
                    **_paragraph("第四处节点问题")["sentences"][0],
                    "text": "第四处将当前节点误写为A039，需要按审查指令纠正。",
                }]},
            ],
        }],
    }
    review = {
        "protocol": "wiki-editorial-review-v1", "node_id": "A013", "verdict": "NO_GO",
        "issues": [
            {"section": "核算边界", "paragraph_index": 2, "issue_type": "local",
             "explanation": "第一处需局部修订。", "repair_instruction": "改写为单一中心。"},
            {"section": "核算边界", "paragraph_index": 3, "issue_type": "local",
             "explanation": "第二处需局部修订。", "repair_instruction": "改写为单一中心。"},
            {"section": "核算边界", "paragraph_index": 4, "issue_type": "claim_dump",
             "explanation": "第三处有两个中心。", "repair_instruction": "拆分为两个独立段落。"},
            {"section": "核算边界", "paragraph_index": 5, "issue_type": "identity_drift",
             "explanation": "第四处使用了错误节点。", "repair_instruction": "将A039更正为A013。"},
        ],
    }
    bound = prepare_legacy_patch_review(content, review)
    repairs = {"repairs": [
        repair_for(bound["issues"][0], 1),
        repair_for(bound["issues"][1], 1),
        repair_for(bound["issues"][2], 2),
        repair_for(bound["issues"][3], 1),
    ]}
    repairs["repairs"][0]["replacements"] = [_replacement("第一处修订")]
    repairs["repairs"][1]["replacements"] = [_replacement("第二处修订")]
    repairs["repairs"][2]["replacements"] = [
        _replacement("第三处第一中心"), _replacement("第三处第二中心"),
    ]
    repairs["repairs"][3]["replacements"] = [_replacement("A013节点纠正")]

    verify = tmp_path / "verify.json"
    content_path = tmp_path / "content.json"
    blueprint = tmp_path / "blueprint.json"
    review_path = tmp_path / "review.json"
    capture = tmp_path / "capture.py"
    output = tmp_path / "patch-runtime"
    output.mkdir()
    _write(verify, {"claims": []})
    _write(content_path, content)
    _write(blueprint, {})
    _write(review_path, review)
    capture.write_text("def validate_result(candidate, blueprint, rows):\n    return {'decision': 'PASS'}\n",
                       encoding="utf-8")
    _write(output / "editorial-repairs.raw.json", {"repairs": [{"stale": True}]})
    _write(output / "editorial-patch-invocation.json", {
        "patch_runtime_revision_sha256": "defective-runtime",
    })

    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        raw_path = Path(command[command.index("-o") + 1])
        assert not raw_path.exists()
        _write(raw_path, repairs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", [
        "run_wiki_editorial_patch.py", str(verify), str(content_path), str(blueprint),
        str(review_path), str(capture), str(output),
    ])

    assert runtime.main() == 0
    assert len(calls) == 1
    usage = json.loads((output / "editorial-patch-usage.json").read_text(encoding="utf-8"))
    receipt = json.loads((output / "editorial-patch-receipt.json").read_text(encoding="utf-8"))
    invocation = json.loads((output / "editorial-patch-invocation.json").read_text(encoding="utf-8"))
    assert usage["exit_code"] == 0
    assert usage["targeted_paragraphs"] == [
        "核算边界.p2", "核算边界.p3", "核算边界.p4", "核算边界.p5",
    ]
    assert receipt["targeted_paragraphs"] == usage["targeted_paragraphs"]
    assert set(receipt["unchanged_paragraphs"]) == {"核算边界.p1"}
    assert receipt["requires_independent_rereview"] is True
    assert (content_path.parent / "frozen-editorial-repair.json").is_file()
    assert invocation["reused_existing_repairs"] is False
    assert invocation["patch_runtime_revision_sha256"] == runtime.PATCH_RUNTIME_REVISION_SHA256
    assert invocation["output_schema_sha256"]
