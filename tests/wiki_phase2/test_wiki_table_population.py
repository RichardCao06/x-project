"""Evidence-table backfills are source-bound, gated and hash locked."""
from __future__ import annotations

import copy

from wiki_lint import is_null_value
from wiki_table_population import (build_candidate, is_gap, population_floor_checks,
                                   render_table, substantive_population)
from wiki_table_population import validate_collection


def _collection() -> dict:
    source = "manufacturer-spec"
    return {
        "node_id": "P003",
        "sources": [{"id": source}],
        "tables": {
            "props": [{
                "field": "产品质量", "condition": "参考配置", "unit": "kg/台",
                "value": "3.0", "source": source, "pedigree": "manufacturer_assumption",
            }],
            "params": [{
                "field": "系统内存", "geo": "EU reference", "unit": "GB/台",
                "basis": "manufacturer_reference_configuration", "int_value": "16",
                "int_source": source, "cn_value": "缺口：中国 SKU 未冻结",
                "cn_source": source, "pedigree": "manufacturer_reference_configuration",
            }],
            "quality": [{
                "field": "地域代表性", "unit": "region", "basis": "source_scope",
                "cn_value": "EU 组装与使用，不代表中国", "cn_source": source,
                "proxy_policy": "中国研究必须重新采集", "pedigree": "assessed_from_source_scope",
            }],
        },
    }


def _page() -> str:
    blocks = []
    for kind in ("props", "params", "quality"):
        blocks.append(f"<!-- EV:{kind}:START -->\nold\n<!-- EV:{kind}:END -->")
    return """---
id: P003
quantity_status: not_populated
dataset_readiness: blocked_pending_node_specific_lci
provenance_refs: [internal-review]
---
""" + "\n".join(blocks) + """

## 🔒 数量（待挂 · NOT POPULATED）

旧的空壳。

## 产品性质与交付状态

<!-- CHANGELOG:START -->
## 修改日志
"""


def test_explicit_gap_is_not_counted_as_a_value() -> None:
    for value in ("缺口：中国 SKU 未冻结", "缺口: BOM 未公开", "未公开供应商"):
        assert is_gap(value)
        assert is_null_value(value)
    assert not is_gap("3.0")
    assert not is_null_value("3.0")


def test_zero_population_is_honest_incomplete_even_when_floors_are_zero() -> None:
    metrics = {
        "props_populated": 0, "flows_populated": 0,
        "emissions_populated": 0, "indicators_populated": 0,
        "params_int_populated": 0, "params_cn_populated": 0,
        "quality_assessed": 3,
    }

    readiness = substantive_population(metrics)

    assert readiness == {
        "populated_fields": 0, "quality_assessments": 3,
        "goal_data_ready": False,
    }


def test_new_contract_requires_search_provenance_for_explicit_gaps(tmp_path) -> None:
    collection = _collection()
    collection["protocol"] = {
        "version": "wiki-table-evidence-v1", "kind": "node-table-collection",
    }
    collection["node_type"] = "product"
    collection["reference_configuration"] = {
        "manufacturer": "test", "model": "test", "scope": "test",
        "freeze_rule": "test",
    }
    collection["sources"][0].update({
        "title": "Manufacturer specification", "type": "manufacturer_spec",
        "version": "2026-01", "locator": "p.1", "authority": "manufacturer",
        "sha256": "", "excerpt_seeds": ["3.0 kg"], "region": "INT",
        "verified_via": "official_url", "url": "https://example.test/spec.pdf",
        "status": "verified",
    })
    collection["gap_provenance_required"] = True
    collection["tables"]["props"][0]["status"] = "populated"
    collection["tables"]["quality"][0]["status"] = "assessed"
    collection["tables"]["params"][0]["status"] = "explicit_gap"
    collection["tables"]["params"][0]["int_value"] = "缺口：无国际值"
    collection["tables"]["params"][0]["int_source"] = ""
    collection["tables"]["params"][0]["cn_source"] = ""
    try:
        validate_collection(collection, tmp_path)
    except ValueError as exc:
        assert "显式缺口缺少检索证据" in str(exc)
    else:
        raise AssertionError("explicit gap without frozen search provenance must fail")


def test_candidate_replaces_tables_and_retires_quantity_shell() -> None:
    collection = _collection()
    registry = {"sources": {}}
    # build_candidate expects complete registry source metadata, so freeze one
    # minimal, deterministic entry through a source-bearing copy.
    collection = copy.deepcopy(collection)
    collection["sources"][0].update({
        "title": "Manufacturer specification", "type": "manufacturer_spec",
        "version": "2026-01", "locator": "p.1", "authority": "manufacturer",
        "sha256": "", "excerpt_seeds": ["3.0 kg"], "region": "INT",
        "verified_via": "official_url", "url": "https://example.test/spec.pdf",
    })
    candidate, staged_registry = build_candidate(collection, _page(), registry)
    assert "| 产品质量 | 参考配置 | kg/台 | 3.0 | manufacturer-spec |" in candidate
    assert "缺口：中国 SKU 未冻结" in candidate
    assert "NOT POPULATED" not in candidate
    assert "quantity_status: partial" in candidate
    assert "dataset_readiness: reference_configuration_only" in candidate
    assert "internal-review" not in candidate
    assert staged_registry["sources"]["manufacturer-spec"]["status"] == "verified"


def test_v2_candidate_normalizes_honest_limited_status_vocabulary() -> None:
    collection = copy.deepcopy(_collection())
    collection["sources"][0].update({
        "title": "Manufacturer specification", "type": "manufacturer_spec",
        "version": "2026-01", "locator": "p.1", "authority": "manufacturer",
        "sha256": "", "excerpt_seeds": ["3.0 kg"], "region": "INT",
        "verified_via": "official_url", "url": "https://example.test/spec.pdf",
    })
    page = _page().replace(
        "id: P003\n",
        "id: P003\nschema_version: wiki-v2\n"
        "provenance_status: evidence_insufficient\n"
        "claim_verification_status: not_verified\n",
    )

    candidate, _ = build_candidate(collection, page, {"sources": {}})

    assert "provenance_status: source_verified" in candidate
    assert "claim_verification_status: partial" in candidate


def test_activity_candidate_retires_quantity_shell_before_flow_section() -> None:
    collection = copy.deepcopy(_collection())
    collection["sources"][0].update({
        "title": "Manufacturer specification", "type": "manufacturer_spec",
        "version": "2026-01", "locator": "p.1", "authority": "manufacturer",
        "sha256": "", "excerpt_seeds": ["3.0 kg"], "region": "INT",
        "verified_via": "official_url", "url": "https://example.test/spec.pdf",
    })
    activity_page = _page().replace("## 产品性质与交付状态", "## 投入产出流")
    candidate, _ = build_candidate(collection, activity_page, {"sources": {}})
    assert "NOT POPULATED" not in candidate
    assert "## 投入产出流" in candidate


def test_activity_candidate_upgrades_legacy_five_table_page_with_props() -> None:
    collection = copy.deepcopy(_collection())
    collection["node_id"] = "A001"
    collection["node_type"] = "activity"
    collection["sources"][0].update({
        "title": "Manufacturer specification", "type": "manufacturer_spec",
        "version": "2026-01", "locator": "p.1", "authority": "manufacturer",
        "sha256": "", "excerpt_seeds": ["reference product"], "region": "INT",
        "verified_via": "official_url", "url": "https://example.test/spec.pdf",
    })
    props = collection["tables"]["props"]
    collection["tables"] = {"props": props}
    page = (_page().replace("id: P003", "id: A001")
            .replace("<!-- EV:props:START -->\nold\n<!-- EV:props:END -->\n", "")
            .replace("<!-- EV:params:START -->", "## 工艺与地区参数\n\n<!-- EV:params:START -->")
            .replace("## 产品性质与交付状态", "## 投入产出流"))
    candidate, _ = build_candidate(collection, page, {"sources": {}})
    assert candidate.count("<!-- EV:props:START -->") == 1
    assert "## 参考产品性质与交接状态" in candidate
    assert "| 产品质量 | 参考配置 | kg/台 | 3.0 | manufacturer-spec |" in candidate


def test_candidate_drops_stale_frontmatter_sources() -> None:
    collection = copy.deepcopy(_collection())
    collection["sources"][0].update({
        "title": "Manufacturer specification", "type": "manufacturer_spec",
        "version": "2026-01", "locator": "p.1", "authority": "manufacturer",
        "sha256": "", "excerpt_seeds": ["3.0 kg"], "region": "INT",
        "verified_via": "official_url", "url": "https://example.test/spec.pdf",
    })
    page = (_page().replace("provenance_refs: [internal-review]",
                            "provenance_refs: [internal-review, stale-source]")
            + "\n[^stale-source]: definition without inline use\n")
    candidate, _ = build_candidate(collection, page, {"sources": {}})
    frontmatter = candidate.split("---", 2)[1]
    assert "stale-source" not in frontmatter


def test_table_renderer_escapes_markdown_pipe() -> None:
    row = _collection()["tables"]["props"][0]
    row["value"] = "3.0 | maximum"
    rendered = render_table("props", [row])
    assert "3.0 \\| maximum" in rendered


def test_declared_cn_population_floor_is_a_hard_gate() -> None:
    metrics = {"props_populated": 7, "params_int_populated": 10,
               "params_cn_populated": 8, "quality_assessed": 10}
    checks = population_floor_checks(metrics, {
        "props_populated": 7, "params_int_populated": 9,
        "params_cn_populated": 9, "quality_assessed": 10,
    })
    assert checks["params_cn_population_floor"] is False


def test_collection_change_log_is_data_driven_and_idempotent() -> None:
    collection = _collection()
    collection["sources"][0].update({
        "title": "Manufacturer specification", "type": "manufacturer_spec",
        "version": "2026-01", "locator": "p.1", "authority": "manufacturer",
        "sha256": "", "excerpt_seeds": ["3.0 kg"], "region": "CN",
        "verified_via": "official_url", "url": "https://example.test/spec.pdf",
    })
    collection["change_log"] = {
        "date": "2026-08-11", "title": "中国公开型号回填",
        "bullets": ["should not be template text"],
    }
    first, registry = build_candidate(collection, _page(), {"sources": {}})
    second, _ = build_candidate(collection, first, registry)
    assert first.count("### 2026-08-11 · 中国公开型号回填") == 1
    assert second.count("### 2026-08-11 · 中国公开型号回填") == 1


def test_existing_same_source_merges_evidence_instead_of_conflicting() -> None:
    collection = copy.deepcopy(_collection())
    collection["sources"][0].update({
        "title": "Manufacturer specification", "type": "manufacturer_spec",
        "version": "2026-01", "locator": "p.2", "authority": "manufacturer",
        "sha256": "", "excerpt_seeds": ["new quote"], "region": "INT",
        "verified_via": "official_url", "url": "https://example.test/spec.pdf",
    })
    registry = {"sources": {"manufacturer-spec": {
        "title": "Manufacturer specification", "type": "manufacturer_spec",
        "version": "2026-01", "locator": "p.1", "authority": "manufacturer",
        "hash": "", "ref_count": 1, "excerpt_seeds": ["old quote"],
        "status": "verified", "region": "INT", "verified_via": "official_url",
        "url": "https://example.test/spec.pdf",
    }}}
    _, staged = build_candidate(collection, _page(), registry)
    source = staged["sources"]["manufacturer-spec"]
    assert source["excerpt_seeds"] == ["old quote", "new quote"]
    assert source["locator"] == "p.1; p.2"


def test_activity_flow_and_emission_tables_use_activity_contract_headers() -> None:
    flow = {"field": "blade server", "direction": "output", "unit": "unit",
            "basis": "reference", "int_value": "缺口：未公开", "int_source": "source",
            "cn_value": "缺口：未公开", "cn_source": "source", "pedigree": "explicit_gap"}
    emission = {"field": "air emissions", "cas": "—", "compartment": "air", "unit": "kg",
                "basis": "reference", "int_value": "缺口：未公开", "int_source": "source",
                "cn_value": "缺口：未公开", "cn_source": "source", "pedigree": "explicit_gap"}
    assert "| 流 | 方向 |" in render_table("flows", [flow])
    assert "| substance | CAS | compartment |" in render_table("emissions", [emission])
    checks = population_floor_checks(
        {"flows_populated": 0, "emissions_populated": 0, "indicators_populated": 0,
         "props_populated": 0, "params_int_populated": 0, "params_cn_populated": 0,
         "quality_assessed": 5},
        {"flows_populated": 0, "emissions_populated": 0, "indicators_populated": 0,
         "params_int_populated": 0, "quality_assessed": 5},
    )
    assert all(checks.values())
