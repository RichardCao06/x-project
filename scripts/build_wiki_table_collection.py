#!/usr/bin/env python3
"""Build an auditable table collection from independently verified Wiki claims."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
from typing import Any


CJK = re.compile(r"[\u3400-\u9fff]")
INTERNAL_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9])[AP]\d{3}(?!\d)", re.I)


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def source_id(url: str) -> str:
    return "verified-" + hashlib.sha256(url.encode()).hexdigest()[:16]


def confirmed_rows(verify: dict[str, Any]) -> list[dict[str, Any]]:
    rows = verify.get("claims") or ((verify.get("result") or {}).get("claims")) or []
    return [row for row in rows if isinstance(row, dict)
            and (row.get("verify") or {}).get("verdict") == "CONFIRMED"
            and (row.get("fetchResult") or {}).get("status") == "found"]


def choose(rows: list[dict[str, Any]], words: tuple[str, ...]) -> dict[str, Any]:
    for word in words:
        for row in rows:
            claim = row.get("claim") or {}
            haystack = " ".join(str(claim.get(k, "")) for k in ("requirement_id", "claim_text", "believed_locator"))
            if word in haystack:
                return row
    return rows[0]


def document_route_matches(route: dict[str, Any], blueprint: dict[str, Any]) -> bool:
    if route.get("node_type") and route.get("node_type") != blueprint.get("node_type"):
        return False
    name = str(blueprint.get("node_name") or "")
    required = [str(value) for value in route.get("node_name_contains", []) if str(value)]
    return all(value.lower() in name.lower() for value in required)


def routed_fields(route: dict[str, Any], fields: dict[str, Any]) -> list[tuple[str, str]]:
    selected: list[tuple[str, str]] = []
    for target in route.get("targets", []):
        table = str(target.get("table") or "")
        exact = str(target.get("field") or "")
        contains = str(target.get("field_contains") or "")
        for field in fields.get(table, []):
            if (exact and field == exact) or (contains and contains in field):
                pair = (table, field)
                if pair not in selected:
                    selected.append(pair)
    return selected


def external_search_term(value: Any) -> str:
    """Remove graph-only IDs while preserving the externally meaningful label."""
    text = INTERNAL_IDENTIFIER.sub(" ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip(" ,，;；|/-")


def load_query_translator() -> Any:
    scout_path = Path(__file__).resolve().with_name("scout_wiki_research_plan.py")
    spec = importlib.util.spec_from_file_location("wiki_query_translation", scout_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load auditable query translation fallback")
    translator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(translator)
    return translator


def runtime_field_translation(field: str, translator: Any) -> tuple[str | None, dict[str, Any]]:
    """Derive a discovery-only English field seed without inventing evidence."""
    source = external_search_term(field)
    audit = translator.translate_zh_search_terms([source])
    candidates = [
        str(value).strip() for value in audit.get("translated_terms") or []
        if str(value or "").strip() and not CJK.search(str(value))
    ]
    if audit.get("method") == "bilingual_passthrough_no_glossary_match" or not candidates:
        return None, audit
    return candidates[0], audit


def append_query(queries: list[dict[str, Any]], *, table: str, field: str,
                 language: str, query: str, **metadata: Any) -> None:
    query = external_search_term(query)
    if not query:
        raise ValueError(f"external search query is empty for {table}.{field}")
    if INTERNAL_IDENTIFIER.search(query):
        raise ValueError(f"external search query retains an internal identifier: {query}")
    if language == "en" and CJK.search(query):
        raise ValueError(f"English external search query retains CJK text: {query}")
    record = {"table": table, "field": field, "language": language,
              "query": query, "status": "planned",
              "query_hash": hashlib.sha256(query.encode()).hexdigest(), **metadata}
    identity = (table, field, language, query)
    if identity not in {(item["table"], item["field"], item["language"], item["query"])
                        for item in queries}:
        queries.append(record)


def reuse_executed_queries(matrix_path: Path, queries: list[dict[str, Any]]) -> int:
    """Carry hash-identical terminal searches across deterministic rebuilds."""
    if not matrix_path.is_file():
        return 0
    previous = load(matrix_path)
    terminal = {"found", "not_found", "fetched"}
    by_identity = {
        (str(row.get("table")), str(row.get("field")), str(row.get("language")),
         str(row.get("query_hash"))): row
        for row in previous.get("queries", []) if row.get("status") in terminal
    }
    reused = 0
    for row in queries:
        key = (row["table"], row["field"], row["language"], row["query_hash"])
        old = by_identity.get(key)
        if not old:
            continue
        for name in ("status", "results", "provider_attempts", "elapsed_ms"):
            if name in old:
                row[name] = old[name]
        reused += 1
    return reused


def activity_flow_direction(blueprint: dict[str, Any], field: str) -> str:
    """Derive a flow direction from the graph-bound content blueprint."""
    explicit = blueprint.get("flow_directions") or {}
    direction = explicit.get(field)
    if direction in {"in", "out"}:
        return direction
    # Compatibility for already-frozen blueprints: activity identity tokens after
    # the node ID are the reference outputs, while the remaining ledger rows are inputs.
    output_names = {str(value) for value in (blueprint.get("identity_tokens") or [])[1:]}
    _, separator, product_name = field.partition(" ")
    return "out" if separator and product_name in output_names else "in"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("blueprint", type=Path)
    ap.add_argument("verify", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--source-hints", type=Path)
    ap.add_argument("--research-plan", type=Path)
    ap.add_argument(
        "--document-routes", type=Path,
        default=Path(__file__).resolve().parents[1] / "config/wiki-table-document-routes.json",
    )
    args = ap.parse_args()
    blueprint, verify = load(args.blueprint), load(args.verify)
    rows = confirmed_rows(verify)
    terminology = (load(args.source_hints).get("terminology") if args.source_hints else None) or {
        "canonical_zh": blueprint["node_name"], "synonyms_zh": [], "canonical_en": "",
        "synonyms_en": [], "related_terms": [], "excluded_terms": [],
    }
    plan = load(args.research_plan) if args.research_plan else {}
    if plan.get("terminology"):
        terminology = plan["terminology"]
    zh_aliases = terminology.get("candidate_aliases_zh", terminology.get("synonyms_zh", []))
    en_aliases = terminology.get("candidate_aliases_en", terminology.get("synonyms_en", []))
    terminology = dict(terminology)
    translator = load_query_translator()
    if not any(str(x or "").strip() for x in [terminology.get("canonical_en"), *en_aliases]):
        translated = translator.translate_zh_search_terms([
            terminology.get("canonical_zh"), *zh_aliases,
        ])
        en_aliases = translated["translated_terms"]
        terminology["translated_search_terms_en"] = en_aliases
        terminology["query_translation"] = translated
    translations = plan.get("field_translations") or {}
    fields = blueprint.get("evidence_tables") or {}
    queries = []
    runtime_translations: dict[str, str] = {}
    translation_audit: list[dict[str, Any]] = []
    for kind, names in fields.items():
        for field in names:
            for language, terms in (
                ("zh", [terminology.get("canonical_zh"), *zh_aliases]),
                ("en", [terminology.get("canonical_en"), *en_aliases]),
            ):
                clean = [external_search_term(x) for x in terms if external_search_term(x)]
                if language == "en":
                    clean = [term for term in clean if not CJK.search(term)]
                if clean:
                    strategy = "field_term"
                    if language == "en":
                        field_term = str(translations.get(field) or "").strip()
                        if not field_term:
                            field_term, audit = runtime_field_translation(str(field), translator)
                            translation_audit.append({
                                "table": kind, "field": field,
                                "status": "runtime_translated" if field_term else "unresolved",
                                "method": audit.get("method"),
                                "unmatched_fragments": audit.get("unmatched_fragments") or [],
                            })
                            if not field_term:
                                continue
                            runtime_translations[str(field)] = field_term
                            strategy = "runtime_field_translation"
                    else:
                        field_term = external_search_term(field)
                    query = f"{' OR '.join(clean)} {field_term}"
                    append_query(queries, table=kind, field=field, language=language, query=query,
                                 query_strategy=strategy)
    route_ids: list[str] = []
    if args.document_routes.is_file():
        route_config = load(args.document_routes)
        if route_config.get("protocol") != "wiki-table-document-routes-v1":
            raise ValueError("document route config protocol mismatch")
        for route in route_config.get("routes", []):
            if not isinstance(route, dict) or not document_route_matches(route, blueprint):
                continue
            targets = routed_fields(route, fields)
            if not targets:
                continue
            route_id = str(route["id"]); route_ids.append(route_id)
            for table, field in targets:
                append_query(
                    queries, table=table, field=field,
                    language=str(route.get("language") or "zh"), query=str(route["query"]),
                    query_strategy="document_type_route", document_route=route_id,
                    document_type=str(route.get("document_type") or ""),
                    seed_candidates=[dict(item) for item in route.get("seed_candidates", [])
                                     if isinstance(item, dict) and item.get("url")],
                )
    matrix_path = args.output / "search-matrix.json"
    reused_queries = reuse_executed_queries(matrix_path, queries)
    terminal = {"found", "not_found", "fetched"}
    matrix = {"protocol": "wiki-multilingual-table-search-v1", "node_id": blueprint["node_id"],
              "terminology": terminology, "queries": queries,
              "document_routes": route_ids,
              "runtime_field_translations": runtime_translations,
              "field_translation_audit": translation_audit,
              "coverage_status": ("executed" if queries and all(q.get("status") in terminal for q in queries)
                                  else "partially_reused" if reused_queries else "planned_not_executed"),
              "reused_executed_queries": reused_queries,
              "rule": "related terms discover candidates only; excluded terms never establish identity"}
    matrix["query_quality_metrics"] = {
        "english_field_translation_coverage": f"{sum(1 for names in fields.values() for field in names if field in translations)}/{sum(len(names) for names in fields.values())}",
        "runtime_english_field_translation_coverage": f"{len(runtime_translations)}/{sum(len(names) for names in fields.values())}",
        "unresolved_english_field_translations": sum(
            row["status"] == "unresolved" for row in translation_audit
        ),
        "mixed_language_english_queries": sum(
            bool(CJK.search(str(row["query"]))) for row in queries if row["language"] == "en"
        ),
        "internal_identifier_queries": sum(
            bool(INTERNAL_IDENTIFIER.search(str(row["query"]))) for row in queries
        ),
        "a039_document_routes": len(route_ids) if str(blueprint.get("node_id")) == "A039" else 0,
    }
    dump(matrix_path, matrix)

    sources: dict[str, dict[str, Any]] = {}
    for row in rows:
        claim, fetch, verdict = row["claim"], row["fetchResult"], row["verify"]
        url = str(fetch["url"]); sid = source_id(url)
        entry = sources.setdefault(sid, {
            "id": sid, "title": str(claim["believed_source"]), "type": "verified-public-source",
            "version": "frozen-fetch", "locator": str(claim.get("believed_locator", "")),
            "authority": re.sub(r"^www\.", "", __import__("urllib.parse").parse.urlsplit(url).hostname or "public-source"),
            "region": "CN" if re.search(r"[\u3400-\u9fff]", str(claim["believed_source"])) else "INT",
            "status": "verified", "url": url, "local_path": "", "sha256": "",
            "excerpt_seeds": [], "verified_via": "independent Wiki Verify attestation and frozen fetch hash",
        })
        quote = str(verdict.get("supporting_quote", "")).strip()
        if quote and quote not in entry["excerpt_seeds"]:
            entry["excerpt_seeds"].append(quote)
        if str(claim.get("believed_locator", "")) not in entry["locator"]:
            entry["locator"] += "; " + str(claim.get("believed_locator", ""))
    prop_routes = {
        "产品节点身份": ("identity.definition", "identity.product_class"),
        "来源工艺边界": ("adjacent.distinction", "scope.classification", "flow_identity"),
        "收集与交接状态": ("delivery_state", "handoff_unit"),
        "相邻废物流区分": ("adjacent.specification", "adjacent.distinction"),
    }
    props = []
    for field in fields.get("props", []):
        if rows:
            selected = choose(rows, prop_routes.get(field, (field,)))
            claim, fetch = selected["claim"], selected["fetchResult"]
            value = re.sub(r"^提名核验[:：]\s*", "", str(claim["claim_text"])).rstrip("。")
            props.append({"field": field, "condition": "冻结节点与来源范围", "unit": "—",
                          "value": value, "source": source_id(str(fetch["url"])),
                          "pedigree": "independently_verified_external_fact", "status": "populated"})
        else:
            props.append({
                "field": field, "condition": "冻结节点与来源范围", "unit": "—",
                "value": "缺口：尚未取得可直接支撑该产品属性的节点特定公开证据",
                "source": "", "pedigree": "explicit_gap_after_multilingual_search",
                "status": "explicit_gap",
            })
    activity_props = [
        {"field": field, "condition": "参考产品交接点", "unit": "待来源定义",
         "value": "缺口：尚未取得可直接支撑该参考产品属性的节点特定公开证据",
         "source": "", "pedigree": "explicit_gap_after_multilingual_search",
         "status": "explicit_gap"}
        for field in fields.get("props", [])
    ]
    params = [{"field": field, "geo": "CN/INT query tracks planned", "unit": "待来源定义", "basis": "reference",
               "int_value": "缺口：多语检索式已生成但尚未冻结可代表本节点的国际定量证据", "int_source": "",
               "cn_value": "缺口：尚未取得节点特定的中国项目实测记录", "cn_source": "",
               "pedigree": "explicit_public_evidence_gap", "status": "explicit_gap"}
              for field in fields.get("params", [])]
    quality = [{"field": field, "unit": "status", "basis": "reference",
                "cn_value": "缺口：当前冻结来源不足以完成该质量维度的节点特定评价",
                "cn_source": "", "proxy_policy": "不得以通用行业值冒充节点实测值",
                "pedigree": "explicit_gap_after_multilingual_search", "status": "explicit_gap"}
               for field in fields.get("quality", [])]
    activity_gap = {
        "unit": "待来源定义", "basis": "reference",
        "int_value": "缺口：尚未取得可代表本节点的国际定量证据",
        "int_source": "",
        "cn_value": "缺口：尚未取得节点特定的中国项目实测记录",
        "cn_source": "",
        "pedigree": "explicit_gap_after_multilingual_search", "status": "explicit_gap",
    }
    flows = [{"field": field,
              "direction": activity_flow_direction(blueprint, field),
              **activity_gap} for field in fields.get("flows", [])]
    emissions = [{"field": field, "cas": "—",
                  "compartment": ("air" if "空气" in field else "water" if "水" in field
                                  else "soil" if "土壤" in field else "waste"),
                  **activity_gap} for field in fields.get("emissions", [])]
    indicators = [{"field": field, "medium": "manufacturing", "mapping_status": "explicit_gap",
                   **activity_gap} for field in fields.get("indicators", [])]
    if blueprint["node_type"] == "activity":
        tables = {"flows": flows, "props": activity_props, "params": params,
                  "emissions": emissions, "indicators": indicators, "quality": quality}
        thresholds = {"flows_populated": 0, "props_populated": 0, "emissions_populated": 0,
                      "indicators_populated": 0, "params_int_populated": 0,
                      "params_cn_populated": 0, "quality_assessed": 0}
    else:
        tables = {"props": props, "params": params, "quality": quality}
        thresholds = {"props_populated": sum(row["status"] == "populated" for row in props),
                      "params_int_populated": 0,
                      "params_cn_populated": 0, "quality_assessed": 0}
    collection = {
        "protocol": {"version": "wiki-table-evidence-v1", "kind": "node-table-collection"},
        "node_id": blueprint["node_id"], "node_type": blueprint["node_type"],
        "reference_configuration": {"manufacturer": ("multiple public sources" if sources
                                                        else "not established; explicit public evidence gap"),
                                    "model": blueprint["node_name"],
                                    "scope": "frozen node identity; CN and INT evidence tracks",
                                    "freeze_rule": "Only independently confirmed node-aligned facts are populated; all unsupported quantitative cells are explicit gaps."},
        "thresholds": thresholds,
        "gap_provenance_required": True,
        "sources": list(sources.values()), "tables": tables,
        "search_matrix_sha256": hashlib.sha256((args.output / "search-matrix.json").read_bytes()).hexdigest(),
    }
    dump(args.output / "collection.json", collection)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
