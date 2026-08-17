#!/usr/bin/env python3
"""Stage, gate and hash-lock node-Wiki evidence-table backfills.

The content pipeline and table-data pipeline are deliberately separate.  This
tool never invents a value from a Blueprint: every populated or explicit-gap
cell must be frozen in a collection record and resolve to a verified registry
source.  ``stage`` is non-mutating, ``gate`` judges the staged candidate, and
``apply`` atomically installs only the hash-locked candidate that received GO.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any


TABLE_HEADERS = {
    "flows": ["流", "方向", "单位", "basis", "国际值 INT", "国际源 INT", "中国值 CN", "中国源 CN", "pedigree"],
    "emissions": ["substance", "CAS", "compartment", "unit", "basis", "国际值 INT", "国际源 INT", "中国值 CN", "中国源 CN", "pedigree"],
    "indicators": ["indicator", "medium", "unit", "basis", "国际值 INT", "国际源 INT", "中国值 CN", "中国源 CN", "mapping_status", "pedigree"],
    "props": ["property", "condition", "unit", "值", "源", "pedigree"],
    "params": ["parameter", "geo", "unit", "basis", "国际值 INT", "国际源 INT", "中国值 CN", "中国源 CN", "pedigree"],
    "quality": ["field", "unit", "basis", "中国项目值 CN", "中国源 CN", "proxy_policy", "pedigree"],
}
TABLE_KEYS = {
    "flows": ["field", "direction", "unit", "basis", "int_value", "int_source", "cn_value", "cn_source", "pedigree"],
    "emissions": ["field", "cas", "compartment", "unit", "basis", "int_value", "int_source", "cn_value", "cn_source", "pedigree"],
    "indicators": ["field", "medium", "unit", "basis", "int_value", "int_source", "cn_value", "cn_source", "mapping_status", "pedigree"],
    "props": ["field", "condition", "unit", "value", "source", "pedigree"],
    "params": ["field", "geo", "unit", "basis", "int_value", "int_source", "cn_value", "cn_source", "pedigree"],
    "quality": ["field", "unit", "basis", "cn_value", "cn_source", "proxy_policy", "pedigree"],
}
NULL_VALUES = {"", "—", "-", "待采", "待核", "待评", "tbd", "n/a", "na"}


def is_gap(value: Any) -> bool:
    normalized = str(value).strip().lower()
    return (normalized in NULL_VALUES or normalized.startswith("缺口：")
            or normalized.startswith("缺口:") or normalized.startswith("未公开"))


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_path(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def parse_list(raw: str) -> list[str]:
    raw = raw.strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        return []
    return [x.strip().strip('"') for x in raw[1:-1].split(",") if x.strip()]


def source_ids(collection: dict[str, Any]) -> set[str]:
    return {str(item["id"]) for item in collection["sources"]}


def validate_collection(collection: dict[str, Any], root: Path) -> dict[str, int]:
    protocol = collection.get("protocol") or {}
    if protocol != {"version": "wiki-table-evidence-v1", "kind": "node-table-collection"}:
        raise ValueError("collection protocol 非 wiki-table-evidence-v1")
    if not re.fullmatch(r"[PA]\d{3}", str(collection.get("node_id") or "")):
        raise ValueError("node_id 非法")
    reference = collection.get("reference_configuration") or {}
    for key in ("manufacturer", "model", "scope", "freeze_rule"):
        if not str(reference.get(key) or "").strip():
            raise ValueError(f"reference_configuration.{key} 缺失")
    ids: set[str] = set()
    for source in collection.get("sources") or []:
        sid = str(source.get("id") or "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]+", sid) or sid in ids:
            raise ValueError(f"来源 id 非法或重复: {sid}")
        ids.add(sid)
        if source.get("status") != "verified":
            raise ValueError(f"table source 必须 verified: {sid}")
        if not re.match(r"^https?://", str(source.get("url") or "")):
            raise ValueError(f"table source URL 非法: {sid}")
        if not source.get("locator") or not source.get("excerpt_seeds") or not source.get("verified_via"):
            raise ValueError(f"table source 缺 locator/excerpt/verify: {sid}")
        local_path = str(source.get("local_path") or "")
        expected = str(source.get("sha256") or "")
        if local_path:
            path = root / local_path
            if not path.is_file() or not re.fullmatch(r"[a-f0-9]{64}", expected):
                raise ValueError(f"冻结来源文件缺失或 hash 非法: {sid}")
            if digest_path(path) != expected:
                raise ValueError(f"冻结来源文件 hash 漂移: {sid}")
    node_type = str(collection.get("node_type") or ("activity" if str(collection["node_id"]).startswith("A") else "product"))
    required_kinds = (["flows", "props", "params", "emissions", "indicators", "quality"]
                      if node_type == "activity" else ["props", "params", "quality"])
    metrics: dict[str, int] = {}
    tables = collection.get("tables") or {}
    if set(tables) != set(required_kinds):
        raise ValueError(f"{node_type} tables 必须恰为 {required_kinds}")
    for kind in required_kinds:
        keys = TABLE_KEYS[kind]
        rows = tables.get(kind) or []
        seen: set[str] = set()
        for row in rows:
            missing = [key for key in keys if key not in row]
            if missing:
                raise ValueError(f"{kind} row 缺字段 {missing}")
            field = str(row["field"])
            if not field or field in seen:
                raise ValueError(f"{kind} field 空或重复: {field}")
            seen.add(field)
            if row.get("status") not in {"populated", "explicit_gap", "assessed"}:
                raise ValueError(f"{kind}.{field} status 非法")
            gap_required = bool(collection.get("gap_provenance_required"))
            gap_by_track = row.get("gap_evidence_by_track") or {}
            for key in keys:
                if key.endswith("source") or key == "source":
                    value_key = ("value" if key == "source" else key.removesuffix("_source") + "_value")
                    value_is_gap = is_gap(str(row.get(value_key) or ""))
                    if gap_required and value_is_gap and not str(row[key]).strip():
                        track = "value" if key == "source" else key.removesuffix("_source")
                        gap = gap_by_track.get(track) or row.get("gap_evidence") or {}
                        if (gap.get("protocol") != "wiki-table-gap-evidence-v1"
                                or not gap.get("reason") or not gap.get("matrix_sha256")
                                or not gap.get("query_hashes")):
                            raise ValueError(f"{kind}.{field}.{track} 显式缺口缺少检索证据")
                        continue
                    if str(row[key]) not in ids:
                        raise ValueError(f"{kind}.{field} 来源未冻结: {row[key]}")
        metrics[f"{kind}_rows"] = len(rows)
    metrics["props_populated"] = sum(r["status"] == "populated" for r in tables.get("props", []))
    metrics["flows_populated"] = sum(r["status"] == "populated" for r in tables.get("flows", []))
    metrics["emissions_populated"] = sum(r["status"] == "populated" for r in tables.get("emissions", []))
    metrics["indicators_populated"] = sum(r["status"] == "populated" for r in tables.get("indicators", []))
    metrics["params_int_populated"] = sum(
        r["status"] == "populated" and not is_gap(r["int_value"])
        for r in tables["params"]
    )
    metrics["params_cn_populated"] = sum(
        r["status"] == "populated" and not is_gap(r["cn_value"])
        for r in tables["params"]
    )
    metrics["quality_assessed"] = sum(r["status"] == "assessed" for r in tables["quality"])
    return metrics


def population_floor_checks(metrics: dict[str, int], thresholds: dict[str, Any]) -> dict[str, bool]:
    """Apply every declared population floor, including the optional CN track."""
    checks = {
        "params_int_population_floor": metrics["params_int_populated"] >= int(thresholds["params_int_populated"]),
        "params_cn_population_floor": metrics["params_cn_populated"] >= int(thresholds.get("params_cn_populated", 0)),
        "quality_assessment_floor": metrics["quality_assessed"] >= int(thresholds["quality_assessed"]),
    }
    for kind in ("props", "flows", "emissions", "indicators"):
        key = f"{kind}_populated"
        if key in thresholds:
            checks[f"{kind}_population_floor"] = metrics[key] >= int(thresholds[key])
    return checks


def registry_entry(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": source["title"], "type": source["type"], "version": source["version"],
        "locator": source["locator"], "authority": source["authority"],
        "hash": source.get("sha256", ""), "ref_count": 1,
        "excerpt_seeds": source["excerpt_seeds"], "status": "verified",
        "region": source["region"], "verified_via": source["verified_via"],
        "url": source["url"],
    }


def render_table(kind: str, rows: list[dict[str, Any]]) -> str:
    header = "| " + " | ".join(TABLE_HEADERS[kind]) + " |"
    rule = "|" + "|".join("---" for _ in TABLE_HEADERS[kind]) + "|"
    body = ["| " + " | ".join(cell(row[key]) for key in TABLE_KEYS[kind]) + " |" for row in rows]
    return "\n".join([header, rule, *body])


def replace_table(page: str, kind: str, rendered: str) -> str:
    pattern = re.compile(rf"(<!-- EV:{kind}:START -->)\n.*?\n(<!-- EV:{kind}:END -->)", re.S)
    if len(pattern.findall(page)) != 1:
        raise ValueError(f"页面 {kind} marker 非唯一")
    return pattern.sub(rf"\1\n{rendered}\n\2", page, count=1)


def ensure_activity_props_marker(page: str) -> str:
    """Upgrade a legacy five-table activity page before table replacement."""
    if page.count("<!-- EV:props:START -->") == page.count("<!-- EV:props:END -->") == 1:
        return page
    if "<!-- EV:props:START -->" in page or "<!-- EV:props:END -->" in page:
        raise ValueError("页面 props marker 不成对或不唯一")
    anchor = re.search(r"(?m)^## [^\n]+\n\n(?=<!-- EV:params:START -->)", page)
    if not anchor:
        raise ValueError("旧活动页缺少可定位的 params 区段，无法迁移六表合同")
    shell = (
        "## 参考产品性质与交接状态\n\n"
        "<!-- EV:props:START -->\n"
        "| property | condition | unit | 值 | 源 | pedigree |\n"
        "|---|---|---|---|---|---|\n"
        "<!-- EV:props:END -->\n\n"
    )
    return page[:anchor.start()] + shell + page[anchor.start():]


def set_frontmatter(page: str, key: str, value: str) -> str:
    pattern = re.compile(rf"(?m)^{re.escape(key)}:\s*.*$")
    if not pattern.search(page):
        raise ValueError(f"frontmatter 缺 {key}")
    return pattern.sub(f"{key}: {value}", page, count=1)


def render_change_log(collection: dict[str, Any]) -> tuple[str, str]:
    item = collection.get("change_log")
    if item:
        date = str(item.get("date") or "").strip()
        title = str(item.get("title") or "").strip()
        bullets = item.get("bullets") or []
        if not date or not title or not bullets or not all(str(x).strip() for x in bullets):
            raise ValueError("change_log 必须包含 date/title/non-empty bullets")
        heading = f"### {date} · {title}"
        body = heading + "\n\n" + "\n".join(f"- {str(x).strip()}" for x in bullets) + "\n"
        return heading, "\n" + body
    node_id = str(collection.get("node_id") or "节点")
    heading = f"### 2026-08-11 · {node_id} 参考配置表格数据回填"
    body = (
        "\n" + heading + "\n\n"
        "- **参考配置：** Dell PowerEdge FC430 官方 PCF 的最高销量配置；规格边界由 Dell 官方规格表交叉约束。\n"
        "- **新增数据：** 回填质量、处理器、内存、存储、网络、共享机箱接口、地理和代表期；中国型号值、公开 BOM、供应商与包装数据保持显式缺口。\n"
        "- **适用限制：** 本批次只形成单一国际参考配置，不代表 P003 全部型号，也不构成中国实测前景 LCI。\n"
        "- **工程控制：** 数据先进入冻结 collection，经 Table Population Gate 后再按页面与 registry 双 hash 原子应用。\n"
    )
    return heading, body


def build_candidate(collection: dict[str, Any], page: str, registry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    node_id = collection["node_id"]
    if not re.search(rf"(?m)^id:\s*{re.escape(node_id)}\s*$", page):
        raise ValueError("collection 与页面 node_id 不一致")
    candidate = page
    if collection.get("node_type") == "activity" and "props" in collection["tables"]:
        candidate = ensure_activity_props_marker(candidate)
    for kind in collection["tables"]:
        candidate = replace_table(candidate, kind, render_table(kind, collection["tables"][kind]))
    # Typed tables supersede the retired monolithic quantity shell.
    candidate = re.sub(
        r"\n## 🔒 数量（待挂 · NOT POPULATED）\n.*?(?=\n## |\Z)",
        "\n", candidate, flags=re.S,
    )
    candidate = set_frontmatter(candidate, "quantity_status", "partial")
    candidate = set_frontmatter(candidate, "dataset_readiness", "reference_configuration_only")
    fm_match = re.search(r"(?m)^provenance_refs:\s*(.*)$", candidate)
    if not fm_match:
        raise ValueError("frontmatter 缺 provenance_refs")
    body = re.search(r"<!-- BODY:START -->(.*?)<!-- BODY:END -->", candidate, re.S)
    body_refs = set(re.findall(r"\[\^([a-z0-9-]+)\](?!:)", body.group(1) if body else ""))
    table_refs = {
        str(value).split("#", 1)[0].strip()
        for rows in collection.get("tables", {}).values()
        for row in rows
        for key, value in row.items()
        if (key == "source" or key.endswith("_source")) and str(value).strip()
    }
    refs = sorted(body_refs | table_refs)
    candidate = re.sub(
        r"(?m)^provenance_refs:\s*.*$",
        "provenance_refs: [" + ", ".join(refs) + "]",
        candidate, count=1,
    )
    if re.search(r"(?m)^schema_version:\s*wiki-v2\s*$", candidate):
        candidate = set_frontmatter(candidate, "provenance_status", "source_verified")
        candidate = set_frontmatter(candidate, "claim_verification_status", "partial")
    log_heading, log = render_change_log(collection)
    marker = "<!-- CHANGELOG:START -->\n## 修改日志\n"
    if marker not in candidate:
        raise ValueError("页面缺 changelog marker")
    if log_heading not in candidate:
        candidate = candidate.replace(marker, marker + log, 1)
    staged_registry = json.loads(json.dumps(registry, ensure_ascii=False))
    entries = staged_registry.setdefault("sources", {})
    for source in collection["sources"]:
        sid = source["id"]
        incoming = registry_entry(source)
        if sid in entries and entries[sid] != incoming:
            current = entries[sid]
            if current.get("url") != incoming.get("url") or current.get("title") != incoming.get("title"):
                raise ValueError(f"registry source 冲突: {sid}")
            incoming["excerpt_seeds"] = list(dict.fromkeys(
                [*current.get("excerpt_seeds", []), *incoming.get("excerpt_seeds", [])]
            ))
            incoming["locator"] = "; ".join(dict.fromkeys(
                part.strip() for part in [str(current.get("locator", "")), str(incoming.get("locator", ""))]
                if part.strip()
            ))
            incoming["ref_count"] = max(int(current.get("ref_count", 1)), int(incoming.get("ref_count", 1)))
        entries[sid] = incoming
    return candidate, staged_registry


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def command_stage(args: argparse.Namespace) -> int:
    collection = load(args.collection)
    metrics = validate_collection(collection, args.root)
    original_page = args.page.read_text(encoding="utf-8")
    original_registry = load(args.registry)
    candidate, staged_registry = build_candidate(collection, original_page, original_registry)
    args.output.mkdir(parents=True, exist_ok=True)
    page_path = args.output / "candidate.md"
    registry_path = args.output / "registry.json"
    page_path.write_text(candidate, encoding="utf-8")
    registry_path.write_text(dump(staged_registry), encoding="utf-8")
    report = {
        "protocol": "wiki-table-stage-v1", "node_id": collection["node_id"],
        "collection_sha256": digest_path(args.collection), "metrics": metrics,
        "original_page_sha256": digest_path(args.page),
        "original_registry_sha256": digest_path(args.registry),
        "candidate_page_sha256": digest_path(page_path),
        "candidate_registry_sha256": digest_path(registry_path),
        "candidate_page": str(page_path), "candidate_registry": str(registry_path),
    }
    (args.output / "stage-report.json").write_text(dump(report), encoding="utf-8")
    print(dump(report), end="")
    return 0


def command_gate(args: argparse.Namespace) -> int:
    collection = load(args.collection)
    metrics = validate_collection(collection, args.root)
    page = args.page.read_text(encoding="utf-8")
    registry = load(args.registry).get("sources", {})
    checks = {
        "reference_configuration_frozen": bool(collection.get("reference_configuration")),
        "deprecated_quantity_shell_removed": "🔒 数量（待挂 · NOT POPULATED）" not in page,
        "quantity_status_partial": bool(re.search(r"(?m)^quantity_status:\s*partial\s*$", page)),
        "dataset_readiness_scoped": bool(re.search(r"(?m)^dataset_readiness:\s*reference_configuration_only\s*$", page)),
        "all_table_sources_verified": all(
            registry.get(sid, {}).get("status") == "verified" and re.match(r"^https?://", registry.get(sid, {}).get("url", ""))
            for sid in source_ids(collection)
        ),
        "no_placeholder_table_values": not any(token in page for token in ("| 待采 |", "| 待核 |", "| 待评 |")),
    }
    checks.update(population_floor_checks(metrics, collection["thresholds"]))
    verdict = "GO" if all(checks.values()) else "NO_GO"
    report = {"protocol": "wiki-table-population-gate-v1", "node_id": collection["node_id"],
              "verdict": verdict, "checks": checks, "metrics": metrics,
              "page_sha256": digest_path(args.page), "registry_sha256": digest_path(args.registry)}
    args.output.write_text(dump(report), encoding="utf-8")
    print(dump(report), end="")
    return 0 if verdict == "GO" else 2


def command_apply(args: argparse.Namespace) -> int:
    stage = load(args.stage / "stage-report.json")
    gate = load(args.gate)
    if gate.get("verdict") != "GO":
        raise ValueError("Table Population Gate 未 GO")
    if digest_path(args.page) != stage["original_page_sha256"] or digest_path(args.registry) != stage["original_registry_sha256"]:
        raise ValueError("apply 前原始页面或 registry hash 漂移")
    candidate_page = args.stage / "candidate.md"
    candidate_registry = args.stage / "registry.json"
    if digest_path(candidate_page) != stage["candidate_page_sha256"] or digest_path(candidate_registry) != stage["candidate_registry_sha256"]:
        raise ValueError("staged candidate hash 漂移")
    if gate.get("page_sha256") != stage["candidate_page_sha256"] or gate.get("registry_sha256") != stage["candidate_registry_sha256"]:
        raise ValueError("gate 与 staged candidate hash 不一致")
    atomic_write(args.page, candidate_page.read_bytes())
    atomic_write(args.registry, candidate_registry.read_bytes())
    report = {"protocol": "wiki-table-apply-v1", "status": "applied", "node_id": gate["node_id"],
              "page_sha256": digest_path(args.page), "registry_sha256": digest_path(args.registry)}
    args.output.write_text(dump(report), encoding="utf-8")
    print(dump(report), end="")
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage")
    for cmd in (stage,):
        cmd.add_argument("--collection", type=Path, required=True); cmd.add_argument("--page", type=Path, required=True)
        cmd.add_argument("--registry", type=Path, required=True); cmd.add_argument("--root", type=Path, default=Path("."))
    stage.add_argument("--output", type=Path, required=True); stage.set_defaults(func=command_stage)
    gate = sub.add_parser("gate")
    gate.add_argument("--collection", type=Path, required=True); gate.add_argument("--page", type=Path, required=True)
    gate.add_argument("--registry", type=Path, required=True); gate.add_argument("--root", type=Path, default=Path("."))
    gate.add_argument("--output", type=Path, required=True); gate.set_defaults(func=command_gate)
    apply = sub.add_parser("apply")
    apply.add_argument("--stage", type=Path, required=True); apply.add_argument("--gate", type=Path, required=True)
    apply.add_argument("--page", type=Path, required=True); apply.add_argument("--registry", type=Path, required=True)
    apply.add_argument("--output", type=Path, required=True); apply.set_defaults(func=command_apply)
    return ap


if __name__ == "__main__":
    args = parser().parse_args()
    raise SystemExit(args.func(args))
