from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from lca_project.domains.graph import GraphAdapter


SOURCE_GRAPH = Path("/Users/shujudagongren/Myspace/lca-cornerstone/docs/steel-name-graph.json")


@pytest.mark.skipif(not SOURCE_GRAPH.is_file(), reason="detached CI has no migration source graph")
def test_grf_001_real_steel_graph_runs_all_11_gates(tmp_path: Path) -> None:
    result = GraphAdapter().validate(SOURCE_GRAPH, workspace=tmp_path)
    assert result.returncode == 0
    assert "11/11" in result.stdout


@pytest.mark.skipif(not SOURCE_GRAPH.is_file(), reason="detached CI has no migration source graph")
@pytest.mark.parametrize(("mutation", "finding"), [("orphan", "不变量A"), ("reference", "不变量B"), ("input", "不变量C")])
def test_grf_002_real_gate_kills_a_b_c_mutations(tmp_path: Path, mutation: str, finding: str) -> None:
    graph = json.loads(SOURCE_GRAPH.read_text(encoding="utf-8"))
    if mutation == "orphan":
        graph["edges"] = [edge for edge in graph["edges"] if edge.get("to") != "P001"]
    elif mutation == "reference":
        graph["edges"] = [edge for edge in graph["edges"] if not (
            edge.get("from") == "A001" and edge.get("type") == "PRODUCES" and edge.get("role") == "reference")]
    else:
        graph["edges"].append({"from": "A001", "to": "A002", "type": "CONSUMES"})
    candidate = tmp_path / f"{mutation}.json"
    candidate.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    result = GraphAdapter().validate(candidate, workspace=tmp_path)
    assert result.returncode != 0
    assert finding in result.stdout


@pytest.mark.skipif(not SOURCE_GRAPH.is_file(), reason="detached CI has no migration source graph")
def test_grf_003_real_gate_kills_identity_collision(tmp_path: Path) -> None:
    graph = json.loads(SOURCE_GRAPH.read_text(encoding="utf-8"))
    duplicate = copy.deepcopy(graph["products"][0])
    duplicate["name"] = "collision mutant"
    graph["products"].append(duplicate)
    candidate = tmp_path / "identity-collision.json"
    candidate.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    result = GraphAdapter().validate(candidate, workspace=tmp_path)
    assert result.returncode != 0
    assert "ID 唯一" in result.stdout
