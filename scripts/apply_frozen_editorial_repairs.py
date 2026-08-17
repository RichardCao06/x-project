#!/usr/bin/env python3
"""Apply bounded A001 editorial repairs to the frozen first-review content."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def sentence(text: str, kind: str, role: str, ids: list[str]) -> dict:
    return {"text": text, "claim_kind": kind, "rhetorical_role": role, "evidence_claim_ids": ids}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("invocation", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    prompt = json.loads(args.invocation.read_text(encoding="utf-8"))["argv"][-1]
    raw = prompt.split("\nCONTENT=", 1)[1]
    document, _ = json.JSONDecoder().raw_decode(raw)
    sections = {item["heading"]: item for item in document["sections"]}

    definition = sections["定义与参考活动"]["paragraphs"]
    definition[1] = {
        "focus": "裸PCB制造作为上游输入交换进入A001",
        "sentences": [
            sentence(
                "本活动的前景建模只覆盖元器件在既有PCB上的贴装、连接与直接相关处理，"
                "HDI PCB裸板, 服务器/交换机用的制板、层压、钻孔和表面处理应由上游活动承担。",
                "modeling_judgment", "thesis", ["A001-2"],
            ),
            sentence(
                "裸板进入A001时应作为具有版本、数量、质量和供应交接信息的产品输入，"
                "不能把其制造步骤拆入本节点来制造虚假的工序完整性或重复计算上游负荷。",
                "modeling_judgment", "boundary", ["A001-2"],
            ),
        ],
    }
    definition.append({
        "focus": "非默认投入与间接投入采用可追溯判据",
        "sentences": [
            sentence(
                "非默认及间接投入只有在能够关联到A001的物料台账、设备、生产期间或工单时才进入节点清单，"
                "并须说明计量口径、消耗期间和分配方法。",
                "modeling_judgment", "thesis", [],
            ),
            sentence(
                "催化剂不是SMT贴装—回流焊的默认投入；只有工厂记录证明其确实用于对应工单时，"
                "才建立节点特异交换，而不能依据其他化学工艺的惯例补入。",
                "modeling_judgment", "application", ["A001-3"],
            ),
            sentence(
                "永久构成物按实际BOM或领料记录计入，工艺辅助物按批次消耗计入，"
                "设备维护物则仅在能够追溯到设备和生产期间时按可审计规则分配。",
                "modeling_judgment", "boundary", ["A001-3"],
            ),
        ],
    })

    reference = sections["参考产品与参考单位"]["paragraphs"]
    reference[3] = {
        "focus": "板级净产出、整机出货与返修良品的数量防重计",
        "sentences": [
            sentence(
                "A001的制造输出只采用同一工单可核对的板级合格净产出；服务器整机出货量可作为下游合理性检查，"
                "但因产品层级和参考单位不同，不得与板卡数量相加或替代板级分母。",
                "modeling_judgment", "thesis", ["A001-6"],
            ),
            sentence(
                "返修后转正的良品只在最终交接时计入一次板级净产出，其返修工时、替换件和复测负荷归入该板，"
                "不得把返修前在制品与返修后良品分别计作两个制造输出。",
                "modeling_judgment", "application", ["A001-18"],
            ),
            sentence(
                "数量对账应同时保存投板数、一次合格数、返修转正数、最终报废数和整机引用数量，"
                "使板级质量分母与下游系统数量之间只建立核对关系，而不发生跨层级重复计产。",
                "modeling_judgment", "boundary", [],
            ),
        ],
    }

    route = sections["技术路线与相邻活动区分"]["paragraphs"]
    recycle_paragraph = next(
        (item for item in route if "返工" in str(item.get("focus", ""))), route[-1]
    )
    smt_paragraph = {
        "focus": "表面贴装特征与其他连接工艺的识别",
        "sentences": [
            sentence(
                "SMT技术将电子元件直接安装在PCB表面而非依赖通孔结构，这一特征可用于识别A001的基本贴装路线。",
                "external_fact", "thesis", ["A001-12"],
            ),
            sentence(
                "工艺卡仍应说明实际板型是否另含通孔插装、选择焊或独立清洗；只有这些步骤实际发生且投入、"
                "能耗或废物流可追溯时，才另设节点或交换，不能由行业惯例推定。",
                "modeling_judgment", "boundary", ["A001-14"],
            ),
        ],
    }
    reflow_paragraph = {
        "focus": "回流焊机理与焊料投入记录位置",
        "sentences": [
            sentence(
                "所述回流焊方法在焊接阶段不额外添加焊料，因此焊膏或预置焊料应记录在印刷或预置环节，"
                "不能把炉内加热虚构为新的焊料投入。",
                "external_fact", "thesis", ["A001-13"],
            ),
            sentence(
                "现场核验应连接焊膏批次、钢网印刷记录、首件确认和返修领料；若返修时另行补焊料，"
                "该投入应记录在返修环节，不能依据一般机理遗漏实际补料。",
                "modeling_judgment", "application", [],
            ),
        ],
    }
    adjacency_paragraph = {
        "focus": "以转换对象、交接产品和计量责任判别相邻活动",
        "sentences": [
            sentence(
                "A001与相邻活动的判别应依次检查转换对象、交接产品流、控制权转移和计量责任："
                "只要活动的直接结果仍是完成贴装与回流焊的板级PCBA，就保留在本节点的工序判定范围内。",
                "modeling_judgment", "thesis", [],
            ),
            sentence(
                "裸板制造改变PCB基材和线路结构，机箱制造形成机械承载件；下游刀片系统集成则把主板PCBA、电源和散热部件"
                "组合为刀片计算单元，再将该单元接入机箱。‘刀片服务器’与‘服务器刀片’在此是同一类计算单元的同义称谓，"
                "不得写成两个连续产品层级。",
                "modeling_judgment", "explanation", ["A001-6"],
            ),
            sentence(
                "若包装、测试或内部运输跨越板级交接点，应依据工单控制权、接收方和独立计量决定归属；"
                "无法确认责任转移时保留为边界缺口，不以同厂发生或成本中心相同作为并入依据。",
                "modeling_judgment", "boundary", [],
            ),
        ],
    }
    route[:] = [smt_paragraph, reflow_paragraph, adjacency_paragraph, recycle_paragraph]

    reconciliation = sections["投入产出与脊边对账"]["paragraphs"][0]
    reconciliation["sentences"][0]["text"] = (
        "冻结图谱规定A001具有14条消费脊边和3条生产脊边；正文据此说明输入、参考产品输出、废物流和内部循环的"
        "核验逻辑，具体边号、方向、交换对象、单位与适用状态则保留在结构化对账表中逐行审计。"
    )
    reconciliation["sentences"][1]["text"] = (
        "消费边应按产品输入、工艺辅助投入、能源服务和废物处理分类核对，生产边则区分合格主板PCBA、"
        "共生焊料浮渣与共生报废PCBA；分类不能替代对每条冻结连接的逐项覆盖。"
    )
    reconciliation["sentences"][2]["text"] = (
        "若某条边的方向、对象或单位与工单事实不符，应在对账表中标为异常并启动图谱修订，"
        "不得通过正文重定义其语义，也不得在异常关闭前把该边当作已有数量的清单数据。"
    )

    environmental = sections["直接排放、废物与监测指标边界"]["paragraphs"][1]
    environmental["focus"] = "按实际发生、批次归属和去向证据统一判定环境流"
    environmental["sentences"] = [
        sentence(
            "空气、水和固体废物采用同一节点纳入判据：环境流必须实际发生，能够关联到A001的型号或生产批次，"
            "并具有可复核的计量、治理或接收去向记录，三项条件缺一时均不得写成节点实测量。",
            "modeling_judgment", "thesis", [],
        ),
        sentence(
            "空气侧仅在存在焊膏挥发物、助焊剂烟气或抽排治理记录时按实测或台账纳入，"
            "工艺名称不能证明排放量、控制效率或最终排放去向。",
            "modeling_judgment", "application", ["A001-21"],
        ),
        sentence(
            "水侧仅在该主板PCBA工单具有实际清洗化学品、用水量和废水或废液去向记录时纳入；"
            "未发生清洗或由其他活动承担时，应明确记录为不适用或边界外。",
            "modeling_judgment", "application", ["A001-22"],
        ),
        sentence(
            "固体废物应区分可返工在制品、共生焊料浮渣、共生报废PCBA和包装废弃物，"
            "其数量与去向还需同批次良率及质量闭合交叉核对，防止同一损耗被重复计入。",
            "modeling_judgment", "implication", ["A001-15"],
        ),
    ]

    applicability = sections["数据适用状态与缺口"]["paragraphs"][1]
    applicability["focus"] = "以前景证据等级和替换程序约束代理使用"
    applicability["sentences"] = [
        sentence(
            "代理的作用是暂时填补已明确的前景LCI字段，而不是把通用SMT资料提升为A001的型号事实；"
            "使用前应标明其数据层级、可替代参数、与直接工单证据的差距及不确定性。",
            "modeling_judgment", "thesis", ["A001-28"],
        ),
        sentence(
            "可被代理的内容应限定为边界已一致且单位可换算的工艺参数；型号级BOM、实际批次产量、良率、"
            "净质量和运输交接若缺少直接记录，应保持为空缺或情景输入，不得由行业平均自动补齐。",
            "modeling_judgment", "boundary", ["A001-27"],
        ),
        sentence(
            "替换程序应为每个代理绑定责任人、预期直接来源和复核触发器；一旦获得同型号工单、计量或去向记录，"
            "即比较差异、更新版本并保存结果变化原因，而不再重复枚举区域化章节已经管理的地点与代表期条件。",
            "modeling_judgment", "application", [],
        ),
    ]

    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
