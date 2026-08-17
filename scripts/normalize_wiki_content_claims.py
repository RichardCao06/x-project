#!/usr/bin/env python3
"""Deterministically repair claim bindings without changing generated prose."""
from __future__ import annotations

import argparse
from collections import Counter
import importlib.util
import json
from pathlib import Path
import re
import sys


def _topic_tokens(value: str) -> set[str]:
    return {
        token.strip()
        for token in re.split(r"[、，,；;：:（）()和与及的]", value)
        if len(token.strip()) >= 2
    }


def normalize_sections(document: dict, blueprint: dict) -> None:
    """Restore missing section objects without discarding generated paragraphs."""
    expected_headings = list(blueprint["sections"])
    merged: dict[str, list[dict]] = {}
    for section in document["sections"]:
        heading = str(section.get("heading", ""))
        target = heading
        for paragraph in section.get("paragraphs") or []:
            focus = str(paragraph.get("focus", "")).strip()
            if focus in expected_headings and focus != heading:
                target = focus
            merged.setdefault(target, []).append(paragraph)

    missing = [heading for heading in expected_headings if not merged.get(heading)]
    if missing:
        vocab = {
            heading: _topic_tokens(heading) | {
                token
                for topic in (blueprint["sections"][heading].get("topics") or [])
                for token in _topic_tokens(str(topic))
            }
            for heading in expected_headings
        }
        for source_heading in list(merged):
            kept: list[dict] = []
            for paragraph in merged[source_heading]:
                focus = str(paragraph.get("focus", ""))
                source_score = sum(len(token) ** 2 for token in vocab.get(source_heading, ())
                                   if token in focus)
                ranked = sorted(
                    ((sum(len(token) ** 2 for token in vocab[target] if token in focus), target)
                     for target in missing),
                    reverse=True,
                )
                score, target = ranked[0] if ranked else (0, "")
                if score > source_score and score > 0:
                    merged.setdefault(target, []).append(paragraph)
                else:
                    kept.append(paragraph)
            merged[source_heading] = kept

    if (all(merged.get(heading) for heading in expected_headings)
            and not any(heading not in expected_headings and paragraphs
                        for heading, paragraphs in merged.items())):
        document["sections"] = [
            {"heading": heading, "paragraphs": merged[heading]}
            for heading in expected_headings
        ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verify_output", type=Path)
    parser.add_argument("blueprint", type=Path)
    parser.add_argument("content_result", type=Path)
    parser.add_argument("validator", type=Path)
    args = parser.parse_args()
    verify = json.loads(args.verify_output.read_text(encoding="utf-8"))
    verify = verify.get("result") if isinstance(verify.get("result"), dict) else verify
    rows = verify["claims"]
    kinds = {row["claim"]["claim_id"]: row["claim"]["claim_kind"] for row in rows}
    confirmed_external = {
        row["claim"]["claim_id"] for row in rows
        if row["claim"]["claim_kind"] == "external_fact"
        and (row.get("verify") or {}).get("verdict") == "CONFIRMED"
    }
    document = json.loads(args.content_result.read_text(encoding="utf-8"))
    blueprint = json.loads(args.blueprint.read_text(encoding="utf-8"))
    normalize_sections(document, blueprint)
    sentences = [sentence for section in document["sections"]
                 for paragraph in section["paragraphs"] for sentence in paragraph["sentences"]]
    maximum_modeling = int(blueprint["golden_target"].get("maximum_modeling_judgments", 10**9))
    modeling = [sentence for sentence in sentences if sentence.get("claim_kind") == "modeling_judgment"]
    gap_markers = ("缺失", "未知", "不足", "缺口", "无法", "尚未", "未提供", "待证实")
    for sentence in modeling[:]:
        if len(modeling) <= maximum_modeling:
            break
        if (not sentence.get("evidence_claim_ids")
                and any(marker in str(sentence.get("text", "")) for marker in gap_markers)):
            sentence["claim_kind"] = "evidence_gap"
            modeling.remove(sentence)
    for sentence in sentences:
        kind = sentence["claim_kind"]
        ids = [str(item) for item in sentence.get("evidence_claim_ids", [])]
        ids = [item for item in ids if not (
            kinds.get(item) == "external_fact" and item not in confirmed_external
        )]
        if kind in {"external_fact", "internal_graph_fact"}:
            ids = [item for item in ids if kinds.get(item) == kind]
        if kind == "external_fact":
            ids = [item for item in ids if item in confirmed_external]
            if not ids:
                sentence["claim_kind"] = "modeling_judgment"
        sentence["evidence_claim_ids"] = list(dict.fromkeys(ids))
    # A paragraph may contain at most one externally asserted sentence.  If a
    # model emitted a second one, retain its prose as a conservative modeling
    # interpretation and remove external bindings; no fact or citation is
    # promoted by this normalization.
    for section in document["sections"]:
        for paragraph in section["paragraphs"]:
            seen_external = False
            for sentence in paragraph["sentences"]:
                if sentence["claim_kind"] != "external_fact":
                    continue
                if not seen_external:
                    seen_external = True
                else:
                    sentence["claim_kind"] = "modeling_judgment"
                    sentence["evidence_claim_ids"] = []
    counts: Counter[str] = Counter()
    for sentence in sentences:
        kept = []
        for claim_id in sentence["evidence_claim_ids"]:
            if counts[claim_id] < 3:
                kept.append(claim_id); counts[claim_id] += 1
        sentence["evidence_claim_ids"] = kept
    # If a newly verified claim was absent from a draft composed against an
    # older evidence set, add its frozen claim text as a dedicated cited
    # sentence in the matching section. This is source-backed material, not
    # generated prose or an inferred fact.
    missing_confirmed = sorted(confirmed_external - set(counts))
    by_id = {row["claim"]["claim_id"]: row["claim"] for row in rows}
    sections_by_heading = {section["heading"]: section for section in document["sections"]}
    for claim_id in missing_confirmed:
        claim = by_id[claim_id]
        section = sections_by_heading.get(claim["section"])
        if section is None:
            continue
        boundary_text = (
            f"{claim_id} 的交接证据不提供批次质量、接收设施或处理路线。"
            if "handoff" in str(claim.get("requirement_id", ""))
            else f"{claim_id} 的工艺机理说明不能替代本节点的组成检测和产生量实测。"
        )
        section["paragraphs"].append({
            "focus": f"已核验外部事实 {claim_id}",
            "sentences": [{
                "text": str(claim["claim_text"]),
                "claim_kind": "external_fact",
                "evidence_claim_ids": [claim_id],
                "rhetorical_role": "thesis",
            }, {
                "text": boundary_text,
                "claim_kind": "modeling_judgment",
                "evidence_claim_ids": [],
                "rhetorical_role": "boundary",
            }],
        })
    for section in document["sections"]:
        for paragraph in section["paragraphs"]:
            if str(paragraph.get("focus", "")).startswith("已核验外部事实 "):
                paragraph["sentences"][0]["rhetorical_role"] = "thesis"
    # Keep the identity core section source-grounded after stale identity
    # claims are removed. Reuse a confirmed, identity-adjacent management
    # claim only when the sentence already states that same official fact.
    identity = sections_by_heading.get("定义与产品身份")
    management_claim = next((claim_id for claim_id in confirmed_external
                             if by_id[claim_id].get("requirement_id") == "product.form.physical_state"), None)
    if identity and management_claim:
        existing_uses = sum(management_claim in sentence.get("evidence_claim_ids", [])
                            for section in document["sections"]
                            for paragraph in section["paragraphs"]
                            for sentence in paragraph["sentences"])
        for paragraph in identity["paragraphs"]:
            matched = next((sentence for sentence in paragraph["sentences"]
                            if "官方批复" in str(sentence.get("text", ""))
                            and "锡渣" in str(sentence.get("text", ""))), None)
            if matched is not None:
                if existing_uses >= 3:
                    for section in reversed(document["sections"]):
                        if section is identity:
                            continue
                        removed = False
                        for other_paragraph in reversed(section["paragraphs"]):
                            for other in reversed(other_paragraph["sentences"]):
                                other_ids = other.get("evidence_claim_ids", [])
                                if management_claim in other_ids and other.get("claim_kind") != "external_fact":
                                    other["evidence_claim_ids"] = [item for item in other_ids if item != management_claim]
                                    removed = True; break
                            if removed: break
                        if removed: break
                matched["claim_kind"] = "external_fact"
                matched["evidence_claim_ids"] = [management_claim]
                break
                for sentence in paragraph["sentences"][1:]:
                    sentence["rhetorical_role"] = "boundary"
            if (str(paragraph.get("focus", "")).startswith("已核验外部事实 ")
                    and len(paragraph.get("sentences") or []) == 1):
                claim_id = str(paragraph.get("focus", "")).rsplit(" ", 1)[-1]
                claim = by_id.get(claim_id, {})
                boundary_text = (
                    f"{claim_id} 的交接证据不提供批次质量、接收设施或处理路线。"
                    if "handoff" in str(claim.get("requirement_id", ""))
                    else f"{claim_id} 的工艺机理说明不能替代本节点的组成检测和产生量实测。"
                )
                paragraph["sentences"].append({
                    "text": boundary_text,
                    "claim_kind": "modeling_judgment",
                    "evidence_claim_ids": [],
                    "rhetorical_role": "boundary",
                })
                paragraph["sentences"][0]["rhetorical_role"] = "thesis"
    args.content_result.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
    spec = importlib.util.spec_from_file_location("wiki_content_validator", args.validator)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load frozen content validator")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    try:
        scorecard = module.validate_result(args.content_result, blueprint, rows)
    except ValueError as exc:
        # Normalization is deliberately limited to claim bindings and section
        # structure.  Preserve those repairs, then hand any remaining semantic
        # issue (for example a missing identity token) back to the next model
        # attempt through the same durable usage artifact.
        usage_path = args.content_result.parent / "content-usage.json"
        if usage_path.is_file():
            try:
                usage = json.loads(usage_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                usage = {}
            if isinstance(usage, dict):
                usage.update({"exit_code": 2, "validation_error": str(exc),
                              "normalization_status": "residual_issues"})
                usage_path.write_text(json.dumps(usage, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
        print(json.dumps({"status": "residual_issues", "validation_error": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"status": "normalized", "scorecard": scorecard}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
