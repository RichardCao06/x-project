from __future__ import annotations

import copy
import hashlib
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def module():
    path = ROOT / "scripts/select_wiki_table_evidence.py"
    spec = importlib.util.spec_from_file_location("select_wiki_table_evidence", path)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


def collection() -> dict:
    fallback = "verified-fallback"
    return {
        "protocol": {"version": "wiki-table-evidence-v1", "kind": "node-table-collection"},
        "node_id": "A001", "node_type": "activity", "sources": [{"id": fallback}],
        "tables": {"indicators": [{
            "field": "一次装配良率", "medium": "manufacturing", "unit": "待来源定义",
            "basis": "reference", "int_value": "缺口：无国际值", "int_source": fallback,
            "cn_value": "缺口：无中国值", "cn_source": fallback,
            "mapping_status": "explicit_gap", "pedigree": "explicit_gap", "status": "explicit_gap",
        }]},
    }


def result(tmp_path: Path, *, url: str, source_class: str, excerpt: str) -> dict:
    payload = tmp_path / (hashlib.sha256(url.encode()).hexdigest() + ".payload")
    payload.write_text(excerpt, encoding="utf-8")
    return {
        "url": url, "title": "Process report", "source_class": source_class,
        "fetch_status": "fetched", "content_sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
        "payload_path": str(payload), "excerpt": excerpt,
    }


def matrix(rows: list[dict]) -> dict:
    return {"coverage_status": "executed", "queries": [{
        "table": "indicators", "field": "一次装配良率", "language": "en",
        "query_hash": "a" * 64, "results": rows,
    }]}


def fanout_matrix(candidate: dict) -> dict:
    shared = {
        "language": "en", "query_hash": "f" * 64,
        "results": [candidate],
    }
    return {"coverage_status": "executed", "queries": [
        {**shared, "table": "indicators", "field": "一次装配良率"},
        {**shared, "table": "props", "field": "参考产品单件净质量"},
    ]}


def fanout_collection() -> dict:
    data = collection()
    data["tables"]["props"] = [{
        "field": "参考产品单件净质量", "condition": "named configuration",
        "unit": "待来源定义", "value": "缺口：无型号级净质量", "source": "verified-fallback",
        "pedigree": "explicit_gap", "status": "explicit_gap",
    }]
    return data


def verifier_module():
    path = ROOT / "scripts/verify_wiki_table_collection.py"
    spec = importlib.util.spec_from_file_location("verify_wiki_table_collection", path)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


def test_authoritative_field_observation_is_promoted_as_proxy(tmp_path: Path) -> None:
    mod = module()
    candidate = result(tmp_path, url="https://example.gov/report", source_class="government_or_regulator",
                       excerpt="The audited first-pass yield was 98.5% for the server-board assembly line.")
    updated, report = mod.select_evidence(collection(), matrix([candidate]), tmp_path)
    row = updated["tables"]["indicators"][0]
    assert row["status"] == "populated"
    assert row["int_value"] == "〔代理值〕98.5"
    assert row["unit"] == "%" and row["basis"] == "proxy"
    assert report["accepted_evidence"][0]["verification_mode"] == "authoritative_single_source"
    assert report["outcome"] == "FULLY_POPULATED"
    assert report["candidate_audits"][0]["decision"] == "accepted"
    assert report["candidate_audits"][0]["reasons"] == [
        "accepted_field_specific_observation"
    ]
    assert report["audit_closure"]["complete"] is True
    assert report["proof_metrics"]["candidate_audit_closure_complete"] is True


def test_single_non_authoritative_candidate_remains_an_audited_gap(tmp_path: Path) -> None:
    mod = module()
    candidate = result(tmp_path, url="https://vendor.example/report",
                       source_class="manufacturer_or_other_technical",
                       excerpt="First-pass yield is 98.5% for this assembly service.")
    updated, report = mod.select_evidence(collection(), matrix([candidate]), tmp_path)
    assert updated["tables"]["indicators"][0]["status"] == "explicit_gap"
    assert report["fields"][0]["decision"] == "explicit_gap"
    assert report["fields"][0]["candidate_count"] == 1
    assert report["outcome"] == "NO_ELIGIBLE_PUBLIC_DATA"
    assert report["reason_counts"] == {"uncorroborated_public_proxy": 1}
    assert report["candidate_audits"][0]["decision"] == "rejected"
    assert report["candidate_audits"][0]["reasons"] == [
        "uncorroborated_public_proxy"
    ]
    row = updated["tables"]["indicators"][0]
    assert row["int_source"] == "" and row["cn_source"] == ""
    assert row["gap_evidence"]["protocol"] == "wiki-table-gap-evidence-v1"
    assert row["gap_evidence"]["query_hashes"] == ["a" * 64]
    assert row["gap_evidence"]["rejected_candidate_urls"] == [
        "https://vendor.example/report"
    ]
    assert set(row["gap_evidence_by_track"]) == {"int", "cn"}


def test_shared_result_across_logical_targets_has_complete_audit_closure(tmp_path: Path) -> None:
    selector = module()
    candidate = {
        "url": "https://example.com/shared-evidence.pdf", "title": "Shared report",
        "fetch_status": "error", "provider": "fixture",
    }
    search_matrix = fanout_matrix(candidate)

    _, report = selector.select_evidence(fanout_collection(), search_matrix, tmp_path)

    assert report["audit_closure"] == {
        "matrix_results": 2, "candidate_audits": 2,
        "every_result_audited_once": True,
        "all_decisions_terminal": True,
        "all_decisions_have_reasons": True,
        "complete": True,
    }
    expected_identities = {
        ("indicators", "一次装配良率", "", "en", "f" * 64,
         "https://example.com/shared-evidence.pdf"),
        ("props", "参考产品单件净质量", "", "en", "f" * 64,
         "https://example.com/shared-evidence.pdf"),
    }
    assert {
        selector.logical_result_identity(audit)
        for audit in report["candidate_audits"]
    } == expected_identities
    verifier = verifier_module()
    assert {
        verifier.logical_result_identity(audit)
        for audit in report["candidate_audits"]
    } == expected_identities
    assert verifier.every_search_result_has_one_candidate_audit(
        search_matrix["queries"], report["candidate_audits"],
    )


def test_duplicate_audit_for_full_logical_identity_fails_closure(tmp_path: Path) -> None:
    selector = module()
    candidate = {
        "url": "https://example.com/shared-evidence.pdf", "title": "Shared report",
        "fetch_status": "error", "provider": "fixture",
    }
    search_matrix = fanout_matrix(candidate)
    _, report = selector.select_evidence(fanout_collection(), search_matrix, tmp_path)
    audits = [*report["candidate_audits"], copy.deepcopy(report["candidate_audits"][0])]

    assert selector.candidate_audit_closure(search_matrix, audits)[
        "every_result_audited_once"
    ] is False
    assert verifier_module().every_search_result_has_one_candidate_audit(
        search_matrix["queries"], audits,
    ) is False


def test_missing_audit_for_full_logical_identity_fails_closure(tmp_path: Path) -> None:
    selector = module()
    candidate = {
        "url": "https://example.com/shared-evidence.pdf", "title": "Shared report",
        "fetch_status": "error", "provider": "fixture",
    }
    search_matrix = fanout_matrix(candidate)
    _, report = selector.select_evidence(fanout_collection(), search_matrix, tmp_path)
    audits = report["candidate_audits"][:-1]

    assert selector.candidate_audit_closure(search_matrix, audits)[
        "every_result_audited_once"
    ] is False
    assert verifier_module().every_search_result_has_one_candidate_audit(
        search_matrix["queries"], audits,
    ) is False


def test_flow_evidence_and_gap_decisions_are_direction_scoped(tmp_path: Path) -> None:
    selector = module()
    candidate = {
        "url": "https://example.com/shared-flow-report.pdf", "title": "Shared report",
        "fetch_status": "error", "provider": "fixture",
    }
    shared = {
        "table": "flows", "field": "P001 reference server", "language": "en",
        "query_hash": "d" * 64, "results": [candidate],
    }
    search_matrix = {"coverage_status": "executed", "queries": [
        {**shared, "direction": "in"}, {**shared, "direction": "out"},
    ]}
    gap = {
        "unit": "待来源定义", "basis": "reference",
        "int_value": "缺口：无国际值", "int_source": "verified-fallback",
        "cn_value": "缺口：无中国值", "cn_source": "verified-fallback",
        "pedigree": "explicit_gap", "status": "explicit_gap",
    }
    data = {
        "protocol": {"version": "wiki-table-evidence-v1", "kind": "node-table-collection"},
        "node_id": "A019", "node_type": "activity", "sources": [{"id": "verified-fallback"}],
        "tables": {"flows": [
            {"field": "P001 reference server", "direction": "in", **gap},
            {"field": "P001 reference server", "direction": "out", **gap},
        ]},
    }

    updated, report = selector.select_evidence(data, search_matrix, tmp_path)

    assert [(row["field"], row["direction"], row["decision"])
            for row in report["fields"]] == [
        ("P001 reference server", "in", "explicit_gap"),
        ("P001 reference server", "out", "explicit_gap"),
    ]
    assert {row["direction"] for row in report["candidate_audits"]} == {"in", "out"}
    assert all(row["gap_evidence"]["query_hashes"] == ["d" * 64]
               for row in updated["tables"]["flows"])
    assert report["proof_metrics"]["gap_provenance_preserved"] is True
    assert report["proof_metrics"]["candidate_audit_closure_complete"] is True
    expected = {
        ("flows", "P001 reference server", "in", "en", "d" * 64,
         "https://example.com/shared-flow-report.pdf"),
        ("flows", "P001 reference server", "out", "en", "d" * 64,
         "https://example.com/shared-flow-report.pdf"),
    }
    assert {selector.logical_result_identity(row)
            for row in report["candidate_audits"]} == expected
    verifier = verifier_module()
    assert {verifier.logical_result_identity(row)
            for row in report["candidate_audits"]} == expected
    assert verifier.every_search_result_has_one_candidate_audit(
        search_matrix["queries"], report["candidate_audits"],
    )


def test_two_independent_matching_sources_are_promoted(tmp_path: Path) -> None:
    mod = module()
    candidates = [
        result(tmp_path, url="https://vendor-a.example/report",
               source_class="manufacturer_or_other_technical", excerpt="First-pass yield: 98.5%."),
        result(tmp_path, url="https://vendor-b.example/report",
               source_class="manufacturer_or_other_technical", excerpt="First-pass yield reached 98.4%."),
    ]
    updated, report = mod.select_evidence(collection(), matrix(candidates), tmp_path)
    assert updated["tables"]["indicators"][0]["status"] == "populated"
    evidence = report["accepted_evidence"][0]
    assert evidence["verification_mode"] == "independent_two_source_corroboration"
    assert len(evidence["supporting_source_ids"]) == 2


def test_two_independent_handoff_descriptions_are_promoted_as_categorical_proxy(
    tmp_path: Path,
) -> None:
    mod = module()
    fallback = "verified-fallback"
    data = {
        "protocol": {"version": "wiki-table-evidence-v1", "kind": "node-table-collection"},
        "node_id": "A019", "node_type": "activity", "sources": [{"id": fallback}],
        "tables": {"props": [{
            "field": "参考产品交接状态", "condition": "A019 reference-product handoff",
            "unit": "待来源定义", "value": "缺口：无交接状态", "source": fallback,
            "pedigree": "explicit_gap", "status": "explicit_gap",
        }]},
    }
    candidates = [
        result(
            tmp_path, url="https://vendor-a.example/bios",
            source_class="manufacturer_or_other_technical",
            excerpt="完成设置后按 F10 保存更改并退出，系统随后重新启动。",
        ),
        result(
            tmp_path, url="https://vendor-b.example/uefi",
            source_class="manufacturer_or_other_technical",
            excerpt="选择保存并退出，所做的 UEFI 设置在服务器重启时应用。",
        ),
    ]
    search_matrix = {"coverage_status": "executed", "queries": [{
        "table": "props", "field": "参考产品交接状态", "language": "zh",
        "query_hash": "b" * 64, "results": candidates,
    }]}

    updated, report = mod.select_evidence(data, search_matrix, tmp_path)

    row = updated["tables"]["props"][0]
    assert row["status"] == "populated"
    assert row["value"].startswith("〔代理状态〕BIOS 配置已持久保存")
    assert row["unit"] == "状态"
    evidence = report["accepted_evidence"][0]
    assert evidence["verification_mode"] == "independent_two_source_corroboration"
    assert evidence["proxy_role"] == "handoff_state_reference"
    assert len(evidence["supporting_source_ids"]) == 2
    assert report["proof_metrics"]["accepted_observations"] == 1
    assert report["proof_metrics"]["populated_fields"] == 1


def test_table_collect_runtime_runs_selection_after_search() -> None:
    source = (ROOT / "src/lca_project/capability_runtime.py").read_text(encoding="utf-8")
    assert "select_wiki_table_evidence.py" in source
    assert "evidence-selection.json" in source


def test_cn_eia_values_are_normalized_with_the_reported_annual_output() -> None:
    mod = module()
    text = """
    年产通信设备电路板 20 万片。实行一班工作制，每班工作 8 小时，全年工作 300 天。
    表 2.1：PCB 板 20.01 万个；无铅锡膏 200kg；电 60 万 kw.h。
    焊接烟尘（锡及其化合 物）产生量为0.00196t/a。
    废电路板在生产过程中产生，产生量约为0.05t/a。
    """
    cases = {
        ("flows", "P043 HDI PCB裸板, 服务器/交换机用"): ("1.0005", "个/片"),
        ("flows", "P062 无铅焊料锡膏, SAC305"): ("0.001", "kg/片"),
        ("flows", "P066 中压电力, ICT制造用"): ("3", "kWh/片"),
        ("flows", "P031 共生报废PCBA"): ("0.00025", "kg/片"),
        ("emissions", "空气排放"): ("9.8e-06", "kg/片"),
        ("params", "有效运行时间"): ("2400", "h/a"),
        ("params", "生产负荷与良率"): ("99.95002499", "%"),
    }
    for (table, field), expected in cases.items():
        rows = mod.extract_document_observations(
            table, field, text, "environmental_impact_assessment"
        )
        assert (rows[0]["value"], rows[0]["unit"]) == expected
        if field != "有效运行时间":
            assert "200000" in rows[0]["derivation"]
    electricity = mod.extract_document_observations(
        "flows", "P066 中压电力, ICT制造用", text,
        "environmental_impact_assessment",
    )[0]
    assert electricity["label"] == "代理值"
    assert electricity["proxy_role"] == "upper_bound"
    assert "whole-project" in electricity["source_scope"]
    air = mod.extract_document_observations(
        "emissions", "空气排放", text, "environmental_impact_assessment",
    )[0]
    assert air["proxy_role"] == "upper_bound"
    assert "wave-solder" in air["source_scope"]


def test_product_pcf_without_exact_unit_process_alignment_is_audited_not_promoted(tmp_path: Path) -> None:
    mod = module()
    data = collection()
    data["tables"] = {"props": [{
        "field": "参考产品完整型号与配置版本", "condition": "交接点", "unit": "—",
        "value": "缺口：无精确型号", "source": "verified-fallback",
        "pedigree": "explicit_gap", "status": "explicit_gap",
    }]}
    candidate = result(
        tmp_path, url="https://vendor.example/server-pcf",
        source_class="manufacturer_or_other_technical",
        excerpt="H3C UniServer B5700 G6 whole-server product carbon footprint.",
    )
    candidate["document_type"] = "product_carbon_footprint"
    pcf_matrix = {"coverage_status": "executed", "queries": [{
        "table": "props", "field": "参考产品完整型号与配置版本", "language": "en",
        "query_hash": "b" * 64, "document_type": "product_carbon_footprint",
        "results": [candidate],
    }]}
    updated, report = mod.select_evidence(data, pcf_matrix, tmp_path)
    assert updated["tables"]["props"][0]["status"] == "explicit_gap"
    assert "pcf_not_decomposable" in report["candidate_audits"][0]["reasons"][0]


def test_process_lca_smt_line_keeps_its_reference_fu_boundary() -> None:
    mod = module()
    text = """
    Energy consumption per FU [kWh]
    Board stacker & cleaner 0.0077 Paste Printer 0.0108 Paste Inspection 0.0015
    SMD Placement 0.0379 Reflow Oven 0.0613 Solder Inspection 0.0015
    Electrical ICT Tester 0.0108 Handling processes 0.0383
    """
    rows = mod.extract_document_observations(
        "indicators", "单位产品装配电耗", text, "process_lca"
    )
    assert rows[0]["value"] == "0.1698"
    assert rows[0]["unit"] == "kWh/reference FU"
    assert "not a server-board measurement" in rows[0]["source_scope"]


def test_a039_html_pdf_routes_extract_field_observations() -> None:
    mod = module()
    cases = [
        ("props", "参考产品单件净质量", "Product net weight: 8.8 kg.",
         "manufacturer_specification_bom", ("8.8", "kg")),
        ("indicators", "一次装配良率", "First-pass assembly yield: 98.5%.",
         "system_integration_manufacturing_record", ("98.5", "%")),
        ("flows", "P066 中压电力, ICT制造用",
         "Annual blade server assembly output 10000 units. Annual electricity consumption 17 MWh.",
         "environmental_report", ("1.7", "kWh/unit")),
        ("indicators", "单位产品装配电耗",
         "Final system assembly electricity consumption 1.7 kWh per assembled unit.",
         "process_lca", ("1.7", "kWh/unit")),
    ]
    for table, field, text, document_type, expected in cases:
        rows = mod.extract_document_observations(table, field, text, document_type)
        assert (rows[0]["value"], rows[0]["unit"]) == expected
        assert rows[0]["source_scope"]


def test_a039_authoritative_spec_observation_is_accepted_and_instrumented(tmp_path: Path) -> None:
    mod = module()
    data = collection()
    data["node_id"] = "A039"
    data["tables"] = {"props": [{
        "field": "参考产品单件净质量", "condition": "named configuration", "unit": "待来源定义",
        "value": "缺口：无型号级净质量", "source": "verified-fallback",
        "pedigree": "explicit_gap", "status": "explicit_gap",
    }]}
    candidate = result(
        tmp_path, url="https://example.gov/blade-server-spec.pdf",
        source_class="standard_or_industry_body", excerpt="Product net weight: 8.8 kg.",
    )
    candidate["document_type"] = "manufacturer_specification_bom"
    spec_matrix = {"coverage_status": "executed", "queries": [{
        "table": "props", "field": "参考产品单件净质量", "language": "en",
        "query_hash": "c" * 64, "document_route": "a039-manufacturer-specifications-and-bom",
        "document_type": "manufacturer_specification_bom", "results": [candidate],
    }]}

    updated, report = mod.select_evidence(data, spec_matrix, tmp_path)
    row = updated["tables"]["props"][0]
    assert row["status"] == "populated" and row["value"] == "〔定义值〕8.8"
    assert len(updated["sources"]) == 2
    assert report["proof_metrics"]["field_observations"] == 1
    assert report["proof_metrics"]["accepted_observations"] == 1
    assert report["proof_metrics"]["populated_fields"] == 1


def test_missing_parser_is_not_misclassified_as_internal_only(tmp_path: Path) -> None:
    mod = module()
    data = collection()
    data["node_id"] = "A039"
    data["tables"] = {"quality": [{
        "field": "BOM质量闭合", "condition": "named configuration", "unit": "—",
        "value": "缺口：未取得闭合声明", "source": "verified-fallback",
        "pedigree": "explicit_gap", "status": "explicit_gap",
    }]}
    empty_matrix = {"coverage_status": "executed", "queries": [{
        "table": "quality", "field": "BOM质量闭合", "language": "en",
        "query_hash": "d" * 64, "status": "not_found", "results": [],
    }]}

    _, report = mod.select_evidence(data, empty_matrix, tmp_path)
    field = report["fields"][0]
    assert field["public_extractability"] == "public_or_internal"
    assert field["extraction_support"] == "not_implemented"
    assert field["reason"] == "public_extraction_rule_missing"
    assert report["proof_metrics"]["unsupported_fields_misclassified_as_internal_only"] == 0
    assert report["proof_metrics"]["gap_provenance_preserved"] is True
