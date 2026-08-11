#!/usr/bin/env python3
"""Apply the frozen P003 editorial-review repairs without adding external facts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_wiki_content_capture import _claims, validate_result


def sentence(text, kind="modeling_judgment", ids=None, role="explanation"):
    return {"text": text, "claim_kind": kind, "rhetorical_role": role,
            "evidence_claim_ids": list(ids or [])}


def paragraph(focus, sentences):
    sentences[0]["rhetorical_role"] = "thesis"
    return {"focus": focus, "sentences": sentences}


def curate(content: dict) -> dict:
    sections = {section["heading"]: section for section in content["sections"]}
    # One canonical product name.  "成品" remains a state qualifier, never a new layer.
    replacements = (("成品服务器刀片", "成品刀片服务器"), ("服务器刀片", "刀片服务器"),
                    ("单台刀片存在", "单台刀片服务器存在"), ("刀片能够", "刀片服务器能够"),
                    ("服务刀片数量", "刀片服务器数量"), ("刀片数量", "刀片服务器数量"))
    for section in content["sections"]:
        for part in section["paragraphs"]:
            for row in part["sentences"]:
                for old, new in replacements:
                    row["text"] = row["text"].replace(old, new)

    definition = sections["定义与产品身份"]["paragraphs"]
    definition[0]["sentences"][1]["text"] = (
        "本文统一使用“刀片服务器”指P003成品对象；行业资料中的“服务器刀片”仅为同义称谓，"
        "不代表另一个产品层级。成品身份由可独立追踪的完整装配状态和交接记录共同界定，不能仅凭主板、处理器组件或营销名称判定。"
    )
    definition[1]["sentences"][0]["text"] = (
        "本节点以一台刀片服务器为建模对象，并将其与承载多台产品的刀片机箱、机架和数据中心基础设施区分。"
    )
    definition[1]["sentences"][1]["text"] = (
        "P003只描述成品刀片服务器输出；机箱背板、电源、风扇、集中管理设备、机架结构和数据中心设施不进入产品本体，"
        "而通过相邻节点、使用阶段过程或明确分配规则处理。"
    )
    definition[1]["sentences"][1]["evidence_claim_ids"] = ["P003-3"]
    definition[2]["sentences"][0]["text"] = (
        "同一产品家族内仅处理器数量、系统内存容量、存储配置或网络接口选件变化时，宜用配置参数和情景组合表达，"
        "并保持P003这一成品身份不变。"
    )
    definition[2]["sentences"][0]["evidence_claim_ids"] = []

    form = sections["性质与形态"]["paragraphs"]
    form[0]["sentences"][1]["text"] = (
        "这里的刀片服务器与“服务器刀片”指同一成品对象；产品构成边界从完成装配的外壳开始，"
        "覆盖板载电路、处理器、系统内存以及随配置交付的存储、网络接口和管理控制模块。"
    )
    config_sentence = sentence(
        "未取得具体配置时，模型不预设处理器、系统内存、存储或扩展部件数量，而以未知配置和情景范围保留差异。",
        ids=["P003-7"], role="boundary")
    form[1]["sentences"][-1]["text"] = (
        "若维修资料只给出备件套装质量而未区分实际换下部件，套装不得直接充当初始BOM，也不得假定每次维修消耗整套备件；"
        "具体配置未知时，同样不能从产品家族资料反推部件数量。"
    )
    form[1]["sentences"][-1]["evidence_claim_ids"] = ["P003-7"]
    old = form[2]["sentences"]
    form[2:] = [
        paragraph("独立服务器功能与共享机箱依赖并存", [
            sentence("刀片服务器能够作为独立的计算节点执行服务器功能，但运行仍依赖刀片机箱提供供电、散热、背板通信和集中管理。"),
            old[1],
        ]),
        paragraph("包装与交付状态边界", [old[2], old[3]]),
    ]

    adjacent = sections["规格与相邻节点区分"]["paragraphs"]
    adjacent[1]["sentences"][2]["text"] = adjacent[1]["sentences"][2]["text"].replace("服务器模块", "服务器模块").replace("刀片服务器、管理", "刀片服务器、管理")
    old = adjacent[2]["sentences"]
    first_left, first_right = old[0]["text"].split("；同时，", 1)
    adjacent[2:] = [
        paragraph("配置字段用于描述同一产品身份下的差异", [
            sentence(first_left + "。"),
            sentence("这些字段用于复现具体交付配置和匹配使用阶段接口，不据此把纯配置变化拆成新的产品节点。"),
        ]),
        paragraph("共享资源分配的条件与失效边界", [
            sentence(first_right, ids=["P003-15"]), old[1], old[2], old[3],
        ]),
    ]

    role = sections["在系统中的角色"]["paragraphs"]
    role[0]["sentences"][1]["text"] = (
        "P003是A039完成总装后交付给下游产品系统的通用计算刀片服务器流，用于把制造活动结果与具体产品配置、数量和净质量对应。"
    )
    upstream = role[1]["sentences"]
    role[1:2] = [
        paragraph("上游部件通过型号级BOM进入A039", [upstream[0], upstream[3]]),
        paragraph("P003向下游传递成品及其适用条件", [upstream[1], upstream[2]]),
    ]
    lifecycle = role[3]["sentences"]
    use_text, repair_text = lifecycle[0]["text"].split("；同时，", 1)
    role[3:] = [
        paragraph("使用与维修阶段在制造节点之外衔接", [sentence(use_text + "。"), sentence(repair_text)]),
        paragraph("报废、再利用与生命周期完整性", [lifecycle[1], lifecycle[2], lifecycle[3]]),
    ]

    scope = sections["分类与适用范围"]["paragraphs"]
    old = scope[2]["sentences"]
    identity_text, identity_limit = old[0]["text"].split("；同时，", 1)
    scope[2:] = [
        paragraph("配置差异何时升格为身份差异", [sentence(identity_text + "。"), sentence(identity_limit)]),
        paragraph("多配置汇总与家族代理适用条件", [old[1], old[2], old[3]]),
    ]

    fields = sections["节点特定采集字段"]["paragraphs"]
    supplier = fields[3]["sentences"]
    fields[3:] = [
        paragraph("供应商覆盖和采购份额", [supplier[0], sentence(
            "若供应商、生产地点或采购份额无法追溯，应降低供应链数据质量评级，且不得把家族级代理描述为完整型号的实测供应链。",
            kind="evidence_gap", role="gap")]),
        paragraph("净质量测量方法的审计记录", [supplier[1], sentence(
            "测量记录应与产品型号、配置和抽样批次关联；缺少去皮步骤、样本范围或异常值处理时，称量结果不能用于证明型号级质量闭合。",
            kind="evidence_gap", role="gap")]),
        paragraph("制造活动与BOM版本一致性", [supplier[2], sentence(
            "BOM、工艺路线、装配地、供应商组合和包装方案不属于同一代表期时，应按版本拆分，不能把跨期拼接结果声明为完整型号级实测LCI。",
            kind="evidence_gap", role="gap")]),
    ]

    regional = sections["区域化补充要求"]["paragraphs"]
    old = regional[1]["sentences"]
    regional[1:] = [
        paragraph("区域电力与运输代理按活动地点匹配", [old[0], sentence(
            "装配地或主要供应商区域变化时应重配相应电力代理，运输路径或方式变化时只重配受影响的运输模型，并保留变更前后的地域依据。",
            role="boundary")]),
        paragraph("代表期和技术代际决定代理是否仍适用", [old[1], old[2], sentence(
            "关键部件跨技术代际时应重新审查部件配置代理；只有受影响的数据链停止沿用，不把局部变化无条件扩大为整套区域结果失效。",
            role="boundary")]),
    ]

    gaps = sections["数据适用状态与缺口"]["paragraphs"]
    gaps[0]["sentences"][0]["text"] = (
        "现有冻结事实只支持P003的成品身份、基本构成、刀片机箱依赖和A039图谱关系判定，不能据此宣称已取得任何具体型号的前景LCI。"
    )
    config = gaps[1]["sentences"]
    gaps[1:2] = [
        paragraph("型号配置差异形成独立的不确定性情景", [config[0], sentence(
            "每个配置情景必须保持处理器、系统内存、存储、网络接口、管理模块与净质量的BOM内部一致，避免跨型号拼接。")]),
        paragraph("共享机箱负荷分配需要单独敏感性检验", [config[1], config[2], config[3]]),
    ]
    gaps[3]["sentences"][3]["text"] = (
        "当供应链代理不能映射到具体配置、关键质量差额未解释，或供应商与代表期证据不可追溯时，代理结果应停止用于型号声明和产品级公开比较。"
    )

    # Refresh unique focuses after moves/splits.
    for section in content["sections"]:
        for index, part in enumerate(section["paragraphs"], 1):
            part["focus"] = f"{section['heading']}：{part['sentences'][0]['text'][:48]}（{index}）"
            part["sentences"][0]["rhetorical_role"] = "thesis"
            for row in part["sentences"][1:]:
                if row["rhetorical_role"] == "thesis": row["rhetorical_role"] = "explanation"
    return content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("content", type=Path); parser.add_argument("verify", type=Path)
    parser.add_argument("blueprint", type=Path); parser.add_argument("output", type=Path)
    args = parser.parse_args(); blueprint = json.loads(args.blueprint.read_text(encoding="utf-8"))
    content = curate(json.loads(args.content.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    score = validate_result(args.output, blueprint, _claims(args.verify, blueprint["node_id"]))
    print(json.dumps(score, ensure_ascii=False)); return 0


if __name__ == "__main__": raise SystemExit(main())
