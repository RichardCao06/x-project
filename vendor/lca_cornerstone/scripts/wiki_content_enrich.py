#!/usr/bin/env python3
"""Deterministically turn a validated content draft into BODY, KUs and evidence tables."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from assemble_wiki_from_ku import badge_for, render_footnotes
from ku_distill import distill
from run_wiki_content_capture import _claims, validate_result


def _cell(value: str) -> str:
    return str(value).replace("|", "\\|")


def render_evidence_tables(blueprint: dict) -> str:
    tables = blueprint["evidence_tables"]
    node_id = blueprint["node_id"]
    if blueprint.get("node_type") == "activity":
        flows = "\n".join(
            f"| {_cell(label)} | 待核 | 待采 | measured_average | 待采 | internal-review | 待采 | internal-review | 待评 |"
            for label in tables["flows"]
        )
        props = "\n".join(
            f"| {_cell(label)} | {node_id} 参考产品交接点 | — | 待核 | internal-review | 待评 |"
            for label in tables["props"]
        )
        emissions = "\n".join(
            f"| {_cell(label)} | 待核 | 待核 | 待采 | measured_average | 待采 | internal-review | 待采 | internal-review | 待评 |"
            for label in tables["emissions"]
        )
        indicators = "\n".join(
            f"| {_cell(label)} | 待核 | 待采 | measured_average | 待采 | internal-review | 待采 | internal-review | unmapped | 待评 |"
            for label in tables["indicators"]
        )
        params = "\n".join(
            f"| {_cell(label)} | target | 待采 | measured_average | 待采 | internal-review | 待采 | internal-review | 待评 |"
            for label in tables["params"]
        )
        quality = "\n".join(
            f"| {_cell(label)} | — | reference | 待核 | internal-review | 不得以相邻产品规格或企业汇总值冒充 {node_id} 单元过程数据 | 待评 |"
            for label in tables["quality"]
        )
        return f"""## 投入产出流

<!-- EV:flows:START -->
| 流 | 方向 | 单位 | basis | 国际值 INT | 国际源 INT | 中国值 CN | 中国源 CN | pedigree |
|---|---|---|---|---|---|---|---|---|
{flows}
<!-- EV:flows:END -->

## 参考产品性质与交接状态

<!-- EV:props:START -->
| property | condition | unit | 值 | 源 | pedigree |
|---|---|---|---|---|---|
{props}
<!-- EV:props:END -->

## 工艺与地区参数

<!-- EV:params:START -->
| parameter | geo | unit | basis | 国际值 INT | 国际源 INT | 中国值 CN | 中国源 CN | pedigree |
|---|---|---|---|---|---|---|---|---|
{params}
<!-- EV:params:END -->

## 直接排放与废物

<!-- EV:emissions:START -->
| substance | CAS | compartment | unit | basis | 国际值 INT | 国际源 INT | 中国值 CN | 中国源 CN | pedigree |
|---|---|---|---|---|---|---|---|---|---|
{emissions}
<!-- EV:emissions:END -->

## 过程监测指标

<!-- EV:indicators:START -->
| indicator | medium | unit | basis | 国际值 INT | 国际源 INT | 中国值 CN | 中国源 CN | mapping_status | pedigree |
|---|---|---|---|---|---|---|---|---|---|
{indicators}
<!-- EV:indicators:END -->

## 数据质量与代表性

<!-- EV:quality:START -->
| field | unit | basis | 中国项目值 CN | 中国源 CN | proxy_policy | pedigree |
|---|---|---|---|---|---|---|
{quality}
<!-- EV:quality:END -->"""
    props = "\n".join(
        f"| {_cell(label)} | {node_id} 产品边界 | — | 待核 | internal-review | 待评 |"
        for label in tables["props"]
    )
    params = "\n".join(
        f"| {_cell(label)} | target | 待采 | measured_average | 待采 | internal-review | 待采 | internal-review | 待评 |"
        for label in tables["params"]
    )
    quality = "\n".join(
        f"| {_cell(label)} | — | reference | 待核 | internal-review | 不得以相邻对象或未披露配置冒充目标数据 | 待评 |"
        for label in tables["quality"]
    )
    return f"""## 产品性质与交付状态

<!-- EV:props:START -->
| property | condition | unit | 值 | 源 | pedigree |
|---|---|---|---|---|---|
{props}
<!-- EV:props:END -->

## 产品规格与地区参数

<!-- EV:params:START -->
| parameter | geo | unit | basis | 国际值 INT | 国际源 INT | 中国值 CN | 中国源 CN | pedigree |
|---|---|---|---|---|---|---|---|---|
{params}
<!-- EV:params:END -->

## 数据质量与代表性

<!-- EV:quality:START -->
| field | unit | basis | 中国项目值 CN | 中国源 CN | proxy_policy | pedigree |
|---|---|---|---|---|---|---|
{quality}
<!-- EV:quality:END -->"""


def render_product_tables(blueprint: dict) -> str:
    """Backward-compatible name; dispatches by blueprint.node_type."""
    return render_evidence_tables(blueprint)


def validate_editorial_policy(
    content_path: Path,
    blueprint: dict,
    editorial_review_path: Path,
    editorial_policy_path: Path,
    publication_mode: str,
) -> tuple[dict, dict]:
    """Validate the effective editorial decision and all of its input bindings."""
    editorial_review = json.loads(editorial_review_path.read_text(encoding="utf-8"))
    editorial_policy = json.loads(editorial_policy_path.read_text(encoding="utf-8"))
    review_sha256 = hashlib.sha256(editorial_review_path.read_bytes()).hexdigest()
    content_sha256 = hashlib.sha256(content_path.read_bytes()).hexdigest()
    valid_review_identity = (
        editorial_review.get("protocol") == "wiki-editorial-review-v1"
        and editorial_review.get("node_id") == blueprint["node_id"]
    )
    valid_policy_binding = (
        publication_mode in {"preview", "reviewed"}
        and editorial_policy.get("protocol") == "wiki-editorial-policy-decision-v1"
        and editorial_policy.get("publication_mode") == publication_mode
        and editorial_policy.get("content_sha256") == content_sha256
        and editorial_policy.get("raw_review_sha256") == review_sha256
        and editorial_policy.get("review_sha256") == review_sha256
    )
    decision = editorial_policy.get("decision")
    issues = editorial_review.get("issues")
    checks = editorial_review.get("checks")
    strict_raw_go = (
        editorial_review.get("verdict") == "GO"
        and isinstance(issues, list) and not issues
        and isinstance(checks, dict) and bool(checks)
        and all(value is True for value in checks.values())
    )
    if decision == "accept":
        valid_decision = (
            strict_raw_go
            and editorial_policy.get("advisory_count") == 0
            and editorial_policy.get("blocking_count") == 0
        )
    elif decision == "accept_with_advisories":
        valid_decision = (
            publication_mode == "preview"
            and editorial_review.get("verdict") != "GO"
            and isinstance(issues, list)
            and editorial_policy.get("advisory_count") == len(issues)
            and editorial_policy.get("blocking_count") == 0
        )
    else:
        valid_decision = False
    if not (valid_review_identity and valid_policy_binding and valid_decision):
        raise ValueError("内容没有通过哈希绑定的 Editorial Policy，不得进入确定性组装")
    return editorial_review, editorial_policy


def enrich(
    verify_path: Path,
    content_path: Path,
    blueprint_path: Path,
    editorial_review_path: Path,
    editorial_policy_path: Path,
    publication_mode: str,
) -> dict:
    blueprint = json.loads(blueprint_path.read_text(encoding="utf-8"))
    editorial_review, editorial_policy = validate_editorial_policy(
        content_path, blueprint, editorial_review_path, editorial_policy_path,
        publication_mode,
    )
    rows = _claims(verify_path, blueprint["node_id"])
    scorecard = validate_result(content_path, blueprint, rows)
    content = json.loads(content_path.read_text(encoding="utf-8"))
    # Verify rows are immutable release evidence.  Keep them byte-for-byte
    # equivalent so ``wiki_batch finalize`` can prove the editor did not
    # rewrite a frozen claim.  Their anchor role is derived from
    # ``research_claim_ids`` / editorial ``evidence_claim_ids`` downstream.
    base_rows = copy.deepcopy(rows)
    base = {row["claim"]["claim_id"]: row for row in base_rows}
    identity = next(iter(base.values()))["claim"]["node_identity"]
    added: list[dict] = []
    counter = 1
    for section in content["sections"]:
        for paragraph in section["paragraphs"]:
            for sentence in paragraph["sentences"]:
                claim_id = f"{blueprint['node_id']}-C{counter:03d}"
                counter += 1
                evidence_ids = list(sentence.get("evidence_claim_ids") or [])
                kind = sentence["claim_kind"]
                if kind == "external_fact":
                    first = base[evidence_ids[0]]
                    believed_source = "DERIVED_VERIFIED_CLAIMS"
                    believed_locator = "; ".join(
                        str(base[source_id]["claim"].get("believed_locator", ""))
                        for source_id in evidence_ids
                    )
                    fetch_result = copy.deepcopy(first.get("fetchResult"))
                    verify = copy.deepcopy(first.get("verify"))
                    verify["reasoning"] = (
                        "编辑正文仅综合已 CONFIRMED 的冻结外部 claim；"
                        f"evidence_claim_ids={','.join(evidence_ids)}。"
                    )
                elif kind == "internal_graph_fact":
                    believed_source = "LCA-CORNERSTONE_GRAPH"
                    believed_locator = "正文编辑层对冻结图谱 claim 的等义表达"
                    fetch_result = None
                    verify = {"verdict": "NOT_FOUND", "supporting_quote": "",
                              "reasoning": "由冻结图谱 claim 确定性支撑，不主张外部事实权限。",
                              "node_alignment": "EXACT"}
                else:
                    believed_source = "INTERNAL_MODELING_JUDGMENT"
                    believed_locator = "Content Blueprint editorial modeling statement"
                    fetch_result = None
                    verify = {"verdict": "NOT_FOUND", "supporting_quote": "",
                              "reasoning": "受 Content Blueprint 约束的内部建模判断；不主张外部事实权限。",
                              "node_alignment": "EXACT"}
                claim = {
                    "claim_id": claim_id,
                    "requirement_id": f"content.{counter - 1:03d}",
                    "node_id": blueprint["node_id"],
                    "industry": next(iter(base.values()))["claim"]["industry"],
                    "node_identity": identity,
                    "section": section["heading"],
                    "claim_text": sentence["text"],
                    "claim_kind": kind,
                    "claim_role": "editorial_assertion",
                    "rhetorical_role": sentence["rhetorical_role"],
                    "paragraph_focus": paragraph["focus"],
                    "evidence_claim_ids": evidence_ids,
                    "believed_source": believed_source,
                    "believed_locator": believed_locator,
                    "attribution_confidence": "controlled",
                }
                row = {"claim": claim, "searchResult": None, "fetchResult": fetch_result,
                       "verify": verify}
                added.append(row)
                sentence["editorial_claim_id"] = claim_id
    all_rows = base_rows + added
    kus = distill(all_rows)
    by_claim = {item["claim_id"]: item for item in kus}
    body_parts: list[str] = []
    referenced_evidence_ids: set[str] = set()
    for section in content["sections"]:
        body_parts.extend([f"## {section['heading']}", ""])
        for paragraph in section["paragraphs"]:
            rendered: list[str] = []
            for sentence in paragraph["sentences"]:
                ku = by_claim[sentence["editorial_claim_id"]]
                evidence_ids = sentence.get("evidence_claim_ids") or []
                if ku["claim_kind"] == "external_fact":
                    cite = "".join(f" [^{by_claim[source_id]['ku_id']}]" for source_id in evidence_ids)
                    referenced_evidence_ids.update(evidence_ids)
                elif ku["claim_kind"] == "internal_graph_fact":
                    cite = " [^internal-graph]"
                else:
                    cite = ""
                rendered.append(f"{sentence['text']}{cite}")
            body_parts.extend([" ".join(rendered), ""])
    footnote_kus = [
        ku for ku in kus
        if ku.get("claim_id") in referenced_evidence_ids
        or (
            ku.get("claim_role") == "editorial_assertion"
            and ku.get("claim_kind") == "internal_graph_fact"
        )
    ]
    body_parts.append(render_footnotes(footnote_kus).strip())
    body = "\n".join(body_parts).strip() + "\n"
    return {
        "protocol": "wiki-content-enriched-v1",
        "editorial_review_attestation": {
            "protocol": editorial_review["protocol"],
            "verdict": editorial_review["verdict"],
            "sha256": hashlib.sha256(editorial_review_path.read_bytes()).hexdigest(),
            "policy_protocol": editorial_policy["protocol"],
            "policy_decision": editorial_policy["decision"],
            "publication_mode": editorial_policy["publication_mode"],
            "policy_sha256": hashlib.sha256(editorial_policy_path.read_bytes()).hexdigest(),
        },
        "nodes": {blueprint["node_id"]: {
            "body": body,
            "evidence_tables": render_evidence_tables(blueprint),
            "claims": all_rows,
            "kus": kus,
            "research_claim_ids": sorted(base),
            "content_claim_ids": [row["claim"]["claim_id"] for row in added],
            "scorecard": {**scorecard, "research_claims": len(rows), "content_claims": len(added),
                          "total_claims": len(all_rows),
                          "editorial_review": editorial_review["verdict"],
                          "editorial_policy": editorial_policy["decision"]},
        }},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verify_output", type=Path)
    parser.add_argument("content_result", type=Path)
    parser.add_argument("blueprint", type=Path)
    parser.add_argument("editorial_review", type=Path)
    parser.add_argument("editorial_policy", type=Path)
    parser.add_argument("publication_mode", choices=("preview", "reviewed"))
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = enrich(args.verify_output.resolve(), args.content_result.resolve(), args.blueprint.resolve(),
                    args.editorial_review.resolve(), args.editorial_policy.resolve(),
                    args.publication_mode)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    node = next(iter(result["nodes"].values()))
    print(json.dumps(node["scorecard"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
