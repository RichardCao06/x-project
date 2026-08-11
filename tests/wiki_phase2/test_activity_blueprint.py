from __future__ import annotations

import importlib.util
from pathlib import Path


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
                              "outputs": [{"product": "blade server", "role": "reference"}]}]}
    result = module().build(graph, "A039")
    assert result["node_type"] == "activity" and len(result["sections"]) == 9
    assert set(result["evidence_tables"]) == {"flows", "emissions", "indicators", "params", "quality"}
    assert "A039" in result["required_tokens"] and "blade server" in result["required_tokens"]
