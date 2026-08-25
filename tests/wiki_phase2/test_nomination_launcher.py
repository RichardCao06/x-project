from __future__ import annotations

import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest

from wiki_batch import validate_nomination_claim_slots
from wiki_quality_contract import nomination_requirements


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "vendor/lca_cornerstone/scripts/run_wiki_nomination_capture.py"


def launcher_module():
    spec = importlib.util.spec_from_file_location("nomination_launcher", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def frozen_node(node_id: str = "P003") -> dict:
    return {
        "node_id": node_id, "industry": "ict_equipment", "name": "服务器, 通用计算, 刀片式",
        "node_type": "product", "facets": {"equipment_class": "server", "form_factor": "blade"},
        "boundary": "foreground", "dossier": {"claim_requirements": [
            {"requirement_id": "identity", "claim_kind": "external_fact"},
            {"requirement_id": "scope", "claim_kind": "modeling_judgment"},
        ]},
    }


def template_schema() -> dict:
    return {"properties": {"claims": {"items": {"properties": {
        "node_id": {"const": "A015"}, "industry": {"const": "oil_refining"},
        "node_identity": {"type": "object"}, "claim_id": {"type": "string"},
    }}}}}


def write_inputs(tmp_path: Path, nodes: list[dict]) -> tuple[Path, Path]:
    workflow = tmp_path / "workflow.js"
    workflow.write_text(
        "const NODES = " + json.dumps(nodes, ensure_ascii=False) + "\n/* DATA-BINDING:END */\n",
        encoding="utf-8",
    )
    template = tmp_path / "template.json"
    template.write_text(json.dumps(template_schema()), encoding="utf-8")
    return workflow, template


def test_nomination_schema_is_bound_to_the_workflow_node_not_historical_example(tmp_path: Path) -> None:
    workflow, template = write_inputs(tmp_path, [frozen_node()])
    node, output = launcher_module().dynamic_schema(workflow, template, tmp_path / "effective.json")
    schema = json.loads(output.read_text(encoding="utf-8"))
    properties = schema["properties"]["claims"]["items"]["properties"]
    assert node["node_id"] == "P003"
    assert properties["node_id"]["const"] == "P003"
    assert properties["industry"]["const"] == "ict_equipment"
    assert properties["claim_id"]["pattern"] == "^P003-[0-9]+$"
    assert properties["node_identity"]["properties"]["facets"]["properties"]["form_factor"]["const"] == "blade"
    assert properties["node_identity"]["additionalProperties"] is False
    assert schema["properties"]["claims"]["minItems"] == 3
    assert schema["properties"]["claims"]["maxItems"] == 3


def test_nomination_launcher_rejects_cross_node_capture(tmp_path: Path) -> None:
    workflow, template = write_inputs(tmp_path, [frozen_node(), frozen_node("P004")])
    with pytest.raises(ValueError, match="唯一节点"):
        launcher_module().dynamic_schema(workflow, template, tmp_path / "effective.json")


def valid_claims(node: dict) -> list[dict]:
    identity = {"display_name": node["name"], "node_type": node["node_type"],
                "facets": node["facets"], "boundary": node["boundary"]}
    rows: list[dict] = []
    for requirement in node["dossier"]["claim_requirements"]:
        copies = 2 if requirement["claim_kind"] == "modeling_judgment" else 1
        for _ in range(copies):
            rows.append({"requirement_id": requirement["requirement_id"],
                         "section": requirement.get("section"),
                         "claim_kind": requirement["claim_kind"],
                         "node_id": node["node_id"], "industry": node["industry"],
                         "node_identity": identity, "claim_id": f"{node['node_id']}-{len(rows)}"})
    return rows


@pytest.fixture
def a019_appended_modeling_layout() -> tuple[dict, list[dict]]:
    """Mirror A019: every slot once, then each second modeling row."""
    node = frozen_node("A019")
    node.update({
        "name": "配置出厂, BIOS配置 | 服务器, 通用计算, 2U",
        "node_type": "activity",
    })
    requirements = nomination_requirements("activity")
    node["dossier"]["claim_requirements"] = requirements
    identity = {
        "display_name": node["name"], "node_type": node["node_type"],
        "facets": node["facets"], "boundary": node["boundary"],
    }

    def claim(requirement: dict, ordinal: int) -> dict:
        kind = requirement["claim_kind"]
        if kind == "internal_graph_fact":
            source = "LCA-CORNERSTONE_GRAPH"
        elif kind in {"modeling_judgment", "evidence_gap"}:
            source = "INTERNAL_MODELING_JUDGMENT"
        else:
            source = f"Official source for {requirement['requirement_id']}"
        return {
            "requirement_id": requirement["requirement_id"],
            "section": requirement["section"],
            "claim_kind": kind,
            "node_id": node["node_id"],
            "industry": node["industry"],
            "node_identity": identity,
            "claim_id": f"raw-{requirement['requirement_id']}-{ordinal}",
            "claim_text": f"{requirement['requirement_id']} claim {ordinal}",
            "believed_source": source,
            "believed_locator": (
                "frozen node dossier and graph connections"
                if kind == "internal_graph_fact"
                else "controlled internal claim"
                if kind in {"modeling_judgment", "evidence_gap"}
                else f"locator for {requirement['requirement_id']}"
            ),
            "attribution_confidence": "medium",
        }

    claims = [claim(requirement, 0) for requirement in requirements]
    claims.extend(
        claim(requirement, 1)
        for requirement in requirements
        if requirement["claim_kind"] == "modeling_judgment"
    )
    return node, claims


def test_nomination_result_validation_rejects_identity_drift_even_after_exit_zero(tmp_path: Path) -> None:
    node = frozen_node()
    for requirement in node["dossier"]["claim_requirements"]:
        requirement["section"] = requirement["requirement_id"]
    result = tmp_path / "result.json"
    claims = valid_claims(node)
    for claim in claims:
        claim.update({"claim_text": f"node-specific claim {claim['claim_id']}", "believed_locator": "section",
                      "attribution_confidence": "medium"})
        claim["believed_source"] = (
            "Official source" if claim["claim_kind"] == "external_fact"
            else "INTERNAL_MODELING_JUDGMENT"
        )
    result.write_text(json.dumps({"protocol": {"version": "wiki-ku-nomination-v2", "mode": "extract"},
                                  "claims": claims}), encoding="utf-8")
    launcher_module().validate_result(result, node)
    claims[0]["node_id"] = "A015"
    result.write_text(json.dumps({"protocol": {"version": "wiki-ku-nomination-v2", "mode": "extract"},
                                  "claims": claims}), encoding="utf-8")
    with pytest.raises(ValueError, match="身份"):
        launcher_module().validate_result(result, node)


def test_canonicalization_moves_protocol_owned_provenance_out_of_agent_control(tmp_path: Path) -> None:
    raw, output = tmp_path / "raw.json", tmp_path / "canonical.json"
    raw.write_text(json.dumps({"claims": [
        {"claim_kind": "modeling_judgment", "believed_source": "agent invented"},
        {"claim_kind": "internal_graph_fact", "believed_source": "agent invented"},
        {"claim_kind": "external_fact", "believed_source": "Official source"},
    ]}), encoding="utf-8")
    launcher_module().canonicalize_result(raw, output)
    claims = json.loads(output.read_text(encoding="utf-8"))["claims"]
    assert claims[0]["believed_source"] == "INTERNAL_MODELING_JUDGMENT"
    assert claims[1]["believed_source"] == "LCA-CORNERSTONE_GRAPH"
    assert claims[2]["believed_source"] == "Official source"


def test_canonicalization_trims_requirement_overflow_to_frozen_quota(tmp_path: Path) -> None:
    raw, output = tmp_path / "raw.json", tmp_path / "canonical.json"
    node = {"node_id": "P030", "dossier": {"claim_requirements": [{
        "requirement_id": "product.quality.uncertainty",
        "claim_kind": "modeling_judgment", "section": "数据适用状态与缺口",
    }]}}
    raw.write_text(json.dumps({"claims": [{
        "requirement_id": "product.quality.uncertainty", "claim_kind": "modeling_judgment",
        "section": "wrong", "believed_source": "agent", "claim_text": str(index),
    } for index in range(3)]}), encoding="utf-8")
    launcher_module().canonicalize_result(raw, output, node)
    claims = json.loads(output.read_text(encoding="utf-8"))["claims"]
    assert len(claims) == 2
    assert [claim["claim_id"] for claim in claims] == ["P030-0", "P030-1"]
    assert all(claim["believed_source"] == "INTERNAL_MODELING_JUDGMENT" for claim in claims)


def test_canonicalization_groups_a019_layout_without_semantic_drift(
    tmp_path: Path, a019_appended_modeling_layout: tuple[dict, list[dict]],
) -> None:
    node, raw_claims = a019_appended_modeling_layout
    raw, output = tmp_path / "raw.json", tmp_path / "canonical.json"
    raw.write_text(json.dumps({
        "protocol": {"version": "wiki-ku-nomination-v2", "mode": "extract"},
        "claims": raw_claims,
    }), encoding="utf-8")
    semantic_fields = (
        "requirement_id", "claim_text", "believed_source",
        "believed_locator", "attribution_confidence",
    )
    before = Counter(tuple(claim[field] for field in semantic_fields) for claim in raw_claims)
    requirement_order = {
        row["requirement_id"]: index
        for index, row in enumerate(node["dossier"]["claim_requirements"])
    }
    raw_ranks = [requirement_order[claim["requirement_id"]] for claim in raw_claims]
    assert sum(right < left for left, right in zip(raw_ranks, raw_ranks[1:])) == 1
    assert sum(
        left > right
        for index, left in enumerate(raw_ranks)
        for right in raw_ranks[index + 1:]
    ) == 71
    within_slot_before = {
        requirement["requirement_id"]: [
            claim["claim_text"] for claim in raw_claims
            if claim["requirement_id"] == requirement["requirement_id"]
        ]
        for requirement in node["dossier"]["claim_requirements"]
    }

    module = launcher_module()
    module.canonicalize_result(raw, output, node)
    document = json.loads(output.read_text(encoding="utf-8"))
    claims = document["claims"]
    requirements = node["dossier"]["claim_requirements"]
    quotas = {"external_fact": 1, "modeling_judgment": 2,
              "internal_graph_fact": 1, "evidence_gap": 1}
    expected_ids = [
        requirement["requirement_id"]
        for requirement in requirements
        for _ in range(quotas[requirement["claim_kind"]])
    ]

    assert [claim["requirement_id"] for claim in claims] == expected_ids
    assert Counter(claim["requirement_id"] for claim in claims) == Counter(expected_ids)
    assert Counter(tuple(claim[field] for field in semantic_fields) for claim in claims) == before
    assert {
        requirement["requirement_id"]: [
            claim["claim_text"] for claim in claims
            if claim["requirement_id"] == requirement["requirement_id"]
        ]
        for requirement in requirements
    } == within_slot_before
    assert [claim["claim_id"] for claim in claims] == [
        f"A019-{index}" for index in range(len(claims))
    ]

    ranks = [requirement_order[claim["requirement_id"]] for claim in claims]
    adjacent_regressions = sum(right < left for left, right in zip(ranks, ranks[1:]))
    pairwise_inversions = sum(
        left > right
        for index, left in enumerate(ranks)
        for right in ranks[index + 1:]
    )
    assert adjacent_regressions == 0
    assert pairwise_inversions == 0
    module.validate_result(output, node)
    assert validate_nomination_claim_slots(
        node["node_id"], node["node_type"], claims, requirements,
    ) == dict(Counter(expected_ids))


def test_launcher_rejects_a019_noncanonical_requirement_order(
    tmp_path: Path, a019_appended_modeling_layout: tuple[dict, list[dict]],
) -> None:
    node, claims = a019_appended_modeling_layout
    result = tmp_path / "result.json"
    for index, claim in enumerate(claims):
        claim["claim_id"] = f"A019-{index}"
    result.write_text(json.dumps({
        "protocol": {"version": "wiki-ku-nomination-v2", "mode": "extract"},
        "claims": claims,
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="requirement_id 顺序漂移"):
        launcher_module().validate_result(result, node)


def test_deterministic_repair_reorders_cached_a019_nomination(
    tmp_path: Path, a019_appended_modeling_layout: tuple[dict, list[dict]],
) -> None:
    node, claims = a019_appended_modeling_layout
    questions = (
        "identity_and_terminology",
        "collection_and_handoff",
        "process_origin_and_boundary",
    )
    candidates = []
    external_index = 0
    for claim in claims:
        if claim["claim_kind"] != "external_fact":
            continue
        title = f"Official source {external_index}"
        claim["believed_source"] = title
        claim["believed_locator"] = (
            f"{questions[external_index % len(questions)]}；locator {external_index}"
        )
        candidates.append({
            "title": title,
            "url": f"https://source{external_index}.example/evidence",
            "language": "zh" if external_index == 0 else "en",
        })
        external_index += 1
    result = tmp_path / "nomination-result.json"
    result.write_text(json.dumps({
        "protocol": {"version": "wiki-ku-nomination-v2", "mode": "extract"},
        "claims": claims,
    }), encoding="utf-8")
    scout = {
        "diversity_repair": {"protocol": "wiki-source-diversity-repair-v1"},
        "candidates": candidates,
    }

    repair = launcher_module().repair_prior_result(result, node, scout)
    repaired_claims = json.loads(result.read_text(encoding="utf-8"))["claims"]
    order = {
        requirement["requirement_id"]: index
        for index, requirement in enumerate(node["dossier"]["claim_requirements"])
    }
    ranks = [order[claim["requirement_id"]] for claim in repaired_claims]

    assert repair["protocol"] == "wiki-prior-nomination-repair-v1"
    assert all(right >= left for left, right in zip(ranks, ranks[1:]))
    launcher_module().validate_result(result, node, scout)


def test_diversity_repair_fills_missing_second_modeling_judgment(tmp_path: Path) -> None:
    node = frozen_node("A015")
    node["dossier"]["claim_requirements"] = [
        {"requirement_id": f"external-{index}", "claim_kind": "external_fact",
         "section": f"section-{index}"} for index in range(3)
    ] + [{"requirement_id": "model", "claim_kind": "modeling_judgment",
          "section": "model section"}]
    identity = {"display_name": node["name"], "node_type": node["node_type"],
                "facets": node["facets"], "boundary": node["boundary"]}
    claims = []
    for index in range(3):
        claims.append({
            "requirement_id": f"external-{index}", "section": f"section-{index}",
            "claim_kind": "external_fact", "node_id": "A015", "industry": "ict_equipment",
            "node_identity": identity, "claim_id": f"A015-{index}",
            "claim_text": f"external fact {index}", "believed_source": f"Source {index}",
            "believed_locator": f"question-{index}；locator", "attribution_confidence": "medium",
        })
    claims.append({
        "requirement_id": "model", "section": "model section",
        "claim_kind": "modeling_judgment", "node_id": "A015", "industry": "ict_equipment",
        "node_identity": identity, "claim_id": "A015-3", "claim_text": "first judgment",
        "believed_source": "INTERNAL_MODELING_JUDGMENT",
        "believed_locator": "controlled internal claim", "attribution_confidence": "medium",
    })
    result = tmp_path / "result.json"
    result.write_text(json.dumps({
        "protocol": {"version": "wiki-ku-nomination-v2", "mode": "extract"}, "claims": claims,
    }), encoding="utf-8")
    scout = {
        "diversity_repair": {"protocol": "wiki-source-diversity-repair-v1"},
        "candidates": [
            {"title": f"Source {index}", "url": f"https://domain{index}.example/source",
             "language": "zh" if index == 0 else "en"}
            for index in range(3)
        ],
    }

    repaired = launcher_module().repair_prior_result(result, node, scout)

    assert repaired["filled_requirements"] == ["model"]
    assert len(json.loads(result.read_text(encoding="utf-8"))["claims"]) == 5


def test_research_scout_requires_three_external_questions_without_forcing_quality_slot(tmp_path: Path) -> None:
    node = frozen_node()
    node["dossier"]["claim_requirements"] = [
        {"requirement_id": "identity", "claim_kind": "external_fact", "section": "identity"},
        {"requirement_id": "handoff", "claim_kind": "external_fact", "section": "handoff"},
        {"requirement_id": "boundary", "claim_kind": "external_fact", "section": "boundary"},
        {"requirement_id": "quality", "claim_kind": "modeling_judgment", "section": "quality"},
    ]
    claims = valid_claims(node)
    questions = iter(["identity_and_terminology", "collection_and_handoff", "recovery_and_destination"])
    scout_candidates = []
    for claim in claims:
        claim.update({
            "claim_text": f"node-specific claim {claim['claim_id']}",
            "attribution_confidence": "medium",
        })
        if claim["claim_kind"] == "external_fact":
            question = next(questions)
            claim["believed_source"] = f"Scout source {question}"
            claim["believed_locator"] = f"{question}；locator"
            scout_candidates.append({
                "title": claim["believed_source"],
                "url": f"https://source{len(scout_candidates)}.example/evidence",
                "language": "zh" if not scout_candidates else "en",
            })
        else:
            claim["believed_source"] = "INTERNAL_MODELING_JUDGMENT"
            claim["believed_locator"] = "controlled internal claim"
    result = tmp_path / "result.json"
    result.write_text(json.dumps({
        "protocol": {"version": "wiki-ku-nomination-v2", "mode": "extract"},
        "claims": claims,
    }), encoding="utf-8")
    launcher_module().validate_result(result, node, {"candidates": scout_candidates})


def test_research_scout_rejects_fewer_than_three_external_questions(tmp_path: Path) -> None:
    node = frozen_node()
    node["dossier"]["claim_requirements"] = [
        {"requirement_id": "identity", "claim_kind": "external_fact", "section": "identity"},
        {"requirement_id": "handoff", "claim_kind": "external_fact", "section": "handoff"},
        {"requirement_id": "boundary", "claim_kind": "external_fact", "section": "boundary"},
    ]
    claims = valid_claims(node)
    scout_candidates = []
    for index, claim in enumerate(claims):
        question = "identity_and_terminology" if index < 2 else "collection_and_handoff"
        claim.update({
            "claim_text": f"node-specific claim {claim['claim_id']}",
            "believed_source": f"Scout source {index}",
            "believed_locator": f"{question}；locator",
            "attribution_confidence": "medium",
        })
        scout_candidates.append({
            "title": claim["believed_source"],
            "url": f"https://source{index}.example/evidence",
            "language": "zh" if index == 0 else "en",
        })
    result = tmp_path / "result.json"
    result.write_text(json.dumps({
        "protocol": {"version": "wiki-ku-nomination-v2", "mode": "extract"},
        "claims": claims,
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="至少 3 个研究问题"):
        launcher_module().validate_result(result, node, {"candidates": scout_candidates})


def test_nomination_prompt_guards_target_product_against_adjacent_object_drift() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "不得把机箱、组件或系统集合写成目标节点" in source
    assert "physical_state 与 delivery_state 必须描述单个目标产品自身" in source
    assert "adjacent.specification 应提名目标本体" in source
    assert "scope.exclusions 应以目标本体为主语" in source
    assert "不表示刀片机箱，也不表示由多个 server blades 构成的集合" in source
    assert "legacy requirement_routes" in source
    assert "不得限制其他来源发现" in source
    assert "不得改投其他来源" not in source
    assert "不得把 requirement 名称本身改写进事实断言" in source
    assert "不得把它塞入" in source
    assert "质量与不确定性建模由冻结的 quality.uncertainty requirement 覆盖" in source
    assert "process_origin_and_boundary 的英文工艺来源" in source
    assert "不得只把英文来源分配给产品身份、交付形态" in source
    assert "必须是单一谓词" in source
    assert "research-scout-source-specific-v9" in source
    assert '"launcher_sha256"' in source
