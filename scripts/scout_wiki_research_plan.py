#!/usr/bin/env python3
"""Discover real source identities before Wiki claim nomination."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from wiki_research_contract import iter_query_tasks

# Longest phrases win.  This glossary is deliberately limited to discovery
# terminology used by the ICT/LCA node catalogue.  Its output is never treated
# as evidence that a Chinese and English canonical name are equivalent.
ZH_EN_SEARCH_GLOSSARY = {
    "AI训练GPU服务器用": "for AI training GPU servers",
    "通用服务器用": "for general-purpose servers",
    "数据中心用": "for data centers",
    "消费级独立显卡": "consumer discrete graphics card",
    "企业级固态硬盘": "enterprise solid-state drive",
    "企业级机械硬盘": "enterprise hard disk drive",
    "不间断电源": "uninterruptible power supply",
    "印刷电路板组件": "printed circuit board assembly",
    "主板PCBA": "motherboard PCBA",
    "网络交换机": "network switch",
    "笔记本电脑": "laptop computer",
    "笔记本": "laptop computer",
    "存储阵列": "storage array",
    "光收发模块": "optical transceiver module",
    "系统集成": "system integration",
    "整机总装": "final system assembly",
    "通用计算": "general-purpose computing",
    "刀片式": "blade form factor",
    "机架PDU": "rack-mounted power distribution unit",
    "机架式": "rack-mounted",
    "机架": "rack-mounted",
    "PDU": "power distribution unit",
    "固件烧录": "firmware programming",
    "烧录测试": "firmware programming and testing",
    "压力老化": "stress burn-in testing",
    "配置出厂": "factory configuration",
    "回流焊接": "reflow soldering",
    "波峰焊接": "wave soldering",
    "选择性焊接": "selective soldering",
    "SMT贴装": "SMT assembly",
    "表面贴装": "surface mount assembly",
    "服务器": "server",
    "主板": "motherboard",
    "显卡": "graphics card",
    "机柜": "server rack",
    "电源": "power supply",
    "硬盘": "disk drive",
    "贴装": "assembly",
    "焊接": "soldering",
    "测试": "testing",
    "制造": "manufacturing",
    "组装": "assembly",
    "生产": "production",
}

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
SEPARATOR_RE = re.compile(r"[\s,，;；|/、·:：()（）\[\]【】]+")

QUERY_FOCUS = {
    "zh": {
        "identity_and_terminology": "生产装配线 制造 测试",
        "process_origin_and_boundary": "生产工艺 机箱装配 系统集成 测试",
        "collection_and_handoff": "存储服务器 整机装配 测试 交付",
        "composition_and_quantity": "机箱 主板 电源 硬盘 装配",
        "recovery_and_destination": "服务器 制造项目 环评 总装 工艺",
        "representativeness_and_quality": "工厂 质量控制 老化测试 出厂检验",
    },
    "en": {
        "identity_and_terminology": "manufacturing process final assembly",
        "process_origin_and_boundary": "final assembly integration testing manufacturing boundary",
        "collection_and_handoff": "factory assembly testing inspection shipment",
        "composition_and_quantity": "system chassis assembly integration drives",
        "recovery_and_destination": "manufacturing environmental report process emissions",
        "representativeness_and_quality": "factory quality control burn-in final testing",
    },
}


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _unique_text(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def translate_zh_search_terms(
    values: list[Any], *, overrides: dict[str, str] | None = None
) -> dict[str, Any]:
    """Translate known Chinese discovery terms into an auditable English seed."""
    source_terms = _unique_text(values)
    translated_terms: list[str] = []
    unmatched_fragments: list[str] = []
    matched_phrases: list[dict[str, str]] = []
    effective_glossary = dict(ZH_EN_SEARCH_GLOSSARY)
    for zh, en in (overrides or {}).items():
        zh_text, en_text = str(zh or "").strip(), str(en or "").strip()
        if zh_text and en_text and not CJK_RE.search(en_text):
            effective_glossary[zh_text] = en_text

    for source in source_terms:
        translated = source
        for zh, en in sorted(effective_glossary.items(), key=lambda item: len(item[0]), reverse=True):
            if zh not in translated:
                continue
            translated = translated.replace(zh, f" {en} ")
            pair = {"zh": zh, "en": en}
            if pair not in matched_phrases:
                matched_phrases.append(pair)

        remaining = _unique_text(CJK_RE.findall(translated))
        for fragment in remaining:
            if fragment not in unmatched_fragments:
                unmatched_fragments.append(fragment)
        english_only = CJK_RE.sub(" ", translated)
        english_only = SEPARATOR_RE.sub(" ", english_only).strip(" .-_\t\n")
        if english_only and english_only not in translated_terms:
            translated_terms.append(english_only)

    if translated_terms and not unmatched_fragments:
        method = ("deterministic_technical_glossary_with_l1_override"
                  if overrides else "deterministic_technical_glossary")
    elif translated_terms:
        method = "deterministic_technical_glossary_partial"
    else:
        # Continue instead of producing an empty English query.  The Chinese
        # seed is retained as a bilingual passthrough and visibly audited.
        translated_terms = source_terms[:]
        method = "bilingual_passthrough_no_glossary_match"

    return {
        "source_language": "zh",
        "target_language": "en",
        "source_terms": source_terms,
        "translated_terms": translated_terms,
        "method": method,
        "matched_phrases": matched_phrases,
        "unmatched_fragments": unmatched_fragments,
        "authority": "discovery_only",
        "identity_authorized": False,
    }


def build_query(
    terminology: dict[str, Any], language: str, research_question: str | dict[str, Any]
) -> dict[str, Any]:
    task = research_question if isinstance(research_question, dict) else None
    dimension = str((task or {}).get("dimension") or research_question)
    question_id = str((task or {}).get("question_id") or dimension)
    intent = (task or {}).get("intent") or {}
    canonical_zh = str(terminology.get("canonical_zh") or "").strip()
    zh_terms = _unique_text([
        canonical_zh,
        *terminology.get("candidate_aliases_zh", []),
    ])
    # Activity display names use ``activity | reference product``.  Searching
    # the entire label overweights generic words such as 系统集成 and previously
    # returned product-adjacent sources.  Search the product half while the
    # language-specific focus phrase supplies the manufacturing activity.
    zh_discovery_terms = zh_terms
    if "|" in canonical_zh and any(
        term in canonical_zh.split("|", 1)[0] for term in ("系统集成", "整机总装")
    ):
        product_term = canonical_zh.split("|", 1)[1].strip()
        zh_discovery_terms = _unique_text([
            product_term,
            *terminology.get("candidate_aliases_zh", []),
        ])
    en_terms = _unique_text([
        terminology.get("canonical_en"),
        *terminology.get("candidate_aliases_en", []),
    ])

    if language == "zh":
        effective_terms = zh_discovery_terms
        translation = {
            "source_language": "zh",
            "target_language": "zh",
            "source_terms": zh_discovery_terms,
            "translated_terms": zh_discovery_terms,
            "method": "declared_chinese_terminology",
            "matched_phrases": [],
            "unmatched_fragments": [],
            "authority": "discovery_only",
            "identity_authorized": False,
        }
    elif en_terms:
        effective_terms = en_terms
        translation = {
            "source_language": "en",
            "target_language": "en",
            "source_terms": en_terms,
            "translated_terms": en_terms,
            "method": "declared_english_terminology",
            "matched_phrases": [],
            "unmatched_fragments": [],
            "authority": "discovery_only",
            "identity_authorized": False,
        }
    else:
        translation = translate_zh_search_terms(zh_discovery_terms)
        effective_terms = translation["translated_terms"]
        if (translation["method"] == "bilingual_passthrough_no_glossary_match"
                or any(CJK_RE.search(str(term)) for term in effective_terms)):
            # Do not label a Chinese passthrough as an English query.  The
            # research-question/focus terms still provide a valid broad English
            # discovery route, while the missing translation remains auditable.
            effective_terms = []
            translation = {
                **translation,
                "query_fallback": "english_research_question_and_focus_only",
            }

    question_text = str(
        ((task or {}).get("question") or {}).get(language)
        or dimension.replace("_", " ")
    ).strip()
    if task:
        focus = " ".join(
            str(value).strip()
            for value in ((intent.get("seed_terms") or {}).get(language) or [])
            if str(value).strip()
        )
    else:
        focus = (QUERY_FOCUS.get(language, {}).get(dimension, "")
                 if "|" in canonical_zh else "")
    # English research-question labels dilute Chinese technical searches and
    # previously pushed activity jobs toward product marketing pages.  The
    # auditable focus phrase carries the same intent in the query language.
    query_parts = [*effective_terms, focus]
    if language == "en" and not task:
        query_parts.insert(len(effective_terms), question_text)
    query = " ".join(query_parts).strip()
    return {
        "research_question": dimension,
        "question_id": question_id,
        "question_text": question_text,
        "intent_id": str(intent.get("intent_id") or f"legacy.{dimension}"),
        "criticality": str((task or {}).get("criticality") or "legacy"),
        "preferred_source_roles": intent.get("preferred_source_roles") or [],
        "preferred_source_classes": intent.get("preferred_source_classes") or [],
        "language": language,
        "query": query,
        "effective_terms": effective_terms,
        "translation": translation,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--repair-gate", type=Path)
    parser.add_argument("--previous-scout", type=Path)
    parser.add_argument("--failed-fetch-dir", type=Path)
    args = parser.parse_args()

    plan, config = load(args.plan), load(args.config)
    spec = importlib.util.spec_from_file_location(
        "search_provider_runtime", ROOT / "scripts/search_provider_runtime.py"
    )
    assert spec and spec.loader
    provider = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provider)

    secret_path = args.config.resolve().parents[1] / str(config.get("secret_file", ".env.search.local"))
    secrets = provider.load_secrets(secret_path)
    providers = config.get("providers") or {}
    routing = config.get("routing") or {}
    terminology = plan.get("terminology") or {}
    repair_gate = load(args.repair_gate) if args.repair_gate else {}
    previous_scout = load(args.previous_scout) if args.previous_scout else {}
    failed_question_ids = {
        str(value) for value in repair_gate.get("failed_requirement_ids") or []
        if str(value).strip()
    }
    previous_candidates = [
        item for item in previous_scout.get("candidates") or [] if isinstance(item, dict)
    ]
    previous_candidate_count = len(previous_candidates)
    failed_urls: set[str] = set()
    if args.failed_fetch_dir and args.failed_fetch_dir.is_dir():
        for record_path in sorted(args.failed_fetch_dir.glob("*.json")):
            try:
                record = (load(record_path).get("record") or {})
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if record.get("status") == "fetched":
                continue
            url = str(record.get("url") or "").strip()
            if url:
                failed_urls.add(url)
    excluded_url_hashes = {
        hashlib.sha256(url.encode("utf-8")).hexdigest() for url in failed_urls
    }
    previous_candidates = [
        item for item in previous_candidates
        if str(item.get("url") or "").strip() not in failed_urls
        and (not failed_question_ids
             or str(item.get("question_id") or "") not in failed_question_ids)
    ]
    candidates: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    query_audit: list[dict[str, Any]] = []
    seen: set[str] = failed_urls | {
        str(item.get("url")) for item in previous_candidates if item.get("url")
    }
    max_per_question = 5
    max_per_provider = 2

    all_tasks = iter_query_tasks(plan)
    if args.repair_gate:
        target_priority = max(2, int(repair_gate.get("attempt") or 0) + 2)
        available_priorities = sorted({
            int((task.get("intent") or {}).get("priority") or 1)
            for task in all_tasks
            if not failed_question_ids or str(task.get("question_id") or "") in failed_question_ids
        })
        selected_priority = next(
            (value for value in available_priorities if value >= target_priority),
            available_priorities[-1] if available_priorities else 1,
        )
        tasks = [
            task for task in all_tasks
            if (not failed_question_ids or str(task.get("question_id") or "") in failed_question_ids)
            and int((task.get("intent") or {}).get("priority") or 1) == selected_priority
        ]
    else:
        selected_priority = 1
        tasks = [
            task for task in all_tasks
            if int((task.get("intent") or {}).get("priority") or 1) == 1
        ]
    for task in tasks:
        dimension = str(task.get("dimension") or task.get("question_id") or "")
        languages = (["zh", "en"] if task.get("criticality") == "required_for_model"
                     else ["zh"])
        if task.get("criticality") == "legacy":
            languages = ["zh"]
        for language in languages:
            query_record = build_query(terminology, language, task)
            query_audit.append(query_record)
            query = query_record["query"]
            for provider_name in routing.get(language, routing.get("technical", [])):
                cfg = providers.get(provider_name) or {}
                if not cfg.get("enabled", False):
                    continue
                attempt_base = {
                    "research_question": dimension,
                    "question_id": query_record["question_id"],
                    "intent_id": query_record["intent_id"],
                    "criticality": query_record["criticality"],
                    "preferred_source_roles": query_record["preferred_source_roles"],
                    "preferred_source_classes": query_record["preferred_source_classes"],
                    "language": language,
                    "provider": provider_name,
                    "query": query,
                    "effective_terms": query_record["effective_terms"],
                    "translation": query_record["translation"],
                }
                try:
                    hits, status = provider.provider_search(
                        provider_name,
                        cfg,
                        query,
                        locator=query_record["intent_id"],
                        secrets=secrets,
                        timeout=30,
                        limit=5,
                    )
                    attempts.append({**attempt_base, "status": status, "results": len(hits)})
                    provider_added = 0
                    for hit in hits:
                        url = str(hit.get("url") or "").strip()
                        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
                        if (not url or url in failed_urls or url_hash in excluded_url_hashes
                                or url in seen):
                            continue
                        seen.add(url)
                        candidates.append({
                            **hit,
                            "url": url,
                            "research_question": dimension,
                            "question_id": query_record["question_id"],
                            "intent_id": query_record["intent_id"],
                            "criticality": query_record["criticality"],
                            "preferred_source_roles": query_record["preferred_source_roles"],
                            "preferred_source_classes": query_record["preferred_source_classes"],
                            "language": language,
                            "query": query,
                            "translation_method": query_record["translation"]["method"],
                            "current_job_status": "candidate_unverified",
                            "repair_novel": bool(args.repair_gate),
                        })
                        provider_added += 1
                        if sum(x.get("question_id") == query_record["question_id"] for x in candidates) >= max_per_question:
                            break
                        if provider_added >= max_per_provider:
                            break
                    if sum(x.get("question_id") == query_record["question_id"] for x in candidates) >= max_per_question:
                        break
                except Exception as exc:
                    attempts.append({
                        **attempt_base,
                        "status": "provider_error",
                        "results": 0,
                        "error": {"code": type(exc).__name__, "message": str(exc)},
                    })

    if args.repair_gate:
        candidates.extend(previous_candidates)
    strategy_signal = {
        "failed_question_ids": sorted(failed_question_ids),
        "selected_intent_priority": selected_priority,
        "queries": [
            {"question_id": item.get("question_id"), "intent_id": item.get("intent_id"),
             "language": item.get("language"), "query": item.get("query")}
            for item in query_audit
        ],
        "previous_strategy_hash": repair_gate.get("strategy_hash"),
        "excluded_url_hashes": sorted(excluded_url_hashes),
    }
    result = {
        "protocol": "wiki-research-scout-v1",
        "query_policy_version": "question-contract-adaptive-v3",
        "question_contract_sha256": plan.get("question_contract_sha256"),
        "node_id": plan["node_id"],
        "research_plan": {
            "path": str(args.plan.resolve()),
            "sha256": hashlib.sha256(args.plan.read_bytes()).hexdigest(),
        },
        "candidates": candidates,
        "attempts": attempts,
        "query_audit": query_audit,
        "diversity_repair": ({
            "protocol": "wiki-source-diversity-repair-v2",
            "trigger_gate_sha256": hashlib.sha256(args.repair_gate.read_bytes()).hexdigest(),
            "failed_question_ids": sorted(failed_question_ids),
            "selected_intent_priority": selected_priority,
            "previous_candidate_count": previous_candidate_count,
            "novel_candidate_count": max(0, len(candidates) - len(previous_candidates)),
            "excluded_urls": sorted(failed_urls),
            "excluded_url_hashes": sorted(excluded_url_hashes),
            "previous_strategy_hash": str(repair_gate.get("strategy_hash") or ""),
            "strategy_hash": hashlib.sha256(json.dumps(
                strategy_signal, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest(),
        } if args.repair_gate else None),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "candidates": len(candidates)}, ensure_ascii=False))
    return 0 if candidates else 2


if __name__ == "__main__":
    raise SystemExit(main())
