#!/usr/bin/env python3
"""Run no-Web Wiki Nomination and freeze its invocation, events, result, and usage."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path


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
    dossier = node["dossier"]
    requirements = dossier.get("claim_requirements") if isinstance(dossier, dict) else None
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("workflow 缺少 claim_requirements")
    identity = {
        "display_name": node["name"], "node_type": node["node_type"],
        "facets": node["facets"], "boundary": node["boundary"],
    }
    schema = json.loads(template.read_text(encoding="utf-8"))
    item = schema["properties"]["claims"]["items"]
    properties = item["properties"]
    properties["node_id"] = {"type": "string", "const": node["node_id"]}
    properties["industry"] = {"type": "string", "const": node["industry"]}
    properties["node_identity"] = constant_schema(identity)
    properties["claim_id"] = {
        "type": "string", "pattern": rf"^{re.escape(str(node['node_id']))}-[0-9]+$",
    }
    minima = {"external_fact": 1, "modeling_judgment": 2,
              "internal_graph_fact": 1, "evidence_gap": 1}
    # Coverage is bidirectional and freezes one expected cardinality.  Keep
    # Nomination on that same exact contract so a semantically valid extra row
    # cannot deadlock reviewed publication later.
    maxima = dict(minima)
    kinds = [str(row.get("claim_kind", "")) for row in requirements if isinstance(row, dict)]
    if any(kind not in minima for kind in kinds):
        raise ValueError("workflow claim_kind 非法")
    schema["properties"]["claims"]["minItems"] = sum(minima[kind] for kind in kinds)
    schema["properties"]["claims"]["maxItems"] = sum(maxima[kind] for kind in kinds)
    output.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return node, output


def validate_result(path: Path, node: dict) -> None:
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
    counts = {key: 0 for key in expected}
    seen_claim_texts: set[str] = set()
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


def canonicalize_result(raw_path: Path, output_path: Path) -> None:
    """Inject protocol-owned provenance constants without rewriting claims."""
    document = json.loads(raw_path.read_text(encoding="utf-8"))
    claims = document.get("claims")
    if not isinstance(claims, list):
        raise ValueError("Nomination raw claims 缺失")
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        kind = claim.get("claim_kind")
        if kind == "internal_graph_fact":
            claim["believed_source"] = "LCA-CORNERSTONE_GRAPH"
            claim["believed_locator"] = "frozen node dossier and graph connections"
        elif kind in {"modeling_judgment", "evidence_gap"}:
            claim["believed_source"] = "INTERNAL_MODELING_JUDGMENT"
            claim["believed_locator"] = "controlled internal claim"
    output_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", type=Path)
    parser.add_argument("schema", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--cost-usd", type=float, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--source-hints", type=Path,
                        help="上一轮确定性 Search 产生的来源身份/locator 目录；不得含摘录或 verdict")
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
    hint_prompt = ("\n上一轮确定性 Search 仅确认了以下来源身份与可定位主题；它们不是已核验证据，"
                   "不得据此声称 CONFIRMED。external_fact 应优先提出能被其中原文逐字支持的单一、窄断言，"
                   "不要写‘通常/可包括/以具体配置为准’等复合或推断句；若存在 requirement_routes，"
                   "对应 requirement_id 必须逐字使用其 source 与 locator，不得改投其他来源；若存在"
                   "claim_constraints，必须遵守且不得把 requirement 名称本身改写进事实断言："
                   + json.dumps(source_hints, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) if source_hints else ""
    facets = ((node.get("node_identity") or {}).get("facets") or {})
    identity_guard = ""
    if facets.get("form_factor") == "blade":
        identity_guard = (
            "\n冻结身份解释：此节点是单个成品级刀片服务器产品；integration_level=system 表示成品集成级，"
            "不表示刀片机箱，也不表示由多个 server blades 构成的集合。任何相反断言均非法。"
        )
    prompt = (
        "执行 Nomination-only；冻结规格已由 launcher 从 Workflow DATA-BINDING 解析如下："
        f"{frozen_spec}{hint_prompt}{identity_guard}\n不得读取文件、调用工具、联网、搜索、调用浏览器、"
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
        "reasoning_effort": "medium", "sandbox": "read-only",
        "disabled_capabilities": DISABLED, "workflow": str(workflow),
        "workflow_sha256": sha256(workflow), "schema_template_sha256": sha256(schema),
        "schema_sha256": sha256(effective_schema), "node_id": node_id,
        "source_hints": source_hints_record,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    exit_code = 124
    validation_error = None
    with events.open("w", encoding="utf-8") as event_stream, stderr.open("w", encoding="utf-8") as error_stream:
        try:
            completed = subprocess.run(command, cwd=root, stdin=subprocess.DEVNULL,
                                       stdout=event_stream, stderr=error_stream, text=True, check=False,
                                       timeout=max(1, args.timeout_seconds))
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            validation_error = "Nomination runtime timeout"
    if exit_code == 0:
        try:
            canonicalize_result(raw_result, result)
            validate_result(result, node)
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
