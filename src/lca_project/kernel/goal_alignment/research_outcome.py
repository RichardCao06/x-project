"""Evaluate whether a completed research flow produced useful LCA evidence."""
from __future__ import annotations

import re
from typing import Any


_INTERNAL_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9])[AP]\d{3}(?!\d)", re.IGNORECASE)
_CJK = re.compile(r"[\u3400-\u9fff]")


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class ResearchOutcomeEvaluator:
    """Build a compact, deterministic goal-progress assessment.

    Passing a research protocol proves that the protocol ran honestly.  It does
    not prove that the result contains data that can move LCA modelling forward.
    This evaluator deliberately measures both facts independently.
    """

    def evaluate(self, docs: dict[str, dict[str, Any]], *,
                 task_completion: dict[str, Any] | None = None) -> dict[str, Any]:
        maturity = _mapping(docs.get("maturity"))
        selection = _mapping(docs.get("table_selection"))
        matrix = _mapping(docs.get("table_matrix"))
        diversity = _mapping(docs.get("source_diversity"))
        counts = _mapping(selection.get("counts"))
        reason_counts = _mapping(selection.get("reason_counts"))
        queries = [_mapping(item) for item in _items(matrix.get("queries"))]
        results = [
            _mapping(result)
            for query in queries
            for result in _items(query.get("results"))
        ]
        audits = [_mapping(item) for item in _items(selection.get("candidate_audits"))]
        accepted = _items(selection.get("accepted_evidence"))
        observations = sum(len(_items(item.get("observations"))) for item in audits)
        fetched = sum(1 for item in results if item.get("fetch_status") == "fetched")
        internal_queries = sum(
            1 for item in queries if _INTERNAL_IDENTIFIER.search(str(item.get("query") or ""))
        )
        mixed_english_queries = sum(
            1 for item in queries
            if str(item.get("language") or "").lower().startswith("en")
            and _CJK.search(str(item.get("query") or ""))
        )
        routes = len(_items(matrix.get("document_routes")))
        target_fields = _integer(counts.get("fields"))
        populated_fields = _integer(counts.get("populated"))
        accepted_observations = len(accepted)
        confirmed_sources = _integer(
            _mapping(diversity.get("metrics")).get("confirmed_urls")
        )
        data_readiness = str(maturity.get("data_readiness") or "")
        maturity_current = bool(maturity)
        workflow_finished = bool(maturity)
        if task_completion is not None:
            task_states = _mapping(task_completion.get("tasks"))
            maturity_current = (
                bool(maturity) and task_states.get("maturity_gate") == "succeeded"
            )
            workflow_finished = (
                maturity_current and task_completion.get("run_status") == "succeeded"
            )
        evaluated = maturity_current and bool(
            data_readiness or maturity.get("candidate_eligible") is not None
        )
        modelling_progress = bool(
            maturity.get("candidate_eligible") is True
            or data_readiness == "data_ready"
            or populated_fields > 0
            or accepted_observations > 0
        )

        reasons: list[str] = []
        explanations: list[str] = []
        changes: list[dict[str, str]] = []
        if evaluated and target_fields > 0 and accepted_observations == 0:
            reasons.append("ZERO_ACCEPTED_FIELD_EVIDENCE")
            explanations.append(
                f"{target_fields} 个目标字段中没有任何字段级证据被采纳。"
            )
        if evaluated and len(results) >= 20 and accepted_observations == 0:
            reasons.append("HIGH_VOLUME_ZERO_YIELD")
            explanations.append(
                f"检索返回 {len(results)} 个候选、抓取 {fetched} 个页面，但采纳证据仍为 0；"
                "瓶颈不再是是否执行检索，而是查询相关性或字段抽取能力。"
            )
        if internal_queries:
            reasons.append("INTERNAL_IDENTIFIER_QUERY_LEAKAGE")
            explanations.append(
                f"{internal_queries} 条查询包含 A039/P018 一类内部节点编号，外部资料通常不使用这些编号。"
            )
            changes.append({
                "causal_input": "query_builder.internal_identifiers",
                "change": "将内部节点编号替换为外部行业术语、部件同义词和产品/工艺上下文。",
                "expected_effect": "提高搜索结果与目标字段的语义相关性。",
            })
        if mixed_english_queries:
            reasons.append("MIXED_LANGUAGE_ENGLISH_QUERY")
            explanations.append(
                f"{mixed_english_queries} 条英语查询仍夹杂中文字段名，降低英文来源召回质量。"
            )
            changes.append({
                "causal_input": "query_builder.language_normalization",
                "change": "在执行英语检索前完整翻译字段名、部件名和工艺名，并校验无中文残留。",
                "expected_effect": "增加英文 EPD、PCF、规格书和技术报告的有效召回。",
            })
        if evaluated and target_fields > 0 and routes == 0:
            reasons.append("MISSING_DOCUMENT_ROUTES")
            explanations.append("没有生成面向 EPD、PCF、规格书、拆解/BOM 或 LCA 数据集的文档路线。")
            changes.append({
                "causal_input": "research_plan.document_routes",
                "change": "按证据角色建立域名、文档类型和字段族定向检索路线。",
                "expected_effect": "让检索从泛网页命中转向可引用、可抽取的技术文件。",
            })
        if evaluated and fetched > 0 and observations == 0:
            reasons.append("FIELD_EXTRACTION_ZERO_YIELD")
            explanations.append(
                f"已抓取 {fetched} 个页面，但字段抽取器产出 0 条字段级观测。"
            )
            changes.append({
                "causal_input": "table_extractor.field_observation_coverage",
                "change": "增加 HTML/PDF 表格、单位、数值区间、BOM 和能耗字段的抽取与映射能力。",
                "expected_effect": "把已抓取文档转化为可审核的字段级证据，而不是只保留网页文本。",
            })
        if evaluated and confirmed_sources == 0:
            reasons.append("NO_CONFIRMED_EXTERNAL_SOURCES")
            explanations.append("没有任何外部来源通过确认，页面内容无法形成可追溯引用链。")
        if evaluated and target_fields > 0 and populated_fields == 0:
            reasons.append("ZERO_MODEL_FIELDS_POPULATED")
            explanations.append(f"{target_fields} 个 LCA 建模目标字段的实际填充数为 0。")

        if reasons and not changes:
            changes.append({
                "causal_input": "research_strategy",
                "change": "由只读诊断 Agent 检查查询、来源确认、抽取和字段映射之间的首个零产出环节。",
                "expected_effect": "定位并改变造成零产出的真实输入，而不是重复相同检索。",
            })

        metrics = {
            "queries_executed": len(queries),
            "candidate_results": len(results),
            "pages_fetched": fetched,
            "document_routes": routes,
            "internal_identifier_queries": internal_queries,
            "mixed_language_english_queries": mixed_english_queries,
            "target_fields": target_fields,
            "field_observations": observations,
            "accepted_observations": accepted_observations,
            "populated_fields": populated_fields,
            "confirmed_sources": confirmed_sources,
            "explicit_gaps": _integer(counts.get("explicit_gaps")),
            "fields_requiring_internal_records": _integer(
                reason_counts.get("field_requires_node_specific_internal_record")
            ),
        }
        proof = [
            {"metric": "accepted_observations", "baseline": accepted_observations,
             "target": ">0", "evidence_artifact": "table-data/evidence-selection.json"},
            {"metric": "populated_fields", "baseline": populated_fields,
             "target": ">0", "evidence_artifact": "table-data/evidence-selection.json"},
            {"metric": "confirmed_sources", "baseline": confirmed_sources,
             "target": ">0", "evidence_artifact": "source-diversity-gate.json"},
        ]
        if internal_queries:
            proof.append({"metric": "internal_identifier_queries", "baseline": internal_queries,
                          "target": "0", "evidence_artifact": "table-data/search-matrix.executed.json"})
        if mixed_english_queries:
            proof.append({"metric": "mixed_language_english_queries",
                          "baseline": mixed_english_queries, "target": "0",
                          "evidence_artifact": "table-data/search-matrix.executed.json"})

        return {
            "schema_version": "research-outcome-assessment-v1",
            "evaluated": evaluated,
            "workflow_finished": workflow_finished,
            "closer_to_modelling_goal": modelling_progress,
            "needs_investigation": bool(evaluated and target_fields > 0 and not modelling_progress),
            "process_integrity": {
                "table_contract_passed": bool(docs.get("table_verdict"))
                and str(docs["table_verdict"].get("verdict") or "").upper() == "PASS",
                "gap_provenance_preserved": _mapping(maturity.get("checks")).get(
                    "explicit_gaps_have_search_provenance"
                ) is True,
            },
            "metrics": metrics,
            "reason_codes": reasons,
            "why_not_closer": explanations,
            "next_causal_input_changes": changes,
            "proof_contract": proof,
        }
