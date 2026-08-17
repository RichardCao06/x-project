#!/usr/bin/env python3
"""Promote frozen search candidates into table evidence, or audit their rejection.

Search hits are discovery candidates, never data by themselves.  A candidate is
promoted only when a field-specific observation can be extracted from its
frozen payload and is either authoritative or independently corroborated.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from typing import Any


AUTHORITATIVE = {"government_or_regulator", "standard_or_industry_body", "peer_reviewed_research"}
INSTITUTIONAL = {"academic_institutional_research"}
NUMBER = r"(?P<value>\d+(?:\.\d+)?)"
RULES: dict[tuple[str, str], re.Pattern[str]] = {
    ("indicators", "一次装配良率"): re.compile(
        r"(?:一次(?:装配|通过)?良率|first[ -]pass yield|\bFPY\b)[^。.!?;；\n%]{0,48}?" + NUMBER + r"\s*(?P<unit>%)", re.I),
    ("indicators", "返工率"): re.compile(
        r"(?:返工率|返修率|rework rate)[^。.!?;；\n%]{0,48}?" + NUMBER + r"\s*(?P<unit>%)", re.I),
    ("indicators", "单位产品装配电耗"): re.compile(
        r"(?:单位(?:产品|板).*?电耗|energy (?:use|consumption).*?(?:board|unit))[^。.!?;；\n]{0,48}?"
        + NUMBER + r"\s*(?P<unit>kWh|Wh|MJ)\s*(?:/|per\s+)(?:台|块|board|unit)", re.I),
    ("params", "单台净质量"): re.compile(
        r"(?:单台净质量|单板(?:净)?质量|net weight|weight per (?:board|unit))[^。.!?;；\n]{0,48}?"
        + NUMBER + r"\s*(?P<unit>kg|g)\s*(?:/\s*(?:台|块)|per\s+(?:board|unit))?", re.I),
    ("params", "装配批次产量"): re.compile(
        r"(?:装配批次产量|批次产量|batch (?:output|quantity|size))[^。.!?;；\n]{0,48}?"
        + NUMBER + r"\s*(?P<unit>台|块|pcs?|units?)", re.I),
}

# Evidence availability is independent from current parser coverage. Keeping
# the two explicit prevents a missing rule from becoming a false assertion
# that only a private node record could ever support the field.
PUBLIC_EXTRACTABILITY = {
    "flows": "public_or_internal", "props": "publicly_extractable",
    "params": "public_or_internal", "emissions": "public_or_internal",
    "indicators": "public_or_internal", "quality": "public_or_internal",
}


def public_extractability(table: str, field: str) -> str:
    return PUBLIC_EXTRACTABILITY.get(table, "unclassified")


def extraction_support(table: str, field: str) -> str:
    clean = re.sub(r"^P\d{3}\s+", "", field)
    if (table, clean) in RULES:
        return "generic_pattern"
    if table == "flows" or (table, field) in {
        ("props", "参考产品单件净质量"), ("params", "装配批次产量"),
        ("params", "有效运行时间"), ("params", "生产负荷与良率"),
        ("indicators", "一次装配良率"), ("indicators", "返工率"),
        ("indicators", "单位产品装配电耗"), ("emissions", "空气排放"),
        ("emissions", "水体排放"), ("emissions", "土壤排放"),
    }:
        return "routed_html_pdf_pattern"
    return "not_implemented"


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_id(url: str) -> str:
    return "verified-" + hashlib.sha256(url.encode()).hexdigest()[:16]


def sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[。！？.!?;；])\s+|[\r\n]+", text) if part.strip()]


def compact_number(value: float) -> str:
    return f"{value:.10g}"


def _cn_annual_output(text: str) -> tuple[float, str] | None:
    patterns = (
        r"年产(?:通信设备)?(?:电路板|通信电路板|通讯主板)[^\d]{0,24}(\d+(?:\.\d+)?)\s*万[片件个]",
        r"年产量[（(]?(?:电路板)?[）)]?[^\d]{0,24}(\d+(?:\.\d+)?)\s*万[片件个]",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return float(match.group(1)) * 10_000, match.group(0)
    return None


def _derived(value: float, unit: str, quote: str, *, formula: str, scope: str,
             score: int = 4, label: str = "代理值", **extra: str) -> dict[str, Any]:
    return {"value": compact_number(value), "unit": unit, "quote": quote[:1000],
            "derivation": formula, "source_scope": scope,
            "comparability_score": score, "label": label, **extra}


def _extract_a039_document_observations(
    table: str, field: str, flat: str, document_type: str,
) -> list[dict[str, Any]]:
    """Extract bounded A039 observations from HTML/PDF text excerpts."""
    rows: list[dict[str, Any]] = []
    clean_field = re.sub(r"^P\d{3}\s+", "", field)
    if document_type == "manufacturer_specification_bom":
        if table == "props" and field == "参考产品单件净质量":
            match = re.search(
                r"(?:product\s+)?(?:net|maximum)?\s*weight[^\d]{0,32}(\d+(?:\.\d+)?)\s*(kg|g)\b",
                flat, re.I,
            )
            if match:
                rows.append(_derived(
                    float(match.group(1)), match.group(2), match.group(0),
                    formula="manufacturer specification value; no arithmetic transformation",
                    scope="named blade-server configuration in a manufacturer specification or BOM",
                    score=5, label="定义值", reference_product="named blade-server configuration",
                ))
        elif table == "flows":
            aliases = {
                "主板PCBA": r"(?:motherboard|system board)(?:\s+PCBA)?",
                "DIMM": r"DIMM(?:\s+memory)?(?:\s+modules?)?",
                "SSD": r"SSD(?:\s+(?:drives?|modules?))?",
                "PSU": r"(?:PSU|power supply units?)",
                "风扇模组": r"fan modules?",
            }
            alias = next((pattern for token, pattern in aliases.items() if token in clean_field), None)
            if alias:
                match = re.search(alias + r"[^\d]{0,32}(\d+(?:\.\d+)?)\s*(?:pcs?|units?|modules?)?", flat, re.I)
                if match:
                    rows.append(_derived(
                        float(match.group(1)), "item/unit", match.group(0),
                        formula="published component count for one named server configuration",
                        scope="manufacturer specification/BOM configuration; not a fleet average",
                        score=5, label="定义值", reference_product="named blade-server configuration",
                    ))
    elif document_type == "system_integration_manufacturing_record":
        patterns = {
            ("params", "装配批次产量"): r"assembly batch (?:output|quantity|size)[^\d]{0,32}(\d+(?:\.\d+)?)\s*(units?|pcs?)",
            ("params", "有效运行时间"): r"(?:effective )?operating (?:time|hours)[^\d]{0,32}(\d+(?:\.\d+)?)\s*(h(?:ours?)?(?:/a)?)",
            ("params", "生产负荷与良率"): r"(?:production load and yield|production yield)[^\d]{0,32}(\d+(?:\.\d+)?)\s*(%)",
            ("indicators", "一次装配良率"): r"(?:first[ -]pass (?:assembly )?yield|FPY)[^\d]{0,32}(\d+(?:\.\d+)?)\s*(%)",
            ("indicators", "返工率"): r"rework rate[^\d]{0,32}(\d+(?:\.\d+)?)\s*(%)",
            ("indicators", "单位产品装配电耗"): r"assembly electricity (?:use|consumption)[^\d]{0,40}(\d+(?:\.\d+)?)\s*(kWh|Wh|MJ)\s*(?:/|per)\s*(?:assembled )?(?:unit|server)",
        }
        pattern = patterns.get((table, field))
        match = re.search(pattern, flat, re.I) if pattern else None
        if match:
            rows.append(_derived(
                float(match.group(1)), match.group(2), match.group(0),
                formula="field value transcribed from a system-integration manufacturing record",
                scope="named blade-server final-assembly and test process",
                score=5, label="定义值", reference_product="assembled blade server",
            ))
    elif document_type == "environmental_report":
        output = re.search(r"annual (?:blade server )?(?:assembly )?output[^\d]{0,32}(\d+(?:\.\d+)?)\s*(?:units?|pcs?)", flat, re.I)
        annual_units = float(output.group(1)) if output else 0.0
        if annual_units and table == "flows" and "中压电力" in clean_field:
            match = re.search(r"annual electricity (?:use|consumption)[^\d]{0,32}(\d+(?:\.\d+)?)\s*(kWh|MWh)", flat, re.I)
            if match:
                annual_kwh = float(match.group(1)) * (1000 if match.group(2).lower() == "mwh" else 1)
                rows.append(_derived(
                    annual_kwh / annual_units, "kWh/unit", f"{output.group(0)}; {match.group(0)}",
                    formula=f"{compact_number(annual_kwh)} kWh/a ÷ {compact_number(annual_units)} units/a",
                    scope="same-report final-assembly electricity normalized by stated annual output",
                    score=5, reference_product="assembled blade server",
                ))
        elif annual_units and table == "emissions":
            compartment = {"空气排放": "air", "水体排放": "water", "土壤排放": "soil"}.get(field)
            match = re.search(compartment + r" emissions?[^\d]{0,32}(\d+(?:\.\d+)?)\s*(kg|t)\s*(?:/a|per year)", flat, re.I) if compartment else None
            if match:
                annual_kg = float(match.group(1)) * (1000 if match.group(2).lower() == "t" else 1)
                rows.append(_derived(
                    annual_kg / annual_units, "kg/unit", f"{output.group(0)}; {match.group(0)}",
                    formula=f"{compact_number(annual_kg)} kg/a ÷ {compact_number(annual_units)} units/a",
                    scope=f"same-report {compartment}-emission total normalized by stated annual output",
                    score=5, reference_product="assembled blade server",
                ))
    elif document_type == "process_lca" and (table == "indicators" or (table == "flows" and "中压电力" in clean_field)):
        match = re.search(
            r"(?:final system |blade server )?assembly electricity (?:use|consumption)[^\d]{0,40}"
            r"(\d+(?:\.\d+)?)\s*(kWh|Wh|MJ)\s*(?:/|per)\s*(?:assembled )?(?:unit|server)",
            flat, re.I,
        )
        if match:
            rows.append(_derived(
                float(match.group(1)), match.group(2) + "/unit", match.group(0),
                formula="published process-LCA value per assembled reference unit",
                scope="final system-assembly process LCA for the stated reference product",
                score=5, reference_product="assembled blade server",
            ))
    return rows


def extract_document_observations(table: str, field: str, text: str,
                                  document_type: str) -> list[dict[str, Any]]:
    flat = re.sub(r"\s+", " ", text)
    # PDF extraction often inserts spaces in the middle of Chinese words at a
    # source line break (for example ``化合 物``).  Remove only those
    # intra-CJK spaces before applying audited field patterns.
    flat = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", flat)
    observations: list[dict[str, Any]] = []
    clean_field = re.sub(r"^P\d{3}\s+", "", field)
    observations.extend(_extract_a039_document_observations(
        table, field, flat, document_type,
    ))
    if observations:
        return observations
    if document_type == "environmental_impact_assessment":
        output = _cn_annual_output(flat)
        if not output:
            return []
        annual_boards, output_quote = output
        if table == "flows" and "PCB裸板" in clean_field:
            match = re.search(r"PCB\s*板\s*(\d+(?:\.\d+)?)\s*万[个片件]", flat, re.I)
            if match:
                annual_input = float(match.group(1)) * 10_000
                observations.append(_derived(
                    annual_input / annual_boards, "个/片", f"{output_quote}；{match.group(0)}",
                    formula=f"{compact_number(annual_input)} 个/a ÷ {compact_number(annual_boards)} 片/a",
                    scope="same-project annual PCB input normalized by stated qualified-board output",
                    reference_product="communication-equipment motherboard",
                ))
        elif table == "flows" and "无铅焊料锡膏" in clean_field:
            match = re.search(r"无铅锡膏\s*(\d+(?:\.\d+)?)\s*kg", flat, re.I)
            if match:
                annual_kg = float(match.group(1))
                observations.append(_derived(
                    annual_kg / annual_boards, "kg/片", f"{output_quote}；{match.group(0)}",
                    formula=f"{compact_number(annual_kg)} kg/a ÷ {compact_number(annual_boards)} 片/a",
                    scope="same-project annual lead-free solder-paste input normalized by stated output",
                    reference_product="communication-equipment motherboard",
                ))
        elif table == "flows" and "中压电力" in clean_field:
            match = re.search(r"(?:^|[；;，,\s])电\s*(\d+(?:\.\d+)?)\s*万\s*k[Ww][.·]?h", flat)
            if match:
                annual_kwh = float(match.group(1)) * 10_000
                observations.append(_derived(
                    annual_kwh / annual_boards, "kWh/片", f"{output_quote}；{match.group(0).strip()}",
                    formula=f"{compact_number(annual_kwh)} kWh/a ÷ {compact_number(annual_boards)} 片/a",
                    scope="whole-project electricity upper bound; includes SMT, assembly, testing, rework, packaging and auxiliaries",
                    score=2, proxy_role="upper_bound",
                    reference_product="communication-equipment motherboard",
                ))
        elif table == "flows" and "报废PCBA" in clean_field:
            matches = list(re.finditer(r"废电路板[^。；]{0,160}?(?:产生量(?:约|为)?\s*)?(\d+(?:\.\d+)?)\s*t/a", flat, re.I))
            if matches:
                match = matches[-1]; annual_kg = float(match.group(1)) * 1000
                observations.append(_derived(
                    annual_kg / annual_boards, "kg/片", f"{output_quote}；{match.group(0)}",
                    formula=f"{compact_number(annual_kg)} kg/a ÷ {compact_number(annual_boards)} 片/a",
                    scope="same-project annual scrap-PCB total normalized by stated output",
                    reference_product="communication-equipment motherboard",
                ))
        elif table == "emissions" and field == "空气排放":
            match = re.search(r"焊接烟尘[（(]锡及其化合物[）)][^。]{0,100}?产生量为\s*(\d+(?:\.\d+)?)\s*t/a", flat)
            if match:
                annual_kg = float(match.group(1)) * 1000
                observations.append(_derived(
                    annual_kg / annual_boards, "kg/片", f"{output_quote}；{match.group(0)}",
                    formula=f"{compact_number(annual_kg)} kg/a ÷ {compact_number(annual_boards)} 片/a",
                    scope="whole-project reflow, wave-solder and repair tin-compound generation before treatment; upper bound for the narrower A001 activity",
                    score=2, proxy_role="upper_bound", substance="锡及其化合物",
                    reference_product="communication-equipment motherboard",
                ))
        elif table == "params" and field == "有效运行时间":
            match = re.search(r"(?:实行)?一班工作制[^。]{0,50}?每班工作\s*(\d+(?:\.\d+)?)\s*小时[^。]{0,50}?全年工作\s*(\d+(?:\.\d+)?)\s*天", flat)
            if match:
                hours, days = float(match.group(1)), float(match.group(2))
                observations.append(_derived(
                    hours * days, "h/a", match.group(0),
                    formula=f"{compact_number(hours)} h/d × {compact_number(days)} d/a",
                    scope="same-project stated production schedule; annual operating-time proxy",
                    score=4, reference_product="communication-equipment motherboard",
                ))
        elif table == "params" and field == "生产负荷与良率":
            match = re.search(r"PCB\s*板\s*(\d+(?:\.\d+)?)\s*万[个片件]", flat, re.I)
            if match:
                annual_input = float(match.group(1)) * 10_000
                observations.append(_derived(
                    annual_boards / annual_input * 100, "%", f"{output_quote}；{match.group(0)}",
                    formula=f"{compact_number(annual_boards)} 片/a ÷ {compact_number(annual_input)} 个/a × 100%",
                    scope="same-project annual output-to-PCB-input ratio; not first-pass yield",
                    score=4, proxy_role="material_balance_yield",
                    reference_product="communication-equipment motherboard",
                ))
    elif document_type == "process_lca" and table in {"flows", "indicators"}:
        header = re.search(r"Energy consumption per FU\s*\[kWh]", flat, re.I)
        process_names = ("Board stacker & cleaner", "Paste Printer", "Paste Inspection", "SMD Placement",
                         "Reflow Oven", "Solder Inspection", "Electrical ICT Tester", "Handling processes")
        values: list[tuple[str, float]] = []
        for name in process_names:
            match = re.search(re.escape(name) + r"\s+(\d+(?:\.\d+)?)", flat, re.I)
            if match:
                values.append((name, float(match.group(1))))
        if header and len(values) == len(process_names):
            total = sum(value for _, value in values)
            terms = " + ".join(compact_number(value) for _, value in values)
            observations.append(_derived(
                total, "kWh/reference FU", f"{header.group(0)}；" + "; ".join(
                    f"{name} {compact_number(value)}" for name, value in values),
                formula=f"{terms} = {compact_number(total)} kWh/reference FU",
                scope="published SMT-line reference functional unit; different PCB product and not a server-board measurement",
                score=3 if table == "indicators" else 1,
                reference_product="radar PCB assembly functional unit",
            ))
    return observations


def extract_observations(table: str, field: str, text: str,
                         document_type: str = "") -> list[dict[str, Any]]:
    routed = extract_document_observations(table, field, text, document_type)
    if routed:
        return routed
    rule = RULES.get((table, re.sub(r"^P\d{3}\s+", "", field)))
    if not rule:
        return []
    observations = []
    for sentence in sentences(text):
        for match in rule.finditer(sentence):
            start, end = max(0, match.start() - 80), min(len(sentence), match.end() + 80)
            observations.append({
                "value": match.group("value"), "unit": match.group("unit"),
                "quote": sentence[start:end], "comparability_score": 3,
                "label": "代理值", "source_scope": "field-specific published observation",
            })
    return observations


def frozen_candidate(result: dict[str, Any], workspace: Path) -> tuple[Path | None, list[str]]:
    reasons = []
    if result.get("fetch_status") != "fetched":
        reasons.append("payload_not_fetched")
    raw_path = Path(str(result.get("payload_path") or ""))
    try:
        path = raw_path.resolve()
        path.relative_to(workspace.resolve())
    except (ValueError, OSError):
        reasons.append("payload_outside_workspace")
        return None, reasons
    expected = str(result.get("content_sha256") or "")
    if not path.is_file() or not re.fullmatch(r"[a-f0-9]{64}", expected):
        reasons.append("payload_or_hash_missing")
    elif digest(path) != expected:
        reasons.append("payload_hash_mismatch")
    return path, reasons


def region_for(query: dict[str, Any], result: dict[str, Any]) -> str:
    host = (urlsplit(str(result.get("url") or "")).hostname or "").lower()
    return "CN" if query.get("language") == "zh" and (host.endswith(".cn") or re.search(r"[\u3400-\u9fff]", str(result.get("title") or ""))) else "INT"


def corroborated(proposal: dict[str, Any], proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if proposal["source_class"] in AUTHORITATIVE | INSTITUTIONAL:
        return [proposal]
    value = float(proposal["observation"]["value"])
    peers = []
    for item in proposals:
        if item["region"] != proposal["region"] or item["observation"]["unit"].lower() != proposal["observation"]["unit"].lower():
            continue
        other = float(item["observation"]["value"])
        if abs(other - value) <= max(abs(value), 1.0) * 0.02:
            peers.append(item)
    hosts = {(urlsplit(item["url"]).hostname or "").lower() for item in peers}
    return peers if len(hosts) >= 2 else []


def register_source(collection: dict[str, Any], proposal: dict[str, Any], workspace: Path) -> str:
    sid = source_id(proposal["url"])
    path = Path(proposal["payload_path"]).resolve()
    entry = {
        "id": sid, "title": proposal["title"] or proposal["url"],
        "type": "verified-public-table-evidence", "version": "frozen-fetch",
        "locator": f"{proposal['field']}: {proposal['observation']['quote'][:300]}",
        "authority": (urlsplit(proposal["url"]).hostname or "public-source").removeprefix("www."),
        "region": proposal["region"], "status": "verified", "url": proposal["url"],
        "local_path": str(path.relative_to(workspace.resolve())),
        "sha256": proposal["content_sha256"],
        "excerpt_seeds": [proposal["observation"]["quote"]],
        "verified_via": "frozen payload hash plus field-specific extraction, explicit denominator and bounded proxy review",
    }
    existing = {str(item.get("id")): item for item in collection.get("sources", [])}
    if sid not in existing:
        collection.setdefault("sources", []).append(entry)
    return sid


def select_evidence(collection: dict[str, Any], matrix: dict[str, Any], workspace: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if matrix.get("coverage_status") != "executed":
        raise ValueError("table evidence selection requires an executed search matrix")
    proposals_by_field: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    audits: list[dict[str, Any]] = []
    for query in matrix.get("queries", []):
        table, field = str(query.get("table")), str(query.get("field"))
        for result in query.get("results", []):
            path, reasons = frozen_candidate(result, workspace)
            observations = [] if reasons else extract_observations(
                table, field, " ".join(str(result.get(key) or "") for key in ("title", "snippet", "excerpt")),
                str(result.get("document_type") or query.get("document_type") or ""),
            )
            if not observations and not reasons:
                if str(result.get("document_type") or query.get("document_type") or "") == "product_carbon_footprint":
                    reasons.append("pcf_not_decomposable_to_unit_process_or_reference_product_mismatch")
                else:
                    reasons.append("no_field_specific_observation")
            audit = {"table": table, "field": field, "language": query.get("language"),
                     "query_hash": query.get("query_hash"), "url": result.get("url"),
                     "title": result.get("title"), "decision": "rejected", "reasons": reasons,
                     "document_route": query.get("document_route"),
                     "document_type": result.get("document_type") or query.get("document_type"),
                     "public_extractability": public_extractability(table, field),
                     "extraction_support": extraction_support(table, field),
                     "observations": observations}
            audits.append(audit)
            for observation in observations:
                proposals_by_field[(table, field)].append({
                    **audit, "observation": observation, "source_class": result.get("source_class"),
                    "content_sha256": result.get("content_sha256"), "payload_path": str(path),
                    "region": region_for(query, result), "url": str(result.get("url")),
                })

    accepted: list[dict[str, Any]] = []
    field_reports = []
    matrix_sha = hashlib.sha256(json.dumps(matrix, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    query_hashes_by_field: dict[tuple[str, str], list[str]] = defaultdict(list)
    for query in matrix.get("queries", []):
        key = (str(query.get("table")), str(query.get("field")))
        query_hash = str(query.get("query_hash") or "")
        if query_hash and query_hash not in query_hashes_by_field[key]:
            query_hashes_by_field[key].append(query_hash)
    for table, rows in collection.get("tables", {}).items():
        for row in rows:
            field = str(row["field"]); proposals = proposals_by_field.get((table, field), [])
            for source_key, value_key in (("source", "value"), ("int_source", "int_value"),
                                          ("cn_source", "cn_value")):
                if source_key in row and str(row.get(value_key) or "").startswith("缺口"):
                    row[source_key] = ""
            # Upstream-authored Golden values remain frozen. Values produced by
            # this selector are re-evaluated on a targeted route retry so a
            # better-scoped source can supersede an earlier broad proxy.
            selector_generated = str(row.get("pedigree") or "").startswith("verified_public_proxy")
            if row.get("status") == "populated" and not selector_generated:
                source_keys = [key for key in ("source", "int_source", "cn_source")
                               if row.get(key) and not str(row.get(key)).startswith("internal-")]
                source = str(row[source_keys[0]]) if source_keys else ""
                record = {"table": table, "field": field, "track": "preverified",
                          "value": str(row.get("value") or row.get("int_value") or row.get("cn_value") or ""),
                          "unit": str(row.get("unit") or ""), "source_id": source,
                          "supporting_source_ids": [source] if source else [],
                          "quote": "", "decision": "accepted_preverified_input",
                          "verification_mode": "upstream_independent_verification"}
                accepted.append(record)
                field_reports.append({"table": table, "field": field, "decision": "populated",
                                      "candidate_count": len(proposals), "evidence": record})
                continue
            if selector_generated:
                row["int_value"] = "缺口：当前检索未冻结可代表本节点的国际定量证据"
                row["cn_value"] = "缺口：当前检索未冻结可代表本节点的中国定量证据"
                row["status"] = "explicit_gap"
            chosen = None; support: list[dict[str, Any]] = []
            for proposal in sorted(proposals, key=lambda item: int(item["observation"].get("comparability_score", 0)), reverse=True):
                peers = corroborated(proposal, proposals)
                if peers:
                    chosen, support = proposal, peers
                    break
            if chosen:
                sid = register_source(collection, chosen, workspace)
                for peer in support:
                    register_source(collection, peer, workspace)
                track = "cn" if chosen["region"] == "CN" else "int"
                if table in {"flows", "emissions", "indicators", "params"}:
                    label = str(chosen["observation"].get("label") or "代理值")
                    row[f"{track}_value"] = f"〔{label}〕{chosen['observation']['value']}"
                    row[f"{track}_source"] = sid
                    row["unit"] = chosen["observation"]["unit"]
                    row["basis"] = "proxy"
                    row["pedigree"] = ("verified_public_proxy_with_explicit_derivation"
                                       if chosen["observation"].get("derivation")
                                       else "verified_public_proxy")
                    row["status"] = "populated"
                    if table == "indicators":
                        row["mapping_status"] = "bounded_public_proxy"
                elif table == "props":
                    label = str(chosen["observation"].get("label") or "代理值")
                    row["value"] = f"〔{label}〕{chosen['observation']['value']}"
                    row["source"] = sid
                    row["unit"] = chosen["observation"]["unit"]
                    row["pedigree"] = "verified_public_proxy_with_explicit_derivation"
                    row["status"] = "populated"
                record = {"table": table, "field": field, "track": track,
                          "value": chosen["observation"]["value"], "unit": chosen["observation"]["unit"],
                          "source_id": sid, "url": chosen["url"],
                          "supporting_source_ids": sorted({source_id(x["url"]) for x in support}),
                          "quote": chosen["observation"]["quote"],
                          "derivation": chosen["observation"].get("derivation", ""),
                          "source_scope": chosen["observation"].get("source_scope", ""),
                          "reference_product": chosen["observation"].get("reference_product", ""),
                          "comparability_score": chosen["observation"].get("comparability_score", 0),
                          "proxy_role": chosen["observation"].get("proxy_role", "reference_value"),
                          "document_route": chosen.get("document_route"),
                          "document_type": chosen.get("document_type"),
                          "decision": "accepted_as_proxy",
                          "verification_mode": ("authoritative_single_source"
                                                if chosen["source_class"] in AUTHORITATIVE
                                                else "institutional_single_source"
                                                if chosen["source_class"] in INSTITUTIONAL
                                                else "independent_two_source_corroboration")}
                accepted.append(record)
                field_reports.append({"table": table, "field": field, "decision": "populated",
                                      "candidate_count": len(proposals), "evidence": record})
            else:
                availability = public_extractability(table, field)
                support = extraction_support(table, field)
                reason = (
                    "uncorroborated_public_proxy" if proposals
                    else "field_requires_node_specific_internal_record"
                    if availability == "internal_record_only"
                    else "public_extraction_rule_missing" if support == "not_implemented"
                    else "no_field_specific_observation"
                    if query_hashes_by_field.get((table, field))
                    else "field_specific_search_not_executed"
                )
                rejected = sorted({
                    str(audit.get("url") or "") for audit in audits
                    if audit.get("table") == table and audit.get("field") == field
                    and audit.get("url")
                })
                row["gap_evidence"] = {
                    "protocol": "wiki-table-gap-evidence-v1",
                    "reason": reason,
                    "matrix_sha256": matrix_sha,
                    "query_hashes": query_hashes_by_field.get((table, field), []),
                    "rejected_candidate_urls": rejected,
                }
                field_reports.append({"table": table, "field": field, "decision": "explicit_gap",
                                      "candidate_count": len(proposals), "reason": reason,
                                      "public_extractability": availability,
                                      "extraction_support": support,
                                      "gap_evidence": row["gap_evidence"]})

            # A row may be only partially populated (for example INT has a
            # defensible proxy while CN remains unknown).  Preserve evidence
            # for every unresolved cell, not merely for wholly empty rows.
            gap_tracks: dict[str, dict[str, Any]] = {}
            rejected = sorted({
                str(audit.get("url") or "") for audit in audits
                if audit.get("table") == table and audit.get("field") == field
                and audit.get("url")
            })
            for source_key, value_key, track in (
                ("source", "value", "value"),
                ("int_source", "int_value", "int"),
                ("cn_source", "cn_value", "cn"),
            ):
                if source_key not in row or not str(row.get(value_key) or "").startswith("缺口"):
                    continue
                gap_tracks[track] = {
                    "protocol": "wiki-table-gap-evidence-v1",
                    "reason": "no_eligible_source_for_track",
                    "matrix_sha256": matrix_sha,
                    "query_hashes": query_hashes_by_field.get((table, field), []),
                    "rejected_candidate_urls": rejected,
                }
            if gap_tracks:
                row["gap_evidence_by_track"] = gap_tracks
                field_reports[-1]["gap_tracks"] = gap_tracks

    accepted_pairs = {(row["table"], row["field"], row.get("url")) for row in accepted}
    for audit in audits:
        if (audit["table"], audit["field"], audit.get("url")) in accepted_pairs:
            audit["decision"] = "accepted"
        elif audit["observations"] and "uncorroborated_public_proxy" not in audit["reasons"]:
            audit["reasons"].append("uncorroborated_public_proxy")

    outcome = ("NO_ELIGIBLE_PUBLIC_DATA" if not accepted else
               "FULLY_POPULATED" if len(accepted) == len(field_reports) else "PARTIALLY_POPULATED")
    reason_counts = {reason: sum(row.get("reason") == reason for row in field_reports)
                     for reason in sorted({str(row.get("reason")) for row in field_reports if row.get("reason")})}
    collection["data_collection_outcome"] = outcome
    report = {"protocol": "wiki-table-evidence-selection-v1", "node_id": collection["node_id"],
              "outcome": outcome, "reason_counts": reason_counts,
              "matrix_sha256": matrix_sha,
              "counts": {"fields": len(field_reports), "populated": len(accepted),
                         "explicit_gaps": sum(x["decision"] == "explicit_gap" for x in field_reports),
                         "candidate_audits": len(audits)},
              "fields": field_reports, "accepted_evidence": accepted, "candidate_audits": audits}
    report["proof_metrics"] = {
        "field_observations": sum(len(audit["observations"]) for audit in audits),
        "accepted_observations": sum(row.get("decision") == "accepted_as_proxy" for row in accepted),
        "populated_fields": sum(row.get("decision") == "populated" for row in field_reports),
        "unsupported_fields_misclassified_as_internal_only": sum(
            row.get("reason") == "field_requires_node_specific_internal_record"
            and row.get("public_extractability") != "internal_record_only"
            for row in field_reports
        ),
        "gap_provenance_preserved": all(
            row.get("decision") != "explicit_gap" or bool(row.get("gap_evidence", {}).get("query_hashes"))
            for row in field_reports
        ),
    }
    return collection, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection", type=Path); parser.add_argument("matrix", type=Path)
    parser.add_argument("output", type=Path); parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args()
    collection, matrix = load(args.collection), load(args.matrix)
    collection, report = select_evidence(collection, matrix, args.workspace.resolve())
    report["matrix_sha256"] = digest(args.matrix)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    collection["evidence_selection_sha256"] = digest(args.output)
    collection["search_matrix_sha256"] = digest(args.matrix)
    args.collection.write_text(json.dumps(collection, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), **report["counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
