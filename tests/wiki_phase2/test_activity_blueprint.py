from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def module():
    path = Path(__file__).resolve().parents[2] / "scripts/build_wiki_content_blueprint.py"
    spec = importlib.util.spec_from_file_location("build_wiki_content_blueprint", path)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


def test_activity_blueprint_is_node_specific_and_complete() -> None:
    graph = {"activities": [{"id": "A039", "name": "blade integration",
                              "facets": {"technology_route": "chassis_integration"},
                              "inputs": ["board", "memory", "storage", "power"],
                              "outputs": [{"product": "blade server", "role": "reference"}]}],
             "products": [{"id": "P001", "name": "board"},
                          {"id": "P002", "name": "memory"},
                          {"id": "P003", "name": "storage"},
                          {"id": "P004", "name": "power"},
                          {"id": "P005", "name": "blade server"}]}
    result = module().build(graph, "A039")
    assert result["node_type"] == "activity" and len(result["sections"]) == 9
    assert set(result["evidence_tables"]) == {"flows", "props", "params", "emissions", "indicators", "quality"}
    assert result["identity_tokens"] == ["A039", "blade server"]
    assert "A039" in result["advisory_tokens"] and "blade server" in result["advisory_tokens"]
    assert "minimum_assertions" not in result["golden_target"]
    assert all("minimum_paragraphs" not in row for row in result["sections"].values())
    assert result["evidence_tables"]["flows"] == [
        "P001 board", "P002 memory", "P003 storage", "P004 power", "P005 blade server",
    ]
    assert result["flow_ledger"] == [
        {"field": "P001 board", "direction": "in"},
        {"field": "P002 memory", "direction": "in"},
        {"field": "P003 storage", "direction": "in"},
        {"field": "P004 power", "direction": "in"},
        {"field": "P005 blade server", "direction": "out"},
    ]
    assert result["flow_directions"] == {
        "P001 board": "in", "P002 memory": "in", "P003 storage": "in",
        "P004 power": "in", "P005 blade server": "out",
    }
    assert result["evidence_tables"]["emissions"] == ["空气排放", "水体排放", "土壤排放"]
    assert result["evidence_tables"]["props"] == [
        "参考产品身份（blade server）", "参考产品完整型号与配置版本", "参考产品单件净质量",
        "参考产品交接状态", "参考产品规格或质量口径", "参考产品包装前边界",
    ]
    assert result["evidence_tables"]["params"][0] == "工艺路线与设备配置"


def test_a019_coproduct_is_advisory_flow_not_hard_identity(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    graph = json.loads((
        root / "vendor/lca_cornerstone/fixtures/wiki-phase2/docs/ict_equipment-name-graph.json"
    ).read_text(encoding="utf-8"))

    result = module().build(graph, "A019")
    materialized = tmp_path / "a019-57076012f975" / "content-blueprint.json"
    materialized.parent.mkdir()
    materialized.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    regenerated = json.loads(materialized.read_text(encoding="utf-8"))
    legacy = dict(regenerated)
    legacy["identity_tokens"] = [
        "A019", "服务器, 通用计算, 2U", "共生包装废料",
    ]

    assert regenerated["identity_tokens"] == ["A019", "服务器, 通用计算, 2U"]
    assert "共生包装废料" in regenerated["advisory_tokens"]
    assert {"field": "P034 共生包装废料", "direction": "out"} in regenerated[
        "flow_ledger"
    ]
    assert "P034 共生包装废料" in regenerated["evidence_tables"]["flows"]
    assert regenerated["evidence_tables"]["props"][0] == "参考产品身份（服务器, 通用计算, 2U）"
    assert hashlib.sha256(materialized.read_bytes()).digest() != hashlib.sha256(
        (json.dumps(legacy, ensure_ascii=False, indent=2) + "\n").encode()
    ).digest()


def test_activity_blueprint_requires_reference_output() -> None:
    graph = {
        "activities": [{
            "id": "A019", "name": "configuration", "inputs": ["server"],
            "outputs": [{"product": "packaging waste", "role": "coproduct"}],
        }],
        "products": [
            {"id": "P001", "name": "server"},
            {"id": "P034", "name": "packaging waste"},
        ],
    }

    with pytest.raises(ValueError, match="at least one reference output"):
        module().build(graph, "A019")


def test_activity_blueprint_preserves_same_product_in_both_directions() -> None:
    graph = {
        "activities": [{
            "id": "A019", "name": "server configuration",
            "inputs": ["reference server", "electricity"],
            "outputs": [{"product": "reference server", "role": "reference"}],
        }],
        "products": [
            {"id": "P001", "name": "reference server"},
            {"id": "P066", "name": "electricity"},
        ],
    }

    result = module().build(graph, "A019")

    assert result["flow_ledger"] == [
        {"field": "P001 reference server", "direction": "in"},
        {"field": "P066 electricity", "direction": "in"},
        {"field": "P001 reference server", "direction": "out"},
    ]
    assert result["evidence_tables"]["flows"] == [
        "P001 reference server", "P066 electricity", "P001 reference server",
    ]
    assert "flow_directions" not in result


def test_a019_fixture_has_five_unique_edge_identities() -> None:
    root = Path(__file__).resolve().parents[2]
    graph = __import__("json").loads((
        root / "vendor/lca_cornerstone/fixtures/wiki-phase2/docs/ict_equipment-name-graph.json"
    ).read_text(encoding="utf-8"))

    ledger = module().build(graph, "A019")["flow_ledger"]

    assert len(ledger) == 5
    assert len({(row["field"], row["direction"]) for row in ledger}) == 5
    assert [row["direction"] for row in ledger if row["field"].startswith("P001 ")] == [
        "in", "out",
    ]
