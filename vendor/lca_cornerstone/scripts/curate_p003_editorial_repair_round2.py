#!/usr/bin/env python3
"""Apply the second frozen P003 editorial-review delta."""
from __future__ import annotations
import argparse, json
from pathlib import Path
from run_wiki_content_capture import _claims, validate_result
from curate_p003_editorial_repair import paragraph, sentence


def curate(content):
    sections = {s["heading"]: s for s in content["sections"]}
    form = sections["性质与形态"]["paragraphs"]
    form[0]["sentences"][1]["text"] = (
        "这里的“独立”只表示刀片服务器能够作为独立计算节点承担服务器功能，并不表示它能脱离刀片机箱运行；"
        "“服务器刀片”只是同一成品对象的同义简称。产品构成边界从完成装配的本体外壳开始。"
    )
    form[1]["sentences"][0]["text"] = (
        "产品清单应记录全部实体部件，并另设“是否可更换”属性；长期保留的外壳、板卡和连接结构与可替换的处理器、"
        "内存条、存储介质或接口模块都属于实体部件。"
    )
    form[2]["sentences"][0] = sentence(
        "刀片服务器能够作为独立服务器计算节点发挥功能，但运行依赖刀片机箱提供共享资源。",
        kind="external_fact", ids=["P003-5", "P003-20"], role="thesis")
    form[2]["sentences"].insert(1, sentence(
        "功能上的节点独立性与供电、散热、背板通信和集中管理方面的物理依赖可以并存，二者不构成两个产品层级。"))

    ref = sections["参考流与交接边界"]["paragraphs"]
    old = ref[2]["sentences"]
    first, detail = old[0]["text"].split("；同时，", 1)
    ref[2:] = [
        paragraph("净质量定义和BOM闭合核查", [sentence(first + "。"), sentence(detail)]),
        paragraph("包装扣除、配置复现与版本一致性", [old[1], old[2], old[3]]),
    ]

    adjacent = sections["规格与相邻节点区分"]["paragraphs"]
    old = adjacent[1]["sentences"]
    rack, board = old[0]["text"].split("；同时，", 1)
    adjacent[1:2] = [
        paragraph("与机架式服务器及其他成品类型的交付边界", [sentence(rack + "。"), old[3]]),
        paragraph("与主板PCBA和未识别模块的产品状态边界", [
            sentence(board),
            sentence("映射字段应分别记录刀片本体结构壳体、独立机架式整机外壳、成品SKU和整机检验状态，避免把板卡冒充成品。"),
            old[2],
        ]),
    ]

    role = sections["在系统中的角色"]["paragraphs"]
    use, repair = role[3]["sentences"]
    role[3:4] = [
        paragraph("使用阶段接收产品配置并外接运行条件", [use, sentence(
            "负载率、寿命和共享服务需求属于使用情景参数，应与制造端产品输出分开保存并在下游模型中赋值。")]),
        paragraph("维修替换承接使用情景中的寿命和配置", [repair, sentence(
            "只有使用情景给出部件寿命、替换频次和实际配置后，才能计算备件需求并与初始BOM执行防重复检查。")]),
    ]

    scope = sections["分类与适用范围"]["paragraphs"]
    old = scope[1]["sentences"]
    scope[1:2] = [
        paragraph("刀片机箱等共享基础设施不属于P003成品", [
            sentence("P003不能被包含刀片机箱、机架或数据中心设施的组合系统替代。", ids=["P003-22"]),
            sentence("被排除基础设施的物理质量和服务负荷应保留在相邻节点或服务接口中，不能通过删除质量清单贡献掩盖边界差异。")]),
        paragraph("主要功能和交付形态决定其他服务器是否适用", [
            sentence("以加速器为主要计算功能的服务器按功能排除，拥有独立机架外壳的机架式服务器按交付形态排除，单独组件则按产品层级排除。"),
            old[2], old[3],
        ]),
    ]
    scope[4]["sentences"][1]["text"] = (
        "当配置覆盖不完整、需要建立家族代表模型时，只有BOM结构、净质量、制造地点和供应链具有可解释相似性的型号才可作为代理，"
        "并须披露未覆盖型号与差异方向。"
    )

    fields = sections["节点特定采集字段"]["paragraphs"]
    old = fields[1]["sentences"]
    left, right = old[0]["text"].split("；同时，", 1)
    fields[1]["sentences"] = [
        sentence("硬件采集字段用于复现具体配置，并识别刀片服务器本体与共享机箱之间的接口边界。", role="thesis"),
        sentence(left + "；" + right),
        sentence(old[1]["text"] + " " + old[2]["text"]),
        old[3],
    ]
    fields[5]["sentences"] = [
        sentence("制造活动版本应验证BOM生效期、工艺路线、装配地、供应商组合和包装方案是否属于同一代表期。", role="thesis"),
        sentence("发现跨期记录时应按版本拆分并单独说明；无法拆分的数据不得声明为完整型号级实测LCI。", kind="evidence_gap", role="gap"),
    ]

    regional = sections["区域化补充要求"]["paragraphs"]
    regional[0]["sentences"][1]["text"] = (
        "记录这些地点是为了把部件生产、最终装配、仓储和交付活动匹配到背景数据库的相应地理层级，不能以总部地址替代实际活动地。"
    )
    regional[0]["sentences"][2]["text"] = (
        "在地点边界明确后，运输路径按来源地至装配地、装配地至区域仓库和仓库至交接点分段，并分别保存运输方式与距离依据。"
    )
    regional[0]["sentences"][3]["text"] = (
        "厂址未知时使用与已知国家或区域粒度一致的运输和电力代理，并把厂址差异保留为地理不确定性，而不是伪造更精细位置。"
    )
    first, trace = regional[1]["sentences"][0]["text"].split("；同时，", 1)
    regional[1]["sentences"] = [sentence(first + "。", role="thesis"), sentence(trace), regional[1]["sentences"][1]]
    regional[2]["sentences"][1]["text"] = (
        "技术代际会改变部件生产地域、供应商组合和运输网络，因此处理器平台、系统内存类型、接口代际或机箱兼容关系变化时，"
        "应同时复核相关区域化数据。"
    )
    regional[2]["sentences"][2]["text"] = (
        "时间窗口变化时更新区域电力和运输网络，技术代际变化时更新受影响的部件配置与供应链代理；局部变化不使无关数据链一并失效。"
    )

    gaps = sections["数据适用状态与缺口"]["paragraphs"]
    gaps[1]["sentences"][1]["text"] = (
        "一致性检查应确认同一情景中的部件料号、数量、净质量和BOM版本来自同一型号；任一字段跨型号或跨版本即判为拼接失效。"
    )
    old = gaps[3]["sentences"]
    evaluation, proxy = old[0]["text"].split("；同时，", 1)
    gaps[3:4] = [
        paragraph("供应链代表性评价和缺失状态", [sentence(evaluation + "。"), sentence(
            "评价结果应明确区分真实缺失、家族级代理和型号级实测覆盖，避免把代理可用性误写成数据已经完整。")]),
        paragraph("供应链代理的使用、失效与停止条件", [
            sentence(proxy),
            sentence("代理失效条件包括BOM结构不相似、净质量无法闭合、主要供应商或装配地改变、制造路线不一致及跨越关键技术代际。"),
            sentence("触发失效条件后，代理只可保留为边界探索，不得用于型号声明、精细比较或产品级公开比较，并应优先补采质量、制造、运输和供应链记录。", kind="evidence_gap", role="gap"),
        ]),
    ]
    for section in content["sections"]:
        for index, part in enumerate(section["paragraphs"], 1):
            part["focus"] = f"{section['heading']}：{part['sentences'][0]['text'][:46]}（R2-{index}）"
            for pos, row in enumerate(part["sentences"]):
                row["rhetorical_role"] = "thesis" if pos == 0 else ("gap" if row["claim_kind"] == "evidence_gap" else row.get("rhetorical_role", "explanation"))
                if pos and row["rhetorical_role"] == "thesis": row["rhetorical_role"] = "explanation"
    return content


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('content',type=Path); ap.add_argument('verify',type=Path); ap.add_argument('blueprint',type=Path); ap.add_argument('output',type=Path); a=ap.parse_args()
    b=json.loads(a.blueprint.read_text()); d=curate(json.loads(a.content.read_text())); a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(validate_result(a.output,b,_claims(a.verify,b['node_id'])),ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
