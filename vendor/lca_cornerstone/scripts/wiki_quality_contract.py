#!/usr/bin/env python3
"""Shared, model-independent quality contract for node Wiki v2 pages.

The contract deliberately lives in Python rather than prompts.  A nomination
model may propose prose and source identities, but it cannot change the page
shape, evidence-table floor, core evidence zones, or privileged-state rules.
"""
from __future__ import annotations

from typing import Any


CLAIM_KINDS = {
    "external_fact", "internal_graph_fact", "modeling_judgment", "evidence_gap",
}

# A requirement is a topic-coverage slot, not a one-sentence quota.  Allow a
# bounded set of node-specific atomic claims per slot so draft pages can be
# rich without letting an unconstrained nomination exhaust Search/Fetch.
MAX_CLAIMS_PER_REQUIREMENT = 4

# A complete draft uses a small set of verified fact anchors plus a richer
# modeling layer.  Verification protects external authority; it is not the
# mechanism used to manufacture paragraph length.
MIN_CLAIMS_PER_EXTERNAL_REQUIREMENT = 1
MIN_CLAIMS_PER_MODELING_REQUIREMENT = 2
MIN_CLAIMS_PER_CONTROLLED_REQUIREMENT = 1

SECTIONS = {
    "product": [
        "定义与产品身份", "性质与形态", "参考流与交接边界", "规格与相邻节点区分",
        "在系统中的角色", "分类与适用范围", "节点特定采集字段", "区域化补充要求",
        "数据适用状态与缺口", "出处",
    ],
    "activity": [
        "定义与参考活动", "参考产品与参考单位", "单元过程边界", "技术路线与相邻活动区分",
        "投入产出与脊边对账", "直接排放、废物与监测指标边界", "节点特定采集字段",
        "区域化补充要求", "数据适用状态与缺口", "出处",
    ],
}

EVIDENCE_TABLES = {
    "product": ["props", "params", "quality"],
    "activity": ["flows", "emissions", "indicators", "params", "quality"],
}

# Rich, node-specific tables may extend the mandatory floor.  They must never
# be deleted merely because the generic v2 renderer does not populate them.
OPTIONAL_EVIDENCE_TABLES = {
    "product": [],
    "activity": ["props"],
}


CLAIM_REQUIREMENTS = {
    "product": [
        ("product.identity.definition", "定义与产品身份", "external_fact"),
        ("product.identity.product_class", "定义与产品身份", "external_fact"),
        ("product.identity.modeling_scope", "定义与产品身份", "modeling_judgment"),
        ("product.form.physical_state", "性质与形态", "external_fact"),
        ("product.form.delivery_state", "性质与形态", "external_fact"),
        ("product.form.modeling_interpretation", "性质与形态", "modeling_judgment"),
        ("product.reference.flow_identity", "参考流与交接边界", "external_fact"),
        ("product.reference.handoff_unit", "参考流与交接边界", "external_fact"),
        ("product.reference.modeling_boundary", "参考流与交接边界", "modeling_judgment"),
        ("product.adjacent.distinction", "规格与相邻节点区分", "external_fact"),
        ("product.adjacent.specification", "规格与相邻节点区分", "external_fact"),
        ("product.adjacent.modeling_resolution", "规格与相邻节点区分", "modeling_judgment"),
        ("product.graph.system_role", "在系统中的角色", "internal_graph_fact"),
        ("product.system.modeling_role", "在系统中的角色", "modeling_judgment"),
        ("product.scope.classification", "分类与适用范围", "external_fact"),
        ("product.scope.exclusions", "分类与适用范围", "external_fact"),
        ("product.scope.modeling_use", "分类与适用范围", "modeling_judgment"),
        ("product.collection.fields", "节点特定采集字段", "modeling_judgment"),
        ("product.regionalization.fields", "区域化补充要求", "modeling_judgment"),
        ("product.quality.uncertainty", "数据适用状态与缺口", "modeling_judgment"),
        ("product.gaps.status", "数据适用状态与缺口", "evidence_gap"),
    ],
    "activity": [
        ("activity.identity.definition", "定义与参考活动", "external_fact"),
        ("activity.identity.catalyst_route", "定义与参考活动", "external_fact"),
        ("activity.identity.mechanism_interpretation", "定义与参考活动", "modeling_judgment"),
        ("activity.reference.product_identity", "参考产品与参考单位", "external_fact"),
        ("activity.reference.unit_handoff", "参考产品与参考单位", "external_fact"),
        ("activity.reference.modeling_rationale", "参考产品与参考单位", "modeling_judgment"),
        ("activity.boundary.included_operations", "单元过程边界", "external_fact"),
        ("activity.boundary.product_handoff", "单元过程边界", "external_fact"),
        ("activity.boundary.modeling_cutoff", "单元过程边界", "modeling_judgment"),
        ("activity.route.adjacent_distinction", "技术路线与相邻活动区分", "external_fact"),
        ("activity.route.internal_recycles", "技术路线与相邻活动区分", "external_fact"),
        ("activity.route.modeling_resolution", "技术路线与相邻活动区分", "modeling_judgment"),
        ("activity.graph.reconciliation", "投入产出与脊边对账", "internal_graph_fact"),
        ("activity.allocation.modeling_treatment", "投入产出与脊边对账", "modeling_judgment"),
        ("activity.environment.air_control", "直接排放、废物与监测指标边界", "external_fact"),
        ("activity.environment.water_control", "直接排放、废物与监测指标边界", "external_fact"),
        ("activity.environment.modeling_monitoring", "直接排放、废物与监测指标边界", "modeling_judgment"),
        ("activity.collection.fields", "节点特定采集字段", "modeling_judgment"),
        ("activity.regionalization.fields", "区域化补充要求", "modeling_judgment"),
        ("activity.quality.uncertainty", "数据适用状态与缺口", "modeling_judgment"),
        ("activity.gaps.status", "数据适用状态与缺口", "evidence_gap"),
    ],
}

TABLE_MIN_ROWS = {
    "product": {"props": 4, "params": 6, "quality": 5},
    "activity": {"flows": 4, "emissions": 3, "indicators": 3, "params": 6, "quality": 5},
}

# A generic source count is not enough.  These identity/boundary zones must be
# independently supported for the page to be called research-ready.
CORE_EVIDENCE_ZONES = {
    "product": {
        "identity": {"定义与产品身份"},
        "handoff": {"参考流与交接边界"},
    },
    "activity": {
        "identity": {"定义与参考活动"},
        "reference": {"参考产品与参考单位"},
        "boundary": {"单元过程边界"},
    },
}


def contract_for(node_type: str) -> dict[str, Any]:
    if node_type not in SECTIONS:
        raise ValueError(f"未知 Wiki node_type: {node_type!r}")
    return {
        "node_type": node_type,
        "sections": list(SECTIONS[node_type]),
        "evidence_tables": list(EVIDENCE_TABLES[node_type]),
        "optional_evidence_tables": list(OPTIONAL_EVIDENCE_TABLES[node_type]),
        "claim_requirements": nomination_requirements(node_type),
        "claim_cardinality": {
            "minimum_by_claim_kind": {
                "external_fact": MIN_CLAIMS_PER_EXTERNAL_REQUIREMENT,
                "internal_graph_fact": MIN_CLAIMS_PER_CONTROLLED_REQUIREMENT,
                "modeling_judgment": MIN_CLAIMS_PER_MODELING_REQUIREMENT,
                "evidence_gap": MIN_CLAIMS_PER_CONTROLLED_REQUIREMENT,
            },
            "maximum_per_requirement": MAX_CLAIMS_PER_REQUIREMENT,
        },
        "table_min_rows": dict(TABLE_MIN_ROWS[node_type]),
        "core_evidence_zones": {
            key: sorted(value) for key, value in CORE_EVIDENCE_ZONES[node_type].items()
        },
        "min_confirmed_external_claims": len(required_external_claim_slots(node_type)),
        "min_independent_authorities": 2,
        "min_confirmed_sections": len(required_external_sections(node_type)),
    }


def required_external_sections(node_type: str) -> set[str]:
    """Return sections whose claims must be external source nominations."""
    if node_type not in SECTIONS:
        raise ValueError(f"未知 Wiki node_type: {node_type!r}")
    if node_type == "product":
        return {
            "定义与产品身份", "性质与形态", "参考流与交接边界",
            "规格与相邻节点区分", "分类与适用范围",
        }
    return {
        "定义与参考活动", "参考产品与参考单位", "单元过程边界",
        "技术路线与相邻活动区分", "直接排放、废物与监测指标边界",
    }


def nomination_requirements(node_type: str) -> list[dict[str, str]]:
    if node_type not in CLAIM_REQUIREMENTS:
        raise ValueError(f"未知 Wiki node_type: {node_type!r}")
    return [
        {"requirement_id": requirement_id, "section": section, "claim_kind": claim_kind}
        for requirement_id, section, claim_kind in CLAIM_REQUIREMENTS[node_type]
    ]


def minimum_claims_for_requirement(requirement: dict[str, str]) -> int:
    kind = requirement.get("claim_kind")
    if kind == "external_fact":
        return MIN_CLAIMS_PER_EXTERNAL_REQUIREMENT
    if kind == "modeling_judgment":
        return MIN_CLAIMS_PER_MODELING_REQUIREMENT
    return MIN_CLAIMS_PER_CONTROLLED_REQUIREMENT


def minimum_nomination_claims(node_type: str) -> int:
    return sum(
        minimum_claims_for_requirement(requirement)
        for requirement in nomination_requirements(node_type)
    )


def required_external_claim_slots(node_type: str) -> set[str]:
    return {
        requirement_id
        for requirement_id, _section, claim_kind in CLAIM_REQUIREMENTS[node_type]
        if claim_kind == "external_fact"
    }


def expected_claim_kind(node_type: str, section: str) -> set[str]:
    """Allowed epistemic class for one deterministic nomination section."""
    return {
        claim_kind
        for _requirement_id, requirement_section, claim_kind in CLAIM_REQUIREMENTS[node_type]
        if requirement_section == section
    }
