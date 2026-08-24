from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def module():
    path = ROOT / "scripts/wiki_search_gates.py"
    spec = importlib.util.spec_from_file_location("wiki_search_gates", path)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def row(claim_id: str, url: str, title: str, excerpt: str, **fetch_fields):
    return {
        "claim": {"claim_id": claim_id, "believed_source": title,
                  "claim_text": "中文提名文本不应用于判断来源语言"},
        "fetchResult": {"url": url, "excerpt": excerpt, **fetch_fields},
        "verify": {"verdict": "CONFIRMED"},
    }


def test_source_language_uses_explicit_provenance_then_fetched_content() -> None:
    gate = module()
    explicit = row("A001-1", "https://example.com/a", "中文标题", "中文内容", language="en-US")
    inferred_en = row("A001-2", "https://example.org/a", "Technical assembly guide",
                      "This technical document describes final assembly and production testing. " * 8)
    inferred_zh = row("A001-3", "https://example.cn/a", "生产装配技术指南",
                      "本技术文件说明整机装配、生产测试、质量控制和产品交接要求。" * 8)

    assert gate.source_language(explicit) == ("en", "fetch.language")
    assert gate.source_language(inferred_en)[0] == "en"
    assert gate.source_language(inferred_zh)[0] == "zh"


def test_preview_diversity_repairs_then_becomes_limited_and_reviewed_is_strict() -> None:
    gate = module()
    verified = {"claims": [row(
        "A001-1", "https://example.com/a", "Technical assembly guide",
        "This technical document describes final assembly and production testing. " * 8,
    )]}
    plan = {"minimum_source_diversity": {
        "preview_hard_confirmed_sources": 1,
        "preview_primary_sources": 3,
        "preview_distinct_domains": 3,
        "preview_technical_sources": 1,
        "preview_language_tracks": 2,
        "reviewed_primary_sources": 3,
        "reviewed_distinct_domains": 3,
        "reviewed_technical_sources": 2,
        "reviewed_language_tracks": 2,
    }}

    preview = gate.diversity_gate(verified, plan, reviewed=False)
    exhausted = gate.diversity_gate(verified, plan, reviewed=False, attempt=2)
    reviewed = gate.diversity_gate(verified, plan, reviewed=True)

    assert preview["decision"] == "REPAIR"
    assert preview["pipeline_continue"] is False
    assert exhausted["decision"] == "LIMITED"
    assert exhausted["pipeline_continue"] is True
    assert exhausted["candidate_eligible"] is False
    assert reviewed["decision"] == "BLOCKED"
    for check in (
        "reviewed_confirmed_urls", "reviewed_distinct_domains",
        "reviewed_technical_sources", "reviewed_language_tracks",
    ):
        assert reviewed["checks"][check] is False
    for role in (
        "identity_source_role", "process_boundary_source_role",
        "adjacent_distinction_source_role",
    ):
        assert reviewed["quality_checks"][role] is False
