"""Regression gates for the Content Blueprint stage added after Verify."""
from __future__ import annotations

import json
from pathlib import Path

from wiki_claim_coverage import body_of
from wiki_content_enrich import render_product_tables
from wiki_draft_content_gate import _blueprint_checks
from run_wiki_content_capture import validate_result


ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor/lca_cornerstone"


def test_old_p003_fixture_is_a_negative_golden_case() -> None:
    page = next((VENDOR / "fixtures/wiki-phase2/wiki/ict_equipment/products").glob("P003--*.md"))
    text = page.read_text(encoding="utf-8")
    blueprint = json.loads(
        (VENDOR / "fixtures/wiki-phase2/content-blueprints/P003.json").read_text(encoding="utf-8")
    )
    checks, _ = _blueprint_checks(text, body_of(text), blueprint)
    assert not all(checks.values())
    assert not checks["content_blueprint_body_depth"]
    assert not checks["content_blueprint_required_topics"]
    assert not checks["content_blueprint_node_specific_tables"]


def test_p003_blueprint_tables_are_node_specific_and_lint_safe() -> None:
    blueprint = json.loads(
        (VENDOR / "fixtures/wiki-phase2/content-blueprints/P003.json").read_text(encoding="utf-8")
    )
    rendered = render_product_tables(blueprint)
    labels = [label for rows in blueprint["evidence_tables"].values() for label in rows]
    assert all(label in rendered for label in labels)
    assert "待采/待核" not in rendered
    assert rendered.count("<!-- EV:props:START -->") == 1
    assert rendered.count("<!-- EV:params:START -->") == 1
    assert rendered.count("<!-- EV:quality:START -->") == 1


def _editorial_contract() -> tuple[dict, list[dict]]:
    blueprint = {
        "node_id": "P003",
        "sections": {"性质与形态": {"minimum_paragraphs": 1}},
        "golden_target": {
            "minimum_body_chars": 1,
            "minimum_assertions": 2,
            "maximum_assertions": 4,
            "minimum_paragraphs": 1,
            "minimum_modeling_judgments": 1,
            "maximum_modeling_judgments": 3,
            "maximum_single_sentence_paragraph_ratio": 0,
            "maximum_sentences_per_paragraph": 4,
            "maximum_external_facts_per_paragraph": 1,
            "maximum_near_duplicate_ratio": 0.72,
        },
        "required_tokens": ["刀片服务器"],
        "forbidden_phrases": [],
    }
    rows = []
    for claim_id, text in (
        ("P003-4", "刀片服务器至少包含一个处理器和系统内存。"),
        ("P003-5", "刀片服务器是高密度独立服务器设备。"),
    ):
        rows.append({
            "claim": {"claim_id": claim_id, "node_id": "P003", "section": "性质与形态",
                      "claim_kind": "external_fact", "claim_text": text},
            "verify": {"verdict": "CONFIRMED"},
        })
    return blueprint, rows


def test_editorial_protocol_fuses_evidence_claims_without_copying_ledger_text(tmp_path: Path) -> None:
    blueprint, rows = _editorial_contract()
    content = {
        "protocol": "wiki-content-draft-v2",
        "node_id": "P003",
        "sections": [{
            "heading": "性质与形态",
            "paragraphs": [{
                "focus": "刀片服务器的产品形态与基本构成",
                "sentences": [
                    {"text": "刀片服务器属于高密度独立服务器形态，其基本构成至少包括处理器和系统内存。",
                     "claim_kind": "external_fact", "rhetorical_role": "thesis",
                     "evidence_claim_ids": ["P003-4", "P003-5"]},
                    {"text": "这一构成边界用于识别随成品交付的计算部件，并与共享机箱提供的供电和散热资源分开记录。",
                     "claim_kind": "modeling_judgment", "rhetorical_role": "boundary",
                     "evidence_claim_ids": []},
                ],
            }],
        }],
    }
    path = tmp_path / "content.json"
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    score = validate_result(path, blueprint, rows)
    assert score["assertions"] == 2
    assert all(score["checks"].values())


def test_editorial_protocol_rejects_adjacent_external_claim_dump(tmp_path: Path) -> None:
    blueprint, rows = _editorial_contract()
    content = {
        "protocol": "wiki-content-draft-v2",
        "node_id": "P003",
        "sections": [{
            "heading": "性质与形态",
            "paragraphs": [{
                "focus": "刀片服务器的产品形态与基本构成",
                "sentences": [
                    {"text": rows[0]["claim"]["claim_text"], "claim_kind": "external_fact",
                     "rhetorical_role": "thesis", "evidence_claim_ids": ["P003-4"]},
                    {"text": rows[1]["claim"]["claim_text"], "claim_kind": "external_fact",
                     "rhetorical_role": "evidence", "evidence_claim_ids": ["P003-5"]},
                ],
            }],
        }],
    }
    path = tmp_path / "claim-dump.json"
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    try:
        validate_result(path, blueprint, rows)
    except ValueError as exc:
        assert "外部事实锚点过多" in str(exc)
    else:
        raise AssertionError("adjacent external claims must be rejected")


def test_nonconfirmed_external_claim_is_not_required_and_graph_fact_can_ground_judgment(tmp_path: Path) -> None:
    blueprint, _ = _editorial_contract()
    rows = [
        {"claim": {"claim_id": "P003-1", "node_id": "P003", "section": "性质与形态",
                   "claim_kind": "external_fact", "claim_text": "相邻对象事实"},
         "verify": {"verdict": "INSUFFICIENT"}},
        {"claim": {"claim_id": "P003-2", "node_id": "P003", "section": "性质与形态",
                   "claim_kind": "internal_graph_fact", "claim_text": "冻结图谱关系"}},
        {"claim": {"claim_id": "P003-3", "node_id": "P003", "section": "性质与形态",
                   "claim_kind": "modeling_judgment", "claim_text": "建模边界"}},
    ]
    content = {"protocol": "wiki-content-draft-v2", "node_id": "P003", "sections": [{
        "heading": "性质与形态", "paragraphs": [{"focus": "刀片服务器冻结图谱关系及边界",
        "sentences": [
            {"text": "刀片服务器的输入输出关系以冻结图谱为准。", "claim_kind": "internal_graph_fact",
             "rhetorical_role": "thesis", "evidence_claim_ids": ["P003-2"]},
            {"text": "该图谱关系用于限定建模边界，但不能替代数量数据。", "claim_kind": "modeling_judgment",
             "rhetorical_role": "boundary", "evidence_claim_ids": ["P003-2", "P003-3"]},
        ]}]}]}
    path = tmp_path / "content.json"
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    assert validate_result(path, blueprint, rows)["checks"]["required_tokens"]
