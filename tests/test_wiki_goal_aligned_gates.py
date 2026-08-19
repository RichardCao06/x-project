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
    assert gate["decision"] == "PASS"
    assert gate["checks"]["english_translation_audited"] is True


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
    assert gate["decision"] == "REPAIR"
    assert gate["checks"]["english_field_translation_coverage_complete"] is False


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
    _write(batch / "table-data/collection.json", {"tables": {}})
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


def test_golden_case_reaches_wiki_candidate(tmp_path: Path) -> None:
    gate = load_script("gate_wiki_maturity.py")
    result = gate.evaluate(maturity_batch(tmp_path, a040_like=False))
    assert result["decision"] == "PASS"
    assert result["candidate_eligible"] is True
    assert result["maturity"] == "wiki_candidate"
    assert result["data_readiness"] == "data_ready"


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
