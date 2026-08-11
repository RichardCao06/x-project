#!/usr/bin/env python3
"""Grade ICT dataset associations and materialize C2 calibration profiles.

Grade semantics:

* C0: external comparator association; not a substitute.
* C1: weak product-family or adjacent-process association.
* C2: verified candidate association; important alignment work remains.
* C3: strong proxy association; project approval is still required.
* C4: exact semantic association; project selection is still required.

The Wiki association layer is not a model binding.  Every association remains
``calculation_permission=none`` until a project-specific model binding or proxy
binding is created.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "registry"
GRAPH_PATH = ROOT / "docs/ict_equipment-name-graph.json"
CHECKED_AT = "2026-07-30"
C2_SOURCE_REF = "ku-cn-smd-yfvs-eia-2019"
HIQLCD_SOURCE_REF = "ku-hiqlcd-public-ict-2026"


GRADE_DEFINITIONS = {
    "C0": "外部对照关联：完整产品、行业聚合或边界大于节点，只能比较，不能替代。",
    "C1": "弱关联：记录属于相关产品族或相邻过程，可进入代理筛选，但身份、功能、配置或封装仍不充分。",
    "C2": "候选关联：活动/产品身份达到候选门，尚需完整 I/O、功能单位、过程边界或目标地域/时间校准。",
    "C3": "强代理关联：核心身份与边界通过，只剩明确、可审计的代理维度；仍需项目级 P 裁决。",
    "C4": "精确语义关联：身份、活动、路线、边界、单位、地域与时间通过；仍需具体项目选择后才形成模型绑定。",
}

ASSOCIATION_POLICIES = {
    "C0": {
        "association_strength": "external_reference",
        "potential_use": "comparison_only",
        "use_label": "外部对照；不可替代",
        "project_use_path": "只可用于结果比较、数量级检查或图缺口探针。",
    },
    "C1": {
        "association_strength": "weak",
        "potential_use": "proxy_screening",
        "use_label": "弱关联；可进入代理筛选",
        "project_use_path": "进入 P1 代理候选；补齐功能、边界和修正证据后才可裁决 P2/P3。",
    },
    "C2": {
        "association_strength": "verified_candidate",
        "potential_use": "direct_or_proxy_candidate",
        "use_label": "候选关联；优先补证",
        "project_use_path": "取得完整 I/O 并完成目标模型校准后，裁决直接引用或 P2/P3 代理。",
    },
    "C3": {
        "association_strength": "strong_proxy",
        "potential_use": "project_proxy_candidate",
        "use_label": "强代理关联；需项目批准",
        "project_use_path": "在具体项目中完成 P3 批准后才可有条件计算。",
    },
    "C4": {
        "association_strength": "exact",
        "potential_use": "direct_use_candidate",
        "use_label": "精确关联；直接引用候选",
        "project_use_path": "核对项目边界、版本和许可后创建 model_dataset_binding。",
    },
}

C1_ROOT_CAUSES = {
    "graph_composite_product": "骨架节点混合多个可独立计量的物理流。",
    "graph_identity_loss": "本地节点身份在跨行业解析或母行业分辨率中丢失。",
    "node_matching_profile_missing": "节点成立，但缺少数据集裁决所需的规格包络。",
    "external_dataset_resolution_gap": "骨架身份清晰，公开数据集或元数据粒度不足。",
    "recall_false_positive": "仅词面或分类相近，不存在可保留的产品族/相邻过程关系。",
}

MOTHER_GRAPH_IDENTITY_GAPS = {"P035", "P079", "P082", "P086", "P087"}

EVIDENCE_BY_DIMENSION = {
    "application": "目标应用和交接态",
    "boundary": "完整单元过程I/O与边界说明",
    "cable_identity": "线缆导体、护套、端子和装配边界",
    "component_identity": "器件子型、封装/尺寸组合和制造路线",
    "configuration": "目标配置BOM与功能单位",
    "connector_generation": "连接器代际、触点、材料和镀层",
    "connector_subtype": "连接器子型和板端/线端边界",
    "enterprise_grade": "企业级HDD容量、转速、接口和可靠性",
    "functional_unit": "源—目标功能单位无损换算",
    "geography": "目标地区工厂代表性参数",
    "harness_identity": "低压线束导体、绝缘、端子与装配数据",
    "hdi_identity": "HDI积层、微孔、层数、铜量和板厚",
    "hdi_route": "HDI积层和微盲埋孔工艺路线",
    "package": "目标封装结构、质量和测试边界",
    "power_class": "服务器PSU功率、冗余、效率和热设计",
    "product": "目标参考产品身份",
    "product_family": "目标产品子型身份",
    "reference_product": "参考产品身份和交接态",
    "route": "目标技术路线",
    "technology_node": "目标技术节点和代表年份",
    "technology_route": "生产/封装/测试技术路线",
    "time": "目标代表期和技术年代",
}


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def candidate_grade(candidate: dict[str, Any]) -> tuple[str, str]:
    gates = candidate.get("hard_gates", {})
    if (
        candidate.get("relationship_kind") == "external_product_aggregate"
        or gates.get("boundary") == "aggregate_only"
    ):
        return "C0", "数据集边界覆盖完整产品/行业聚合，直接挂接会与已展开前景链重复计入。"

    if candidate.get("candidate_track") == "formal_product_metadata":
        required_identity = (
            gates.get("product") == "pass"
            and gates.get("reference_product") == "pass"
            and gates.get("reference_unit") == "pass"
        )
        has_fail = any(value == "fail" for value in gates.values())
        if required_identity and not has_fail:
            return (
                "C2",
                "产品、参考产品与参考单位已由官方记录核验；完整 I/O、目标地域/时间"
                "校准或商业数据访问仍未通过，因此仅为元数据候选。",
            )

    if candidate.get("relationship_kind") == "activity_process":
        activity = gates.get("activity")
        route = gates.get("route")
        has_fail = any(value == "fail" for value in gates.values())
        if activity in {"pass", "partial"} and route in {"pass", "partial"} and not has_fail:
            return "C2", "活动和 SMT/回流焊路线达到近似门；功能单位、过程边界及中国工厂参数仍需校准。"

    failed = [
        key for key, value in gates.items()
        if value not in {"pass", "not_applicable"}
    ]
    failed_text = "、".join(failed) if failed else "未暴露的节点特定身份字段"
    detail = str(candidate.get("reason") or "").strip()
    return "C1", f"未通过节点特定硬门：{failed_text}。{detail}"


def c1_diagnosis(candidate: dict[str, Any]) -> dict[str, Any]:
    node_id = candidate["node_ref"].split("::")[-1]
    gates = candidate.get("hard_gates", {})
    failed = sorted(
        key for key, value in gates.items()
        if value not in {"pass", "not_applicable"}
    )
    if node_id in MOTHER_GRAPH_IDENTITY_GAPS:
        root_cause = "graph_identity_loss"
        graph_action = "keep_local_identity_and_open_mother_graph_gap"
        secondary = ["external_dataset_resolution_gap"]
    else:
        root_cause = "external_dataset_resolution_gap"
        graph_action = "keep_node_and_collect_specific_dataset_evidence"
        secondary = []
    evidence = dedupe([
        EVIDENCE_BY_DIMENSION.get(key, f"补齐硬门字段：{key}")
        for key in failed
    ])
    if not evidence:
        evidence = ["取得参考产品、功能单位和完整过程边界元数据"]
    return {
        "c1_root_cause": root_cause,
        "secondary_causes": secondary,
        "failed_identity_dimensions": failed,
        "graph_action": graph_action,
        "evidence_needed": evidence,
        "terminal_c1": False,
        "review_note": candidate.get("reason", ""),
    }


def dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


def reference_output(activity: dict[str, Any]) -> str:
    for output in activity.get("outputs", []):
        if output.get("role") == "reference":
            return str(output.get("product", ""))
    return ""


def association_target_kind(item: dict[str, Any]) -> str:
    node_id = item["node_ref"].rsplit("::", 1)[-1]
    relationship = item.get("relationship_kind")
    if relationship == "market_supply":
        return "market_activity"
    if relationship == "external_product_aggregate":
        return "external_product_result"
    if node_id.startswith("A"):
        return (
            "external_process_result"
            if item.get("match_grade") == "C0"
            else "process_dataset"
        )
    if relationship == "reference_product_of":
        return "reference_product"
    if relationship == "waste_flow_of":
        return "waste_flow_and_treatment_process"
    return "reference_product_and_producer_process"


def association_record(item: dict[str, Any]) -> dict[str, Any]:
    grade = item["match_grade"]
    policy = ASSOCIATION_POLICIES[grade]
    source_id = item.get("candidate_id") or item.get("binding_id")
    return {
        "association_id": source_id.replace("cand::", "assoc::").replace(
            "binding::", "assoc::"
        ),
        "node_ref": item["node_ref"],
        "node_spine_hash": item["node_spine_hash"],
        "dataset_key": item["dataset_key"],
        "relationship_kind": item["relationship_kind"],
        "association_target_kind": association_target_kind(item),
        "association_grade": grade,
        "association_definition": GRADE_DEFINITIONS[grade],
        "association_strength": policy["association_strength"],
        "potential_use": policy["potential_use"],
        "use_label": policy["use_label"],
        "project_use_path": policy["project_use_path"],
        "project_binding_required": True,
        "knowledge_layer_status": "verified_association",
        "calculation_permission": "none",
        "reason": item.get("grade_reason") or item.get("reason", ""),
        "evidence_refs": item.get("evidence_refs", []),
        "source_record_id": source_id,
        "checked_at": CHECKED_AT,
    }


def source_functional_unit(dataset: dict[str, Any]) -> dict[str, Any]:
    unit = dataset.get("reference_unit", "not_exposed")
    if dataset.get("database") == "ecoinvent":
        interpretation = (
            "公开说明明确表示贴装 1 m² 印制线路板的无铅 SMT mounting service。"
        )
        unresolved = [
            "目标 PCBA 单件成品板面积（m²/片）",
            "板面积口径是否按单面、双面或投影面积",
            "拼板数、边料及良率",
            "m² mounting service 与每片目标 PCBA 的换算",
        ]
    else:
        interpretation = (
            "官方公开目录给出 kg 参考单位和 open-input-PCB 的参数化 SMD 产线名称；"
            "公开元数据未说明 kg 对应输入裸板还是装联输出。"
        )
        unresolved = [
            "取得许可文档确认 kg 参考流的物理含义",
            "目标 PCBA 单件质量（kg/片）",
            "拼板、边料及良率",
            "kg SMD-line reference flow 与每片目标 PCBA 的换算",
        ]
    return {
        "source_reference_product": dataset.get("reference_product"),
        "source_reference_unit": unit,
        "evidence_status": "official_metadata_verified",
        "interpretation": interpretation,
        "unresolved_conversion_fields": unresolved,
    }


def source_evidence_ref(dataset: dict[str, Any]) -> str:
    database = dataset.get("database")
    if database == "ecoinvent":
        return "ku-ecoinvent-v312-overview-ict"
    if database == "HiQLCD":
        return HIQLCD_SOURCE_REF
    return "ku-sphera-mlc-2026-catalog"


def source_boundary(dataset: dict[str, Any]) -> dict[str, Any]:
    if dataset.get("database") == "ecoinvent":
        known = [
            "表面贴装元器件到印制线路板的 mounting service",
            "无铅焊膏路线",
            "参考产品说明以 1 m² 印制线路板为服务基准",
        ]
        unresolved = [
            "许可清单中的全部技术系统输入、排放和废物",
            "是否包含锡膏印刷、SPI、AOI、返修和公用工程",
            "报废 PCBA、焊料浮渣和边料的分项及分配",
        ]
    else:
        known = [
            "参数化 SMD 装配线",
            "open input printed circuit board",
            "设备组合 1SP、2CS、1CP、1R、1Rf，目录吞吐量 300/h",
            "partly aggregated process",
        ]
        unresolved = [
            "聚合层内包含的设备、公用工程和辅助材料",
            "元器件和焊膏是否为开放输入",
            "报废 PCBA、焊料浮渣和边料的分项及分配",
        ]
    return {
        "known_from_public_metadata": known,
        "unresolved_before_promotion": unresolved,
    }


def china_project_proxy() -> dict[str, Any]:
    return {
        "evidence_ref": C2_SOURCE_REF,
        "value_tag": "proxy",
        "representativeness": (
            "中国汽车仪表 SMD 项目公开环评；可证明中国项目的工序、设备、年产能和"
            "项目级物料/能源数量，但不是服务器、交换机、存储或显卡 PCBA，且电力含生产与生活。"
        ),
        "calculation_permission": False,
        "display_summary": "中国项目代理：锡膏 2.78 g/片；项目电力 1.39 kWh/片（均禁止计算）",
        "observed_route": [
            "锡膏印刷",
            "SPI",
            "贴片",
            "回流焊",
            "AOI",
            "线路板测试",
            "裁板",
            "组装与包装",
        ],
        "equipment_configuration": {
            "solder_paste_printer": "1 台",
            "spi": "1 台",
            "pick_and_place": "2 台",
            "aoi": "1 台",
            "circuit_board_tester": "1 台",
            "board_cutter": "1 台",
            "reflow_oven": "1 台",
        },
        "annual_observations": {
            "pcb_input": "36 万片/年",
            "surface_mount_components": "10 t/年",
            "lead_free_solder_paste": "1 t/年",
            "flux": "0.2 t/年",
            "cleaning_agent": "0.2 t/年",
            "electricity": "50 万 kWh/年（生产、生活合计）",
            "waste_pcb": "0.5 t/年",
            "waste_cleaning_liquid": "0.2 t/年",
            "waste_flux": "0.1 t/年",
        },
        "derived_indicators": {
            "solder_paste": "2.78 g/片（项目代理）",
            "surface_mount_components": "27.78 g/片（项目代理）",
            "project_electricity": "1.39 kWh/片（含生产、生活，不得作 SMT 单元过程值）",
            "waste_pcb": "1.39 g/片（含裁板边料，不等同报废 PCBA）",
        },
        "operating_schedule": "300 d/年；2 班/d；8 h/班",
        "unresolved_target_factory_fields": [
            "目标中国工厂、厂址和代表期",
            "服务器/交换机/存储/显卡 PCBA 的实际板面积、质量和拼板方式",
            "分表计量的 SMT 线电力、氮气、压缩空气和空调能耗",
            "目标产品的锡膏量、元器件质量、一次通过率和返修率",
            "焊料浮渣、报废 PCBA、边料和清洗废物的分项实测量",
            "生产、测试、组装、包装及厂务公摊的切分规则",
        ],
    }


def product_c2_profile(
    candidate: dict[str, Any],
    dataset: dict[str, Any],
    product: dict[str, Any],
) -> dict[str, Any]:
    node_id = candidate["node_ref"].split("::")[-1]
    source_unit = dataset.get("reference_unit", "not_exposed")
    target_fields: list[str]
    if node_id == "P062":
        china_summary = "全球锡膏过程；中国供应商、配方辅料及制造能耗尚未校准"
        target_fields = [
            "中国锡膏供应商、工厂和代表期",
            "SAC305 金属粉、助焊剂、树脂及填料的质量组成",
            "雾化制粉、混膏、冷却、压缩空气和氩气的分项消耗",
            "包装、损耗、废膏、清洗残液及回收边界",
        ]
        known = [
            "参考产品为焊前 Sn96.5Ag3.0Cu0.5 锡膏",
            "kg 质量参考单位",
            "全球 2025 年、有效至 2028 年的 attributional cradle-to-gate 聚合过程",
            "公开 XML 仅暴露 1 个参考产品交换，不含完整 LCI 清单",
        ]
    elif node_id == "P065":
        china_summary = "RoW 成箱过程；中国箱型、克重、再生纤维和印刷转换参数尚未校准"
        target_fields = [
            "中国纸箱供应商、工厂和代表期",
            "箱型、楞型、面纸/芯纸克重与再生纤维比例",
            "单箱质量、展开面积、印刷覆盖率、油墨和粘合剂",
            "裁切边料、废箱、不合格率及回收边界",
        ]
        known = [
            "参考产品为成品 corrugated board box",
            "kg 质量参考单位",
            "公开说明包含裁切、印刷、开槽、折叠和粘合",
            "RoW、2008–2025；完整交换清单受 ecoinvent 许可限制",
        ]
    elif dataset.get("database") == "HiQLCD":
        target_fields_by_node = {
            "P044": [
                "目标片式电阻的尺寸、阻值、精度、厚膜/薄膜路线和单件质量",
                "目标中国供应商、工厂、省份和代表期",
                "生产混合与消费混合的选择、运输边界和完整交换清单",
                "成品率、电镀/烧结能源、废浆料和不合格品处理",
            ],
            "P080": [
                "目标 MLCC 的尺寸、容量、额定电压、介质类别和单件质量",
                "目标中国供应商、工厂、省份和代表期",
                "内外电极材料、烧成路线、成品率和完整交换清单",
                "生产混合与消费混合的选择及运输边界",
            ],
            "P085": [
                "目标托盘尺寸、承载等级、单件质量和木材种类/来源",
                "含水率、干燥能源、防护处理、复用次数与维修率",
                "目标中国供应商、工厂、省份和代表期",
                "生产混合与消费混合的选择、运输边界和完整交换清单",
            ],
            "P066": [
                "目标 ICT 工厂所在省级电网、代表年份和购电合同边界",
                "中压交接点、电压等级、厂内变压与线路损耗",
                "自备电源、绿电交易、分布式光伏和余电处理",
                "持证数据库中的供应组合、基础设施与完整交换清单",
            ],
        }
        target_fields = target_fields_by_node.get(node_id, [
            "目标产品规格、交接态和无损功能单位换算",
            "目标中国工厂、省份和代表期",
            "生产混合与消费混合的选择及运输边界",
            "持证数据库中的完整技术系统输入、基本流和废物清单",
        ])
        china_summary = (
            f"HiQLCD 中国记录（{dataset.get('geography')}，"
            f"{dataset.get('time_period')}）；产品和单位已核验，完整 I/O 与"
            "目标工厂参数仍待许可内校准"
        )
        known = [
            f"参考产品：{dataset.get('reference_product')}",
            f"参考单位：{source_unit}",
            f"公开活动类型/边界：{dataset.get('public_activity_type')} / "
            f"{dataset.get('public_boundary')}",
            f"地域与时间：{dataset.get('geography')}；{dataset.get('time_period')}",
            str(dataset.get("product_information") or dataset.get("notes")),
            "公开页面未暴露完整交换清单和可计算 LCIA 结果",
        ]
    else:
        china_summary = "中国中压市场地域已匹配；目标省份/年份、供电组合与持证交换清单尚未钉死"
        target_fields = [
            "目标工厂所在省级电网、代表年份和购电合同边界",
            "中压交接点、电压等级、厂内变压与线路损耗",
            "自备电源、绿电交易、分布式光伏和余电处理",
            "持证数据库中的供应组合、基础设施与完整交换清单",
        ]
        known = [
            "参考产品为 1–24 kV 中压电力",
            "kWh 能量参考单位",
            "中国 market group，包含输电基础设施、国别损耗和电压转换损耗",
            "2015–2025；公开目录不提供供应组合和完整交换清单",
        ]
    promotion = [
        "取得源数据集完整技术系统输入、基本流、废物与多功能处理信息",
        "确认目标研究的代表期并与源数据集时间范围对账",
        "取得目标中国供应情景或工厂的代表性参数",
        "完成独立复核后才能形成正式 C3/C4 绑定",
    ]
    return {
        "profile_id": f"c2::{candidate['candidate_id']}",
        "candidate_id": candidate["candidate_id"],
        "node_ref": candidate["node_ref"],
        "dataset_key": candidate["dataset_key"],
        "profile_kind": "product_metadata_candidate",
        "grade": "C2",
        "calculation_permission": False,
        "target_functional_unit": {
            "reference_product": product["name"],
            "provisional_basis": f"1 {source_unit} {product['name']}",
            "basis_status": "source_unit_aligned_but_target_dataset_not_calibrated",
            "note": "参考产品与单位已对齐；正式使用仍需目标规格和完整 I/O 对账。",
        },
        "source_functional_unit": {
            "source_reference_product": dataset.get("reference_product"),
            "source_reference_unit": source_unit,
            "evidence_status": "official_metadata_verified",
            "interpretation": dataset.get("product_information") or dataset.get("notes"),
            "unresolved_conversion_fields": target_fields,
        },
        "process_boundary": {
            "target_node_boundary": product.get("boundary"),
            "source": {
                "known_from_public_metadata": known,
                "unresolved_before_promotion": promotion,
            },
            "alignment_status": "identity_aligned_boundary_unverified",
        },
        "china_factory_parameters": {
            "evidence_ref": source_evidence_ref(dataset),
            "value_tag": "proxy",
            "proxy_availability": "no_calculation_value_selected",
            "calculation_permission": False,
            "display_summary": china_summary,
            "unresolved_target_factory_fields": target_fields,
        },
        "promotion_requirements": promotion,
        "checked_at": CHECKED_AT,
    }


def c2_profile(
    candidate: dict[str, Any],
    dataset: dict[str, Any],
    node: dict[str, Any],
) -> dict[str, Any]:
    if candidate["node_ref"].split("::")[-1].startswith("P"):
        return product_c2_profile(candidate, dataset, node)
    activity = node
    ref_product = reference_output(activity)
    return {
        "profile_id": f"c2::{candidate['candidate_id']}",
        "candidate_id": candidate["candidate_id"],
        "node_ref": candidate["node_ref"],
        "dataset_key": candidate["dataset_key"],
        "grade": "C2",
        "calculation_permission": False,
        "target_functional_unit": {
            "reference_product": ref_product,
            "provisional_basis": f"1 piece {ref_product}",
            "basis_status": "provisional_method_choice",
            "note": "正式数据集必须在 piece、kg 或 m² 中钉死参考单位，并保存无损换算参数。",
        },
        "source_functional_unit": source_functional_unit(dataset),
        "process_boundary": {
            "target_inputs": list(activity.get("inputs", [])),
            "target_outputs": list(activity.get("outputs", [])),
            "source": source_boundary(dataset),
            "alignment_status": "partial",
        },
        "china_factory_parameters": china_project_proxy(),
        "promotion_requirements": [
            "钉死目标参考流和功能单位并完成 m²/kg/piece 换算",
            "取得源数据集完整过程边界，逐项与图内输入输出对账",
            "取得目标中国工厂代表期的分表计量和物料平衡",
            "明确废物、共产品、返修和厂务公摊规则",
            "独立复核后才能由 C2 升级为 C3 或 C4",
        ],
        "checked_at": CHECKED_AT,
    }


def grade_registries() -> dict[str, int]:
    candidate_path = REGISTRY / "lca_binding_candidates.json"
    binding_path = REGISTRY / "lca_bindings.json"
    rejection_path = REGISTRY / "lca_binding_rejections.json"
    catalog_path = REGISTRY / "lca_dataset_catalog.json"
    status_path = REGISTRY / "lca_node_match_status.json"

    candidate_doc = load(candidate_path)
    binding_doc = load(binding_path)
    rejection_doc = load(rejection_path)
    status_doc = load(status_path)
    catalog = {
        item["dataset_key"]: item
        for item in load(catalog_path)["datasets"]
    }
    graph = load(GRAPH_PATH)
    activities = {item["id"]: item for item in graph["activities"]}
    products = {item["id"]: item for item in graph["products"]}

    profiles = []
    grade_counts: dict[str, int] = {grade: 0 for grade in GRADE_DEFINITIONS}
    grades_by_node: dict[str, Counter[str]] = defaultdict(Counter)
    candidates_by_node: Counter[str] = Counter()
    rejections_by_node: Counter[str] = Counter(
        item["node_ref"] for item in rejection_doc["rejections"]
    )
    for candidate in candidate_doc["candidates"]:
        grade, reason = candidate_grade(candidate)
        candidate["match_grade"] = grade
        candidate["grade_definition"] = GRADE_DEFINITIONS[grade]
        candidate["grade_reason"] = reason
        candidate["calculation_permission"] = False
        candidate.pop("c2_profile_ref", None)
        for field in (
            "c1_root_cause",
            "secondary_causes",
            "failed_identity_dimensions",
            "graph_action",
            "evidence_needed",
            "terminal_c1",
            "review_note",
        ):
            candidate.pop(field, None)
        if grade == "C1":
            candidate.update(c1_diagnosis(candidate))
        grade_counts[grade] += 1
        grades_by_node[candidate["node_ref"]][grade] += 1
        candidates_by_node[candidate["node_ref"]] += 1
        if grade == "C2":
            node_id = candidate["node_ref"].split("::")[-1]
            node = activities[node_id] if node_id.startswith("A") else products[node_id]
            profile = c2_profile(candidate, catalog[candidate["dataset_key"]], node)
            candidate["c2_profile_ref"] = profile["profile_id"]
            candidate["evidence_refs"] = sorted(set(
                candidate.get("evidence_refs", [])
                + [source_evidence_ref(catalog[candidate["dataset_key"]])]
                + ([C2_SOURCE_REF] if node_id.startswith("A") else [])
            ))
            profiles.append(profile)

    for binding in binding_doc["bindings"]:
        grade = "C4" if binding["binding_status"] == "exact_binding" else "C3"
        binding["match_grade"] = grade
        binding["grade_definition"] = GRADE_DEFINITIONS[grade]
        grade_counts[grade] += 1
        grades_by_node[binding["node_ref"]][grade] += 1

    grade_order = {"C0": 0, "C1": 1, "C2": 2, "C3": 3, "C4": 4}
    for status in status_doc["nodes"]:
        counts = grades_by_node.get(status["node_ref"], Counter())
        status["candidate_grade_counts"] = {
            grade: counts.get(grade, 0) for grade in ("C0", "C1", "C2")
        }
        formal = [grade for grade in ("C3", "C4") if counts.get(grade)]
        visible = [grade for grade, count in counts.items() if count]
        status["highest_match_grade"] = (
            max(formal or visible, key=grade_order.get) if (formal or visible) else "none"
        )
        status["association_grade_counts"] = {
            grade: counts.get(grade, 0)
            for grade in ("C0", "C1", "C2", "C3", "C4")
        }
        status["association_count"] = sum(counts.values())
        status["highest_association_grade"] = status["highest_match_grade"]
        status["candidate_count"] = candidates_by_node[status["node_ref"]]
        status["rejection_count"] = rejections_by_node[status["node_ref"]]
        status["model_binding_count"] = 0
        status["reference_status"] = (
            "associations_available"
            if status["association_count"]
            else "no_usable_association_in_checked_sources"
        )

    candidate_doc["_meta"]["schema_version"] = "lca-association-candidates-v3"
    candidate_doc["_meta"]["checked_at"] = CHECKED_AT
    candidate_doc["_meta"]["note"] = (
        "Candidates are verified dataset associations for discovery and proxy "
        "screening; they are not project model bindings."
    )
    candidate_doc["_meta"]["grade_scale"] = GRADE_DEFINITIONS
    candidate_doc["_meta"]["c1_root_cause_scale"] = C1_ROOT_CAUSES
    candidate_doc["_meta"]["grade_counts"] = {
        grade: grade_counts[grade] for grade in ("C0", "C1", "C2")
    }
    status_doc["_meta"]["schema_version"] = "lca-node-reference-status-v2"
    status_doc["_meta"]["source_evidence_frozen_at"] = CHECKED_AT
    status_doc["_meta"]["association_adjudicated_at"] = CHECKED_AT
    status_doc["_meta"]["checked_at"] = CHECKED_AT
    status_doc["_meta"]["association_count"] = sum(grade_counts.values())
    status_doc["_meta"]["association_grade_counts"] = {
        grade: grade_counts[grade] for grade in ("C0", "C1", "C2", "C3", "C4")
    }
    status_doc["_meta"]["project_binding_count"] = 0
    status_doc["_meta"]["project_proxy_count"] = 0
    status_doc["_meta"]["note"] = (
        "Node-level C0-C4 records are reusable reference associations, not "
        "project model bindings and not calculation permissions."
    )
    binding_doc["_meta"]["schema_version"] = "lca-bindings-v2-deprecated-empty"
    binding_doc["_meta"]["checked_at"] = CHECKED_AT
    binding_doc["_meta"]["note"] = (
        "Deprecated compatibility snapshot. Project selections belong in "
        "model_dataset_bindings.json or model_proxy_bindings.json."
    )
    binding_doc["_meta"]["grade_scale"] = GRADE_DEFINITIONS
    binding_doc["_meta"]["grade_counts"] = {
        grade: grade_counts[grade] for grade in ("C3", "C4")
    }

    write(candidate_path, candidate_doc)
    write(binding_path, binding_doc)
    write(status_path, status_doc)
    associations = [
        association_record(item)
        for item in candidate_doc["candidates"] + binding_doc["bindings"]
    ]
    associations.sort(key=lambda item: item["association_id"])
    write(REGISTRY / "lca_dataset_associations.json", {
        "_meta": {
            "schema_version": "lca-dataset-associations-v1",
            "scope": "ICT reusable node-to-dataset reference knowledge layer",
            "source_evidence_frozen_at": CHECKED_AT,
            "association_adjudicated_at": CHECKED_AT,
            "association_count": len(associations),
            "association_grade_counts": {
                grade: grade_counts[grade]
                for grade in ("C0", "C1", "C2", "C3", "C4")
            },
            "grade_scale": GRADE_DEFINITIONS,
            "use_policy": ASSOCIATION_POLICIES,
            "calculation_rule": (
                "No association grants calculation permission. A concrete project "
                "must create a model dataset/proxy binding."
            ),
        },
        "associations": associations,
    })
    write(REGISTRY / "model_dataset_bindings.json", {
        "_meta": {
            "schema_version": "model-dataset-bindings-v1",
            "scope": "project-specific exact dataset selections",
            "binding_count": 0,
        },
        "bindings": [],
    })
    write(REGISTRY / "model_proxy_bindings.json", {
        "_meta": {
            "schema_version": "model-proxy-bindings-v1",
            "scope": "project-specific P0-P3 proxy adjudications",
            "binding_count": 0,
        },
        "bindings": [],
    })
    write(REGISTRY / "lca_c2_profiles.json", {
        "_meta": {
            "schema_version": "lca-c2-profiles-v1",
            "scope": "ICT activity-process and product-metadata C2 calibration profiles",
            "checked_at": CHECKED_AT,
            "profile_count": len(profiles),
            "shared_china_proxy_source": C2_SOURCE_REF,
            "note": "C2 is a visible reference association but never calculation-ready; project use requires a separate direct/proxy adjudication.",
        },
        "profiles": sorted(profiles, key=lambda item: item["profile_id"]),
    })
    return {
        "C0": grade_counts["C0"],
        "C1": grade_counts["C1"],
        "C2": grade_counts["C2"],
        "C3": grade_counts["C3"],
        "C4": grade_counts["C4"],
        "associations": len(associations),
        "profiles": len(profiles),
    }


def main() -> None:
    print(json.dumps(grade_registries(), ensure_ascii=False))


if __name__ == "__main__":
    main()
