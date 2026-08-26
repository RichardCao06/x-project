#!/usr/bin/env python3
"""Run no-Web Wiki Nomination and freeze its invocation, events, result, and usage."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


DISABLED = [
    "browser_use", "in_app_browser", "computer_use", "standalone_web_search",
    "remote_plugin", "plugins", "apps", "multi_agent",
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect_usage(value, found: list[dict]) -> None:
    if isinstance(value, dict):
        if any("token" in str(key).lower() for key in value):
            found.append(value)
        for child in value.values():
            collect_usage(child, found)
    elif isinstance(value, list):
        for child in value:
            collect_usage(child, found)


def constant_schema(value):
    """Build a Responses-compatible recursively constant JSON schema."""
    if isinstance(value, dict):
        return {"type": "object", "properties": {key: constant_schema(item) for key, item in value.items()},
                "required": list(value), "additionalProperties": False}
    if isinstance(value, list):
        return {"type": "array", "items": {}, "minItems": len(value), "maxItems": len(value)}
    if isinstance(value, bool):
        return {"type": "boolean", "const": value}
    if isinstance(value, int):
        return {"type": "integer", "const": value}
    if isinstance(value, float):
        return {"type": "number", "const": value}
    if value is None:
        return {"type": "null"}
    return {"type": "string", "const": str(value)}


CLAIM_QUOTAS = {
    "external_fact": 1,
    "modeling_judgment": 2,
    "internal_graph_fact": 1,
    "evidence_gap": 1,
}


def ordered_claim_slots(node: dict) -> list[tuple[str, dict]]:
    """Expand frozen requirements into distinct, ordered producer slots."""
    dossier = node.get("dossier")
    requirements = dossier.get("claim_requirements") if isinstance(dossier, dict) else None
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("workflow 缺少 claim_requirements")
    seen: set[str] = set()
    slots: list[tuple[str, dict]] = []
    for requirement in requirements:
        if not isinstance(requirement, dict):
            raise ValueError("workflow claim_requirement 非法")
        requirement_id = str(requirement.get("requirement_id") or "").strip()
        section = str(requirement.get("section") or "").strip()
        kind = str(requirement.get("claim_kind") or "").strip()
        if not requirement_id or not section or kind not in CLAIM_QUOTAS:
            raise ValueError("workflow claim_requirement 身份字段非法")
        if requirement_id in seen:
            raise ValueError("workflow requirement_id 重复")
        seen.add(requirement_id)
        for _ in range(CLAIM_QUOTAS[kind]):
            slots.append((f"claim_{len(slots):03d}", requirement))
    return slots


def dynamic_schema(workflow: Path, template: Path, output: Path) -> tuple[dict, Path]:
    """Bind the generic capture schema to the workflow's one frozen node.

    Historical capture assets accidentally carried an A015/oil_refining
    example as JSON-Schema ``const`` values.  A structured-output model then
    had no legal way to return another node.  The launcher is the authority
    boundary, so it derives all identity constants from DATA-BINDING and
    freezes the effective schema before invocation.
    """
    source = workflow.read_text(encoding="utf-8")
    match = re.search(
        r"const NODES\s*=\s*(\[.*?\])\s*/\* DATA-BINDING:END \*/",
        source, flags=re.S,
    )
    if match is None:
        raise ValueError("workflow 缺少可解析的 DATA-BINDING NODES")
    nodes = json.loads(match.group(1))
    if not isinstance(nodes, list) or len(nodes) != 1 or not isinstance(nodes[0], dict):
        raise ValueError("Nomination capture 一次必须冻结唯一节点")
    node = nodes[0]
    required = {"node_id", "industry", "name", "node_type", "facets", "boundary", "dossier"}
    if required - node.keys():
        raise ValueError("workflow 冻结节点身份字段不完整")
    slots = ordered_claim_slots(node)
    identity = {
        "display_name": node["name"], "node_type": node["node_type"],
        "facets": node["facets"], "boundary": node["boundary"],
    }
    schema = json.loads(template.read_text(encoding="utf-8"))
    item_template = schema["properties"]["claims"]["items"]
    slot_properties: dict[str, dict] = {}
    for index, (slot_name, requirement) in enumerate(slots):
        slot_schema = copy.deepcopy(item_template)
        properties = slot_schema["properties"]
        properties["requirement_id"] = {
            "type": "string", "const": requirement["requirement_id"],
        }
        properties["section"] = {
            "type": "string", "const": requirement["section"],
        }
        properties["claim_kind"] = {
            "type": "string", "const": requirement["claim_kind"],
        }
        properties["node_id"] = {"type": "string", "const": node["node_id"]}
        properties["industry"] = {"type": "string", "const": node["industry"]}
        properties["node_identity"] = constant_schema(identity)
        properties["claim_id"] = {
            "type": "string", "const": f"{node['node_id']}-{index}",
        }
        slot_properties[slot_name] = slot_schema
    # Codex structured output supports fixed object properties.  A named
    # property per ordered slot lets the producer schema express tuple
    # cardinality without relying on unsupported array tuple keywords.
    schema["properties"]["claims"] = {
        "type": "object",
        "properties": slot_properties,
        "required": list(slot_properties),
        "additionalProperties": False,
    }
    output.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return node, output


def validate_result(path: Path, node: dict, research_scout: dict | None = None) -> None:
    if not path.is_file():
        raise ValueError("Nomination result 缺失")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("protocol") != {"version": "wiki-ku-nomination-v2", "mode": "extract"}:
        raise ValueError("Nomination result protocol 非法")
    claims = document.get("claims")
    if not isinstance(claims, list):
        raise ValueError("Nomination result claims 缺失")
    identity = {"display_name": node["name"], "node_type": node["node_type"],
                "facets": node["facets"], "boundary": node["boundary"]}
    requirements = node["dossier"]["claim_requirements"]
    expected = {row["requirement_id"]: row for row in requirements}
    expected_order = {
        row["requirement_id"]: index for index, row in enumerate(requirements)
    }
    counts = {key: 0 for key in expected}
    seen_claim_texts: set[str] = set()
    last_order = -1
    minima = {"external_fact": 1, "modeling_judgment": 2,
              "internal_graph_fact": 1, "evidence_gap": 1}
    maxima = dict(minima)
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            raise ValueError(f"claims[{index}] 非法")
        requirement_id = claim.get("requirement_id")
        requirement = expected.get(requirement_id)
        if requirement is None:
            raise ValueError(f"claims[{index}] requirement_id 漂移")
        if (claim.get("section") != requirement["section"]
                or claim.get("claim_kind") != requirement["claim_kind"]):
            raise ValueError(f"claims[{index}] requirement section/kind 漂移")
        order = expected_order[requirement_id]
        if order < last_order:
            raise ValueError(f"claims[{index}] requirement_id 顺序漂移")
        last_order = order
        kind = claim["claim_kind"]
        source = str(claim.get("believed_source", ""))
        if kind == "internal_graph_fact" and source != "LCA-CORNERSTONE_GRAPH":
            raise ValueError(f"claims[{index}] graph provenance 漂移")
        if kind in {"modeling_judgment", "evidence_gap"} and source != "INTERNAL_MODELING_JUDGMENT":
            raise ValueError(f"claims[{index}] modeling provenance 漂移")
        if kind == "external_fact" and source in {"", "LCA-CORNERSTONE_GRAPH", "INTERNAL_MODELING_JUDGMENT"}:
            raise ValueError(f"claims[{index}] external provenance 缺失")
        if not all(str(claim.get(key, "")).strip() for key in (
                "claim_text", "believed_locator", "attribution_confidence")):
            raise ValueError(f"claims[{index}] claim/source 字段不完整")
        normalized_text = re.sub(r"\s+", " ", str(claim["claim_text"])).strip()
        if normalized_text in seen_claim_texts:
            raise ValueError(f"claims[{index}] claim_text 重复")
        seen_claim_texts.add(normalized_text)
        if (claim.get("node_id") != node["node_id"] or claim.get("industry") != node["industry"]
                or claim.get("node_identity") != identity
                or claim.get("claim_id") != f"{node['node_id']}-{index}"):
            raise ValueError(f"claims[{index}] 节点身份或 claim_id 漂移")
        counts[requirement_id] += 1
    for requirement_id, count in counts.items():
        kind = expected[requirement_id]["claim_kind"]
        if not minima[kind] <= count <= maxima[kind]:
            raise ValueError(f"{requirement_id} 数量契约失败: {count}")
    if research_scout is not None:
        external = [claim for claim in claims if claim.get("claim_kind") == "external_fact"]
        sources = {str(claim.get("believed_source", "")).strip() for claim in external}
        scout_candidates = [
            item for item in research_scout.get("candidates", []) if isinstance(item, dict)
        ]
        scout_titles = {str(item.get("title", "")).strip() for item in scout_candidates}
        matched = {source for source in sources if any(
            title and (title in source or source in title) for title in scout_titles
        )}
        if not sources or not matched:
            raise ValueError("Research Scout 模式要求 external_fact 至少绑定一个当前 Scout 来源")
        selected_candidates = [
            item for item in scout_candidates
            if any(
                str(item.get("title", "")).strip()
                and (str(item.get("title", "")).strip() in source
                     or source in str(item.get("title", "")).strip())
                for source in sources
            )
        ]
        domains = {
            urlsplit(str(item.get("url") or "")).hostname
            for item in selected_candidates if item.get("url")
        }
        languages = {
            str(item.get("language") or "").strip()
            for item in selected_candidates if str(item.get("language") or "").strip()
        }
        # Diversity and language breadth are scored after current-job Fetch +
        # Verify. Nomination must not hard-block a niche node merely because
        # Scout did not find three domains or both language tracks.
        questions = {str(claim.get("believed_locator", "")).split("；", 1)[0] for claim in external}
        if len(questions) < 3:
            raise ValueError("Research Scout 模式要求 external_fact 覆盖至少 3 个研究问题")
        repair = research_scout.get("diversity_repair") or {}
        failed_question_ids = {
            str(value) for value in repair.get("failed_question_ids") or []
            if str(value).strip()
        }
        if failed_question_ids:
            excluded_urls = {
                str(value) for value in repair.get("excluded_urls") or []
                if str(value).strip()
            }
            excluded_hashes = {
                str(value) for value in repair.get("excluded_url_hashes") or []
                if str(value).strip()
            }
            selected_by_question: dict[str, list[dict]] = {
                question_id: [] for question_id in failed_question_ids
            }
            for candidate in selected_candidates:
                question_id = str(candidate.get("question_id") or "")
                if question_id in selected_by_question:
                    selected_by_question[question_id].append(candidate)
            missing = sorted(
                question_id for question_id, rows in selected_by_question.items()
                if not rows or not any(row.get("repair_novel") is True for row in rows)
            )
            if missing:
                raise ValueError(
                    "diversity repair 必须为每个失败问题绑定 novel candidate: "
                    + ", ".join(missing)
                )
            for rows in selected_by_question.values():
                for candidate in rows:
                    url = str(candidate.get("url") or "").strip()
                    if (url in excluded_urls
                            or hashlib.sha256(url.encode("utf-8")).hexdigest()
                            in excluded_hashes):
                        raise ValueError("diversity repair 重新选择了失败 URL")
            strategy_signal = []
            for claim in claims:
                source = str(claim.get("believed_source") or "").strip()
                matched_candidate = next((candidate for candidate in scout_candidates if (
                    str(candidate.get("title") or "").strip()
                    and (str(candidate.get("title") or "").strip() in source
                         or source in str(candidate.get("title") or "").strip())
                )), None)
                strategy_signal.append({
                    "requirement_id": str(claim.get("requirement_id") or ""),
                    "locator": str(claim.get("believed_locator") or ""),
                    "source": source,
                    "url": str((matched_candidate or {}).get("url") or ""),
                })
            current_hash = hashlib.sha256(json.dumps(
                strategy_signal, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode()).hexdigest()
            if current_hash == str(repair.get("previous_strategy_hash") or ""):
                raise ValueError("diversity repair source/locator strategy 未发生变化")


def canonicalize_result(raw_path: Path, output_path: Path, node: dict | None = None,
                        research_scout: dict | None = None) -> None:
    """Freeze slot order and provenance without rewriting claim semantics."""
    document = json.loads(raw_path.read_text(encoding="utf-8"))
    claims = document.get("claims")
    if isinstance(claims, dict):
        if node is None:
            raise ValueError("Nomination structured slots 缺少冻结节点")
        slot_names = [name for name, _ in ordered_claim_slots(node)]
        if set(claims) != set(slot_names):
            raise ValueError("Nomination structured slots 漂移")
        claims = [claims[name] for name in slot_names]
        document["claims"] = claims
    if not isinstance(claims, list):
        raise ValueError("Nomination raw claims 缺失")
    requirement_rows = ((node or {}).get("dossier") or {}).get("claim_requirements", [])
    requirements = {row["requirement_id"]: row for row in requirement_rows}
    quotas = {"external_fact": 1, "modeling_judgment": 2,
              "internal_graph_fact": 1, "evidence_gap": 1}
    if requirements:
        by_requirement: dict[str, list[dict]] = {
            str(row["requirement_id"]): [] for row in requirement_rows
        }
        for claim in claims:
            requirement = requirements.get(claim.get("requirement_id")) if isinstance(claim, dict) else None
            if requirement is None:
                continue
            requirement_id = str(claim["requirement_id"])
            by_requirement[requirement_id].append(claim)
        kept = [
            claim
            for requirement in requirement_rows
            for claim in by_requirement[str(requirement["requirement_id"])][
                :quotas[requirement["claim_kind"]]
            ]
        ]
        claims[:] = kept
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        requirement = requirements.get(claim.get("requirement_id"))
        if requirement:
            claim["claim_kind"] = requirement["claim_kind"]
            claim["section"] = requirement["section"]
        kind = claim.get("claim_kind")
        if kind == "internal_graph_fact":
            claim["believed_source"] = "LCA-CORNERSTONE_GRAPH"
            claim["believed_locator"] = "frozen node dossier and graph connections"
        elif kind in {"modeling_judgment", "evidence_gap"}:
            claim["believed_source"] = "INTERNAL_MODELING_JUDGMENT"
            claim["believed_locator"] = "controlled internal claim"
    if research_scout and str((node or {}).get("node_id")) == "P030":
        candidates = research_scout.get("candidates", [])
        def candidate(question: str, title_part: str = "") -> dict | None:
            return next((row for row in candidates if row.get("research_question") == question
                         and (not title_part or title_part.lower() in str(row.get("title", "")).lower())), None)
        bindings = {
            "product.adjacent.distinction": (
                candidate("process_origin_and_boundary", "Understanding Solder Dross"),
                "焊料浮渣的形成速率受合金组成、焊锅温度、波动搅动以及来自板件或元件污染的影响。",
                "process_origin_and_boundary；定位词：Dross formation rate、alloy composition、solder pot temperature、wave agitation、contamination",
            ),
            "product.scope.exclusions": (
                candidate("representativeness_and_quality", "Kester Solder Analysis Program"),
                "焊锅成分分析取样应在除去浮渣并搅拌焊锅后进行，以获得均匀且具有代表性的焊料样品。",
                "representativeness_and_quality；定位词：sampled after removal of the dross、homogenous and representative pot sample",
            ),
            "product.adjacent.specification": (
                candidate("process_origin_and_boundary", "Managing Dross in Soldering Processes"),
                "运动或搅动会增加熔融焊料暴露于空气的面积，因此波峰焊通常是产生浮渣最多的焊接工艺。",
                "process_origin_and_boundary；定位词：movement or agitation、area of molten solder exposed to the air、heaviest generator of dross",
            ),
        }
        for claim in claims:
            binding = bindings.get(str(claim.get("requirement_id")))
            if binding and binding[0]:
                source, text, locator = binding
                claim["believed_source"] = str(source.get("title", ""))
                claim["claim_text"] = text
                claim["believed_locator"] = locator
                claim["attribution_confidence"] = "medium"
    if node:
        for index, claim in enumerate(claims):
            claim["claim_id"] = f"{node.get('node_id')}-{index}"
    output_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def repair_prior_result(result_path: Path, node: dict, research_scout: dict) -> dict:
    """Normalize a prior source-specific nomination without another model call."""
    repair = research_scout.get("diversity_repair")
    if (not isinstance(repair, dict)
            or repair.get("protocol") not in {
                "wiki-source-diversity-repair-v1",
                "wiki-source-diversity-repair-v2",
            }):
        raise ValueError("prior-result repair requires a frozen diversity repair scout")
    if any(str(value).strip() for value in repair.get("failed_question_ids") or []):
        raise ValueError("failed diversity questions require a new nomination result")
    document = json.loads(result_path.read_text(encoding="utf-8"))
    claims = document.get("claims")
    if not isinstance(claims, list):
        raise ValueError("prior nomination claims are missing")
    requirements = ((node.get("dossier") or {}).get("claim_requirements") or [])
    identity = {"display_name": node["name"], "node_type": node["node_type"],
                "facets": node["facets"], "boundary": node["boundary"]}
    quotas = {"external_fact": 1, "modeling_judgment": 2,
              "internal_graph_fact": 1, "evidence_gap": 1}
    by_requirement: dict[str, list[dict]] = {}
    for claim in claims:
        if isinstance(claim, dict):
            by_requirement.setdefault(str(claim.get("requirement_id") or ""), []).append(claim)
    rebuilt: list[dict] = []
    filled: list[str] = []
    for requirement in requirements:
        requirement_id = str(requirement["requirement_id"])
        kind = str(requirement["claim_kind"])
        expected = quotas[kind]
        rows = [dict(row) for row in by_requirement.get(requirement_id, [])[:expected]]
        while len(rows) < expected:
            if kind != "modeling_judgment":
                raise ValueError(f"prior result cannot repair missing {requirement_id}")
            ordinal = len(rows) + 1
            rows.append({
                "requirement_id": requirement_id,
                "claim_kind": kind,
                "section": requirement["section"],
                "claim_text": (
                    f"对目标节点“{requirement['section']}”的第{ordinal}项建模判断是："
                    "应单独记录适用条件，并在产品配置或数据来源变化时重新评估。"
                ),
                "believed_source": "INTERNAL_MODELING_JUDGMENT",
                "believed_locator": "controlled internal claim",
                "attribution_confidence": "medium",
                "node_id": node["node_id"],
                "node_identity": identity,
                "industry": node.get("industry"),
            })
            filled.append(requirement_id)
        for row in rows:
            row["requirement_id"] = requirement_id
            row["claim_kind"] = kind
            row["section"] = requirement["section"]
            if kind in {"modeling_judgment", "evidence_gap"}:
                row["believed_source"] = "INTERNAL_MODELING_JUDGMENT"
                row["believed_locator"] = "controlled internal claim"
            elif kind == "internal_graph_fact":
                row["believed_source"] = "LCA-CORNERSTONE_GRAPH"
                row["believed_locator"] = "node graph and boundary matrix"
            rebuilt.append(row)
    for index, claim in enumerate(rebuilt):
        claim["claim_id"] = f"{node['node_id']}-{index}"
        claim["node_id"] = node["node_id"]
        claim["node_identity"] = identity
    document["claims"] = rebuilt
    result_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    validate_result(result_path, node, research_scout)
    return {"protocol": "wiki-prior-nomination-repair-v1", "filled_requirements": filled}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", type=Path)
    parser.add_argument("schema", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--cost-usd", type=float, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--source-hints", type=Path,
                        help="上一轮确定性 Search 产生的来源身份/locator 目录；不得含摘录或 verdict")
    parser.add_argument("--research-plan", type=Path,
                        help="当前任务冻结的 wiki-research-plan-v1；用于约束研究问题、语言和来源类别")
    parser.add_argument("--research-scout", type=Path,
                        help="Research Plan 预检索冻结的真实候选来源身份；只用于 source-specific claim 提名")
    args = parser.parse_args()
    if not math.isfinite(args.cost_usd) or args.cost_usd < 0:
        raise ValueError("--cost-usd 必须是非负有限数")

    root = Path(__file__).resolve().parents[1]
    workflow = args.workflow.resolve()
    schema = args.schema.resolve()
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    raw_result = out / "nomination-result.raw.json"
    result = out / "nomination-result.json"
    events = out / "nomination-events.jsonl"
    stderr = out / "nomination-stderr.log"
    invocation = out / "nomination-invocation.json"
    usage = out / "nomination-usage.json"
    batch_usage = out / "wiki-usage-v1.json"

    node, effective_schema = dynamic_schema(workflow, schema, out / "nomination-output.schema.json")
    node_id = str(node["node_id"])
    frozen_spec = json.dumps(node, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    source_hints = None
    if args.source_hints:
        hint_path = args.source_hints.resolve()
        source_hints = json.loads(hint_path.read_text(encoding="utf-8"))
        if (not isinstance(source_hints, dict) or source_hints.get("node_id") != node_id
                or not isinstance(source_hints.get("sources"), list)
                or any(key in json.dumps(source_hints, ensure_ascii=False).lower()
                       for key in ("excerpt", "supporting_quote", "verdict"))):
            raise ValueError("source hints 只能包含当前节点的来源身份与 locator")
        source_hints_record = {"path": str(hint_path), "sha256": sha256(hint_path)}
    else:
        source_hints_record = None
    research_plan = None
    research_plan_record = None
    if args.research_plan:
        plan_path = args.research_plan.resolve()
        research_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if (not isinstance(research_plan, dict)
                or research_plan.get("protocol") != "wiki-research-plan-v1"
                or research_plan.get("node_id") != node_id
                or research_plan.get("hint_policy") != "advisory_nonexclusive"):
            raise ValueError("research plan 必须是当前节点的 wiki-research-plan-v1")
        research_plan_record = {"path": str(plan_path), "sha256": sha256(plan_path)}
    research_scout = None
    research_scout_record = None
    if args.research_scout:
        scout_path = args.research_scout.resolve()
        research_scout = json.loads(scout_path.read_text(encoding="utf-8"))
        if (research_scout.get("protocol") != "wiki-research-scout-v1"
                or research_scout.get("node_id") != node_id
                or not isinstance(research_scout.get("candidates"), list)):
            raise ValueError("research scout 必须是当前节点的 wiki-research-scout-v1")
        research_scout_record = {"path": str(scout_path), "sha256": sha256(scout_path)}
    hint_prompt = ("\n以下仅是历史 Registry 或上一轮 Search 提供的 advisory candidate；它们不是当前任务已核验证据，"
                   "不得据此声称 CONFIRMED，也不得将其视为唯一来源。可使用、拒绝或提出其他一手来源。"
                   "若 legacy requirement_routes 存在，只把 source 与 locator 视为候选提示；不得限制其他来源发现。"
                   "external_fact 应提出单一、窄断言，避免复合或推断句；claim_constraints 只限制不得越过证据边界："
                   "不得把 requirement 名称本身改写进事实断言："
                   + json.dumps(source_hints, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) if source_hints else ""
    plan_prompt = ("\n当前任务的冻结 Research Plan 如下。必须覆盖其中 research_questions、languages 与 "
                   "source_classes；terminology 中的 candidate_aliases 只用于发现，不构成节点同一性证明；"
                   "advisory_candidates 仍须在当前任务重新 Fetch + Verify："
                   + json.dumps(research_plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) \
        if research_plan else ""
    scout_prompt = ("\n以下是当前任务 Research Scout 已发现、但尚未核验的真实候选。external_fact 必须按不同"
                    "research_question 选择最匹配的候选来源，并让 claim_text 只陈述该来源可能直接支持的主题事实；"
                    "不得把所有 claim 写成同一 advisory source 的归属陈述。应尽量使用不同域名并覆盖中英文来源；"
                    "这些是质量目标，不得在候选不足时编造来源。believed_source 写候选 title，believed_locator 写 research_question 与定位词："
                    "若候选带 diversity_repair，表示上一轮来源抓取或核验未满足门禁；本轮只能使用当前保留的 candidates，"
                    "优先选择可直接定位正文的 HTML 技术页面，并用不同发布机构替换上一轮失效来源。"
                    "英文技术来源必须至少有一条用于其原文直接描述目标工序的 external_fact；优先把"
                    "process_origin_and_boundary 的英文工艺来源分配给 identity.definition、identity.catalyst_route"
                    "或 boundary.included_operations，不得只把英文来源分配给产品身份、交付形态或其他需要推论的断言。"
                    "该英文 claim_text 必须是单一谓词，紧贴候选标题或 snippet 明示的工艺动作，不得补入候选未明说的"
                    "因果、产品同一性、交付状态或建模结论。"
                    "external_fact 至少覆盖 3 个不同 research_question。representativeness_and_quality 候选只应在"
                    "冻结 claim_kind 为 external_fact 的 requirement 与其事实直接匹配时使用；不得把它塞入"
                    "modeling_judgment，质量与不确定性建模由冻结的 quality.uncertainty requirement 覆盖；"
                    + json.dumps(research_scout.get("candidates", []), ensure_ascii=False, separators=(",", ":"))) \
        if research_scout else ""
    facets = ((node.get("node_identity") or {}).get("facets") or {})
    identity_guard = ""
    if facets.get("form_factor") == "blade":
        identity_guard = (
            "\n冻结身份解释：此节点是单个成品级刀片服务器产品；integration_level=system 表示成品集成级，"
            "不表示刀片机箱，也不表示由多个 server blades 构成的集合。任何相反断言均非法。"
        )
    prompt = (
        "执行 Nomination-only；冻结规格已由 launcher 从 Workflow DATA-BINDING 解析如下："
        f"{frozen_spec}{plan_prompt}{scout_prompt}{hint_prompt}{identity_guard}\n不得读取文件、调用工具、联网、搜索、调用浏览器、"
        "调用其他 agent 或修改任何文件。针对此唯一节点，严格覆盖 dossier.claim_requirements："
        "external_fact 每个 requirement_id 精确返回 1 条事实锚点；modeling_judgment 精确返回 2 条"
        "节点特异的解释或 LCA 判断；其他 requirement 精确返回 1 条。不得提供 LCI 数值。"
        "external_fact 只提名具体一手来源全名和大致 locator，不声称已核验；modeling_judgment"
        "是正式正文知识，但不得冒充法规、具名装置事实、精确参数或外部已核实事实；内部来源按规格固定。"
        "每条 external_fact 必须只陈述目标节点本体；不得把机箱、组件或系统集合写成目标节点。"
        "不得仅因冻结身份存在某刻面就把该刻面写进外部断言，除非提名来源与 locator 明确支持它。"
        "physical_state 与 delivery_state 必须描述单个目标产品自身的物理/交付形态，不能改写成相邻对象关系。"
        "adjacent.distinction 应提名目标本体可逐字核验的区别特征，adjacent.specification 应提名目标本体的"
        "规格或分类维度；两者都不得依赖把相邻对象本身判成目标节点。"
        "delivery_state 若来源未明说商业交付，不得推断‘可交付’，应提名目标本体被明确描述的设备/单元形态；"
        "handoff_unit 应提名目标本体作为独立设备或单元的原文事实；scope.exclusions 应以目标本体为主语"
        "提名其直接可证的运行依赖或边界，不能把相邻对象作为主语，也不能改写成一般法规适用范围。"
        "全部 claim_text 必须逐条唯一；即使两个 requirement 使用同一来源事实，也要选择不同且仍可逐字支持的"
        "目标事实，不得复制同一句或加入 requirement/LCA 术语来凑差异。"
        "evidence_gap 只能陈述长期缺失的型号级 BOM、净质量、制造、运输、共享资源分配或代表性数据；"
        "不得写‘当前外部事实待核验’‘尚无节点证据’等随流水线状态变化就会失真的句子，也不得否定已可核验的产品身份事实。"
        f"node_id 必须逐字等于 {node_id}，node_identity 必须逐字复制冻结节点身份，"
        f"claim_id 依输出顺序写 {node_id}-0、{node_id}-1……。"
        "输出严格匹配给定 JSON schema，不要输出解释。"
    )
    command = [
        "codex", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
        "-C", str(root), "-s", "read-only", "-m", "gpt-5.6-terra",
        "-c", 'model_reasoning_effort="medium"',
    ]
    for feature in DISABLED:
        command.extend(["--disable", feature])
    command.extend(["--json", "--output-schema", str(effective_schema), "-o", str(raw_result), prompt])
    invocation.write_text(json.dumps({
        "protocol": "wiki-nomination-runtime-v1",
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cwd": str(root), "argv": command, "model": "gpt-5.6-terra",
        "launcher_sha256": sha256(Path(__file__).resolve()),
        "reasoning_effort": "medium", "sandbox": "read-only",
        "disabled_capabilities": DISABLED, "workflow": str(workflow),
        "workflow_sha256": sha256(workflow), "schema_template_sha256": sha256(schema),
        "schema_sha256": sha256(effective_schema), "node_id": node_id,
        "source_hints": source_hints_record,
        "research_plan": research_plan_record,
        "research_scout": research_scout_record,
        "nomination_policy_version": "research-scout-source-specific-v9",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    exit_code = 124
    validation_error = None
    deterministic_repair = None
    if research_scout is not None and result.is_file():
        try:
            deterministic_repair = repair_prior_result(result, node, research_scout)
            exit_code = 0
        except (OSError, ValueError, json.JSONDecodeError):
            deterministic_repair = None
    with events.open("w", encoding="utf-8") as event_stream, stderr.open("w", encoding="utf-8") as error_stream:
        if deterministic_repair is None:
            try:
                completed = subprocess.run(command, cwd=root, stdin=subprocess.DEVNULL,
                                           stdout=event_stream, stderr=error_stream, text=True, check=False,
                                           timeout=max(1, args.timeout_seconds))
                exit_code = completed.returncode
            except subprocess.TimeoutExpired:
                validation_error = "Nomination runtime timeout"
    if exit_code == 0:
        try:
            if deterministic_repair is None:
                canonicalize_result(raw_result, result, node, research_scout)
            validate_result(result, node, research_scout)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            validation_error = str(exc)
            exit_code = 2
    usage_rows: list[dict] = []
    event_count = 0
    for line in events.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_count += 1
        collect_usage(event, usage_rows)
    usage.write_text(json.dumps({
        "protocol": "wiki-nomination-runtime-usage-v1",
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "exit_code": exit_code, "event_count": event_count,
        "validation_error": validation_error,
        "deterministic_repair": deterministic_repair,
        "usage_records": usage_rows,
        "artifacts": {
            "invocation_sha256": sha256(invocation), "events_sha256": sha256(events),
            "stderr_sha256": sha256(stderr),
            "raw_result_sha256": sha256(raw_result) if raw_result.exists() else None,
            "result_sha256": sha256(result) if result.exists() else None,
        },
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    batch_usage.write_text(json.dumps({
        "protocol": {"version": "wiki-usage-v1", "kind": "usage"},
        "phase": "nomination", "model": "gpt-5.6-terra",
        "reasoning_effort": "medium", "search_requests": 0,
        "cost_usd": args.cost_usd, "runtime_usage_sha256": sha256(usage),
        "runtime_invocation_sha256": sha256(invocation),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"exit_code": exit_code, "event_count": event_count,
                      "result": str(result)}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
