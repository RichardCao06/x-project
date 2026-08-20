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
    MAXIMUM_CLAIM_USE_COUNT,
    apply_legacy_repairs,
    claim_binding_metrics,
    claim_remaining_uses,
    claim_uses_outside_targets,
    normalize_legacy_repair_claim_bindings,
    prepare_legacy_patch_review,
)


DISABLED = [
    "browser_use", "in_app_browser", "computer_use", "standalone_web_search",
    "remote_plugin", "plugins", "apps", "multi_agent",
]
PATCH_RUNTIME_REVISION = "wiki-editorial-patch-section-coordination-v7"
PATCH_RUNTIME_REVISION_SHA256 = hashlib.sha256(PATCH_RUNTIME_REVISION.encode()).hexdigest()
NORMALIZER_REVISION_SHA256 = hashlib.sha256(
    LEGACY_CLAIM_NORMALIZER_REVISION.encode()
).hexdigest()


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def payload_sha256(payload: object) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _replacement_schema(*, minimum: int, maximum: int) -> dict:
    return {"type": "array", "minItems": minimum, "maxItems": maximum,
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
                }}}


def build_output_schema(patch_review: dict) -> dict:
    """Bind every output branch to one reviewed target and its operation contract."""
    branches = []
    for issue in patch_review.get("issues") or []:
        operation = str(issue.get("operation") or "")
        if operation == "replace":
            minimum, maximum = 1, 1
        elif operation == "split_replace":
            minimum, maximum = 2, 4
        elif operation == "delete":
            minimum, maximum = 0, 0
        else:
            raise EditorialPatchError(f"unsupported editorial operation: {operation}")
        branches.append({
            "type": "object", "additionalProperties": False,
            "required": ["issue_id", "section_id", "paragraph_id", "target_hash",
                         "replacements", "preserved_claim_ids"],
            "properties": {
                "issue_id": {"type": "string", "enum": [issue.get("issue_id")]},
                "section_id": {"type": "string", "enum": [issue.get("section_id")]},
                "paragraph_id": {"type": "string", "enum": [issue.get("paragraph_id")]},
                "target_hash": {"type": "string", "enum": [issue.get("target_hash")]},
                "preserved_claim_ids": {"type": "array", "items": {"type": "string"}},
                "replacements": _replacement_schema(minimum=minimum, maximum=maximum),
            },
        })
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False, "required": ["repairs"],
        "properties": {"repairs": {"type": "array", "minItems": len(branches),
            "maxItems": len(branches), "items": {"anyOf": branches}}},
    }


def cached_repairs_match_review(payload: object, patch_review: dict) -> bool:
    """Fail closed unless cached repairs exactly match target identity and cardinality."""
    if not isinstance(payload, dict) or not isinstance(payload.get("repairs"), list):
        return False
    repairs = payload["repairs"]
    issues = patch_review.get("issues") or []
    if len(repairs) != len(issues) or not all(isinstance(item, dict) for item in repairs):
        return False
    supplied = {str(item.get("issue_id")): item for item in repairs}
    if len(supplied) != len(repairs):
        return False
    for issue in issues:
        repair = supplied.get(str(issue.get("issue_id")))
        if repair is None or any(
            repair.get(field) != issue.get(field)
            for field in ("issue_id", "section_id", "paragraph_id", "target_hash")
        ):
            return False
        replacements = repair.get("replacements")
        count = len(replacements) if isinstance(replacements, list) else -1
        if not isinstance(replacements, list) or not all(
            isinstance(replacement, dict) for replacement in replacements
        ):
            return False
        operation = issue.get("operation")
        if (operation == "replace" and count != 1) or (
            operation == "split_replace" and not 2 <= count <= 4
        ) or (
            operation == "delete" and count != 0
        ):
            return False
        if operation not in {"replace", "split_replace", "delete"}:
            return False
    return True


def can_reuse_repairs(previous_invocation: object, payload: object, patch_review: dict, *,
                      previous_usage: object, previous_receipt: object,
                      content_sha256: str, review_sha256: str,
                      patch_review_sha256: str, output_schema_sha256: str,
                      prompt_sha256: str, normalizer_sha256: str) -> bool:
    """Reuse only successful output bound to every causal input revision."""
    reuse_key = {
        "content_sha256": content_sha256,
        "review_sha256": review_sha256,
        "patch_review_sha256": patch_review_sha256,
        "output_schema_sha256": output_schema_sha256,
        "prompt_sha256": prompt_sha256,
        "patch_runtime_revision_sha256": PATCH_RUNTIME_REVISION_SHA256,
        "normalizer_revision_sha256": NORMALIZER_REVISION_SHA256,
        "normalizer_sha256": normalizer_sha256,
    }
    successful_usage = bool(
        isinstance(previous_usage, dict)
        and previous_usage.get("protocol") == "wiki-editorial-patch-usage-v1"
        and previous_usage.get("exit_code") == 0
        and previous_usage.get("reuse_key") == reuse_key
        and previous_usage.get("raw_repairs_sha256") == payload_sha256(payload)
    )
    successful_receipt = bool(
        isinstance(previous_receipt, dict)
        and previous_receipt.get("protocol") == "wiki-editorial-patch-receipt-v1"
        and isinstance(previous_receipt.get("scorecard"), dict)
        and previous_receipt["scorecard"].get(
            "internal_graph_fact_sentences_without_evidence"
        ) == 0
        and isinstance(previous_receipt["scorecard"].get("maximum_claim_use_count"), int)
        and previous_receipt["scorecard"]["maximum_claim_use_count"]
            <= MAXIMUM_CLAIM_USE_COUNT
        and previous_receipt.get("reuse_key") == reuse_key
        and previous_receipt.get("raw_repairs_sha256") == payload_sha256(payload)
    )
    return bool(
        isinstance(previous_invocation, dict)
        and all(previous_invocation.get(key) == value for key, value in reuse_key.items())
        and (successful_usage or successful_receipt)
        and cached_repairs_match_review(payload, patch_review)
    )


def build_prompt(document: dict, targets: list[dict], claims: list[dict]) -> str:
    """Build a node-local prompt without policy copied from a previous repair."""
    node_id = str(document.get("node_id") or "").strip()
    if not node_id:
        raise EditorialPatchError("content document requires node_id")
    targeted_paragraphs = {
        (
            str(target.get("issue", {}).get("section_id") or ""),
            str(target.get("issue", {}).get("paragraph_id") or ""),
        )
        for target in targets
    }
    remaining_uses = claim_remaining_uses(
        document,
        targeted_paragraphs,
        (claim.get("claim_id") for claim in claims),
    )
    uses_outside_targets = claim_uses_outside_targets(
        document,
        targeted_paragraphs,
        (claim.get("claim_id") for claim in claims),
    )
    capacity_bound_claims = [
        {
            **claim,
            "maximum_total_uses": MAXIMUM_CLAIM_USE_COUNT,
            "uses_outside_targets": uses_outside_targets[str(claim.get("claim_id"))],
            "remaining_uses": remaining_uses[str(claim.get("claim_id"))],
        }
        for claim in claims
        if str(claim.get("claim_id") or "") in remaining_uses
    ]
    return (
        "你是中文技术百科的段落修订编辑。禁止工具、联网和读取文件。只替换 TARGETS 中被独立审查点名的段落，"
        "每个 issue 精确返回一个 repair，并逐字回传 issue_id、section_id、paragraph_id、target_hash。"
        "operation=replace 时 replacements 精确返回一段；operation=split_replace 时返回至少两段，顺序就是插入顺序。"
        "operation=delete 时 replacements 必须返回空数组，不得用占位段替代删除。"
        "每个 replacement 保持单一中心、2-4句且首句是唯一 thesis；只引用 CLAIMS 中存在的 claim_id。"
        "external_fact 只能使用 verdict=CONFIRMED 的 external_fact；不得扩大原事实。"
        "CLAIMS 中每项的 remaining_uses 是全部 replacement 句子可使用该 claim_id 的总次数预算；"
        "所有 replacements 合计不得超过该预算，且不得用 preserved_claim_ids 绕过预算。"
        "internal_graph_fact 的正文必须由所绑定 claim 的 claim_text 直接支持，不得把 claim_text 未陈述的功能角色"
        "（例如供电、热管理、结构或导风）写成 internal_graph_fact；这类解释只能标为 modeling_judgment，"
        "证据不足时必须标为 evidence_gap。"
        "tokens_must_preserve 中的每个字面量必须原样出现在 replacements 正文中。"
        "逐项执行当前 issue 的 instruction；不得引入当前文档、TARGETS 或 CLAIMS 未提供的节点、产品或工厂规则。"
        "修复指令要求替换、删除或更正错误标识时，不得恢复被取代标识。"
        "修复指令要求删除无关引用时允许不保留该 claim；preserved_claim_ids 只列替换段落实际保留的 ID。"
        "同一 section 的多个 TARGETS 必须先形成一个联合改写计划，再分别输出 repairs；各段中心、实体清单和"
        "核验结论不得互相重复。instruction 要求将内容集中到另一被点名段落时，只在该段完整陈述，其他段仅保留"
        "其指定的分类或论证；instruction 要求本段仅保留某些标识时，必须删除其余标识与清单。"
        "若 instruction 提到的目的段不在 TARGETS 中，不得修改该目的段，也不得在当前段写‘应移至’之类编辑说明；"
        "应从当前段删除被要求移动的内容，只保留 instruction 明确要求当前段保留的论点和标识。"
        "不要修改、总结或返回未被点名的段落。输出只匹配 schema。\n"
        f"NODE_ID={node_id}\n"
        f"TARGETS={json.dumps(targets, ensure_ascii=False, separators=(',', ':'))}\n"
        f"CLAIMS={json.dumps(capacity_bound_claims, ensure_ascii=False, separators=(',', ':'))}"
    )


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
    schema = build_output_schema(patch_review)
    schema_path = output_dir / "editorial-patch-output.schema.json"
    schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    current_schema_hash = sha256(schema_path)
    repairs_raw = output_dir / "editorial-repairs.raw.json"
    events = output_dir / "editorial-patch-events.jsonl"
    stderr = output_dir / "editorial-patch-stderr.log"
    invocation = output_dir / "editorial-patch-invocation.json"
    usage = output_dir / "editorial-patch-usage.json"
    receipt_path = output_dir / "editorial-patch-receipt.json"
    current_content_hash = sha256(content_path)
    current_review_hash = sha256(review_path)
    current_patch_review_hash = sha256(patch_review_path)
    normalizer_path = Path(normalize_legacy_repair_claim_bindings.__code__.co_filename).resolve()
    normalizer_sha256 = sha256(normalizer_path)
    prompt = build_prompt(document, targets, claims)
    prompt_sha256 = hashlib.sha256(prompt.encode()).hexdigest()
    reuse_existing_repairs = False
    if repairs_raw.is_file() and invocation.is_file():
        try:
            previous_invocation = load(invocation)
            reuse_existing_repairs = can_reuse_repairs(
                previous_invocation, load(repairs_raw), patch_review,
                previous_usage=load(usage) if usage.is_file() else None,
                previous_receipt=load(receipt_path) if receipt_path.is_file() else None,
                content_sha256=current_content_hash,
                review_sha256=current_review_hash,
                patch_review_sha256=current_patch_review_hash,
                output_schema_sha256=current_schema_hash,
                prompt_sha256=prompt_sha256,
                normalizer_sha256=normalizer_sha256,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            reuse_existing_repairs = False
    if not reuse_existing_repairs:
        # The invocation is written before model execution. Removing an
        # incompatible payload first prevents a failed attempt from making an
        # old repair appear current on the following retry.
        repairs_raw.unlink(missing_ok=True)
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
        "output_schema_sha256": current_schema_hash,
        "prompt_sha256": prompt_sha256,
        "patch_runtime_revision": PATCH_RUNTIME_REVISION,
        "patch_runtime_revision_sha256": PATCH_RUNTIME_REVISION_SHA256,
        "normalizer_revision": LEGACY_CLAIM_NORMALIZER_REVISION,
        "normalizer_revision_sha256": NORMALIZER_REVISION_SHA256,
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
                "output_schema_sha256": current_schema_hash,
                "prompt_sha256": prompt_sha256,
                "patch_runtime_revision_sha256": PATCH_RUNTIME_REVISION_SHA256,
                "normalizer_revision": LEGACY_CLAIM_NORMALIZER_REVISION,
                "normalizer_revision_sha256": NORMALIZER_REVISION_SHA256,
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
    reuse_key = {
        "content_sha256": current_content_hash,
        "review_sha256": current_review_hash,
        "patch_review_sha256": current_patch_review_hash,
        "output_schema_sha256": current_schema_hash,
        "prompt_sha256": prompt_sha256,
        "patch_runtime_revision_sha256": PATCH_RUNTIME_REVISION_SHA256,
        "normalizer_revision_sha256": NORMALIZER_REVISION_SHA256,
        "normalizer_sha256": normalizer_sha256,
    }
    raw_repairs_sha256 = None
    if exit_code == 0:
        try:
            raw_repairs = load(repairs_raw)
            raw_repairs_sha256 = payload_sha256(raw_repairs)
            repairs = normalize_legacy_repair_claim_bindings(
                raw_repairs.get("repairs"), rows, document,
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
            receipt["reuse_key"] = reuse_key
            receipt["raw_repairs_sha256"] = raw_repairs_sha256
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
        "reuse_key": reuse_key,
        "raw_repairs_sha256": raw_repairs_sha256,
        "targeted_paragraphs": receipt.get("targeted_paragraphs", []) if receipt else [],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"exit_code": exit_code, "error": error,
                      "targeted_paragraphs": receipt.get("targeted_paragraphs", []) if receipt else []},
                     ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
