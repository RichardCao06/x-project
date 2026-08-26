#!/usr/bin/env python3
"""Build a deterministic node-specific Content Blueprint from a graph node."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


ACTIVITY_SECTIONS = {
    "定义与参考活动": ["转换动作和技术路线", "参考产品锚点", "产品制造与总装活动的层级边界"],
    "参考产品与参考单位": ["参考产品身份", "按台交接的参考单位", "配置、批次和净产出的记录方式"],
    "单元过程边界": ["纳入的装配、连接、配置与测试", "上游部件制造的排除", "完成装配和产品交接点"],
    "技术路线与相邻活动区分": ["刀片系统集成路线", "与机箱制造、PCBA制造和使用阶段的区分", "返工和内部循环的处理"],
    "投入产出与脊边对账": ["全部图谱输入", "参考产品输出", "良率、损耗、分配和质量闭合"],
    "直接排放、废物与监测指标边界": ["能源和辅助物消耗", "空气、水和固废监测", "不得用企业汇总指标冒充节点数据"],
    "节点特定采集字段": ["型号级BOM和配置版本", "装配能耗、工时、良率与返工", "包装、测试和批次追溯"],
    "区域化补充要求": ["装配地点和电力区域", "部件来源地及运输路径", "代表期和技术代际"],
    "数据适用状态与缺口": ["已核实产品事实与前景LCI的区分", "代理适用条件", "数据缺口、失效条件和停止使用条件"],
}

PRODUCT_SECTIONS = {
    "定义与产品身份": ["冻结产品节点身份", "与过程、主产品和相邻废物流的区分"],
    "性质与形态": ["可观测物理状态", "收集状态与不得推断的组成信息"],
    "参考流与交接边界": ["参考流身份", "计量单位、收集和交接点"],
    "规格与相邻节点区分": ["来源工艺与规格维度", "与报废PCBA及助焊剂残渣的区分"],
    "在系统中的角色": ["全部上游生产活动", "副产物流与主产品输出的关系"],
    "分类与适用范围": ["前景废物流分类", "适用路线、排除项与证据边界"],
    "节点特定采集字段": ["批次称量与含锡率", "合金、含水率、去向和回收凭证"],
    "区域化补充要求": ["产生地点与处置地点", "运输路径、代表期和监管状态"],
    "数据适用状态与缺口": ["已核实事实与模型判断", "代理条件、失效条件和停止使用条件"],
}

PRODUCT_FAMILY_TERMS = {
    "laptop": ("笔记本", "laptop", "notebook"),
    "server": ("服务器", "server"),
    "switch": ("交换机", "switch"),
    "storage": ("存储阵列", "storage array"),
}


def _families(name: str) -> set[str]:
    lowered = name.lower()
    return {family for family, terms in PRODUCT_FAMILY_TERMS.items()
            if any(term.lower() in lowered for term in terms)}


def build(graph: dict, node_id: str) -> dict:
    activities = {str(item["id"]): item for item in graph.get("activities", [])}
    products = {str(item["id"]): item for item in graph.get("products", [])}
    node = activities.get(node_id) or products.get(node_id)
    if node is None:
        raise ValueError(f"node not found: {node_id}")
    node_type = "activity" if node_id in activities else "product"
    inputs = [str(item) for item in node.get("inputs", [])]
    output_rows = [item for item in node.get("outputs", []) if isinstance(item, dict)]
    outputs = [str(item.get("product")) for item in output_rows]
    if node_type == "activity" and (not inputs or not outputs):
        raise ValueError("activity blueprint requires graph inputs and outputs")
    if node_type == "product":
        producers = [aid for aid, activity in activities.items()
                     if node_id in [str(row.get("product")) for row in activity.get("outputs", [])
                                    if isinstance(row, dict)]]
        name = str(node.get("name") or node.get("display_name") or node_id)
        required = [node_id, name, "参考流", "计量", "批次", "含锡率", "回收", "运输路径",
                    "代表期", "代理", "失效条件", "分配", *producers[:4]]
        return {
            "protocol": "wiki-content-blueprint-v1", "node_id": node_id,
            "node_type": "product", "node_name": name,
            "golden_target": {"recommended_assertions": 36,
                              "maximum_assertions": 140, "recommended_paragraphs": 16,
                              "recommended_modeling_judgments": 20,
                              "maximum_modeling_judgments": 110,
                              "maximum_sentences_per_paragraph": 4,
                              "maximum_external_facts_per_paragraph": 1,
                              "maximum_near_duplicate_ratio": 0.72,
                              "maximum_single_sentence_paragraph_ratio": 0.25},
            "editorial_fusions": [],
            "sections": {heading: {"recommended_paragraphs": 2 if index < 7 else 1,
                                   "topics": topics}
                         for index, (heading, topics) in enumerate(PRODUCT_SECTIONS.items())},
            "identity_tokens": [node_id, name],
            "advisory_tokens": list(dict.fromkeys(required)),
            "forbidden_phrases": ["尚无已核验的节点特定证据",
                                  "该 claim slot 的目标节点特异性外部证据尚未达到 CONFIRMED",
                                  "未核实·模型回忆"],
            "evidence_tables": {
                "props": ["产品节点身份", "来源工艺边界", "收集与交接状态", "相邻废物流区分"],
                "params": ["批次净质量", "含锡率", "合金体系", "含水率", "产生地点与代表期",
                           "回收或处置地点", "包装与运输交接"],
                "quality": ["批次称量覆盖", "物质组成检测", "产生工艺覆盖", "去向凭证覆盖",
                            "地理与时间代表性", "代理选择与失效条件"],
            },
        }
    reference_outputs = [str(item.get("product")) for item in output_rows
                         if item.get("role") == "reference"]
    if not reference_outputs:
        raise ValueError("activity blueprint requires at least one reference output")
    route = str((node.get("facets") or {}).get("technology_route", ""))
    product_ids = {str(item.get("name")): str(item["id"])
                   for item in graph.get("products", []) if item.get("id") and item.get("name")}
    unresolved_flows = [name for name in [*inputs, *outputs] if name not in product_ids]
    if unresolved_flows:
        raise ValueError(
            "activity flow labels are not bound to product IDs: "
            + ", ".join(dict.fromkeys(unresolved_flows))
        )
    flow_ledger = [
        {"field": f"{product_ids[name]} {name}", "direction": direction}
        for names, direction in ((inputs, "in"), (outputs, "out"))
        for name in names
    ]
    flow_labels = [row["field"] for row in flow_ledger]
    directions_by_field = {
        field: {row["direction"] for row in flow_ledger if row["field"] == field}
        for field in flow_labels
    }
    # Compatibility consumers may use the scalar map only when every label has
    # one direction.  The ordered ledger remains the canonical contract.
    legacy_flow_directions = (
        {row["field"]: row["direction"] for row in flow_ledger}
        if all(len(directions) == 1 for directions in directions_by_field.values())
        else None
    )
    output_families = set().union(*(_families(name) for name in outputs)) if outputs else set()
    semantic_conflicts = []
    for input_name in inputs:
        input_families = _families(input_name)
        if output_families and input_families and input_families.isdisjoint(output_families):
            semantic_conflicts.append({
                "kind": "input_reference_product_family_mismatch",
                "input": input_name,
                "outputs": outputs,
                "input_families": sorted(input_families),
                "output_families": sorted(output_families),
                "resolution_required": "node-specific BOM/configuration mapping or graph correction",
            })
    required = [node_id, *outputs, "BOM", "参考单位", "良率", "返工", "装配地点", "运输路径",
                "代表期", "代理", "失效条件", "分配"]
    # Keep the contract node-specific without making every component name a
    # prose-stuffing requirement.
    required.extend(inputs[:4])
    return {
        "protocol": "wiki-content-blueprint-v1", "node_id": node_id, "node_type": node_type,
        "node_name": node["name"], "technology_route": route,
        # Flow identity is edge-scoped: one product may legitimately be both
        # consumed and produced by the same activity.  A field-keyed mapping
        # cannot represent that graph without overwriting one direction.
        "flow_ledger": flow_ledger,
        **({"flow_directions": legacy_flow_directions}
           if legacy_flow_directions is not None else {}),
        "semantic_conflicts": semantic_conflicts,
        "golden_target": {"recommended_assertions": 36,
                          "maximum_assertions": 140, "recommended_paragraphs": 18,
                          "recommended_modeling_judgments": 20, "maximum_modeling_judgments": 110,
                          "maximum_sentences_per_paragraph": 4,
                          "maximum_external_facts_per_paragraph": 1,
                          "maximum_near_duplicate_ratio": 0.72,
                          "maximum_single_sentence_paragraph_ratio": 0.25},
        "editorial_fusions": [],
        "sections": {heading: {"recommended_paragraphs": 2, "topics": topics}
                     for heading, topics in ACTIVITY_SECTIONS.items()},
        "identity_tokens": [node_id, *reference_outputs],
        "advisory_tokens": list(dict.fromkeys(required)),
        "forbidden_phrases": ["尚无已核验的节点特定证据",
                              "该 claim slot 的目标节点特异性外部证据尚未达到 CONFIRMED",
                              "未核实·模型回忆"],
        "evidence_tables": {
            # The flow table is also the deterministic spine-edge ledger, so it
            # must carry every input/output ID rather than a prose-sized sample.
            "flows": flow_labels,
            # Activity properties describe the reference product at the
            # activity handoff.  They are distinct from process operating
            # parameters and therefore have their own mandatory table.
            "props": [f"参考产品身份（{reference_outputs[0]}）", "参考产品完整型号与配置版本",
                      "参考产品单件净质量", "参考产品交接状态",
                      "参考产品规格或质量口径", "参考产品包装前边界"],
            "params": ["工艺路线与设备配置", "装配批次产量", "有效运行时间",
                       "生产负荷与良率", "装配地点与代表期", "共享能源与辅助系统边界"],
            # Waste products belong in flows.  Elementary emissions are limited
            # to natural-environment compartments.
            "emissions": ["空气排放", "水体排放", "土壤排放"],
            "indicators": ["一次装配良率", "返工率", "单位产品装配电耗"],
            "quality": ["BOM质量闭合", "输入输出质量闭合", "供应商覆盖", "地理与时间代表性",
                        "代理选择与失效条件"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph", type=Path)
    parser.add_argument("node_id")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = build(json.loads(args.graph.read_text(encoding="utf-8")), args.node_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "node_id": args.node_id,
                      "sections": len(result["sections"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
