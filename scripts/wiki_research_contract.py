#!/usr/bin/env python3
"""Deterministic Research Question Contract v2 compiler.

The compiler freezes semantic questions and acceptance boundaries while leaving
query wording, provider routing, and exploration order to the runtime Agent.
It intentionally has no model dependency so the same node and ontology version
produce the same core contract in every session.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


ONTOLOGY_VERSION = "wiki-research-question-v2"


QUESTION_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "dimension": "identity_and_terminology",
        "criticality": "required_for_model",
        "required_question_ids": ["identity.activity_definition"],
        "source_roles": ["manufacturer_technical", "authoritative_taxonomy"],
        "preferred_source_classes": ["manufacturer_technical", "standard_or_industry_body"],
        "subquestions": (
            {
                "question_id": "identity.activity_definition",
                "predicate": "is_distinct_activity_for_reference_product",
                "zh": "{subject_zh} 是否是制造边界中的独立活动，而不是对整机制造的泛称？",
                "en": "Is {subject_en} a distinct activity within the manufacturing boundary rather than a generic label for whole-product manufacturing?",
                "requirement_patterns": ["activity.identity", "identity.definition", "product.identity", "reference.product"],
                "exclusions": ["whole product manufacturing", "product marketing description"],
                "query_intents": ["definition", "factory_process"],
            },
            {
                "question_id": "identity.term_equivalence",
                "predicate": "has_supported_bilingual_terminology",
                "zh": "该节点的中英文名称是否指向同一对象、活动与边界？",
                "en": "Do the Chinese and English terms identify the same object, activity, and boundary?",
                "requirement_patterns": ["terminology", "term.equivalence", "identity.alias"],
                "exclusions": ["search-only alias treated as identity evidence"],
                "query_intents": ["terminology", "manufacturer_naming"],
            },
            {
                "question_id": "identity.adjacent_distinction",
                "predicate": "is_distinct_from_adjacent_activities",
                "zh": "该活动与相邻活动、维护操作和下游集成步骤的边界分别是什么？",
                "en": "How is the activity distinguished from adjacent activities, maintenance operations, and downstream integration steps?",
                "requirement_patterns": ["adjacent", "distinction", "exclusion", "neighbor"],
                "exclusions": ["adjacent activity used as direct identity proof"],
                "query_intents": ["adjacent_distinction", "process_comparison"],
            },
        ),
    },
    {
        "dimension": "process_origin_and_boundary",
        "criticality": "required_for_model",
        "required_question_ids": ["process.origin_boundary"],
        "source_roles": ["technical_primary_source"],
        "preferred_source_classes": ["manufacturer_technical", "standard_or_industry_body"],
        "subquestions": (
            {
                "question_id": "process.origin_boundary",
                "predicate": "defines_process_origin_and_boundary",
                "zh": "该活动从什么状态开始、包含哪些操作，并在什么状态结束？",
                "en": "What state starts the activity, which operations are included, and what state ends it?",
                "requirement_patterns": ["process", "boundary", "origin", "route", "operation"],
                "exclusions": ["upstream component manufacturing", "downstream use phase"],
                "query_intents": ["process_boundary", "manufacturing_route"],
            },
        ),
    },
    {
        "dimension": "collection_and_handoff",
        "criticality": "required_for_model",
        "required_question_ids": ["handoff.entry_exit_state"],
        "source_roles": ["manufacturer_technical", "node_specific_record"],
        "preferred_source_classes": ["manufacturer_technical", "node_specific_records"],
        "subquestions": (
            {
                "question_id": "handoff.entry_exit_state",
                "predicate": "defines_collection_and_handoff_state",
                "zh": "活动的输入收集状态、完成判据和交接对象分别是什么？",
                "en": "What are the input collection state, completion criteria, and handoff object of the activity?",
                "requirement_patterns": ["collection", "handoff", "entry", "exit", "input", "delivery"],
                "exclusions": ["shipment after downstream integration"],
                "query_intents": ["collection_state", "handoff_criteria"],
            },
        ),
    },
    {
        "dimension": "composition_and_quantity",
        "criticality": "required_for_model",
        "required_question_ids": ["quantity.reference_flow"],
        "source_roles": ["authoritative_quantitative", "independent_corroboration"],
        "preferred_source_classes": ["node_specific_records", "manufacturer_technical", "peer_reviewed_research"],
        "subquestions": (
            {
                "question_id": "quantity.reference_flow",
                "predicate": "defines_reference_flow_and_quantity_basis",
                "zh": "哪些投入、产出、组成和计量口径构成该活动的参考流？",
                "en": "Which inputs, outputs, composition, and measurement basis define the activity reference flow?",
                "requirement_patterns": ["composition", "quantity", "mass", "flow", "parameter", "input", "output"],
                "exclusions": ["unscoped product specification used as process quantity"],
                "query_intents": ["reference_flow", "composition_quantity"],
            },
        ),
    },
    {
        "dimension": "recovery_and_destination",
        "criticality": "contextual",
        "required_question_ids": [],
        "source_roles": ["destination_record", "regulator_or_authority"],
        "preferred_source_classes": ["node_specific_records", "government_or_regulator"],
        "subquestions": (
            {
                "question_id": "destination.recovery_route",
                "predicate": "defines_recovery_or_destination_route",
                "zh": "活动产生的物料、废物或不合格品进入什么回收、返工或处置路径？",
                "en": "Which recovery, rework, or disposal route receives materials, waste, or nonconforming outputs from the activity?",
                "requirement_patterns": ["recovery", "destination", "disposal", "waste", "rework"],
                "exclusions": ["generic end-of-life route unrelated to the factory activity"],
                "query_intents": ["recovery_route", "destination_record"],
            },
        ),
    },
    {
        "dimension": "representativeness_and_quality",
        "criticality": "recommended",
        "required_question_ids": [],
        "source_roles": ["target_region_source", "quality_record"],
        "preferred_source_classes": ["node_specific_records", "manufacturer_technical", "peer_reviewed_research"],
        "subquestions": (
            {
                "question_id": "quality.representativeness",
                "predicate": "defines_geographic_temporal_and_technical_representativeness",
                "zh": "证据在地域、时间、技术和质量控制方面能否代表目标节点？",
                "en": "Does the evidence represent the target node geographically, temporally, technically, and in quality control?",
                "requirement_patterns": ["representative", "quality", "geography", "regional", "period", "time"],
                "exclusions": ["undisclosed proxy treated as node-specific data"],
                "query_intents": ["representativeness", "quality_control"],
            },
        ),
    },
)


INTENT_SEEDS: dict[str, dict[str, list[str]]] = {
    "definition": {"zh": ["定义", "制造活动"], "en": ["definition", "manufacturing activity"]},
    "factory_process": {"zh": ["工厂", "工艺"], "en": ["factory", "process"]},
    "terminology": {"zh": ["术语", "名称"], "en": ["terminology", "name"]},
    "manufacturer_naming": {"zh": ["制造商", "技术文档"], "en": ["manufacturer", "technical documentation"]},
    "adjacent_distinction": {"zh": ["区别", "边界"], "en": ["distinction", "boundary"]},
    "process_comparison": {"zh": ["工艺对比"], "en": ["process comparison"]},
    "process_boundary": {"zh": ["起点", "终点", "系统边界"], "en": ["start state", "end state", "system boundary"]},
    "manufacturing_route": {"zh": ["制造路线"], "en": ["manufacturing route"]},
    "collection_state": {"zh": ["输入状态", "收集"], "en": ["input state", "collection"]},
    "handoff_criteria": {"zh": ["完成判据", "交接"], "en": ["completion criteria", "handoff"]},
    "reference_flow": {"zh": ["参考流", "计量口径"], "en": ["reference flow", "measurement basis"]},
    "composition_quantity": {"zh": ["组成", "数量"], "en": ["composition", "quantity"]},
    "recovery_route": {"zh": ["回收", "返工", "处置"], "en": ["recovery", "rework", "disposal"]},
    "destination_record": {"zh": ["去向凭证"], "en": ["destination record"]},
    "representativeness": {"zh": ["地域", "时间", "技术代表性"], "en": ["geographic", "temporal", "technical representativeness"]},
    "quality_control": {"zh": ["质量控制", "出厂检验"], "en": ["quality control", "factory inspection"]},
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def contract_sha256(contracts: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_canonical(contracts).encode()).hexdigest()


def requirement_question_id(requirement_id: str) -> str | None:
    """Map a frozen content requirement to one semantic research question."""
    value = str(requirement_id or "").strip().lower()
    if not value:
        return None
    # Precedence is intentional for overlapping names such as
    # ``boundary.product_handoff``. The selected binding is persisted in the
    # plan so a later Agent never has to repeat this interpretation.
    rules = (
        ("identity.adjacent_distinction", (".adjacent.", ".route.adjacent_distinction", ".scope.exclusions")),
        ("handoff.entry_exit_state", (".unit_handoff", ".handoff_unit", ".product_handoff", ".delivery_state")),
        ("identity.activity_definition", (".identity.definition", ".identity.product_class", ".reference.product_identity")),
        ("identity.term_equivalence", (".identity.alias", ".terminology.")),
        ("quantity.reference_flow", (".reference.flow_identity", ".collection.fields", ".composition.", ".quantity.", ".mass.")),
        ("destination.recovery_route", (".route.internal_recycles", ".recovery.", ".destination.", ".disposal.", ".environment.")),
        ("quality.representativeness", (".regionalization.", ".quality.", ".uncertainty", ".gaps.")),
        ("process.origin_boundary", (".boundary.", ".route.", ".physical_state", ".modeling_boundary", ".included_operations")),
    )
    for question_id, fragments in rules:
        if any(fragment in value for fragment in fragments):
            return question_id
    return None


def build_question_contracts(
    node_id: str, node_name: str, terminology: dict[str, Any],
    requirement_ids: list[str] | None = None,
    requirement_kinds: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    subject_zh = str(terminology.get("canonical_zh") or node_name or node_id).strip()
    english_terms = [
        terminology.get("canonical_en"),
        *(terminology.get("candidate_aliases_en") or []),
        *(terminology.get("translated_search_terms_en") or []),
    ]
    subject_en = next((str(value).strip() for value in english_terms if str(value or "").strip()), node_id)
    scope = {
        "node_id": str(node_id),
        "node_name": str(node_name),
        "reference_product": subject_zh.split("|", 1)[1].strip() if "|" in subject_zh else subject_zh,
    }
    # The evidence ledger may only require externally verifiable facts.
    # Modeling judgments and internal graph facts are reviewed by their own
    # logic/table/content contracts; binding them to external-source closure
    # creates an impossible gate that no amount of search can satisfy.
    evidence_requirement_ids = [
        str(requirement_id) for requirement_id in requirement_ids or []
        if requirement_kinds is None
        or requirement_kinds.get(str(requirement_id)) == "external_fact"
    ]
    contracts: list[dict[str, Any]] = []
    for template in QUESTION_TEMPLATES:
        subquestions = []
        for source in template["subquestions"]:
            intents = []
            for ordinal, intent in enumerate(source["query_intents"], start=1):
                seeds = INTENT_SEEDS[intent]
                intents.append({
                    "intent_id": f"{source['question_id']}.{intent}",
                    "purpose": intent,
                    "language_policy": "adaptive_zh_then_en_if_available",
                    "seed_terms": {"zh": seeds["zh"], "en": seeds["en"]},
                    "preferred_source_roles": list(template["source_roles"]),
                    "preferred_source_classes": list(template["preferred_source_classes"]),
                    "priority": ordinal,
                })
            subquestions.append({
                "question_id": source["question_id"],
                "requirement_ids": sorted({
                    str(requirement_id) for requirement_id in evidence_requirement_ids
                    if requirement_question_id(str(requirement_id)) == source["question_id"]
                }),
                "closure_rule": "all_bound_requirements_confirmed",
                "question": {
                    "zh": source["zh"].format(subject_zh=subject_zh, subject_en=subject_en),
                    "en": source["en"].format(subject_zh=subject_zh, subject_en=subject_en),
                },
                "semantic_frame": {
                    "subject": {"node_id": str(node_id), "zh": subject_zh, "en": subject_en},
                    "predicate": source["predicate"],
                    "scope": scope,
                    "exclusions": list(source["exclusions"]),
                },
                "requirement_patterns": list(source["requirement_patterns"]),
                "query_intents": intents,
            })
        available_required = {
            str(question["question_id"])
            for question in subquestions if question.get("requirement_ids")
        }
        contracts.append({
            "dimension": template["dimension"],
            "criticality": template["criticality"],
            "applicability": "applicable_unless_explicitly_overridden",
            "required_question_ids": [
                question_id for question_id in template["required_question_ids"]
                if requirement_kinds is None or question_id in available_required
            ],
            "source_role_requirements": list(template["source_roles"]),
            "preferred_source_classes": list(template["preferred_source_classes"]),
            "acceptance": {
                "support_type": "direct_or_explicitly_composed",
                "must_match_subject_scope": True,
                "allow_explicit_gap": template["criticality"] != "required_for_model",
            },
            "subquestions": subquestions,
        })
    return contracts


def iter_query_tasks(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return stable explicit query tasks; legacy plans retain one task per label."""
    contracts = plan.get("research_question_contracts")
    if not isinstance(contracts, list) or not contracts:
        return [
            {
                "dimension": str(question),
                "question_id": str(question),
                "question": {"zh": str(question).replace("_", " "), "en": str(question).replace("_", " ")},
                "intent": {
                    "intent_id": f"legacy.{question}", "purpose": str(question),
                    "language_policy": "legacy_single_track",
                    "seed_terms": {"zh": [], "en": []},
                    "preferred_source_roles": [], "preferred_source_classes": [], "priority": 1,
                },
                "criticality": "legacy",
            }
            for question in plan.get("research_questions", [])
        ]
    tasks = []
    for contract in contracts:
        for question in contract.get("subquestions") or []:
            for intent in question.get("query_intents") or []:
                tasks.append({
                    "dimension": str(contract.get("dimension") or ""),
                    "criticality": str(contract.get("criticality") or "recommended"),
                    "question_id": str(question.get("question_id") or ""),
                    "question": question.get("question") or {},
                    "semantic_frame": question.get("semantic_frame") or {},
                    "requirement_patterns": question.get("requirement_patterns") or [],
                    "intent": intent,
                })
    return tasks


def match_question(plan: dict[str, Any], requirement_id: str) -> dict[str, Any] | None:
    """Resolve a claim requirement through the plan's explicit binding patterns."""
    normalized = str(requirement_id or "").strip().lower()
    if not normalized:
        return None
    has_compiled_bindings = False
    for contract in plan.get("research_question_contracts") or []:
        for question in contract.get("subquestions") or []:
            exact = {str(value).strip().lower() for value in question.get("requirement_ids") or []}
            has_compiled_bindings = has_compiled_bindings or bool(exact)
            if normalized in exact:
                return {
                    "dimension": contract.get("dimension"),
                    "criticality": contract.get("criticality"),
                    "question_id": question.get("question_id"),
                    "question": question.get("question") or {},
                    "semantic_frame": question.get("semantic_frame") or {},
                    "source_role_requirements": contract.get("source_role_requirements") or [],
                    "preferred_source_classes": contract.get("preferred_source_classes") or [],
                    "query_intents": question.get("query_intents") or [],
                    "mapping_status": "exact_contract_binding",
                }
    if has_compiled_bindings:
        return None
    for contract in plan.get("research_question_contracts") or []:
        for question in contract.get("subquestions") or []:
            patterns = [str(value).lower() for value in question.get("requirement_patterns") or []]
            if any(pattern and pattern in normalized for pattern in patterns):
                return {
                    "dimension": contract.get("dimension"),
                    "criticality": contract.get("criticality"),
                    "question_id": question.get("question_id"),
                    "question": question.get("question") or {},
                    "semantic_frame": question.get("semantic_frame") or {},
                    "source_role_requirements": contract.get("source_role_requirements") or [],
                    "preferred_source_classes": contract.get("preferred_source_classes") or [],
                    "query_intents": question.get("query_intents") or [],
                    "mapping_status": "compatibility_pattern_binding",
                }
    return None


def validate_question_contracts(plan: dict[str, Any]) -> dict[str, Any]:
    contracts = plan.get("research_question_contracts")
    expected_dimensions = {item["dimension"] for item in QUESTION_TEMPLATES}
    if not isinstance(contracts, list):
        return {"present": False, "valid": False, "errors": ["research_question_contracts_missing"]}
    errors: list[str] = []
    dimensions = {str(item.get("dimension") or "") for item in contracts if isinstance(item, dict)}
    if dimensions != expected_dimensions:
        errors.append("question_contract_dimensions_incomplete")
    question_ids: list[str] = []
    requirement_owners: dict[str, str] = {}
    required_without_bindings: list[str] = []
    for contract in contracts:
        if not isinstance(contract, dict) or not contract.get("criticality"):
            errors.append("question_contract_invalid")
            continue
        subquestions = contract.get("subquestions")
        if not isinstance(subquestions, list) or not subquestions:
            errors.append(f"{contract.get('dimension')}:subquestions_missing")
            continue
        required = set(contract.get("required_question_ids") or [])
        available = {str(item.get("question_id") or "") for item in subquestions if isinstance(item, dict)}
        if not required <= available:
            errors.append(f"{contract.get('dimension')}:required_questions_missing")
        for question in subquestions:
            question_id = str(question.get("question_id") or "")
            question_ids.append(question_id)
            exact_requirements = [str(value) for value in question.get("requirement_ids") or []]
            if question_id in required and not exact_requirements:
                required_without_bindings.append(question_id)
            for requirement_id in exact_requirements:
                previous = requirement_owners.setdefault(requirement_id, question_id)
                if previous != question_id:
                    errors.append(f"{requirement_id}:duplicate_requirement_binding")
            if not question_id or not all(str((question.get("question") or {}).get(lang) or "").strip() for lang in ("zh", "en")):
                errors.append(f"{contract.get('dimension')}:question_text_invalid")
            if not (question.get("semantic_frame") or {}).get("predicate"):
                errors.append(f"{question_id}:semantic_frame_invalid")
            if not question.get("query_intents"):
                errors.append(f"{question_id}:query_intents_missing")
    if len(question_ids) != len(set(question_ids)):
        errors.append("duplicate_question_ids")
    if requirement_owners and required_without_bindings:
        errors.extend(f"{question_id}:required_question_unbound"
                      for question_id in required_without_bindings)
    expected_hash = contract_sha256(contracts)
    if plan.get("question_contract_sha256") != expected_hash:
        errors.append("question_contract_hash_mismatch")
    if plan.get("research_question_contract_version") != ONTOLOGY_VERSION:
        errors.append("question_contract_version_invalid")
    return {"present": True, "valid": not errors, "errors": errors, "question_count": len(question_ids)}
