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


def _direct_claim_support_score(sentence: dict, claim: dict, position: int) -> tuple[int, int]:
    """Rank fact bindings by direct overlap with the frozen claim.

    A claim-use cap is a presentation constraint, not permission to leave a
    fact sentence without provenance.  Prefer sentences that repeat the
    frozen graph identifiers/directions, and treat interpretive or prescriptive
    prose as the first candidate for demotion when a claim is over capacity.
    The negative position keeps the result deterministic for equal scores.
    """
    text = str(sentence.get("text") or "")
    claim_text = str(claim.get("claim_text") or "")
    identifiers = set(re.findall(r"\b[A-Z]\d{3}\b", claim_text))
    directions = {token for token in ("输入", "输出", "消耗", "生产") if token in claim_text}
    score = 20 * sum(identifier in text for identifier in identifiers)
    score += 5 * sum(direction in text for direction in directions)
    interpretive_markers = (
        "应", "解释", "用于", "相对应", "表示", "对账", "实际发生量",
        "明确", "不将", "不得", "视为", "可按", "建议",
    )
    score -= 3 * sum(marker in text for marker in interpretive_markers)
    return score, -position


def enforce_claim_use_cap(sentences: list[dict], claims_by_id: dict[str, dict],
                          *, maximum: int = 3) -> Counter[str]:
    """Cap bindings while keeping every retained fact sentence valid.

    The old implementation kept the first three uses and silently stripped
    later bindings.  That could produce ``internal_graph_fact`` or
    ``external_fact`` sentences with an empty ``evidence_claim_ids`` array,
    an impossible state that the content validator correctly rejects.  Rank
    graph facts by direct frozen-claim support, preserve at most ``maximum``,
    and deterministically demote overflow fact sentences to modeling
    judgments instead of emitting an invalid draft.
    """
    selected_by_claim: dict[str, set[int]] = {}
    for claim_id, claim in claims_by_id.items():
        candidates = [
            (index, sentence)
            for index, sentence in enumerate(sentences)
            if claim_id in (sentence.get("evidence_claim_ids") or [])
        ]
        if str(claim.get("claim_kind") or "") == "internal_graph_fact":
            ranked = sorted(
                candidates,
                key=lambda item: _direct_claim_support_score(item[1], claim, item[0]),
                reverse=True,
            )
        else:
            ranked = candidates
        selected_by_claim[claim_id] = {index for index, _sentence in ranked[:maximum]}

    counts: Counter[str] = Counter()
    for index, sentence in enumerate(sentences):
        ids = [
            claim_id for claim_id in sentence.get("evidence_claim_ids") or []
            if index in selected_by_claim.get(str(claim_id), set())
        ]
        sentence["evidence_claim_ids"] = ids
        counts.update(ids)
        if (sentence.get("claim_kind") in {"external_fact", "internal_graph_fact"}
                and not ids):
            sentence["claim_kind"] = "modeling_judgment"
    return counts


def restore_direct_internal_graph_bindings(document: dict,
                                           claims_by_id: dict[str, dict]) -> None:
    """Bind conservative ledger restatements to the frozen graph claim.

    Editorial repair may split one composite graph sentence into separate
    input and output sentences.  A model can then label those replacements as
    modeling judgments because an external-search verifier reports NOT_FOUND
    for the internal claim.  That erases the only fact state from the graph
    reconciliation section.  Restore the binding only when the sentence is in
    the claim's own section, names the frozen node and at least one frozen
    flow, states an input/output direction, introduces no foreign graph ID,
    and makes no verification, allocation, or identity inference.
    """
    graph_claims = {
        claim_id: claim for claim_id, claim in claims_by_id.items()
        if str(claim.get("claim_kind") or "") == "internal_graph_fact"
    }
    forbidden_markers = (
        "已核实", "已验证", "证明", "证实", "确认来源",
        "应", "视为", "分配", "建议", "可能", "不得", "异常", "失效",
    )
    for section in document.get("sections") or []:
        heading = str(section.get("heading") or "")
        for paragraph in section.get("paragraphs") or []:
            for sentence in paragraph.get("sentences") or []:
                if sentence.get("claim_kind") != "modeling_judgment":
                    continue
                text = str(sentence.get("text") or "")
                if any(marker in text for marker in forbidden_markers):
                    continue
                text_ids = set(re.findall(r"\b[A-Z]\d{3}\b", text))
                if not text_ids or not any(direction in text for direction in ("输入", "输出")):
                    continue
                for claim_id, claim in graph_claims.items():
                    if str(claim.get("section") or "") != heading:
                        continue
                    claim_ids = set(re.findall(
                        r"\b[A-Z]\d{3}\b", str(claim.get("claim_text") or "")
                    ))
                    node_id = str(claim.get("node_id") or "")
                    flow_ids = claim_ids - ({node_id} if node_id else set())
                    if (node_id and node_id in text_ids and text_ids <= claim_ids
                            and bool(text_ids & flow_ids)):
                        sentence["claim_kind"] = "internal_graph_fact"
                        sentence["evidence_claim_ids"] = [claim_id]
                        break


def claim_boundary_text(claim_id: str, claim: dict) -> str:
    """Return a requirement-specific scope boundary for an inserted fact.

    Reusing one generic sentence for every newly confirmed claim created
    near-duplicate pairs that the same content contract then rejected.  Keep
    the conservative boundary, but bind its wording to the claim's semantic
    role so separate evidence paragraphs have separate editorial duties.
    """
    requirement = str(claim.get("requirement_id") or "")
    if "handoff" in requirement:
        return f"{claim_id} 的交接证据不提供批次质量、接收设施或处理路线。"
    if requirement.startswith("activity.identity"):
        return f"{claim_id} 仅用于界定配置活动身份，不证明具体设备的参数值或生产批次。"
    if requirement.startswith("activity.boundary"):
        return f"{claim_id} 只界定纳入操作或完成状态，不提供单台设备的资源消耗、工时或产出数量。"
    if requirement.startswith("activity.route"):
        return f"{claim_id} 只说明技术路线或相邻活动边界，不替代节点投入产出的实测对账。"
    if requirement.startswith("activity.environment"):
        return f"{claim_id} 只限定环境控制范围，不提供本节点排放或废物产生量。"
    return f"{claim_id} 的事实范围不能替代本节点的组成检测、产生量实测或批次记录。"


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
    by_id = {row["claim"]["claim_id"]: row["claim"] for row in rows}
    kinds = {claim_id: claim["claim_kind"] for claim_id, claim in by_id.items()}
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
    restore_direct_internal_graph_bindings(document, by_id)
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
    counts = enforce_claim_use_cap(sentences, by_id)
    # Confirmed evidence is a candidate pool, not a requirement to publish
    # every verified claim.  The content validator deliberately treats unused
    # confirmed claims as advisory (``not_selected_for_prose``).  Older code
    # appended every missing claim as an ``已核验外部事实 A019-N`` paragraph.
    # That reintroduced evidence-card prose after editorial repair deleted or
    # moved it, producing citation intrusion, claim dumps, and a non-converging
    # review loop.  Preserve the editor's selection: core-question closure and
    # section evidence gates decide whether enough evidence remains.
    missing_confirmed: list[str] = []
    sections_by_heading = {section["heading"]: section for section in document["sections"]}
    for claim_id in missing_confirmed:
        claim = by_id[claim_id]
        section = sections_by_heading.get(claim["section"])
        if section is None:
            continue
        boundary_text = claim_boundary_text(claim_id, claim)
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
                boundary_text = claim_boundary_text(claim_id, claim)
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
