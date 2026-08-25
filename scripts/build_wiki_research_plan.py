#!/usr/bin/env python3
"""Freeze a multilingual Research Plan before any claim nomination."""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, re, sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from wiki_research_contract import (
    ONTOLOGY_VERSION,
    build_question_contracts,
    contract_sha256,
)

FIELD_EN = {
    "product": {
        "props": {
            "产品节点身份": "product identity",
            "来源工艺边界": "source process boundary",
            "收集与交接状态": "collection and handoff state",
            "相邻废物流区分": "distinction from adjacent waste streams",
        },
        "params": {
            "批次净质量": "batch net mass", "含锡率": "tin content assay",
            "合金体系": "solder alloy system", "含水率": "moisture content",
            "产生地点与代表期": "generation location and reference period",
            "回收或处置地点": "recovery or disposal destination",
            "包装与运输交接": "packaging and transport handoff",
        },
        "quality": {
            "批次称量覆盖": "batch weighing coverage",
            "物质组成检测": "material composition testing",
            "产生工艺覆盖": "generating process coverage",
            "去向凭证覆盖": "destination record coverage",
            "地理与时间代表性": "geographical and temporal representativeness",
            "代理选择与失效条件": "proxy selection and invalidation conditions",
        },
    },
    # A039 is deliberately bound to the frozen activity table schema.  IDs are
    # retained in the evidence-table keys for graph/hash alignment, but never
    # appear in the external-search values.
    "A039": {
        "flows": {
            "P018 主板PCBA, 通用服务器用": "general-purpose server motherboard PCBA",
            "P026 DIMM内存条": "DIMM memory module",
            "P027 SSD模组": "SSD module",
            "P029 PSU电源模组": "power supply unit module",
            "P057 钢钣金机箱/导轨, 服务器用": "server steel sheet-metal chassis and rails",
            "P055 铝散热器/铝挤型, 服务器用": "server aluminum heat sink and extrusion",
            "P051 风扇模组, 服务器/机架用, 成品": "finished server and rack fan module",
            "P063 导热硅脂/导热垫, TIM": "thermal interface material grease and pad",
            "P066 中压电力, ICT制造用": "medium-voltage electricity for ICT manufacturing",
            "P003 服务器, 通用计算, 刀片式": "general-purpose blade server",
        },
        "props": {
            "参考产品身份（服务器, 通用计算, 刀片式）": "reference product identity for a general-purpose blade server",
            "参考产品完整型号与配置版本": "complete reference product model and configuration version",
            "参考产品单件净质量": "reference product net mass per unit",
            "参考产品交接状态": "reference product handoff state",
            "参考产品规格或质量口径": "reference product specification or quality basis",
            "参考产品包装前边界": "reference product pre-packaging boundary",
        },
        "params": {
            "工艺路线与设备配置": "manufacturing route and equipment configuration",
            "装配批次产量": "assembly batch output",
            "有效运行时间": "effective operating time",
            "生产负荷与良率": "production load and yield",
            "装配地点与代表期": "assembly location and reference period",
            "共享能源与辅助系统边界": "shared energy and auxiliary-system boundary",
        },
        "emissions": {
            "空气排放": "emissions to air", "水体排放": "emissions to water",
            "土壤排放": "emissions to soil",
        },
        "indicators": {
            "一次装配良率": "first-pass assembly yield", "返工率": "rework rate",
            "单位产品装配电耗": "assembly electricity consumption per product",
        },
        "quality": {
            "BOM质量闭合": "BOM mass closure", "输入输出质量闭合": "input-output mass closure",
            "供应商覆盖": "supplier coverage",
            "地理与时间代表性": "geographical and temporal representativeness",
            "代理选择与失效条件": "proxy selection and invalidation conditions",
        },
    },
}

# A013 uses the same activity-table contract as A039, with a graph-specific
# flow ledger and reference-product identity.  Keeping every flow label in the
# frozen map prevents the English query serializer from falling back to mixed
# Chinese/internal-ID queries for the 35-field switch-assembly schema.
FIELD_EN["A013"] = {
    "flows": {
        "P022 交换机主板PCBA, 100G/400G": "100G/400G switch motherboard PCBA",
        "P046 光模块, 400G/800G": "400G/800G optical transceiver module",
        "P029 PSU电源模组": "power supply unit module",
        "P055 铝散热器/铝挤型, 服务器用": "server aluminum heat sink and extrusion",
        "P057 钢钣金机箱/导轨, 服务器用": "server steel sheet-metal chassis and rails",
        "P051 风扇模组, 服务器/机架用, 成品": "finished server and rack fan module",
        "P066 中压电力, ICT制造用": "medium-voltage electricity for ICT manufacturing",
        "P038 交换ASIC封装器件, 100G/400G": "100G/400G switch ASIC package",
        "P063 导热硅脂/导热垫, TIM": "thermal interface material grease and pad",
        "P064 塑料导风罩/前面板/线缆护套": "plastic air baffle, front panel, and cable jacket",
        "P061 低压铜电缆线束, ICT设备电源用": (
            "low-voltage copper cable harness for ICT equipment power"
        ),
        "P008 网络交换机, 100G/400G, 2U": "100G/400G 2U network switch",
    },
    "props": {
        "参考产品身份（网络交换机, 100G/400G, 2U）": (
            "reference product identity for a 100G/400G 2U network switch"
        ),
        **{
            field: english
            for field, english in FIELD_EN["A039"]["props"].items()
            if not field.startswith("参考产品身份（")
        },
    },
    **{
        table: dict(fields)
        for table, fields in FIELD_EN["A039"].items()
        if table not in {"flows", "props"}
    },
}


def _flatten(schema: dict[str, dict[str, str]]) -> dict[str, str]:
    return {field: english for fields in schema.values() for field, english in fields.items()}


def field_translation_contract(node_id: str) -> tuple[dict[str, str], dict | None]:
    schema = FIELD_EN.get(node_id)
    if schema:
        translations = _flatten(schema)
        required = {table: list(fields) for table, fields in schema.items()}
        return translations, {
            "scope": "node_and_table_schema",
            "node_id": node_id,
            "required_fields_by_table": required,
            "required_field_count": sum(len(fields) for fields in required.values()),
            "complete_english_coverage_required": True,
            "internal_identifiers_allowed_in_values": False,
            "cjk_allowed_in_english_values": False,
        }
    # Preserve the existing product-node contract outside the repaired A039
    # activity. Other activity schemas must supply a node-specific map before
    # their English table searches can be serialized.
    return _flatten(FIELD_EN["product"]) if node_id.startswith("P") else {}, None

def load(p): return json.loads(Path(p).read_text(encoding="utf-8"))
def dump(p,v): Path(p).parent.mkdir(parents=True,exist_ok=True); Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")

def load_translation_repair(output: Path, node_id: str) -> tuple[dict[str,str],dict]:
    path=output.with_name("research-plan-translation-repair.json")
    if not path.is_file(): return {},{}
    repair=load(path)
    valid=(repair.get("protocol")=="wiki-research-translation-repair-v1"
           and repair.get("status")=="ready"
           and repair.get("authority")=="discovery_only"
           and repair.get("identity_authorized") is False
           and str(repair.get("node_id") or "")==str(node_id))
    values=repair.get("repairs") or {}
    if not valid or not isinstance(values,dict): return {},{}
    cjk=re.compile(r"[\u3400-\u9fff]")
    overrides={str(k).strip():str(v).strip() for k,v in values.items()
               if str(k).strip() and str(v).strip() and not cjk.search(str(v))}
    if len(overrides)!=len(values): return {},{}
    return overrides,repair

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("workflow",type=Path); ap.add_argument("output",type=Path); ap.add_argument("--source-hints",type=Path); ap.add_argument("--registry",type=Path)
    a=ap.parse_args(); source=a.workflow.read_text(encoding="utf-8")
    m=re.search(r"const NODES\s*=\s*(\[.*?\])\s*/\* DATA-BINDING:END \*/",source,re.S)
    if not m: raise ValueError("workflow has no frozen NODES")
    node=json.loads(m.group(1))[0]; hints=load(a.source_hints) if a.source_hints else {}
    terms=hints.get("terminology") or {"canonical_zh":node["name"],"candidate_aliases_zh":[],"canonical_en":"","candidate_aliases_en":[],"related_terms":[],"excluded_terms":[]}
    # Legacy synonym fields are candidates until a current-job terminology verdict confirms equivalence.
    terms["candidate_aliases_zh"]=terms.pop("synonyms_zh",terms.get("candidate_aliases_zh",[]))
    terms["candidate_aliases_en"]=terms.pop("synonyms_en",terms.get("candidate_aliases_en",[]))
    if not any(str(value or "").strip() for value in [
        terms.get("canonical_en"), *terms.get("candidate_aliases_en", []),
    ]):
        translator_path = Path(__file__).resolve().with_name("scout_wiki_research_plan.py")
        spec = importlib.util.spec_from_file_location("wiki_research_translation", translator_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load auditable research query translation")
        translator = importlib.util.module_from_spec(spec); spec.loader.exec_module(translator)
        overrides,repair_artifact=load_translation_repair(a.output,node["node_id"])
        translated = translator.translate_zh_search_terms([
            terms.get("canonical_zh"), *terms.get("candidate_aliases_zh", []),
        ],overrides=overrides)
        if repair_artifact:
            translated["repair_artifact_sha256"]=repair_artifact.get("artifact_sha256")
            translated["repair_source_plan_sha256"]=repair_artifact.get("source_plan_sha256")
        terms["translated_search_terms_en"] = translated["translated_terms"]
        terms["query_translation"] = translated
    terminology_review={"status":"unresolved","rule":"aliases expand discovery but cannot establish identity","required_evidence_classes":["technical_standard_or_industry_authority","current_job_primary_source"],"prohibited_auto_equivalence":True}
    candidates=[]
    for s in hints.get("sources",[]): candidates.append({"title":s.get("name"),"url":s.get("canonical_url"),"topics":s.get("locator_topics",[]),"provenance":"advisory_source_hint","current_job_status":"candidate_unverified"})
    if a.registry and a.registry.is_file():
        registry=load(a.registry); entries=registry.get("sources",{})
        if isinstance(entries,dict):
            needles=[str(terms.get("canonical_zh", "")),str(terms.get("canonical_en", "")),*terms.get("candidate_aliases_zh",[]),*terms.get("candidate_aliases_en",[])]
            for source_id,item in entries.items():
                if not isinstance(item,dict): continue
                haystack=json.dumps(item,ensure_ascii=False).lower()
                if not any(str(n).strip().lower() in haystack for n in needles if str(n).strip()): continue
                raw_url=str(item.get("url") or item.get("locator") or ""); match=re.search(r"https?://[^；;\s]+",raw_url)
                if match: candidates.append({"source_id":source_id,"title":item.get("title"),"url":match.group(0),"topics":[],"provenance":"historical_registry","historical_status":item.get("status"),"current_job_status":"candidate_unverified"})
    unique={str(row.get("url")):row for row in candidates if row.get("url")}; candidates=list(unique.values())
    field_translations,translation_contract=field_translation_contract(str(node["node_id"]))
    requirement_ids = sorted(set(re.findall(
        r'''["']requirement_id["']\s*:\s*["']([^"']+)["']''', source
    )))
    question_contracts = build_question_contracts(
        str(node["node_id"]), str(node["name"]), terms, requirement_ids
    )
    plan={
        "protocol":"wiki-research-plan-v1",
        "schema_version":"wiki-research-plan-v2",
        "node_id":node["node_id"],
        "node_name":node["name"],
        "terminology":terms,
        "terminology_review":terminology_review,
        "languages":["zh","en"],
        "research_questions":[contract["dimension"] for contract in question_contracts],
        "research_question_contract_version":ONTOLOGY_VERSION,
        "research_question_contracts":question_contracts,
        "question_contract_sha256":contract_sha256(question_contracts),
        "source_classes":[
            "government_or_regulator","standard_or_industry_body",
            "manufacturer_technical","peer_reviewed_research","node_specific_records",
        ],
        "source_role_contract":{
            "identity":"authoritative_or_current_job_manufacturer",
            "process_boundary":"technical_primary_source",
            "adjacent_distinction":"technical_or_industry_source",
            "regional_representativeness":"target_region_source_or_explicit_gap",
            "quantitative":"authoritative_single_or_independent_two_source",
        },
        # Count targets are portfolio quality goals.  The downstream v2 gate
        # uses question-level evidence sufficiency for blocking decisions.
        "minimum_source_diversity":{
            "constraint_class":"quality_target",
            "default_effect":"warn_and_expand",
            "preview_hard_confirmed_sources":1,
            "preview_primary_sources":3,
            "preview_distinct_domains":3,
            "preview_technical_sources":1,
            "preview_language_tracks":2,
            "reviewed_primary_sources":3,
            "reviewed_distinct_domains":3,
            "reviewed_technical_sources":2,
            "reviewed_language_tracks":2,
            "quantitative_independent_sources":2,
            "node_specific_source_overrides_quantitative_minimum":False,
        },
        "advisory_candidates":candidates,
        "historical_registry_policy":"candidate_only_refetch_and_reverify",
        "hint_policy":"advisory_nonexclusive",
        "field_translations":field_translations,
        "field_translation_policy":{
            "constraint_class":"quality_target",
            "default_effect":"warn_and_expand",
            "mode":"adaptive_runtime_expansion",
            "static_coverage_complete":bool(translation_contract),
            "missing_translation_behavior":"execute_chinese_track_record_gap_and_expand_from_results",
            "reviewed_bilingual_coverage_verified_downstream":True,
        },
    }
    if translation_contract:
        plan["field_translation_contract"]=translation_contract
    plan["plan_sha256"]=hashlib.sha256(json.dumps(plan,ensure_ascii=False,sort_keys=True).encode()).hexdigest(); dump(a.output,plan); return 0
if __name__=="__main__": raise SystemExit(main())
