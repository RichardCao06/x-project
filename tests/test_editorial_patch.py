from __future__ import annotations

from collections import Counter
from copy import deepcopy
import re

import pytest

from lca_project.domains.editorial_patch import (
    EditorialPatchError, apply_legacy_repairs, apply_repairs, canonical_hash,
    claim_binding_metrics, legacy_paragraph_manifest, normalize_legacy_repair_claim_bindings,
    paragraph_manifest, prepare_legacy_patch_review, render_sections,
)


def fixtures():
    ids = [f"s{i}" for i in range(1, 10)]
    blueprint = {"section_order": ids,
                 "sections": {sid: {"heading": f"标题{i}"} for i, sid in enumerate(ids, 1)}}
    draft = {"protocol": "wiki-content-draft-v3", "node_id": "A001", "sections": [
        {"section_id": sid, "paragraphs": [{"paragraph_id": "p1", "focus": f"焦点{i}",
         "sentences": [{"text": f"这是第{i}节的完整事实说明。", "claim_kind": "modeling_judgment",
                        "rhetorical_role": "thesis", "evidence_claim_ids": [f"c{i}"]}]}]}
        for i, sid in enumerate(ids, 1)
    ]}
    target = paragraph_manifest(draft)["s3.p1"]
    review = {"protocol": "wiki-editorial-patch-review-v1", "node_id": "A001",
              "verdict": "NO_GO", "issues": [{"issue_id": "E1", "section_id": "s3",
              "paragraph_id": "p1", "target_hash": target, "type": "duplicate_argument",
              "operation": "replace", "instruction": "局部修复", "facts_must_preserve": ["c3"]}]}
    repair = {"issue_id": "E1", "section_id": "s3", "paragraph_id": "p1",
              "target_hash": target, "replacement": {"focus": "修复后的焦点",
              "sentences": [{"text": "这是修复后仍然完整且更清晰的事实说明。",
              "claim_kind": "modeling_judgment", "rhetorical_role": "thesis",
              "evidence_claim_ids": ["c3"]}]}, "preserved_claim_ids": ["c3"]}
    return blueprint, draft, review, repair


def test_patch_changes_only_hash_bound_paragraph_and_requires_rereview() -> None:
    blueprint, draft, review, repair = fixtures()
    before = paragraph_manifest(draft)
    result, receipt = apply_repairs(draft, blueprint, review, [repair])
    after = paragraph_manifest(result)
    assert {key for key in before if before[key] != after[key]} == {"s3.p1"}
    assert receipt["requires_independent_rereview"] is True
    assert receipt["unchanged_paragraphs"] == {key: value for key, value in before.items()
                                                if key != "s3.p1"}
    rendered = render_sections(result, blueprint)
    assert [item["heading"] for item in rendered] == [f"标题{i}" for i in range(1, 10)]


def test_patch_rejects_stale_target_or_claim_loss() -> None:
    blueprint, draft, review, repair = fixtures()
    stale = deepcopy(draft); stale["sections"][2]["paragraphs"][0]["focus"] = "并发改动"
    with pytest.raises(EditorialPatchError, match="target hash conflict"):
        apply_repairs(stale, blueprint, review, [repair])
    repair["replacement"]["sentences"][0]["evidence_claim_ids"] = []
    with pytest.raises(EditorialPatchError, match="required facts"):
        apply_repairs(draft, blueprint, review, [repair])


def test_model_heading_is_rejected_and_hash_is_canonical() -> None:
    blueprint, draft, review, repair = fixtures()
    draft["sections"][0]["heading"] = "模型擅自标题"
    with pytest.raises(EditorialPatchError, match="may not supply headings"):
        apply_repairs(draft, blueprint, review, [repair])
    assert canonical_hash({"b": 1, "a": 2}) == canonical_hash({"a": 2, "b": 1})


def test_v2_review_is_hash_bound_and_only_targeted_paragraph_changes() -> None:
    draft = {"protocol": "wiki-content-draft-v2", "node_id": "A015", "sections": [
        {"heading": "定义", "paragraphs": [
            {"focus": "保持不变的第一段", "sentences": [
                {"text": "这一段没有被编辑审查点名，因此必须保持不变。",
                 "claim_kind": "modeling_judgment", "rhetorical_role": "thesis",
                 "evidence_claim_ids": []},
                {"text": "哈希收据应证明该段内容完全没有发生变化。",
                 "claim_kind": "modeling_judgment", "rhetorical_role": "explanation",
                 "evidence_claim_ids": []},
            ]},
            {"focus": "需要修复的第二段", "sentences": [
                {"text": "原段落包含与中心无关的外部事实引用。",
                 "claim_kind": "external_fact", "rhetorical_role": "thesis",
                 "evidence_claim_ids": ["A015-1"]},
                {"text": "该引用会打断当前段落的边界论证。",
                 "claim_kind": "modeling_judgment", "rhetorical_role": "boundary",
                 "evidence_claim_ids": []},
            ]},
        ]},
    ]}
    review = {"protocol": "wiki-editorial-review-v1", "node_id": "A015",
              "verdict": "NO_GO", "issues": [
        {"section": "定义", "paragraph_index": 2, "issue_type": "citation_intrusion",
         "explanation": "引用和段落主旨没有直接关系。", "repair_instruction": "删除无关引用并重写边界说明。"},
        {"section": "定义", "paragraph_index": 2, "issue_type": "disconnected",
         "explanation": "相邻句之间缺少论证关系。", "repair_instruction": "建立论点和边界之间的关系。"},
    ]}
    bound = prepare_legacy_patch_review(draft, review)
    assert len(bound["issues"]) == 1
    issue = bound["issues"][0]
    repair = {"issue_id": issue["issue_id"], "section_id": "定义", "paragraph_id": "p2",
              "target_hash": issue["target_hash"], "preserved_claim_ids": [],
              "replacement": {"focus": "修复后的边界中心", "sentences": [
                  {"text": "本段只说明目标活动的建模边界，不再引用无关产品事实。",
                   "claim_kind": "modeling_judgment", "rhetorical_role": "thesis",
                   "evidence_claim_ids": []},
                  {"text": "现场记录不足时应明确保留缺口，不能用相邻对象事实替代。",
                   "claim_kind": "modeling_judgment", "rhetorical_role": "boundary",
                   "evidence_claim_ids": []},
              ]}}
    before = legacy_paragraph_manifest(draft)
    result, receipt = apply_legacy_repairs(draft, bound, [repair])
    after = legacy_paragraph_manifest(result)

    assert before["定义.p1"] == after["定义.p1"]
    assert before["定义.p2"] != after["定义.p2"]
    assert receipt["targeted_paragraphs"] == ["定义.p2"]
    assert receipt["requires_independent_rereview"] is True


def test_two_legacy_splits_have_stable_ids_and_preserve_local_identity_and_peers() -> None:
    identity = "P057 钢钣金机箱/导轨, 服务器用"
    draft = {"protocol": "wiki-content-draft-v2", "node_id": "A039", "sections": [
        {"heading": "投入产出与脊边对账", "paragraphs": [
            {"focus": "未点名的资源边界", "sentences": [
                {"text": "这个前置段落必须保持逐字不变并由收据给出规范哈希。",
                 "claim_kind": "modeling_judgment", "rhetorical_role": "thesis",
                 "evidence_claim_ids": []},
            ]},
            {"focus": "共享资源与质量闭合混写", "sentences": [
                {"text": f"冻结图将 {identity} 作为投入，同时原文混写共享资源和退料。",
                 "claim_kind": "internal_graph_fact", "rhetorical_role": "thesis",
                 "evidence_claim_ids": ["A039-57"]},
            ]},
            {"focus": "未点名的质量说明", "sentences": [
                {"text": "这个后置段落即使因拆分发生位置移动也必须保持规范哈希。",
                 "claim_kind": "modeling_judgment", "rhetorical_role": "thesis",
                 "evidence_claim_ids": []},
            ]},
        ]},
        {"heading": "直接排放、废物与监测指标边界", "paragraphs": [
            {"focus": "废物与用电混写", "sentences": [
                {"text": "原文把废物流质量核算与装配用电计量写在同一个中心。",
                 "claim_kind": "modeling_judgment", "rhetorical_role": "thesis",
                 "evidence_claim_ids": []},
            ]},
        ]},
    ]}
    review = {"protocol": "wiki-editorial-review-v1", "node_id": "A039",
              "verdict": "NO_GO", "issues": [
        {"section": "投入产出与脊边对账", "paragraph_index": 2,
         "issue_type": "unsupported_fusion",
         "explanation": "两个核算中心被合并。",
         "repair_instruction": f"拆成两段并完整保留‘{identity}’；不得虚构共享机箱产品层。"},
        {"section": "直接排放、废物与监测指标边界", "paragraph_index": 1,
         "issue_type": "claim_dump", "explanation": "两个测量中心被合并。",
         "repair_instruction": "拆分废物核算与装配用电测量，分别成段。"},
    ]}
    bound = prepare_legacy_patch_review(draft, review)
    assert [issue["operation"] for issue in bound["issues"]] == [
        "split_replace", "split_replace",
    ]
    assert identity in bound["issues"][0]["tokens_must_preserve"]
    repairs = [
        {"issue_id": "E001", "section_id": "投入产出与脊边对账", "paragraph_id": "p2",
         "target_hash": bound["issues"][0]["target_hash"], "preserved_claim_ids": ["A039-57"],
         "replacements": [
             {"focus": "共享资源只按可归属记录分配", "sentences": [
                 {"text": f"{identity} 是冻结图中的节点投入，不代表共享机箱产品层。",
                  "claim_kind": "internal_graph_fact", "rhetorical_role": "thesis",
                  "evidence_claim_ids": ["A039-57"]},
             ]},
             {"focus": "退料与不良品单独闭合", "sentences": [
                 {"text": "退料与不良品应在同一批次质量账中闭合，不能混入共享资源分配。",
                  "claim_kind": "modeling_judgment", "rhetorical_role": "thesis",
                  "evidence_claim_ids": []},
             ]},
         ]},
        {"issue_id": "E002", "section_id": "直接排放、废物与监测指标边界",
         "paragraph_id": "p1", "target_hash": bound["issues"][1]["target_hash"],
         "preserved_claim_ids": [], "replacements": [
             {"focus": "废物流按质量和去向核算", "sentences": [
                 {"text": "废物流以批次质量和接收去向记录形成单独的质量核算链。",
                  "claim_kind": "modeling_judgment", "rhetorical_role": "thesis",
                  "evidence_claim_ids": []},
             ]},
             {"focus": "装配用电按可归属电表测量", "sentences": [
                 {"text": "装配用电只有可归属到A039时才作为节点测量，否则仅作筛查。",
                  "claim_kind": "modeling_judgment", "rhetorical_role": "thesis",
                  "evidence_claim_ids": []},
             ]},
         ]},
    ]
    before = legacy_paragraph_manifest(draft)
    result, receipt = apply_legacy_repairs(draft, bound, repairs)

    assert [row["focus"] for row in result["sections"][0]["paragraphs"]] == [
        "未点名的资源边界", "共享资源只按可归属记录分配", "退料与不良品单独闭合", "未点名的质量说明",
    ]
    assert receipt["unchanged_paragraphs"] == {
        "投入产出与脊边对账.p1": before["投入产出与脊边对账.p1"],
        "投入产出与脊边对账.p3": before["投入产出与脊边对账.p3"],
    }
    assert {change["paragraph"]: [item["paragraph_id"] for item in change["after"]]
            for change in receipt["paragraph_changes"]} == {
        "投入产出与脊边对账.p2": ["p2", "p2.split2"],
        "直接排放、废物与监测指标边界.p1": ["p1", "p1.split2"],
    }
    repairs[0]["replacements"][0]["sentences"][0]["text"] = "节点图将P057作为投入。"
    with pytest.raises(EditorialPatchError, match="paragraph-local tokens"):
        apply_legacy_repairs(draft, bound, repairs)


def test_legacy_preservation_tokens_split_ideographic_identity_lists_atomically() -> None:
    identities = [
        "P022 交换机主板PCBA", "P046 光模块", "P029 PSU电源模组",
        "P055 散热器", "P057 钢钣金机箱/导轨, 服务器用",
    ]
    draft = {"protocol": "wiki-content-draft-v2", "node_id": "A013", "sections": [{
        "heading": "投入边界", "paragraphs": [{
            "focus": "冻结图投入清单", "sentences": [{
                "text": f"冻结图投入包括{'、'.join(identities)}。",
                "claim_kind": "internal_graph_fact", "rhetorical_role": "thesis",
                "evidence_claim_ids": [],
            }],
        }],
    }]}
    review = {"protocol": "wiki-editorial-review-v1", "node_id": "A013",
              "verdict": "NO_GO", "issues": [{
                  "section": "投入边界", "paragraph_index": 1, "issue_type": "local",
                  "explanation": "规范化投入名称。", "repair_instruction": "保持所有冻结图投入标识。",
              }]}

    tokens = prepare_legacy_patch_review(draft, review)["issues"][0]["tokens_must_preserve"]

    assert all(identity in tokens for identity in identities)
    assert all(identity.split()[0] in tokens for identity in identities)
    assert not [token for token in tokens
                if len(re.findall(r"(?<![A-Za-z0-9])[AP]\d{3}(?!\d)", token)) > 1]


def test_legacy_correction_requires_replacement_identifier_not_superseded_identifier() -> None:
    draft = {"protocol": "wiki-content-draft-v2", "node_id": "A013", "sections": [{
        "heading": "定义", "paragraphs": [{
            "focus": "节点标识纠错", "sentences": [{
                "text": "原段误将当前活动写成A039。", "claim_kind": "modeling_judgment",
                "rhetorical_role": "thesis", "evidence_claim_ids": [],
            }],
        }],
    }]}
    review = {"protocol": "wiki-editorial-review-v1", "node_id": "A013",
              "verdict": "NO_GO", "issues": [{
                  "section": "定义", "paragraph_index": 1, "issue_type": "identity_drift",
                  "explanation": "节点标识错误。", "repair_instruction": "将A039更正为A013。",
              }]}

    tokens = prepare_legacy_patch_review(draft, review)["issues"][0]["tokens_must_preserve"]

    assert "A013" in tokens
    assert "A039" not in tokens


def test_a013_canonical_label_instruction_replaces_legacy_shorthand_tokens() -> None:
    draft = {"protocol": "wiki-content-draft-v2", "node_id": "A013", "sections": [{
        "heading": "投入产出与脊边对账", "paragraphs": [{
            "focus": "全部输入至总装的映射", "sentences": [{
                "text": (
                    "组件包括P022 交换机主板PCBA、P038 ASIC和P064 塑料件；"
                    "同时计入P038 ASIC与P022 交换机主板PCBA前应核验BOM。"
                ),
                "claim_kind": "internal_graph_fact", "rhetorical_role": "thesis",
                "evidence_claim_ids": [],
            }],
        }],
    }]}
    review = {"protocol": "wiki-editorial-review-v1", "node_id": "A013",
              "verdict": "NO_GO", "issues": [{
                  "section": "投入产出与脊边对账", "paragraph_index": 1,
                  "issue_type": "claim_dump+identity_drift",
                  "explanation": "旧简称与规范流名冲突。",
                  "repair_instruction": (
                      "拆成完整流对账和BOM核验两个段落；统一使用图谱完整名称，例如"
                      "“P022 交换机主板PCBA, 100G/400G”和"
                      "“P038 交换ASIC封装器件, 100G/400G”。"
                  ),
              }]}

    bound = prepare_legacy_patch_review(draft, review)
    issue = bound["issues"][0]
    tokens = issue["tokens_must_preserve"]

    assert "P022 交换机主板PCBA, 100G/400G" in tokens
    assert "P038 交换ASIC封装器件, 100G/400G" in tokens
    assert "P022 交换机主板PCBA" not in tokens
    assert "P038 ASIC" not in tokens
    assert not [token for token in tokens if token.endswith(("和", "与", "”", "“"))]

    repairs = [{
        "issue_id": issue["issue_id"], "section_id": issue["section_id"],
        "paragraph_id": issue["paragraph_id"], "target_hash": issue["target_hash"],
        "preserved_claim_ids": [], "replacements": [{
            "focus": "完整规范流名对账", "sentences": [{
                "text": (
                    "A013将P022 交换机主板PCBA, 100G/400G、"
                    "P038 交换ASIC封装器件, 100G/400G和P064 塑料件映射为输入。"
                ),
                "claim_kind": "internal_graph_fact", "rhetorical_role": "thesis",
                "evidence_claim_ids": [],
            }],
        }, {
            "focus": "BOM重复计量核验", "sentences": [{
                "text": "同时计入P022与P038前必须核验型号级BOM，不能确认时修正输入流。",
                "claim_kind": "modeling_judgment", "rhetorical_role": "thesis",
                "evidence_claim_ids": [],
            }],
        }],
    }]

    result, receipt = apply_legacy_repairs(draft, bound, repairs)

    assert len(result["sections"][0]["paragraphs"]) == 2
    assert receipt["targeted_paragraphs"] == ["投入产出与脊边对账.p1"]


def test_v3_split_assigns_deterministic_ids_and_preserves_untargeted_hashes() -> None:
    blueprint, draft, review, repair = fixtures()
    review["issues"][0]["operation"] = "split_replace"
    review["issues"][0]["tokens_must_preserve"] = ["完整事实"]
    original = repair.pop("replacement")
    repair["replacements"] = [original, {
        "focus": "第二个独立中心",
        "sentences": [{"text": "第二段继续说明完整事实并维持同一冻结证据。",
                       "claim_kind": "modeling_judgment", "rhetorical_role": "thesis",
                       "evidence_claim_ids": ["c3"]}],
    }]
    before = paragraph_manifest(draft)
    result, receipt = apply_repairs(draft, blueprint, review, [repair])
    after = paragraph_manifest(result)

    assert [row["paragraph_id"] for row in result["sections"][2]["paragraphs"]] == [
        "p1", "p1.split2",
    ]
    assert receipt["unchanged_paragraphs"] == {
        key: digest for key, digest in before.items() if key != "s3.p1"
    }
    assert after["s3.p1.split2"] == receipt["paragraph_changes"][0]["after"][1]["sha256"]


def test_legacy_patch_filters_mixed_claim_kinds_inside_target_only() -> None:
    repairs = [{"issue_id": "E1", "preserved_claim_ids": ["A040-16", "A040-17"],
                "replacement": {"focus": "冻结边与BOM证据的区分", "sentences": [
                    {"text": "冻结图中存在该输入边。", "claim_kind": "internal_graph_fact",
                     "rhetorical_role": "thesis", "evidence_claim_ids": ["A040-16", "A040-17"]},
                    {"text": "该边仍需型号级BOM证据。", "claim_kind": "modeling_judgment",
                     "rhetorical_role": "boundary", "evidence_claim_ids": ["A040-17"]},
                ]}}]
    rows = [
        {"claim": {"claim_id": "A040-16", "claim_kind": "internal_graph_fact"}},
        {"claim": {"claim_id": "A040-17", "claim_kind": "modeling_judgment"}},
    ]

    normalized = normalize_legacy_repair_claim_bindings(repairs, rows)

    sentences = normalized[0]["replacement"]["sentences"]
    assert sentences[0]["evidence_claim_ids"] == ["A040-16"]
    assert sentences[1]["evidence_claim_ids"] == ["A040-17"]
    assert normalized[0]["preserved_claim_ids"] == ["A040-16", "A040-17"]
    assert repairs[0]["replacement"]["sentences"][0]["evidence_claim_ids"] == [
        "A040-16", "A040-17",
    ]


def test_legacy_patch_caps_claim_uses_against_untouched_paragraphs() -> None:
    document = {"sections": [{"heading": "定义", "paragraphs": [
        {"sentences": [{"evidence_claim_ids": ["A040-24"]}]},
        {"sentences": [{"evidence_claim_ids": ["A040-24"]}]},
        {"sentences": [{"evidence_claim_ids": []}]},
    ]}]}
    repairs = [{"issue_id": "E1", "section_id": "定义", "paragraph_id": "p3",
                "preserved_claim_ids": ["A040-24"], "replacement": {
                    "focus": "目标段落", "sentences": [
                        {"text": "第一句", "claim_kind": "modeling_judgment",
                         "evidence_claim_ids": ["A040-24"]},
                        {"text": "第二句", "claim_kind": "modeling_judgment",
                         "evidence_claim_ids": ["A040-24"]},
                    ],
                }}]
    rows = [{"claim": {"claim_id": "A040-24", "claim_kind": "modeling_judgment"}}]

    normalized = normalize_legacy_repair_claim_bindings(repairs, rows, document)

    sentences = normalized[0]["replacement"]["sentences"]
    assert sentences[0]["evidence_claim_ids"] == ["A040-24"]
    assert sentences[1]["evidence_claim_ids"] == []


def test_legacy_patch_reserves_internal_graph_fact_binding_before_optional_citations() -> None:
    document = {"sections": [{"heading": "定义", "paragraphs": [
        {"sentences": [{"evidence_claim_ids": ["A039-16"]}]},
        {"sentences": [{"evidence_claim_ids": ["A039-16"]}]},
        {"sentences": [{"evidence_claim_ids": []}]},
        {"sentences": [{"evidence_claim_ids": []}]},
    ]}]}
    repairs = [
        {"issue_id": "E001", "section_id": "定义", "paragraph_id": "p3",
         "preserved_claim_ids": ["A039-16"], "replacement": {
             "focus": "可选建模说明", "sentences": [{
                 "text": "该冻结输入也可辅助说明建模边界。",
                 "claim_kind": "modeling_judgment", "evidence_claim_ids": ["A039-16"],
             }],
         }},
        {"issue_id": "E002", "section_id": "定义", "paragraph_id": "p4",
         "preserved_claim_ids": ["A039-16", "A039-6"], "replacement": {
             "focus": "冻结图事实", "sentences": [{
                 "text": "冻结图记录这些投入边和对应输出边。",
                 "claim_kind": "internal_graph_fact",
                 "evidence_claim_ids": ["A039-16", "A039-6"],
             }],
         }},
    ]
    rows = [
        {"claim": {"claim_id": "A039-16", "claim_kind": "internal_graph_fact"}},
        {"claim": {"claim_id": "A039-6", "claim_kind": "modeling_judgment"}},
    ]

    normalized = normalize_legacy_repair_claim_bindings(repairs, rows, document)

    assert normalized[0]["replacement"]["sentences"][0]["evidence_claim_ids"] == []
    assert normalized[1]["replacement"]["sentences"][0]["evidence_claim_ids"] == ["A039-16"]
    all_ids = [
        claim_id
        for paragraph in document["sections"][0]["paragraphs"][:2]
        for sentence in paragraph["sentences"]
        for claim_id in sentence["evidence_claim_ids"]
    ] + [
        claim_id
        for repair in normalized
        for sentence in repair["replacement"]["sentences"]
        for claim_id in sentence["evidence_claim_ids"]
    ]
    assert Counter(all_ids)["A039-16"] == 3


def test_legacy_patch_fails_closed_when_required_fact_binding_has_no_capacity() -> None:
    document = {"sections": [{"heading": "定义", "paragraphs": [
        {"sentences": [{"evidence_claim_ids": ["A039-16"]}]},
        {"sentences": [{"evidence_claim_ids": ["A039-16"]}]},
        {"sentences": [{"evidence_claim_ids": ["A039-16"]}]},
        {"sentences": [{"evidence_claim_ids": []}]},
    ]}]}
    repairs = [{
        "issue_id": "E002", "section_id": "定义", "paragraph_id": "p4",
        "preserved_claim_ids": ["A039-16"], "replacement": {
            "focus": "冻结图事实", "sentences": [{
                "text": "冻结图记录这些投入边和对应输出边。",
                "claim_kind": "internal_graph_fact", "evidence_claim_ids": ["A039-16"],
            }],
        },
    }]
    rows = [{"claim": {"claim_id": "A039-16", "claim_kind": "internal_graph_fact"}}]

    with pytest.raises(EditorialPatchError, match="required fact binding"):
        normalize_legacy_repair_claim_bindings(repairs, rows, document)


def test_editorial_patch_proof_metrics_report_cap_and_graph_bindings() -> None:
    document = {"sections": [{"paragraphs": [{"sentences": [
        {"claim_kind": "internal_graph_fact", "evidence_claim_ids": ["A039-16"]},
        {"claim_kind": "modeling_judgment", "evidence_claim_ids": ["A039-16"]},
        {"claim_kind": "internal_graph_fact", "evidence_claim_ids": ["A039-16"]},
    ]}]}]}

    assert claim_binding_metrics(document) == {
        "internal_graph_fact_sentences_without_evidence": 0,
        "maximum_claim_use_count": 3,
        "bound_internal_graph_fact_sentences_by_claim_id": {"A039-16": 2},
    }
