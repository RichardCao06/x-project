#!/usr/bin/env python3
"""Repair only hash-bound paragraphs identified by an editorial NO_GO review."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

from lca_project.domains.editorial_patch import (
    EditorialPatchError,
    LEGACY_CLAIM_NORMALIZER_REVISION,
    apply_legacy_repairs,
    claim_binding_metrics,
    normalize_legacy_repair_claim_bindings,
    prepare_legacy_patch_review,
)


DISABLED = [
    "browser_use", "in_app_browser", "computer_use", "standalone_web_search",
    "remote_plugin", "plugins", "apps", "multi_agent",
]


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verify_output", type=Path)
    parser.add_argument("content_result", type=Path)
    parser.add_argument("blueprint", type=Path)
    parser.add_argument("editorial_review", type=Path)
    parser.add_argument("content_capture_script", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()

    verify_path = args.verify_output.resolve()
    content_path = args.content_result.resolve()
    blueprint_path = args.blueprint.resolve()
    review_path = args.editorial_review.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    document = load(content_path)
    blueprint = load(blueprint_path)
    patch_review = prepare_legacy_patch_review(document, load(review_path))
    patch_review_path = output_dir / "editorial-patch-review.json"
    patch_review_path.write_text(json.dumps(patch_review, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")

    targets = []
    for issue in patch_review["issues"]:
        section = next(row for row in document["sections"]
                       if row["heading"] == issue["section_id"])
        index = int(issue["paragraph_id"].removeprefix("p")) - 1
        targets.append({"issue": issue, "paragraph": section["paragraphs"][index]})
    verified = load(verify_path)
    rows = verified.get("claims") or verified.get("result", {}).get("claims") or []
    claims = [{
        "claim_id": (row.get("claim") or {}).get("claim_id"),
        "claim_kind": (row.get("claim") or {}).get("claim_kind"),
        "claim_text": (row.get("claim") or {}).get("claim_text"),
        "verdict": (row.get("verify") or {}).get("verdict"),
    } for row in rows]
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False, "required": ["repairs"],
        "properties": {"repairs": {"type": "array", "minItems": len(targets),
            "maxItems": len(targets), "items": {
                "type": "object", "additionalProperties": False,
                "required": ["issue_id", "section_id", "paragraph_id", "target_hash",
                             "replacements", "preserved_claim_ids"],
                "properties": {
                    "issue_id": {"type": "string"}, "section_id": {"type": "string"},
                    "paragraph_id": {"type": "string"}, "target_hash": {"type": "string"},
                    "preserved_claim_ids": {"type": "array", "items": {"type": "string"}},
                    "replacements": {"type": "array", "minItems": 1, "maxItems": 4,
                        "items": {"type": "object", "additionalProperties": False,
                            "required": ["focus", "sentences"], "properties": {
                                "focus": {"type": "string", "minLength": 8},
                                "sentences": {"type": "array", "minItems": 2, "maxItems": 4,
                                    "items": {"type": "object", "additionalProperties": False,
                                        "required": ["text", "claim_kind", "rhetorical_role",
                                                     "evidence_claim_ids"],
                                        "properties": {
                                            "text": {"type": "string", "minLength": 10},
                                            "claim_kind": {"enum": ["external_fact", "internal_graph_fact",
                                                                      "modeling_judgment", "evidence_gap"]},
                                            "rhetorical_role": {"type": "string"},
                                            "evidence_claim_ids": {"type": "array",
                                                                   "items": {"type": "string"}},
                                        }}},
                            }}},
                },
            }}},
    }
    schema_path = output_dir / "editorial-patch-output.schema.json"
    schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    repairs_raw = output_dir / "editorial-repairs.raw.json"
    events = output_dir / "editorial-patch-events.jsonl"
    stderr = output_dir / "editorial-patch-stderr.log"
    invocation = output_dir / "editorial-patch-invocation.json"
    usage = output_dir / "editorial-patch-usage.json"
    current_content_hash = sha256(content_path)
    current_review_hash = sha256(review_path)
    current_patch_review_hash = sha256(patch_review_path)
    normalizer_path = Path(normalize_legacy_repair_claim_bindings.__code__.co_filename).resolve()
    normalizer_sha256 = sha256(normalizer_path)
    reuse_existing_repairs = False
    if repairs_raw.is_file() and invocation.is_file():
        try:
            previous_invocation = load(invocation)
            reuse_existing_repairs = (
                previous_invocation.get("content_sha256") == current_content_hash
                and previous_invocation.get("review_sha256") == current_review_hash
                and previous_invocation.get("patch_review_sha256") == current_patch_review_hash
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            reuse_existing_repairs = False
    prompt = (
        "你是中文技术百科的段落修订编辑。禁止工具、联网和读取文件。只替换 TARGETS 中被独立审查点名的段落，"
        "每个 issue 精确返回一个 repair，并逐字回传 issue_id、section_id、paragraph_id、target_hash。"
        "operation=replace 时 replacements 精确返回一段；operation=split_replace 时返回至少两段，顺序就是插入顺序。"
        "每个 replacement 保持单一中心、2-4句且首句是唯一 thesis；只引用 CLAIMS 中存在的 claim_id。"
        "external_fact 只能使用 verdict=CONFIRMED 的 external_fact；不得扩大原事实。"
        "tokens_must_preserve 中的每个字面量必须原样出现在 replacements 正文中，包括"
        "‘P057 钢钣金机箱/导轨, 服务器用’，不得虚构共享机箱产品层。"
        "共享资源分配与退料/不良质量闭合必须分别成段；厂级记录只有可审计归属到 A039 时才是节点证据，"
        "否则只能作为筛查或交叉核对；废物核算与装配用电测量必须分别成段。"
        "修复指令要求删除无关引用时允许不保留该 claim；preserved_claim_ids 只列替换段落实际保留的 ID。"
        "不要修改、总结或返回未被点名的段落。输出只匹配 schema。\n"
        f"TARGETS={json.dumps(targets, ensure_ascii=False, separators=(',', ':'))}\n"
        f"CLAIMS={json.dumps(claims, ensure_ascii=False, separators=(',', ':'))}"
    )
    root = Path(__file__).resolve().parents[1]
    command = ["codex", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
               "-C", str(root), "-s", "read-only", "-m", "gpt-5.6-terra",
               "-c", 'model_reasoning_effort="medium"']
    for feature in DISABLED:
        command.extend(["--disable", feature])
    command.extend(["--json", "--output-schema", str(schema_path), "-o", str(repairs_raw), prompt])
    invocation.write_text(json.dumps({
        "protocol": "wiki-editorial-patch-runtime-v1",
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "argv": command, "content_sha256": current_content_hash,
        "review_sha256": current_review_hash,
        "patch_review_sha256": current_patch_review_hash,
        "normalizer_revision": LEGACY_CLAIM_NORMALIZER_REVISION,
        "normalizer_sha256": normalizer_sha256,
        "reused_existing_repairs": reuse_existing_repairs,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    exit_code = 124
    error = None
    with events.open("w", encoding="utf-8") as event_stream, stderr.open("w", encoding="utf-8") as error_stream:
        if reuse_existing_repairs:
            event_stream.write(json.dumps({
                "type": "editorial.patch_reused", "content_sha256": current_content_hash,
                "review_sha256": current_review_hash,
                "normalizer_revision": LEGACY_CLAIM_NORMALIZER_REVISION,
                "normalizer_sha256": normalizer_sha256,
            }, ensure_ascii=False) + "\n")
            exit_code = 0
        else:
            try:
                completed = subprocess.run(command, cwd=root, stdin=subprocess.DEVNULL,
                                           stdout=event_stream, stderr=error_stream, text=True,
                                           check=False, timeout=max(1, args.timeout_seconds))
                exit_code = completed.returncode
            except subprocess.TimeoutExpired:
                error = "Editorial paragraph patch timeout"
    receipt = None
    if exit_code == 0:
        try:
            repairs = normalize_legacy_repair_claim_bindings(
                load(repairs_raw).get("repairs"), rows, document,
            )
            patched, receipt = apply_legacy_repairs(document, patch_review, repairs)
            candidate = output_dir / "content-result.patched.json"
            candidate.write_text(json.dumps(patched, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            spec = importlib.util.spec_from_file_location("content_capture", args.content_capture_script.resolve())
            if not spec or not spec.loader:
                raise EditorialPatchError("cannot load content validator")
            capture = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(capture)
            scorecard = capture.validate_result(candidate, blueprint, rows)
            scorecard.update(claim_binding_metrics(patched))
            receipt["scorecard"] = scorecard
            receipt["normalizer_revision"] = LEGACY_CLAIM_NORMALIZER_REVISION
            receipt["normalizer_sha256"] = normalizer_sha256
            receipt_path = output_dir / "editorial-patch-receipt.json"
            receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")
            candidate.replace(content_path)
            marker = content_path.parent / "frozen-editorial-repair.json"
            marker.write_text(json.dumps({
                "protocol": "wiki-frozen-editorial-repair-v1",
                "content_sha256": sha256(content_path),
                "review_sha256": sha256(review_path),
                "receipt_sha256": sha256(receipt_path),
                "requires_independent_rereview": True,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (EditorialPatchError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            error, exit_code = str(exc), 2
    usage.write_text(json.dumps({
        "protocol": "wiki-editorial-patch-usage-v1",
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "exit_code": exit_code, "error": error,
        "targeted_paragraphs": receipt.get("targeted_paragraphs", []) if receipt else [],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"exit_code": exit_code, "error": error,
                      "targeted_paragraphs": receipt.get("targeted_paragraphs", []) if receipt else []},
                     ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
