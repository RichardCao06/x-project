from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


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


def test_diversity_repair_cannot_normalize_a_prior_external_nomination(tmp_path: Path) -> None:
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

    before = result.read_bytes()
    with pytest.raises(ValueError, match="must regenerate external source nominations"):
        launcher_module().repair_prior_result(result, node, scout)
    assert result.read_bytes() == before


def test_diversity_repair_regenerates_and_attests_active_scout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = launcher_module()
    node = frozen_node("A015")
    node["dossier"]["claim_requirements"] = [
        {"requirement_id": role, "claim_kind": "external_fact", "section": role}
        for role in ("identity", "process_boundary", "adjacent_distinction")
    ]
    workflow, schema = write_inputs(tmp_path, [node])
    out = tmp_path / "runtime"
    out.mkdir()
    stale = valid_claims(node)
    for claim in stale:
        claim.update({
            "claim_text": f"stale {claim['claim_id']}", "believed_source": "Rejected source",
            "believed_locator": "identity_and_terminology；stale",
            "attribution_confidence": "medium",
        })
    (out / "nomination-result.json").write_text(json.dumps({
        "protocol": {"version": "wiki-ku-nomination-v2", "mode": "extract"},
        "claims": stale,
    }), encoding="utf-8")
    stale_result_sha256 = launcher.sha256(out / "nomination-result.json")
    candidates = [
        {"title": f"Replacement {index}", "url": f"https://new{index}.example/page",
         "language": "zh" if index == 0 else "en", "research_question": question,
         "current_job_status": "candidate_unverified"}
        for index, question in enumerate((
            "identity_and_terminology", "process_origin_and_boundary", "collection_and_handoff"
        ))
    ]
    scout = tmp_path / "repair-scout.json"
    scout.write_text(json.dumps({
        "protocol": "wiki-research-scout-v1", "node_id": "A015", "candidates": candidates,
        "diversity_repair": {
            "protocol": "wiki-source-diversity-repair-v1",
            "excluded_urls": ["https://rejected.example/page"],
        },
    }), encoding="utf-8")
    regenerated = valid_claims(node)
    for index, claim in enumerate(regenerated):
        claim.update({
            "claim_text": f"replacement fact {index}",
            "believed_source": candidates[index]["title"],
            "believed_locator": f"{candidates[index]['research_question']}；replacement locator",
            "attribution_confidence": "medium",
        })
    model_result = {
        "protocol": {"version": "wiki-ku-nomination-v2", "mode": "extract"},
        "claims": regenerated,
    }
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        raw = Path(command[command.index("-o") + 1])
        raw.write_text(json.dumps(model_result), encoding="utf-8")
        return __import__("subprocess").CompletedProcess(command, 0)

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    monkeypatch.setattr("sys.argv", [
        str(SCRIPT), str(workflow), str(schema), str(out), "--cost-usd", "0",
        "--research-scout", str(scout),
    ])

    assert launcher.main() == 0
    assert len(calls) == 1
    result_path = out / "nomination-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert {row["believed_source"] for row in result["claims"]} == {
        "Replacement 0", "Replacement 1", "Replacement 2",
    }
    invocation = json.loads((out / "nomination-invocation.json").read_text(encoding="utf-8"))
    assert invocation["research_scout"]["sha256"] == launcher.sha256(scout)
    assert invocation["result"]["sha256"] != stale_result_sha256
    assert invocation["result"] == {
        "path": str(result_path.resolve()),
        "sha256": launcher.sha256(result_path),
        "research_scout_sha256": launcher.sha256(scout),
    }
    usage = json.loads((out / "nomination-usage.json").read_text(encoding="utf-8"))
    assert usage["deterministic_repair"] is None


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
