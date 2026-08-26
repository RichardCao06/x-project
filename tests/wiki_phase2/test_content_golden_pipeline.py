"""Regression gates for the Content Blueprint stage added after Verify."""
from __future__ import annotations

import json
import importlib.util
from copy import deepcopy
from pathlib import Path
import subprocess
import sys

import pytest

from wiki_claim_coverage import body_of
from wiki_content_enrich import render_product_tables
from wiki_draft_content_gate import _blueprint_checks
from run_wiki_content_capture import validate_result


ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor/lca_cornerstone"
_NORMALIZER_SPEC = importlib.util.spec_from_file_location(
    "normalize_wiki_content_claims", ROOT / "scripts/normalize_wiki_content_claims.py"
)
assert _NORMALIZER_SPEC and _NORMALIZER_SPEC.loader
_NORMALIZER = importlib.util.module_from_spec(_NORMALIZER_SPEC)
_NORMALIZER_SPEC.loader.exec_module(_NORMALIZER)
normalize_sections = _NORMALIZER.normalize_sections

_BLUEPRINT_SPEC = importlib.util.spec_from_file_location(
    "build_wiki_content_blueprint", ROOT / "scripts/build_wiki_content_blueprint.py"
)
assert _BLUEPRINT_SPEC and _BLUEPRINT_SPEC.loader
_BLUEPRINT = importlib.util.module_from_spec(_BLUEPRINT_SPEC)
_BLUEPRINT_SPEC.loader.exec_module(_BLUEPRINT)


def _a019_contract_and_draft() -> tuple[dict, dict]:
    graph = json.loads((
        VENDOR / "fixtures/wiki-phase2/docs/ict_equipment-name-graph.json"
    ).read_text(encoding="utf-8"))
    blueprint = _BLUEPRINT.build(graph, "A019")
    paragraphs = {
        "定义与参考活动": (
            "A019以服务器配置交接为活动边界",
            "A019对服务器, 通用计算, 2U执行配置与出厂交接。",
            "该定义不把上游整机制造数量推定为本节点实测值。",
        ),
        "参考产品与参考单位": (
            "参考产品及按台计量口径需要冻结",
            "参考产品按台记录配置版本和合格交接状态。",
            "计量口径必须注明批次范围及包装前后的边界差异。",
        ),
        "单元过程边界": (
            "配置测试及交接步骤限定单元过程",
            "单元过程纳入配置、测试以及合格产品交接步骤。",
            "部件制造和使用阶段不因流程相邻而自动纳入。",
        ),
        "技术路线与相邻活动区分": (
            "固件配置路线需要区别相邻制造活动",
            "技术路线记录固件设置和终检，不替代机箱制造清单。",
            "返工若回到本过程，应单列循环次数和资源消耗。",
        ),
        "投入产出与脊边对账": (
            "全部输入输出沿冻结图谱逐项对账",
            "P034 包装废料作为输出流单列，不作为节点硬身份。",
            "质量闭合仍需批次投入、合格产出与损耗的实测数据。",
        ),
        "直接排放、废物与监测指标边界": (
            "环境排放和废物流采用不同记录边界",
            "废物流按产品脊边记录，环境排放则按受纳介质分类。",
            "企业汇总指标不能替代配置节点的直接监测结果。",
        ),
        "节点特定采集字段": (
            "型号批次字段支撑节点清单的可追溯性",
            "采集字段包括配置版本、批次产量、工时和返工率。",
            "缺失字段应保持证据缺口状态，不能用默认数值补齐。",
        ),
        "区域化补充要求": (
            "地点电力和运输路径共同限定区域适用性",
            "区域化记录应连接装配地点、电力区域与部件来源地。",
            "代表期之外的路线变化需要重新判断代理适用条件。",
        ),
        "数据适用状态与缺口": (
            "已核实身份与前景清单缺口必须分层表达",
            "产品身份可由图谱固定，但前景清单仍缺节点实测支持。",
            "在补齐质量和能耗前，结果只能作为证据受限预览。",
        ),
    }
    document = {
        "protocol": "wiki-content-draft-v2", "node_id": "A019",
        "sections": [
            {"heading": heading, "paragraphs": [{
                "focus": paragraphs[heading][0],
                "sentences": [
                    {"text": paragraphs[heading][1], "claim_kind": "modeling_judgment",
                     "rhetorical_role": "thesis", "evidence_claim_ids": []},
                    {"text": paragraphs[heading][2], "claim_kind": "evidence_gap",
                     "rhetorical_role": "boundary", "evidence_claim_ids": []},
                ],
            }]}
            for heading in blueprint["sections"]
        ],
    }
    return blueprint, document


def test_a019_role_correct_blueprint_passes_all_golden_checks_without_coproduct_label(
    tmp_path: Path,
) -> None:
    blueprint, document = _a019_contract_and_draft()
    body = "\n".join(
        sentence["text"]
        for section in document["sections"]
        for paragraph in section["paragraphs"]
        for sentence in paragraph["sentences"]
    )
    assert "共生包装废料" not in body
    assert "P034" in body

    path = tmp_path / "content-result.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    scorecard = validate_result(path, blueprint, [])

    assert scorecard["checks"] == {
        "assertions_not_stuffed": True,
        "modeling_not_stuffed": True,
        "single_sentence_ratio": True,
        "paragraph_focuses_unique": True,
        "near_duplicate_free": True,
        "identity_tokens": True,
        "forbidden_phrases": True,
    }


def test_a019_golden_validation_still_requires_reference_identity(tmp_path: Path) -> None:
    blueprint, document = _a019_contract_and_draft()
    mutated = deepcopy(document)
    mutated["sections"][0]["paragraphs"][0]["sentences"][0]["text"] = (
        "该活动执行配置与出厂交接，但此句省略参考产品名称。"
    )
    path = tmp_path / "content-result.json"
    path.write_text(json.dumps(mutated, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="'identity_tokens': False"):
        validate_result(path, blueprint, [])


def test_old_p003_fixture_is_a_negative_golden_case() -> None:
    page = next((VENDOR / "fixtures/wiki-phase2/wiki/ict_equipment/products").glob("P003--*.md"))
    text = page.read_text(encoding="utf-8")
    blueprint = json.loads(
        (VENDOR / "fixtures/wiki-phase2/content-blueprints/P003.json").read_text(encoding="utf-8")
    )
    checks, _ = _blueprint_checks(text, body_of(text), blueprint)
    assert not all(checks.values())
    assert "content_blueprint_body_depth" not in checks
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


def test_normalizer_recovers_semantically_merged_activity_sections() -> None:
    blueprint = {
        "sections": {
            "定义与参考活动": {"topics": ["转换动作和技术路线"]},
            "参考产品与参考单位": {"topics": ["参考产品身份", "按台交接的参考单位"]},
            "单元过程边界": {"topics": ["纳入的装配、连接、配置与测试"]},
            "技术路线与相邻活动区分": {"topics": ["返工和内部循环的处理"]},
        },
    }
    document = {"sections": [
        {"heading": "定义与参考活动", "paragraphs": [
            {"focus": "A015转换动作"}, {"focus": "参考产品身份"},
            {"focus": "按台交接的参考单位"},
        ]},
        {"heading": "单元过程边界", "paragraphs": [
            {"focus": "纳入的装配连接"}, {"focus": "返工和内部循环的处理"},
        ]},
    ]}

    normalize_sections(document, blueprint)

    assert [section["heading"] for section in document["sections"]] == list(blueprint["sections"])
    assert [row["focus"] for row in document["sections"][1]["paragraphs"]] == [
        "参考产品身份", "按台交接的参考单位",
    ]


def _editorial_contract() -> tuple[dict, list[dict]]:
    blueprint = {
        "node_id": "P003",
        "sections": {"性质与形态": {"minimum_paragraphs": 1}},
        "golden_target": {
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
    assert validate_result(path, blueprint, rows)["checks"]["identity_tokens"]


def test_unused_confirmed_claim_and_legacy_quantity_minima_are_advisory(tmp_path: Path) -> None:
    blueprint, rows = _editorial_contract()
    blueprint["golden_target"].update({
        "minimum_assertions": 48,
        "minimum_paragraphs": 18,
        "minimum_modeling_judgments": 32,
    })
    content = {
        "protocol": "wiki-content-draft-v2",
        "node_id": "P003",
        "sections": [{
            "heading": "性质与形态",
            "paragraphs": [{
                "focus": "刀片服务器产品身份和证据适用边界",
                "sentences": [
                    {"text": "刀片服务器至少包含处理器和系统内存。",
                     "claim_kind": "external_fact", "rhetorical_role": "thesis",
                     "evidence_claim_ids": ["P003-4"]},
                    {"text": "该事实用于确认产品身份，不用于推断具体型号配置。",
                     "claim_kind": "modeling_judgment", "rhetorical_role": "boundary",
                     "evidence_claim_ids": []},
                ],
            }],
        }],
    }
    path = tmp_path / "content.json"
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")

    score = validate_result(path, blueprint, rows)

    assert score["unused_claim_ids"] == ["P003-5"]
    assert score["unused_claim_reason"] == "not_selected_for_prose"
    assert not score["advisories"]["claim_coverage"]
    assert not score["advisories"]["recommended_assertions"]
    assert all(score["checks"].values())


def test_normalizer_persists_repairs_and_hands_residual_issue_to_model(tmp_path: Path) -> None:
    verify = tmp_path / "verify.json"
    blueprint = tmp_path / "blueprint.json"
    runtime = tmp_path / "content-runtime"
    content = runtime / "content-result.json"
    usage = runtime / "content-usage.json"
    validator = tmp_path / "validator.py"
    runtime.mkdir()
    verify.write_text(json.dumps({"claims": [{
        "claim": {"claim_id": "A040-29", "node_id": "A040", "section": "定义",
                  "claim_kind": "modeling_judgment", "claim_text": "冻结建模判断"},
        "verify": {"verdict": "CONFIRMED"},
    }]}), encoding="utf-8")
    blueprint.write_text(json.dumps({
        "sections": {"定义": {"topics": []}},
        "golden_target": {"maximum_modeling_judgments": 20},
    }), encoding="utf-8")
    content.write_text(json.dumps({"sections": [{"heading": "定义", "paragraphs": [{
        "focus": "测试", "sentences": [{
            "text": f"句子 {index}", "claim_kind": "modeling_judgment",
            "evidence_claim_ids": ["A040-29"], "rhetorical_role": "boundary",
        } for index in range(4)],
    }]}]}), encoding="utf-8")
    usage.write_text(json.dumps({"validation_error": "overused=['A040-29']"}),
                     encoding="utf-8")
    validator.write_text(
        "def validate_result(*args):\n"
        "    raise ValueError(\"Content Golden contract 失败: {'identity_tokens': False}\")\n",
        encoding="utf-8",
    )

    completed = subprocess.run([
        sys.executable, str(ROOT / "scripts/normalize_wiki_content_claims.py"),
        str(verify), str(blueprint), str(content), str(validator),
    ], text=True, capture_output=True, check=False)

    assert completed.returncode == 2
    repaired = json.loads(content.read_text(encoding="utf-8"))
    uses = sum(
        sentence["evidence_claim_ids"].count("A040-29")
        for section in repaired["sections"]
        for paragraph in section["paragraphs"]
        for sentence in paragraph["sentences"]
    )
    assert uses == 3
    residual = json.loads(usage.read_text(encoding="utf-8"))
    assert residual["normalization_status"] == "residual_issues"
    assert "identity_tokens" in residual["validation_error"]
