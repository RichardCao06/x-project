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


def test_nomination_prompt_guards_target_product_against_adjacent_object_drift() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "不得把机箱、组件或系统集合写成目标节点" in source
    assert "physical_state 与 delivery_state 必须描述单个目标产品自身" in source
    assert "adjacent.specification 应提名目标本体" in source
    assert "scope.exclusions 应以目标本体为主语" in source
    assert "不表示刀片机箱，也不表示由多个 server blades 构成的集合" in source
    assert "若存在 requirement_routes" in source
    assert "不得把 requirement 名称本身改写进事实断言" in source
