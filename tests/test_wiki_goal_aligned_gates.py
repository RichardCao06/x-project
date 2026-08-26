from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

from lca_project.kernel.goal_alignment.research_translation_repair import (
    build_repair_artifact,
    write_repair_artifact,
)


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_english_name_gets_audited_english_discovery_terms(tmp_path: Path) -> None:
    workflow = tmp_path / "nomination.workflow.run.js"
    workflow.write_text(
        'const NODES = [{"node_id":"A040","name":"系统集成, 整机总装 | 笔记本电脑"}] '
        '/* DATA-BINDING:END */\n', encoding="utf-8",
    )
    hints = tmp_path / "hints.json"
    hints.write_text(json.dumps({"terminology": {
        "canonical_zh": "系统集成, 整机总装 | 笔记本电脑",
        "synonyms_zh": ["笔记本整机总装"],
        "canonical_en": "", "synonyms_en": [],
    }}, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "research-plan.json"
    subprocess.run([
        sys.executable, str(ROOT / "scripts/build_wiki_research_plan.py"),
        str(workflow), str(output), "--source-hints", str(hints),
    ], check=True)
    plan = json.loads(output.read_text(encoding="utf-8"))
    terms = plan["terminology"]["translated_search_terms_en"]
    assert terms and all(not re.search(r"[\u3400-\u9fff]", term) for term in terms)
    assert "laptop computer" in " ".join(terms)
    gate = load_script("gate_wiki_research_plan.py").evaluate(plan)
    # Static English field coverage is visible, but discovery can start on the
    # Chinese track and expand English terms from runtime search evidence.
    assert gate["decision"] == "PASS"
    assert gate["pipeline_continue"] is True
    assert gate["checks"]["english_translation_audited"] is True
    assert gate["checks"]["english_field_translation_coverage_complete"] is False
    assert gate["warnings"] == ["english_field_translation_coverage_complete"]


def test_l1_translation_repair_artifact_changes_next_research_plan(tmp_path: Path) -> None:
    workflow = tmp_path / "nomination.workflow.run.js"
    workflow.write_text(
        'const NODES = [{"node_id":"A039","name":"系统集成, 整机总装 | 服务器, 通用计算, 刀片式"}] '
        '/* DATA-BINDING:END */\n', encoding="utf-8",
    )
    output = tmp_path / "research-plan.json"
    failed_plan = {
        "node_id": "A039",
        "node_name": "系统集成, 整机总装 | 服务器, 通用计算, 刀片式",
        "terminology": {"query_translation": {
            "source_terms": ["系统集成, 整机总装 | 服务器, 通用计算, 刀片式"],
            "unmatched_fragments": ["通用计算", "刀片式"],
        }},
    }
    artifact = build_repair_artifact(failed_plan)
    assert artifact["status"] == "ready"
    assert write_repair_artifact(
        tmp_path / "research-plan-translation-repair.json", artifact
    ) is True

    subprocess.run([
        sys.executable, str(ROOT / "scripts/build_wiki_research_plan.py"),
        str(workflow), str(output),
    ], check=True)

    plan = json.loads(output.read_text(encoding="utf-8"))
    translation = plan["terminology"]["query_translation"]
    assert translation["method"] == "deterministic_technical_glossary_with_l1_override"
    assert translation["unmatched_fragments"] == []
    assert translation["repair_artifact_sha256"] == artifact["artifact_sha256"]
    assert load_script("gate_wiki_research_plan.py").evaluate(plan)["decision"] == "PASS"
    contract = plan["field_translation_contract"]
    assert contract["scope"] == "node_and_table_schema"
    assert contract["required_field_count"] == 33
    assert len(plan["field_translations"]) == 33
    assert all(not re.search(r"[\u3400-\u9fff]|(?<![A-Za-z0-9])[AP]\d{3}(?!\d)", value)
               for value in plan["field_translations"].values())

    del plan["field_translations"]["返工率"]
    gate = load_script("gate_wiki_research_plan.py").evaluate(plan)
    assert gate["decision"] == "PASS"
    assert gate["checks"]["english_field_translation_coverage_complete"] is False
    assert "english_field_translation_coverage_complete" in gate["warnings"]


def test_a013_activity_field_contract_matches_blueprint_and_gate_fails_closed() -> None:
    blueprint_builder = load_script("build_wiki_content_blueprint.py")
    plan_builder = load_script("build_wiki_research_plan.py")
    gate_builder = load_script("gate_wiki_research_plan.py")
    graph = json.loads((
        ROOT / "vendor/lca_cornerstone/fixtures/wiki-phase2/docs/ict_equipment-name-graph.json"
    ).read_text(encoding="utf-8"))
    blueprint = blueprint_builder.build(graph, "A013")
    translations, contract = plan_builder.field_translation_contract("A013")
    expected = {
        field for fields in blueprint["evidence_tables"].values() for field in fields
    }

    assert contract is not None
    assert contract["required_field_count"] == 35
    assert set(translations) == expected
    assert all(not re.search(
        r"[\u3400-\u9fff]|(?<![A-Za-z0-9])[AP]\d{3}(?!\d)", value,
    ) for value in translations.values())
    plan = {
        "node_id": "A013", "languages": ["zh", "en"],
        "terminology": {
            "canonical_zh": blueprint["node_name"],
            "canonical_en": "100G/400G 2U network switch final assembly",
            "candidate_aliases_en": [],
        },
        "research_questions": sorted(gate_builder.REQUIRED_QUESTIONS),
        "source_role_contract": {
            key: "test" for key in gate_builder.REQUIRED_SOURCE_ROLES
        },
        "field_translations": translations, "field_translation_contract": contract,
    }
    assert gate_builder.evaluate(plan)["decision"] == "PASS"

    plan.pop("field_translation_contract")
    plan["field_translations"] = {}
    rejected = gate_builder.evaluate(plan)
    assert rejected["decision"] == "PASS"
    assert rejected["checks"]["english_field_translation_coverage_complete"] is False
    assert rejected["maturity_ceiling"] == "evidence_limited"


def test_research_plan_gate_still_blocks_missing_executable_chinese_identity() -> None:
    gate = load_script("gate_wiki_research_plan.py")
    result = gate.evaluate({
        "node_id": "A019", "languages": ["zh", "en"],
        "terminology": {},
        "research_questions": sorted(gate.REQUIRED_QUESTIONS),
        "source_role_contract": {
            key: "test" for key in gate.REQUIRED_SOURCE_ROLES
        },
    })

    assert result["decision"] == "REPAIR"
    assert result["pipeline_continue"] is False
    assert result["failures"] == ["canonical_chinese_present"]


def test_semantic_closure_has_no_character_count_requirement() -> None:
    gate = load_script("gate_wiki_content_closure.py")
    headings = ["定义", "边界", "投入", "产出", "数据", "建模", "质量", "应用", "限制"]
    blueprint = {"sections": {heading: {} for heading in headings}}
    content = {"sections": [{
        "heading": heading,
        "paragraphs": [{"sentences": [{
            "text": "待补证。", "claim_kind": "evidence_gap", "evidence_claim_ids": [],
        }]}],
    } for heading in headings]}
    result = gate.evaluate(blueprint, content, {"claims": []}, {"decision": "PASS"})
    assert result["decision"] == "PASS"
    assert result["candidate_eligible"] is True
    assert "body_chars" not in result["checks"]
    assert "body_chars" not in result["metrics"]


def test_semantic_closure_accepts_frozen_graph_fact_for_core_reconciliation() -> None:
    gate = load_script("gate_wiki_content_closure.py")
    headings = ["定义", "参考", "边界", "路线", "投入产出"]
    blueprint = {"sections": {heading: {} for heading in headings}}
    content = {"sections": []}
    claims = []
    for index, heading in enumerate(headings):
        kind = "internal_graph_fact" if heading == "投入产出" else "evidence_gap"
        claim_ids = ["A019-16"] if kind == "internal_graph_fact" else []
        content["sections"].append({
            "heading": heading,
            "paragraphs": [{"sentences": [{
                "text": "冻结图列明该节点的投入与产出。",
                "claim_kind": kind,
                "evidence_claim_ids": claim_ids,
            }]}],
        })
    claims.append({
        "claim": {
            "claim_id": "A019-16", "claim_kind": "internal_graph_fact",
            "requirement_id": "activity.graph.reconciliation",
        },
        "verify": {"verdict": "NOT_FOUND"},
    })

    result = gate.evaluate(
        blueprint, content, {"claims": claims},
        {"decision": "PASS", "candidate_eligible": True},
    )

    assert result["decision"] == "PASS"
    assert result["checks"]["core_sections_fact_or_explicit_gap"] is True
    assert result["metrics"]["core_sections"]["投入产出"] is True


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def maturity_batch(tmp_path: Path, *, a040_like: bool) -> Path:
    batch = tmp_path / ("a040" if a040_like else "golden")
    conflict = [{
        "type": "input_output_product_family_conflict",
        "input": "blade server", "output": "laptop computer",
    }] if a040_like else []
    limited = a040_like
    _write(batch / "research-plan-gate.json", {"decision": "PASS"})
    _write(batch / "source-diversity-gate.json", {
        "decision": "LIMITED" if limited else "PASS",
        "warnings": ["preview_distinct_domains"] if limited else [],
    })
    _write(batch / "content-blueprint.json", {"semantic_conflicts": conflict})
    _write(batch / "content-closure-gate.json", {"candidate_eligible": not limited})
    _write(batch / "editorial-loop/editorial-policy-decision.json", {"decision": "accept"})
    _write(batch / "draft-content-gate.json", {
        "candidate_eligible": not limited, "pages": [],
    })
    _write(batch / "table-data/source-verdict.json", {"verdict": "PASS"})
    gap = {
        "table": "flows", "field": "P026 blade server", "decision": "explicit_gap",
        "gap_evidence": {
            "protocol": "wiki-table-gap-evidence-v1",
            "reason": "no_eligible_source_for_track",
            "matrix_sha256": "b" * 64,
            "query_hashes": ["a" * 64],
        },
    }
    _write(batch / "table-data/evidence-selection.json", {
        "outcome": "NO_ELIGIBLE_PUBLIC_DATA" if limited else "FULLY_POPULATED",
        "fields": [gap] if limited else [],
        "accepted_evidence": [] if limited else [{"field": "mass"}],
    })
    populated = 0 if limited else 1
    _write(batch / "table-data/collection.json", {"tables": {
        "props": ([] if limited else [{"field": "mass", "status": "populated"}]),
    }})
    _write(batch / "table-data/table-population-gate.json", {
        "verdict": "INCOMPLETE" if limited else "GO",
        "contract_valid": True,
        "goal_readiness": {"populated_fields": populated,
                           "goal_data_ready": bool(populated)},
    })
    _write(batch / "verify-output.json", {"claims": [{
        "claim": {"requirement_id": "identity.definition"},
        "verify": {"verdict": "CONFIRMED"},
    }]})
    return batch


def test_a040_like_semantic_and_evidence_debt_cannot_become_candidate(tmp_path: Path) -> None:
    gate = load_script("gate_wiki_maturity.py")
    result = gate.evaluate(maturity_batch(tmp_path, a040_like=True))
    assert result["candidate_eligible"] is False
    assert result["maturity"] == "diagnostic_preview"
    assert result["data_readiness"] == "no_eligible_public_data"
    assert result["checks"]["graph_semantic_conflicts_resolved"] is False
    assert result["checks"]["explicit_gaps_have_search_provenance"] is True
    assert result["checks"]["accepted_field_evidence_nonzero"] is False
    assert result["checks"]["populated_model_fields_nonzero"] is False


def test_golden_case_reaches_wiki_candidate(tmp_path: Path) -> None:
    gate = load_script("gate_wiki_maturity.py")
    result = gate.evaluate(maturity_batch(tmp_path, a040_like=False))
    assert result["decision"] == "PASS"
    assert result["candidate_eligible"] is True
    assert result["maturity"] == "wiki_candidate"
    assert result["data_readiness"] == "data_ready"
    assert result["checks"]["accepted_field_evidence_nonzero"] is True
    assert result["checks"]["populated_model_fields_nonzero"] is True


def test_source_diversity_limit_remains_evidence_limited_without_new_research(
    tmp_path: Path,
) -> None:
    gate = load_script("gate_wiki_maturity.py")
    batch = maturity_batch(tmp_path, a040_like=False)
    _write(batch / "source-diversity-gate.json", {
        "decision": "LIMITED", "pipeline_continue": True,
        "warnings": ["only_two_confirmed_domains"],
    })

    result = gate.evaluate(batch)

    assert result["decision"] == "LIMITED"
    assert result["pipeline_continue"] is True
    assert result["candidate_eligible"] is False
    assert result["maturity"] == "evidence_limited"
    assert result["checks"]["source_roles_candidate_ready"] is False


def test_exhausted_source_scarcity_materializes_as_terminal_evidence_limited(
    tmp_path: Path,
) -> None:
    gate = load_script("gate_wiki_maturity.py")
    batch = maturity_batch(tmp_path, a040_like=False)
    _write(batch / "source-diversity-gate.json", {
        "decision": "EVIDENCE_LIMITED", "pipeline_continue": True,
        "candidate_eligible": False,
        "materialization_branch": {
            "kind": "explicit_gap_evidence_limited", "release_prohibited": True,
        },
    })

    result = gate.evaluate(batch)

    assert result["decision"] == "LIMITED"
    assert result["maturity"] == "evidence_limited"
    assert result["candidate_eligible"] is False
    assert result["pipeline_continue"] is False


def test_recoverable_fetch_or_extraction_gap_keeps_autonomous_pipeline_open(
    tmp_path: Path,
) -> None:
    gate = load_script("gate_wiki_maturity.py")
    batch = maturity_batch(tmp_path, a040_like=False)
    _write(batch / "table-data/evidence-selection.json", {
        "outcome": "NO_ELIGIBLE_PUBLIC_DATA", "accepted_evidence": [],
        "fields": [], "reason_counts": {},
        "candidate_audits": [{
            "decision": "rejected", "reasons": ["payload_not_fetched"],
        }],
    })
    _write(batch / "table-data/collection.json", {"tables": {"props": []}})
    _write(batch / "table-data/table-population-gate.json", {
        "verdict": "INCOMPLETE", "contract_valid": True,
        "goal_readiness": {"populated_fields": 0, "goal_data_ready": False},
    })

    result = gate.evaluate(batch)

    assert result["candidate_eligible"] is False
    assert result["pipeline_continue"] is True
    assert result["quality_debt"]["recoverable_reason_codes"] == [
        "payload_not_fetched"
    ]


def test_exhausted_internal_only_gap_stops_autonomous_research_loop(
    tmp_path: Path,
) -> None:
    gate = load_script("gate_wiki_maturity.py")
    batch = maturity_batch(tmp_path, a040_like=False)
    _write(batch / "table-data/evidence-selection.json", {
        "outcome": "NO_ELIGIBLE_PUBLIC_DATA", "accepted_evidence": [],
        "fields": [],
        "reason_counts": {"field_requires_node_specific_internal_record": 2},
        "candidate_audits": [],
    })
    _write(batch / "table-data/collection.json", {"tables": {"props": []}})
    _write(batch / "table-data/table-population-gate.json", {
        "verdict": "INCOMPLETE", "contract_valid": True,
        "goal_readiness": {"populated_fields": 0, "goal_data_ready": False},
    })

    result = gate.evaluate(batch)

    assert result["candidate_eligible"] is False
    assert result["pipeline_continue"] is False
