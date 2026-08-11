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


def build(graph: dict, node_id: str) -> dict:
    activities = {str(item["id"]): item for item in graph.get("activities", [])}
    if node_id not in activities:
        raise ValueError(f"activity not found: {node_id}")
    node = activities[node_id]
    inputs = [str(item) for item in node.get("inputs", [])]
    outputs = [str(item.get("product")) for item in node.get("outputs", []) if isinstance(item, dict)]
    if not inputs or not outputs:
        raise ValueError("activity blueprint requires graph inputs and outputs")
    route = str((node.get("facets") or {}).get("technology_route", ""))
    required = [node_id, *outputs, "BOM", "参考单位", "良率", "返工", "装配地点", "运输路径",
                "代表期", "代理", "失效条件", "分配"]
    # Keep the contract node-specific without making every component name a
    # prose-stuffing requirement.
    required.extend(inputs[:4])
    return {
        "protocol": "wiki-content-blueprint-v1", "node_id": node_id, "node_type": "activity",
        "node_name": node["name"], "technology_route": route,
        "golden_target": {"minimum_body_chars": 6500, "minimum_assertions": 48,
                          "maximum_assertions": 140, "minimum_paragraphs": 18,
                          "minimum_modeling_judgments": 32, "maximum_modeling_judgments": 110,
                          "maximum_sentences_per_paragraph": 4,
                          "maximum_external_facts_per_paragraph": 1,
                          "maximum_near_duplicate_ratio": 0.72,
                          "maximum_single_sentence_paragraph_ratio": 0.25},
        "editorial_fusions": [],
        "sections": {heading: {"minimum_paragraphs": 2, "topics": topics}
                     for heading, topics in ACTIVITY_SECTIONS.items()},
        "required_tokens": list(dict.fromkeys(required)),
        "forbidden_phrases": ["尚无已核验的节点特定证据",
                              "该 claim slot 的目标节点特异性外部证据尚未达到 CONFIRMED",
                              "未核实·模型回忆"],
        "evidence_tables": {
            "flows": [*inputs[:3], outputs[0]],
            "emissions": ["空气排放", "废水排放", "固体废物"],
            "indicators": ["一次装配良率", "返工率", "单位产品装配电耗"],
            "params": ["完整型号与配置版本", "单台净质量", "型号级BOM版本", "装配批次产量",
                       "装配地点与代表期", "包装与运输交接"],
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
