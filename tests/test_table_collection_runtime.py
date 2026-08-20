from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import sys

from lca_project.capability_runtime import _reusable_executed_table_matrix


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_p030_terminology_has_bilingual_synonyms_and_exclusions() -> None:
    hints = json.loads((ROOT / "skills/generate-node-wiki/source-hints/P030.json").read_text())
    terms = hints["terminology"]
    assert terms["canonical_zh"] == "共生焊料浮渣"
    assert "锡渣" in terms["synonyms_zh"]
    assert terms["canonical_en"] == "solder dross"
    assert "wave solder dross" in terms["synonyms_en"]
    assert "flux residue" in terms["excluded_terms"]


def test_search_matrix_distinguishes_planning_from_executed_search() -> None:
    source = (ROOT / "scripts/build_wiki_table_collection.py").read_text()
    executor = (ROOT / "scripts/execute_table_search_matrix.py").read_text()
    assert '"planned_not_executed"' in source
    assert '"status": "planned"' in source
    assert "reuse_executed_queries" in source
    assert '"coverage_status": "executed"' in executor


def test_table_search_executes_duplicate_route_query_once_and_fans_out_fields() -> None:
    executor = load_script("execute_table_search_matrix.py")
    shared = {
        "query_hash": "a" * 64, "query": "blade server BOM PDF", "language": "en",
        "document_route": "a039-bom", "document_type": "manufacturer_bom",
        "seed_candidates": [{"url": "https://example.com/bom.pdf"}],
        "status": "planned",
    }
    rows = [
        {**shared, "table": "flows", "field": "DIMM"},
        {**shared, "table": "props", "field": "net mass"},
        {**shared, "query_hash": "b" * 64, "query": "blade server energy PDF",
         "table": "indicators", "field": "assembly electricity"},
    ]

    unique, keys = executor.deduplicate_execution_rows(rows)
    assert len(unique) == 2
    results = [
        {**row, "status": "fetched", "results": [{"url": "https://example.com/evidence"}],
         "provider_attempts": [{"provider": "test", "status": "ok"}]}
        for row in unique
    ]
    expanded = executor.expand_execution_results(rows, keys, results)

    assert len(expanded) == 3
    assert [(row["table"], row["field"]) for row in expanded] == [
        ("flows", "DIMM"), ("props", "net mass"),
        ("indicators", "assembly electricity"),
    ]
    assert all(row["status"] == "fetched" for row in expanded)
    assert all("query_id" not in row for row in expanded)
    assert expanded[0]["results"] == expanded[1]["results"]


def test_zero_confirmed_sources_bootstraps_gap_only_product_collection(
    tmp_path: Path, monkeypatch,
) -> None:
    builder = load_script("build_wiki_table_collection.py")
    blueprint = tmp_path / "content-blueprint.json"
    blueprint.write_text(json.dumps({
        "node_id": "P999", "node_type": "product", "node_name": "测试产品",
        "evidence_tables": {
            "props": ["产品节点身份"],
            "params": ["参考质量"],
            "quality": ["地域代表性"],
        },
    }), encoding="utf-8")
    verified = tmp_path / "verify-output.json"
    verified.write_text('{"claims": []}\n', encoding="utf-8")
    hints = tmp_path / "source-hints.json"
    hints.write_text(json.dumps({"terminology": {
        "canonical_zh": "测试产品", "synonyms_zh": [],
        "canonical_en": "test product", "synonyms_en": [],
        "related_terms": [], "excluded_terms": [],
    }}), encoding="utf-8")
    output = tmp_path / "table-data"
    research_plan = tmp_path / "research-plan.json"
    research_plan.write_text(json.dumps({"field_translations": {
        "产品节点身份": "product identity", "参考质量": "reference mass",
        "地域代表性": "geographical representativeness",
    }}, ensure_ascii=False), encoding="utf-8")
    missing_routes = tmp_path / "no-document-routes.json"
    monkeypatch.setattr(sys, "argv", [
        "build_wiki_table_collection.py", str(blueprint), str(verified), str(output),
        "--source-hints", str(hints), "--research-plan", str(research_plan),
        "--document-routes", str(missing_routes),
    ])

    assert builder.main() == 0

    matrix = json.loads((output / "search-matrix.json").read_text(encoding="utf-8"))
    assert matrix["coverage_status"] == "planned_not_executed"
    assert {(row["table"], row["field"], row["language"]) for row in matrix["queries"]} == {
        ("props", "产品节点身份", "zh"), ("props", "产品节点身份", "en"),
        ("params", "参考质量", "zh"), ("params", "参考质量", "en"),
        ("quality", "地域代表性", "zh"), ("quality", "地域代表性", "en"),
    }
    collection = json.loads((output / "collection.json").read_text(encoding="utf-8"))
    assert collection["sources"] == []
    assert collection["thresholds"]["props_populated"] == 0
    assert collection["reference_configuration"]["manufacturer"].startswith("not established")
    assert all(
        row["status"] == "explicit_gap"
        for rows in collection["tables"].values() for row in rows
    )
    assert all(
        not row.get(key)
        for rows in collection["tables"].values() for row in rows
        for key in ("source", "int_source", "cn_source") if key in row
    )

    selector = load_script("select_wiki_table_evidence.py")
    executed = {
        **matrix, "coverage_status": "executed",
        "queries": [{**row, "status": "not_found", "results": []}
                    for row in matrix["queries"]],
    }
    selected, report = selector.select_evidence(collection, executed, tmp_path)
    assert report["outcome"] == "NO_ELIGIBLE_PUBLIC_DATA"
    assert report["counts"] == {
        "fields": 3, "populated": 0, "explicit_gaps": 3, "candidate_audits": 0,
    }
    assert all(
        row["gap_evidence"]["query_hashes"]
        for rows in selected["tables"].values() for row in rows
    )


def test_completed_table_search_matrix_can_be_reused_without_network(tmp_path: Path) -> None:
    plan = tmp_path / "search-matrix.json"
    plan.write_text('{"queries": [{"status": "planned"}]}\n', encoding="utf-8")
    manifest = tmp_path / "search-execution-manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    executed = tmp_path / "search-matrix.executed.json"
    executed.write_text(json.dumps({
        "protocol": "wiki-table-search-executed-v2",
        "coverage_status": "executed",
        "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "execution_manifest": str(manifest),
        "queries": [{"status": "fetched"}],
    }), encoding="utf-8")

    assert _reusable_executed_table_matrix(plan, executed)
    document = json.loads(executed.read_text(encoding="utf-8"))
    document["coverage_status"] = "planned_not_executed"
    executed.write_text(json.dumps(document), encoding="utf-8")
    assert not _reusable_executed_table_matrix(plan, executed)


def test_activity_collection_uses_the_six_activity_table_kinds() -> None:
    source = (ROOT / "scripts/build_wiki_table_collection.py").read_text()
    assert 'if blueprint["node_type"] == "activity"' in source
    assert 'tables = {"flows": flows, "props": activity_props, "params": params' in source
    assert '"props_populated": 0' in source
    assert '"flows_populated": 0, "props_populated": 0, "emissions_populated": 0' in source
    assert 'else "soil" if "土壤" in field else "waste"' in source


def test_activity_flow_directions_come_from_the_graph_bound_blueprint() -> None:
    builder = load_script("build_wiki_table_collection.py")
    legacy_blueprint = {"identity_tokens": ["A040", "笔记本电脑"]}
    assert builder.activity_flow_direction(
        legacy_blueprint, "P018 主板PCBA, 通用服务器用"
    ) == "in"
    assert builder.activity_flow_direction(legacy_blueprint, "P006 笔记本电脑") == "out"
    assert builder.activity_flow_direction(
        {"flow_directions": {"P031 共生报废PCBA": "out"}}, "P031 共生报废PCBA"
    ) == "out"


def test_table_verifier_requires_a_hash_bound_evidence_selection() -> None:
    verifier = (ROOT / "scripts/verify_wiki_table_collection.py").read_text()
    assert 'matrix_path = args.output / "search-matrix.executed.json"' in verifier
    assert 'requires search-matrix.executed.json' in verifier
    assert '"evidence_selection_present"' in verifier
    assert '"evidence_selection_hash_bound"' in verifier
    assert '"populated_rows_are_selected"' in verifier
    assert '"selection_decisions_match_collection"' in verifier


def test_table_search_uses_audited_translation_when_english_terms_are_missing() -> None:
    builder = (ROOT / "scripts/build_wiki_table_collection.py").read_text()
    verifier = (ROOT / "scripts/verify_wiki_table_collection.py").read_text()
    assert 'translate_zh_search_terms' in builder
    assert 'terminology["translated_search_terms_en"]' in builder
    assert 'terminology["query_translation"]' in builder
    assert 'translated_search_terms_en' in verifier


def test_table_search_adds_document_type_routes_and_frozen_seed_candidates() -> None:
    routes = json.loads((ROOT / "config/wiki-table-document-routes.json").read_text())
    document_types = {row["document_type"] for row in routes["routes"]}
    assert {"environmental_impact_assessment", "process_lca", "product_carbon_footprint"} <= document_types
    assert all(row.get("seed_candidates") for row in routes["routes"])
    builder = (ROOT / "scripts/build_wiki_table_collection.py").read_text()
    executor = (ROOT / "scripts/execute_table_search_matrix.py").read_text()
    assert 'query_strategy="document_type_route"' in builder
    assert '"curated_document_route"' in executor
    assert "DOCUMENT_ROUTE_EXCERPT_CHARS=250_000" in executor


def test_a039_queries_have_complete_english_fields_no_internal_ids_and_bounded_routes(
    tmp_path: Path, monkeypatch,
) -> None:
    blueprint_builder = load_script("build_wiki_content_blueprint.py")
    plan_builder = load_script("build_wiki_research_plan.py")
    table_builder = load_script("build_wiki_table_collection.py")
    graph = json.loads((
        ROOT / "vendor/lca_cornerstone/fixtures/wiki-phase2/docs/ict_equipment-name-graph.json"
    ).read_text(encoding="utf-8"))
    blueprint_value = blueprint_builder.build(graph, "A039")
    blueprint = tmp_path / "content-blueprint.json"
    blueprint.write_text(json.dumps(blueprint_value, ensure_ascii=False), encoding="utf-8")
    verified = tmp_path / "verify-output.json"
    verified.write_text('{"claims": []}\n', encoding="utf-8")
    translations, contract = plan_builder.field_translation_contract("A039")
    research_plan = tmp_path / "research-plan.json"
    research_plan.write_text(json.dumps({
        "terminology": {"canonical_zh": blueprint_value["node_name"],
                        "candidate_aliases_zh": [], "canonical_en": "blade server final assembly",
                        "candidate_aliases_en": []},
        "field_translations": translations, "field_translation_contract": contract,
    }, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "table-data"
    monkeypatch.setattr(sys, "argv", [
        "build_wiki_table_collection.py", str(blueprint), str(verified), str(output),
        "--research-plan", str(research_plan),
    ])

    assert table_builder.main() == 0
    matrix = json.loads((output / "search-matrix.json").read_text(encoding="utf-8"))
    assert matrix["query_quality_metrics"] == {
        "english_field_translation_coverage": "33/33",
        "mixed_language_english_queries": 0,
        "internal_identifier_queries": 0,
        "a039_document_routes": 4,
    }
    assert all(not __import__("re").search(r"(?<![A-Za-z0-9])[AP]\d{3}(?!\d)", row["query"])
               for row in matrix["queries"])
    assert all(not __import__("re").search(r"[\u3400-\u9fff]", row["query"])
               for row in matrix["queries"] if row["language"] == "en")

    routed = {(row["table"], row["field"]) for row in matrix["queries"]
              if row.get("document_route", "").startswith("a039-")}
    expected = {(table, field) for table, fields in blueprint_value["evidence_tables"].items()
                for field in fields}
    assert routed == expected


def test_query_serializer_fails_closed_on_mixed_language_english_query() -> None:
    builder = load_script("build_wiki_table_collection.py")
    try:
        builder.append_query([], table="params", field="装配批次产量", language="en",
                             query="blade server 装配批次产量")
    except ValueError as exc:
        assert "retains CJK" in str(exc)
    else:
        raise AssertionError("mixed-language English query was accepted")


def test_table_search_gate_rejects_planned_and_errors(tmp_path: Path) -> None:
    gate = load_script("gate_table_search_execution.py")
    # Test the CLI behavior through its deterministic source contract.
    source = (ROOT / "scripts/gate_table_search_execution.py").read_text()
    assert 's!="planned"' in source
    assert '"error","budget_skipped"' in source
    assert 'len(success)==len(rows)' in source


def test_source_hints_are_advisory_not_exclusive() -> None:
    launcher = (ROOT / "vendor/lca_cornerstone/scripts/run_wiki_nomination_capture.py").read_text()
    assert "advisory candidate" in launcher
    assert "不得改投其他来源" not in launcher


def test_v7_freezes_research_and_execution_gates_before_verify() -> None:
    workflow = json.loads((ROOT / "workflows/wiki-node-production@7.json").read_text())
    by_id = {row["id"]: row for row in workflow["steps"]}
    assert by_id["research_ready"]["needs"] == ["research_plan"]
    assert by_id["verify"]["needs"] == ["search_execution_gate"]
    assert by_id["source_diversity_gate"]["needs"] == ["terminology_verify"]
    assert by_id["table_verify"]["needs"] == ["table_search_execution_gate"]


def test_v8_preserves_gates_for_persistent_incremental_runtime() -> None:
    workflow = json.loads((ROOT / "workflows/wiki-node-production@8.json").read_text())
    by_id = {row["id"]: row for row in workflow["steps"]}
    assert workflow["version"] == "8"
    assert by_id["verify"]["needs"] == ["search_execution_gate"]
    assert by_id["table_verify"]["needs"] == ["table_search_execution_gate"]


def test_v9_adds_goal_aligned_plan_content_and_maturity_gates() -> None:
    workflow = json.loads((ROOT / "workflows/wiki-node-production@9.json").read_text())
    by_id = {row["id"]: row for row in workflow["steps"]}
    assert workflow["version"] == "9"
    assert by_id["research_plan_gate"]["needs"] == ["research_plan"]
    assert by_id["research_ready"]["needs"] == ["research_plan_gate"]
    assert by_id["content_closure_gate"]["needs"] == ["content_compose"]
    assert by_id["editorial_review"]["needs"] == ["content_closure_gate"]
    assert by_id["table_collect"]["needs"] == ["content_blueprint"]
    assert by_id["maturity_gate"]["needs"] == ["table_apply"]
    assert by_id["preview"]["needs"] == ["maturity_gate"]
