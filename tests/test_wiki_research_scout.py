from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location(
        "scout_wiki_research_plan", ROOT / "scripts/scout_wiki_research_plan.py"
    )
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_missing_english_terms_are_translated_for_english_search() -> None:
    record = module().build_query(
        {
            "canonical_zh": "SMT贴装, 回流焊接 | 主板PCBA, 通用服务器用",
            "candidate_aliases_zh": [],
            "canonical_en": "",
            "candidate_aliases_en": [],
        },
        "en",
        "process_origin_and_boundary",
    )

    assert record["query"] == (
        "SMT assembly reflow soldering motherboard PCBA "
        "for general-purpose servers process origin and boundary "
        "final assembly integration testing manufacturing boundary"
    )
    assert record["translation"]["method"] == "deterministic_technical_glossary"
    assert record["translation"]["source_terms"] == [
        "SMT贴装, 回流焊接 | 主板PCBA, 通用服务器用"
    ]
    assert record["translation"]["identity_authorized"] is False


def test_declared_english_terms_take_precedence_over_translation() -> None:
    record = module().build_query(
        {
            "canonical_zh": "焊锡渣",
            "candidate_aliases_zh": [],
            "canonical_en": "solder dross",
            "candidate_aliases_en": ["tin dross"],
        },
        "en",
        "composition_and_quantity",
    )

    assert record["query"] == "solder dross tin dross composition and quantity"
    assert record["translation"]["method"] == "declared_english_terminology"


def test_unknown_chinese_term_continues_as_audited_bilingual_passthrough() -> None:
    record = module().build_query(
        {
            "canonical_zh": "未知专有工艺",
            "candidate_aliases_zh": [],
            "canonical_en": "",
            "candidate_aliases_en": [],
        },
        "en",
        "representativeness_and_quality",
    )

    assert record["query"].startswith("未知专有工艺 ")
    assert record["translation"]["method"] == "bilingual_passthrough_no_glossary_match"
    assert record["translation"]["unmatched_fragments"] == ["未知专有工艺"]


def test_system_assembly_activity_searches_product_with_process_focus() -> None:
    record = module().build_query(
        {
            "canonical_zh": "系统集成, 整机总装 | 存储阵列, 全闪存, 4U",
            "candidate_aliases_zh": [],
            "canonical_en": "",
            "candidate_aliases_en": [],
        },
        "zh",
        "identity_and_terminology",
    )

    assert record["query"] == "存储阵列, 全闪存, 4U 生产装配线 制造 测试"


def test_a039_blade_server_terms_are_fully_translated() -> None:
    record = module().build_query(
        {
            "canonical_zh": "系统集成, 整机总装 | 服务器, 通用计算, 刀片式",
            "candidate_aliases_zh": [],
            "canonical_en": "",
            "candidate_aliases_en": [],
        },
        "en",
        "identity_and_terminology",
    )

    assert "general-purpose computing" in record["query"]
    assert "blade form factor" in record["query"]
    assert record["translation"]["unmatched_fragments"] == []
    assert record["translation"]["method"] == "deterministic_technical_glossary"
