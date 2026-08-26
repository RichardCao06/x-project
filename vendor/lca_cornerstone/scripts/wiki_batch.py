#!/usr/bin/env python3
"""Batch production control plane for node Wiki provenance releases.

This script deliberately keeps file I/O and safety checks outside the Workflow
runtime.  Production Workflow agents only nominate claims or Verify frozen
evidence; legacy SearchFetch agents remain pilot-only.

The release journal enforces planned -> prepared -> research_ready -> verified
-> frozen -> apply_ready -> gated -> applied -> published.  ``blocked`` and
``failed`` are resumable only with an explicit ``--resume``.

Production never runs the built-in SearchFetch Workflow templates.  Empty
pages use nomination-only Extract; deterministic evidence is then embedded in
verify-only Workflows.  Content Apply is a draft-only substep at ``frozen``;
post-apply claim coverage authorizes ``apply_ready``; all release gates run
before the privileged reviewed Apply, so a failed gate can never leave behind
an invalid reviewed page.

The ``--pilot`` mode samples at most three existing claims per node and limits
new-node extraction to three claims.  Pilot results validate orchestration only;
they MUST NOT upgrade a whole page to ``body_status=reviewed``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from assemble_wiki_from_ku import render_footnotes, render_node
from ku_distill import distill
from merge_wiki_ku import (
    MergeError,
    apply_plan,
    apply_text_transaction,
    plan_extract_merge,
    plan_merge,
    rehydrate_committed_plan,
    transaction_matches_plan,
)
from prep_node_wiki import atomize, splice
from validate_wiki_workflow import (
    _extract_binding,
    validate_nomination_result,
    validate_result,
    validate_workflow,
)
from wiki_source_discovery import validate_evidence
from wiki_draft_content_gate import gate_merge_plan
from wiki_quality_contract import (
    CLAIM_KINDS,
    EVIDENCE_TABLES,
    MAX_CLAIMS_PER_REQUIREMENT,
    OPTIONAL_EVIDENCE_TABLES,
    SECTIONS,
    expected_claim_kind,
    minimum_claims_for_requirement,
    minimum_nomination_claims,
    nomination_requirements,
)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".claude" / "workflows"
BATCH_PROTOCOL = "wiki-batch-v2"
COVERAGE_PROTOCOL = "wiki-claim-coverage-v1"
EVIDENCE_PROTOCOL = "wiki-source-evidence-v1"
USAGE_PROTOCOL = "wiki-usage-v1"
MODEL_POLICY = {
    "nomination": {"model": "gpt-5.6-terra", "reasoning_effort": "medium"},
    "verify_only": {"model": "gpt-5.6-sol", "reasoning_effort": "medium"},
    "release_checker": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
}
LINEAR_STATES = [
    "planned", "prepared", "research_ready", "verified", "frozen",
    "apply_ready", "gated", "applied", "published",
]
TERMINAL_STATES = {"blocked", "failed"}
LEGAL_TRANSITIONS = {
    state: {LINEAR_STATES[index + 1], "blocked", "failed"}
    for index, state in enumerate(LINEAR_STATES[:-1])
}
LEGAL_TRANSITIONS["published"] = set()
LEGAL_TRANSITIONS["blocked"] = set(LINEAR_STATES) | {"failed"}
LEGAL_TRANSITIONS["failed"] = set(LINEAR_STATES) | {"blocked"}
PILOT_ICT10 = [
    "P040", "P042",                 # current product exemplars
    "P004", "P019", "A002", "A011", # legacy / transitional bodies
    "P041", "P066", "A003", "A018", # empty product/activity pages
]

PRODUCT_SECTIONS = SECTIONS["product"]
ACTIVITY_SECTIONS = SECTIONS["activity"]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def file_record(path: Path) -> dict[str, str]:
    path = path.resolve()
    return {"path": str(path), "sha256": sha256(path.read_text(encoding="utf-8"))}


def journal_path_for(batch_dir: Path) -> Path:
    return batch_dir / "journal.json"


def init_journal(batch_dir: Path, manifest_path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    journal = {
        "protocol": {"version": BATCH_PROTOCOL, "kind": "release-journal"},
        "state": "planned",
        "manifest": file_record(manifest_path),
        "artifacts": {},
        "history": [{
            "from": None,
            "to": "planned",
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "command": "plan",
        }],
    }
    if not dry_run:
        write_json(journal_path_for(batch_dir), journal)
    return journal


def read_journal(batch_dir: Path) -> dict[str, Any]:
    path = journal_path_for(batch_dir)
    if not path.exists():
        raise ValueError(f"缺少批次 journal: {path}")
    journal = read_json(path)
    if (journal.get("protocol") or {}).get("kind") != "release-journal":
        raise ValueError("journal 协议不受支持")
    if journal.get("state") not in set(LINEAR_STATES) | TERMINAL_STATES:
        raise ValueError(f"journal 状态非法: {journal.get('state')}")
    return journal


def transition_journal(
    batch_dir: Path,
    new_state: str,
    command: str,
    *,
    artifacts: dict[str, Any] | None = None,
    resume: bool = False,
    dry_run: bool = False,
    detail: str = "",
    repair_rewind: bool = False,
) -> dict[str, Any]:
    journal = read_journal(batch_dir)
    old_state = journal["state"]
    if old_state == new_state:
        if not resume:
            raise ValueError(f"批次已处于 {new_state}；重复执行须显式传 --resume")
        if artifacts:
            journal.setdefault("artifacts", {}).update(artifacts)
        journal.setdefault("history", []).append({
            "from": old_state,
            "to": new_state,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "command": command,
            "detail": "resume",
        })
        if not dry_run:
            write_json(journal_path_for(batch_dir), journal)
        return journal
    transition_base = old_state
    if old_state in TERMINAL_STATES:
        if not resume:
            raise ValueError(f"批次处于 {old_state}；恢复须显式传 --resume")
        transition_base = str(journal.get("resume_from") or "")
        if transition_base not in LINEAR_STATES:
            raise ValueError(f"journal 缺少合法 resume_from: {transition_base!r}")
        if new_state == transition_base:
            journal["state"] = new_state
            journal.pop("resume_from", None)
            if artifacts:
                journal.setdefault("artifacts", {}).update(artifacts)
            journal.setdefault("history", []).append({
                "from": old_state,
                "to": new_state,
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "command": command,
                "detail": detail or "resume recovered current stage",
            })
            if not dry_run:
                write_json(journal_path_for(batch_dir), journal)
            return journal
    if repair_rewind and resume and new_state in LINEAR_STATES and transition_base in LINEAR_STATES \
            and LINEAR_STATES.index(new_state) < LINEAR_STATES.index(transition_base):
        journal["state"] = new_state
        journal.pop("resume_from", None)
        if artifacts:
            journal.setdefault("artifacts", {}).update(artifacts)
        journal.setdefault("history", []).append({
            "from": old_state,
            "to": new_state,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "command": command,
            "detail": detail or f"repair rewind from {transition_base}",
        })
        if not dry_run:
            write_json(journal_path_for(batch_dir), journal)
        return journal
    if new_state not in LEGAL_TRANSITIONS.get(transition_base, set()):
        raise ValueError(f"非法状态转移: {old_state}(resume_from={transition_base}) -> {new_state}")
    journal["state"] = new_state
    if new_state in TERMINAL_STATES:
        journal["resume_from"] = transition_base
    else:
        journal.pop("resume_from", None)
    if artifacts:
        journal.setdefault("artifacts", {}).update(artifacts)
    journal.setdefault("history", []).append({
        "from": old_state,
        "to": new_state,
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "command": command,
        "detail": detail,
    })
    if not dry_run:
        write_json(journal_path_for(batch_dir), journal)
    return journal


def mark_journal_failure(batch_dir: Path, command: str, error: Exception) -> None:
    path = journal_path_for(batch_dir)
    if not path.exists():
        return
    try:
        journal = read_journal(batch_dir)
        if journal["state"] == "published":
            return
        old = journal["state"]
        journal["state"] = "failed"
        journal["resume_from"] = old if old in LINEAR_STATES else journal.get("resume_from")
        journal.setdefault("history", []).append({
            "from": old,
            "to": "failed",
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "command": command,
            "detail": str(error),
        })
        write_json(path, journal)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass


def protocol_kind(document: dict[str, Any]) -> tuple[str, str]:
    protocol = document.get("protocol") or {}
    return str(protocol.get("version") or ""), str(protocol.get("kind") or "")


def validate_external_plan(path: Path, version: str, *, execution_mode: str) -> dict[str, Any]:
    document = read_json(path)
    actual, _ = protocol_kind(document)
    if actual != version:
        raise ValueError(f"{path} 协议必须是 {version}")
    mode = str(document.get("execution_mode") or (document.get("protocol") or {}).get("mode") or "")
    if mode != execution_mode:
        raise ValueError(f"{path} execution_mode 必须是 {execution_mode}")
    serialized = json.dumps(document, ensure_ascii=False).lower()
    if "wiki-ku-provenance.js" in serialized or "wiki-ku-provenance-repair.js" in serialized:
        raise ValueError("production 禁止引用内置 WebSearch Workflow 模板")
    return document


def validate_usage(path: Path, *, phase: str) -> dict[str, Any]:
    usage = read_json(path)
    version, _ = protocol_kind(usage)
    if version != USAGE_PROTOCOL:
        raise ValueError(f"usage 协议必须是 {USAGE_PROTOCOL}")
    actual_phase = str(usage.get("phase") or (usage.get("protocol") or {}).get("phase") or "")
    if actual_phase != phase:
        raise ValueError(f"usage.phase 必须是 {phase}")
    expected = MODEL_POLICY.get(phase)
    if expected is not None:
        actual = {
            "model": str(usage.get("model") or ""),
            "reasoning_effort": str(usage.get("reasoning_effort") or ""),
        }
        if actual != expected:
            raise ValueError(f"{phase} 实际模型配置漂移: {actual} != {expected}")
    if phase in {"nomination", "verify_only"}:
        searches = int(usage.get("search_requests", usage.get("searches", 0)) or 0)
        if searches != 0:
            raise ValueError(f"{phase} usage 不得包含搜索请求")
    cost = float(usage.get("cost_usd", -1))
    if not math.isfinite(cost) or cost < 0:
        raise ValueError("usage.cost_usd 必须是非负有限数")
    return usage


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not match:
        raise ValueError("Wiki 页面缺少 frontmatter")
    result: dict[str, str] = {}
    for line in match.group(1).splitlines():
        field = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if field:
            result[field.group(1)] = field.group(2).strip().strip('"')
    return result


def parse_list(value: str) -> list[str]:
    value = value.strip()
    if not value.startswith("[") or not value.endswith("]"):
        return []
    return [
        item.strip().strip('"').strip("'")
        for item in value[1:-1].split(",")
        if item.strip()
    ]


def find_pages(wiki_root: Path) -> dict[str, Path]:
    pages: dict[str, Path] = {}
    # Only canonical node pages participate in the batch.  Market projections
    # and other derived views may legitimately reuse local IDs.
    for folder in ("products", "activities"):
        for path in (wiki_root / folder).glob("*.md"):
            match = re.match(r"([PA]\d{3})--", path.name)
            if match:
                node_id = match.group(1)
                if node_id in pages:
                    raise ValueError(f"节点 {node_id} 有多个 Wiki 页面")
                pages[node_id] = path
    return pages


def body_of(text: str) -> str:
    match = re.search(r"<!-- BODY:START -->(.*?)<!-- BODY:END -->", text, re.S)
    if not match:
        raise ValueError("Wiki 页面缺少 BODY 标记")
    return match.group(1)


def headings(body: str) -> list[str]:
    return [
        re.sub(r"^[^A-Za-z0-9\u4e00-\u9fff]+", "", item).strip()
        for item in re.findall(r"^##\s+(.+?)\s*$", body, re.M)
    ]


def schema_status(
    node_type: str,
    body_status: str,
    body: str,
    *,
    schema_version: str = "",
    page_text: str = "",
    frontmatter: dict[str, str] | None = None,
) -> str:
    if body_status == "empty" or "正文待 workflow 填肉" in body:
        return "empty"
    required = PRODUCT_SECTIONS if node_type == "product" else ACTIVITY_SECTIONS
    present = headings(body)
    cursor = -1
    for title in required:
        try:
            cursor = present.index(title, cursor + 1)
        except ValueError:
            return "legacy"
    if schema_version != "wiki-v2":
        return "legacy"
    expected_tables = EVIDENCE_TABLES[node_type]
    if any(
        page_text.count(f"<!-- EV:{kind}:START -->") != 1
        or page_text.count(f"<!-- EV:{kind}:END -->") != 1
        for kind in expected_tables
    ):
        return "legacy"
    if "未核实·模型回忆" in body:
        return "legacy"
    # A page can satisfy the section/table shape while still being an empty
    # generated shell.  Repeated slot-level fallback text is not substantive
    # draft content and must be rebuilt from a fresh dossier.  Likewise the
    # implementation plan §14 retired the old monolithic quantity placeholder
    # in favour of the typed EV tables.
    if body.count("该 claim slot 的目标节点特异性外部证据尚未达到 CONFIRMED") >= 3:
        return "legacy"
    if re.search(r"^##\s+🔒\s*数量（待挂\s*·\s*NOT POPULATED）\s*$", page_text, re.M):
        return "legacy"
    fm = frontmatter or {}
    required_status = {
        "content_maturity", "structure_status", "provenance_status",
        "claim_verification_status", "quantity_status", "dataset_readiness",
        "change_log_status",
    }
    if any(not fm.get(key) for key in required_status):
        return "legacy"
    return "current"


def graph_inventory(graph: dict[str, Any]) -> tuple[dict[str, dict], dict[str, dict]]:
    products = {node["id"]: node for node in graph.get("products", [])}
    activities = {node["id"]: node for node in graph.get("activities", [])}
    return products, activities


def connected_ids(
    node_id: str,
    node_type: str,
    products: dict[str, dict],
    activities: dict[str, dict],
) -> dict[str, list[str]]:
    product_by_name = {node["name"]: node_id for node_id, node in products.items()}
    if node_type == "activity":
        activity = activities[node_id]
        consumes = [
            product_by_name[name]
            for name in activity.get("inputs", [])
            if name in product_by_name
        ]
        produces = [
            product_by_name[item["product"]]
            for item in activity.get("outputs", [])
            if item.get("product") in product_by_name
        ]
        return {"produces": produces, "consumes": consumes}
    produced_by = []
    consumed_by = []
    product_name = products[node_id]["name"]
    for activity_id, activity in activities.items():
        if product_name in activity.get("inputs", []):
            consumed_by.append(activity_id)
        if any(item.get("product") == product_name for item in activity.get("outputs", [])):
            produced_by.append(activity_id)
    return {"produced_by": produced_by, "consumed_by": consumed_by}


def default_batch_dir(industry: str, batch_id: str) -> Path:
    return ROOT / "runs" / "wiki-batches" / industry / batch_id


def selected_ids(args: argparse.Namespace) -> list[str]:
    if args.pilot_ict10:
        if args.industry != "ict_equipment":
            raise ValueError("--pilot-ict10 只适用于 ict_equipment")
        return PILOT_ICT10
    if args.all:
        graph = read_json(ROOT / "docs" / f"{args.industry}-name-graph.json")
        return [
            node["id"]
            for node in graph.get("products", []) + graph.get("activities", [])
        ]
    if not args.nodes:
        raise ValueError("必须传 --nodes、--all，或使用 --pilot-ict10")
    result = []
    for value in args.nodes:
        result.extend(item for item in value.split(",") if item)
    return result


def command_plan(args: argparse.Namespace) -> int:
    for label, value in (
        ("min_coverage", args.min_coverage),
        ("min_url_quote_compliance", args.min_url_quote_compliance),
        ("max_unresolved_ratio", args.max_unresolved_ratio),
        ("max_contradicted_ratio", args.max_contradicted_ratio),
        ("max_manual_ratio", args.max_manual_ratio),
    ):
        if not 0 <= value <= 1:
            raise ValueError(f"{label} 必须在 0..1")
    if args.max_search_requests < 0 or args.max_cost_usd < 0:
        raise ValueError("预算不得为负数")
    graph_path = ROOT / "docs" / f"{args.industry}-name-graph.json"
    wiki_root = ROOT / "wiki" / args.industry
    registry_path = ROOT / "sources" / args.industry / "registry.json"
    graph = read_json(graph_path)
    products, activities = graph_inventory(graph)
    pages = find_pages(wiki_root)
    source_doc = read_json(registry_path)
    verified_sources = {
        key for key, value in source_doc.get("sources", {}).items()
        if value.get("status") == "verified"
    }
    association_path = ROOT / "registry" / "lca_dataset_associations.json"
    associations = read_json(association_path).get("associations", []) if association_path.exists() else []
    associations_by_node: dict[str, int] = defaultdict(int)
    for item in associations:
        associations_by_node[item.get("node_ref", "")] += 1

    nodes = []
    for node_id in selected_ids(args):
        node_type = "product" if node_id in products else "activity" if node_id in activities else ""
        if not node_type:
            raise ValueError(f"{node_id} 不在 {graph_path}")
        if node_id not in pages:
            raise ValueError(f"{node_id} 没有 Wiki 页面")
        node = products.get(node_id) or activities[node_id]
        text = pages[node_id].read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        body = body_of(text)
        status = schema_status(
            node_type,
            fm.get("body_status", "empty"),
            body,
            schema_version=fm.get("schema_version", ""),
            page_text=text,
            frontmatter=fm,
        )
        # A legacy/non-conformant body is not repaired sentence-by-sentence:
        # that preserves the very model-recall prose the v2 contract is meant
        # to eliminate.  It is rebuilt from a fresh frozen dossier and claims.
        mode = "extract" if status == "empty" else "audit" if status == "current" else "rebuild"
        source_refs = parse_list(fm.get("provenance_refs", "[]"))
        node_ref = f"{args.industry}::{node_id}"
        required = PRODUCT_SECTIONS if node_type == "product" else ACTIVITY_SECTIONS
        dossier = {
            "node_ref": node_ref,
            "display_name": node["name"],
            "node_type": node_type,
            "boundary": node.get("boundary", "foreground" if node_type == "activity" else "unset"),
            "facets": node.get("facets", {}),
            "external": node.get("external", {}),
            "home_industry": node.get("home_industry"),
            "home_status": node.get("home_status"),
            "resolves_to": node.get("resolves_to"),
            "connections": connected_ids(node_id, node_type, products, activities),
            "required_sections": required,
            "allowed_evidence_tables": EVIDENCE_TABLES[node_type],
            "optional_evidence_tables": OPTIONAL_EVIDENCE_TABLES[node_type],
            "claim_requirements": nomination_requirements(node_type),
            "preserve_existing_evidence": all(
                text.count(f"<!-- EV:{kind}:START -->") == 1
                and text.count(f"<!-- EV:{kind}:END -->") == 1
                for kind in EVIDENCE_TABLES[node_type]
            ),
            "source_refs": source_refs,
            "verified_source_refs": [ref for ref in source_refs if ref in verified_sources],
            "lca_association_count": associations_by_node[node_ref],
        }
        nodes.append({
            "node_id": node_id,
            "node_ref": node_ref,
            "node_type": node_type,
            "display_name": node["name"],
            "page": str(pages[node_id].relative_to(ROOT)),
            "body_status": fm.get("body_status", "empty"),
            "body_schema_status": status,
            "recommended_mode": mode,
            "body_sha256": sha256(body),
            "spine_hash": fm.get("spine_hash", ""),
            "confidence": node.get("confidence", "unset"),
            "dossier": dossier,
        })

    batch_id = args.batch_id or (
        f"pilot-ict10-{dt.date.today().isoformat()}"
        if args.pilot_ict10
        else f"{args.industry}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    batch_dir = args.output or default_batch_dir(args.industry, batch_id)
    manifest = {
        "protocol": {
            "version": BATCH_PROTOCOL,
            "kind": "manifest",
            "pilot_only": bool(args.pilot),
        },
        "industry": args.industry,
        "batch_id": batch_id,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "claim_budget_per_node": args.claim_budget if args.pilot else None,
        "reviewed_upgrade_allowed": False if args.pilot else None,
        "model_policy": MODEL_POLICY,
        "release_policy": {
            "min_coverage_ratio": args.min_coverage,
            "min_url_quote_compliance_ratio": args.min_url_quote_compliance,
            "max_unresolved_ratio": args.max_unresolved_ratio,
            "max_contradicted_ratio": args.max_contradicted_ratio,
            "max_manual_review_ratio": args.max_manual_ratio,
            "max_search_requests": args.max_search_requests,
            "max_cost_usd": args.max_cost_usd,
            "required_gates": [
                "validate_graph", "wiki_lint", "lca_node_search_matrix",
                "lca_dataset_binding",
            ],
        },
        "nodes": nodes,
        "summary": {
            "nodes": len(nodes),
            "products": sum(item["node_type"] == "product" for item in nodes),
            "activities": sum(item["node_type"] == "activity" for item in nodes),
            "empty": sum(item["body_schema_status"] == "empty" for item in nodes),
            "legacy": sum(item["body_schema_status"] == "legacy" for item in nodes),
            "current": sum(item["body_schema_status"] == "current" for item in nodes),
        },
    }
    manifest_path = batch_dir / "manifest.json"
    if not args.dry_run:
        write_json(manifest_path, manifest)
        init_journal(batch_dir, manifest_path)
    print(json.dumps({
        "manifest": str(manifest_path),
        **manifest["summary"],
        "pilot_only": manifest["protocol"]["pilot_only"],
        "dry_run": args.dry_run,
    }, ensure_ascii=False))
    return 0


def sample_claims(claims: list[dict[str, Any]], budget: int | None) -> list[dict[str, Any]]:
    if not budget or len(claims) <= budget:
        return claims
    by_section: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for claim in claims:
        by_section[claim.get("section", "")].append(claim)
    selected = []
    for section in sorted(by_section):
        selected.append(by_section[section][0])
        if len(selected) == budget:
            return selected
    return selected[:budget]


def claim_count_contract(
    mode: str,
    budget: int | None,
    prepared_count: int | None = None,
    maximum_count: int | None = None,
) -> dict[str, int | str]:
    """Freeze the count promised by the generated workflow.

    Pilot extract freezes its explicit budget.  Production extract treats the
    deterministic requirement count as a minimum and permits a bounded number
    of atomic claims per requirement.  Repair counts are known after
    deterministic atomization and stay exact.
    """
    value = (budget if budget is not None else prepared_count) if mode == "extract" else prepared_count
    if value is None:
        raise ValueError(f"{mode} 缺少可冻结的断言数")
    if mode == "extract" and budget is None:
        return {
            "kind": "range",
            "min": value,
            "max": maximum_count if maximum_count is not None else value * MAX_CLAIMS_PER_REQUIREMENT,
        }
    return {"kind": "exact", "value": value}


def claim_count_matches(contract: dict[str, Any], actual: int) -> bool:
    if contract.get("kind") == "exact":
        return actual == contract.get("value")
    if contract.get("kind") == "range":
        return contract.get("min", 0) <= actual <= contract.get("max", -1)
    return False


def validate_nomination_claim_slots(
    node_id: str,
    node_type: str,
    claims: list[dict[str, Any]],
    expected_requirements: list[dict[str, str]],
) -> dict[str, int]:
    """Require every topic slot and enough claims to author a useful draft."""
    expected_by_id = {
        row["requirement_id"]: row for row in expected_requirements
    }
    expected_order = {
        row["requirement_id"]: index
        for index, row in enumerate(expected_requirements)
    }
    requirement_counts: dict[str, int] = defaultdict(int)
    last_order = -1
    for claim in claims:
        requirement_id = str(claim.get("requirement_id") or "")
        section = str(claim.get("section") or "")
        kind = str(claim.get("claim_kind") or "")
        expected = expected_by_id.get(requirement_id)
        if expected is None:
            raise ValueError(
                f"{node_id} nomination 使用未声明 requirement_id: {requirement_id!r}"
            )
        actual = {
            "requirement_id": requirement_id,
            "section": section,
            "claim_kind": kind,
        }
        if actual != expected:
            raise ValueError(
                f"{node_id}/{requirement_id} nomination slot 字段漂移: "
                f"{actual} != {expected}"
            )
        order = expected_order[requirement_id]
        if order < last_order:
            raise ValueError(
                f"{node_id} nomination claim slot 顺序漂移: {requirement_id}"
            )
        last_order = order
        requirement_counts[requirement_id] += 1
        if requirement_counts[requirement_id] > MAX_CLAIMS_PER_REQUIREMENT:
            raise ValueError(
                f"{node_id}/{requirement_id} nomination 超过每槽上限 "
                f"{MAX_CLAIMS_PER_REQUIREMENT}"
            )
        if kind not in CLAIM_KINDS:
            raise ValueError(f"{node_id}/{section} 缺少或使用非法 claim_kind: {kind!r}")
        allowed = expected_claim_kind(node_type, section)
        if kind not in allowed:
            raise ValueError(
                f"{node_id}/{section} claim_kind={kind} 绕过固定证据路由，要求 {sorted(allowed)}"
            )
    missing_requirements = [
        row["requirement_id"]
        for row in expected_requirements
        if requirement_counts[row["requirement_id"]]
        < minimum_claims_for_requirement(row)
    ]
    if missing_requirements:
        raise ValueError(
            f"{node_id} nomination 未达到各 slot 的内容下限: {missing_requirements}"
        )
    return dict(requirement_counts)


def command_prepare(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.resolve()
    manifest = read_json(manifest_path)
    industry = manifest["industry"]
    batch_dir = manifest_path.parent
    pilot_only = bool((manifest.get("protocol") or {}).get("pilot_only", False))
    production_plans: dict[str, Any] = {}
    wiki_root = ROOT / "wiki" / industry
    pages = find_pages(wiki_root)
    registry = read_json(ROOT / "sources" / industry / "registry.json")["sources"]
    verified = {key for key, value in registry.items() if value.get("status") == "verified"}
    budget = manifest.get("claim_budget_per_node")

    extract_nodes = []
    repair_claims = []
    full_claim_counts: dict[str, int] = {}
    sampled_claim_counts: dict[str, int | None] = {}
    claim_count_contracts: dict[str, dict[str, int | str]] = {}
    for item in manifest["nodes"]:
        node_id = item["node_id"]
        if item["recommended_mode"] in {"extract", "rebuild"}:
            dossier = item["dossier"]
            extract_nodes.append({
                "node_id": node_id,
                "node_type": item["node_type"],
                "industry": industry,
                "industry_cn": "信息与通信技术设备" if industry == "ict_equipment" else industry,
                "name": item["display_name"],
                "facets": dossier["facets"],
                "boundary": dossier["boundary"],
                "dossier": dossier,
                "claim_budget": budget,
                "write_mode": item["recommended_mode"],
            })
            required_claims = minimum_nomination_claims(item["node_type"])
            contract = claim_count_contract(
                "extract",
                budget,
                required_claims,
                len(dossier["claim_requirements"]) * MAX_CLAIMS_PER_REQUIREMENT,
            )
            full_claim_counts[node_id] = int(
                contract["value"] if contract["kind"] == "exact" else contract["min"]
            )
            claim_count_contracts[node_id] = contract
            sampled_claim_counts[node_id] = (
                contract["value"] if contract["kind"] == "exact" else None
            )
            continue

        text = pages[node_id].read_text(encoding="utf-8")
        body = body_of(text)
        raw = atomize(body, registry, verified, skip_verified=False)
        claims = []
        body_hash = sha256(body)
        for claim in raw:
            claim_id = hashlib.sha1(
                f'{node_id}|{claim["section"]}|{claim["claim_text"]}'.encode("utf-8")
            ).hexdigest()[:16]
            claims.append({
                "claim_id": f"{node_id}-{claim_id}",
                "node_id": node_id,
                "industry": industry,
                "body_sha256": body_hash,
                "node_identity": {
                    "display_name": item["display_name"],
                    "node_type": item["node_type"],
                    "facets": item["dossier"]["facets"],
                    "boundary": item["dossier"]["boundary"],
                },
                **claim,
            })
        sampled = sample_claims(claims, budget)
        full_claim_counts[node_id] = len(claims)
        sampled_claim_counts[node_id] = len(sampled)
        claim_count_contracts[node_id] = claim_count_contract(
            "repair", budget, len(sampled)
        )
        repair_claims.extend(sampled)

    workflows = []
    if extract_nodes:
        path = batch_dir / ("extract.workflow.run.js" if pilot_only else "nomination.workflow.run.js")
        template = (
            WORKFLOWS / "wiki-ku-provenance.js"
            if pilot_only else WORKFLOWS / "wiki-ku-nominate.js"
        )
        if not args.dry_run:
            splice(template, "NODES", extract_nodes, path)
        workflows.append({
            "mode": "extract" if pilot_only else "nomination",
            "path": str(path.relative_to(ROOT)),
            "nodes": [item["node_id"] for item in extract_nodes],
            "protocol_check": validate_workflow(path) if not args.dry_run else {"dry_run": True},
        })
    if repair_claims:
        if pilot_only:
            path = batch_dir / "repair.workflow.run.js"
            if not args.dry_run:
                splice(WORKFLOWS / "wiki-ku-provenance-repair.js", "CLAIMS", repair_claims, path)
            workflows.append({
                "mode": "repair",
                "path": str(path.relative_to(ROOT)),
                "nodes": sorted({item["node_id"] for item in repair_claims}),
                "protocol_check": validate_workflow(path) if not args.dry_run else {"dry_run": True},
            })
        else:
            claims_path = batch_dir / "production-claims.json"
            claims_doc = {
                "protocol": {
                    "version": "wiki-production-claims-v1",
                    "kind": "claims",
                    "mode": "repair",
                },
                "industry": industry,
                "claims": repair_claims,
            }
            if not args.dry_run:
                write_json(claims_path, claims_doc)
                production_plans["repair_claims"] = file_record(claims_path)
            else:
                production_plans["repair_claims"] = {"path": str(claims_path), "dry_run": True}

    prepared = {
        "protocol": {"version": BATCH_PROTOCOL, "kind": "prepared-batch"},
        "manifest": str(manifest_path.relative_to(ROOT)),
        "pilot_only": manifest["protocol"].get("pilot_only", False),
        "workflows": workflows,
        "full_claim_counts": full_claim_counts,
        "sampled_claim_counts": sampled_claim_counts,
        "claim_count_contracts": claim_count_contracts,
        "reviewed_upgrade_allowed": not manifest["protocol"].get("pilot_only", False),
        "production_inputs": production_plans,
    }
    prepared_path = batch_dir / "prepared.json"
    if not args.dry_run:
        write_json(prepared_path, prepared)
        transition_journal(
            batch_dir, "prepared", "prepare", resume=args.resume,
            artifacts={"prepared": file_record(prepared_path)},
        )
    print(json.dumps({
        "prepared": str((batch_dir / "prepared.json").relative_to(ROOT)),
        "workflows": len(workflows),
        "extract_nodes": sum(item.get("write_mode") == "extract" for item in extract_nodes),
        "rebuild_nodes": sum(item.get("write_mode") == "rebuild" for item in extract_nodes),
        "repair_nodes": len(sampled_claim_counts) - len(extract_nodes),
        "repair_claims_full": sum(full_claim_counts.values()),
        "repair_claims_sampled": len(repair_claims),
        "production": not pilot_only,
        "dry_run": args.dry_run,
    }, ensure_ascii=False))
    return 0


def cohort_for(item: dict[str, Any]) -> str:
    if item["body_schema_status"] != "empty":
        return "existing_reaudit"
    if item["confidence"] == "longtail":
        return "longtail"
    dossier = item["dossier"]
    if item["node_type"] == "product" and dossier["boundary"] == "background":
        return (
            "background_linked"
            if dossier.get("home_status") == "linked"
            else "background_unresolved"
        )
    return (
        "foreground_product"
        if item["node_type"] == "product"
        else "foreground_activity"
    )


def command_partition(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.resolve()
    manifest = read_json(manifest_path)
    output_root = manifest_path.parent / "batches"
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in manifest["nodes"]:
        grouped[cohort_for(item)].append(item)

    batches = []
    for cohort, items in sorted(grouped.items()):
        for offset in range(0, len(items), args.batch_size):
            chunk = items[offset:offset + args.batch_size]
            sequence = offset // args.batch_size + 1
            batch_id = f"{manifest['batch_id']}--{cohort}-{sequence:02d}"
            batch_dir = output_root / batch_id
            child = {
                **manifest,
                "batch_id": batch_id,
                "parent_manifest": str(manifest_path.relative_to(ROOT)),
                "cohort": cohort,
                "nodes": chunk,
                "summary": {
                    "nodes": len(chunk),
                    "products": sum(item["node_type"] == "product" for item in chunk),
                    "activities": sum(item["node_type"] == "activity" for item in chunk),
                    "empty": sum(item["body_schema_status"] == "empty" for item in chunk),
                    "legacy": sum(item["body_schema_status"] == "legacy" for item in chunk),
                    "current": sum(item["body_schema_status"] == "current" for item in chunk),
                },
            }
            child_manifest_path = batch_dir / "manifest.json"
            write_json(child_manifest_path, child)
            init_journal(batch_dir, child_manifest_path)
            batches.append({
                "batch_id": batch_id,
                "cohort": cohort,
                "manifest": str((batch_dir / "manifest.json").relative_to(ROOT)),
                **child["summary"],
            })
    index = {
        "protocol": {"version": "wiki-batch-v1", "kind": "partition-index"},
        "parent_manifest": str(manifest_path.relative_to(ROOT)),
        "batch_size": args.batch_size,
        "nodes": len(manifest["nodes"]),
        "batches": batches,
    }
    write_json(output_root / "index.json", index)
    print(json.dumps({
        "index": str((output_root / "index.json").relative_to(ROOT)),
        "nodes": index["nodes"],
        "batches": len(batches),
        "cohorts": {
            cohort: sum(len(batch) for batch in [items])
            for cohort, items in sorted(grouped.items())
        },
    }, ensure_ascii=False))
    return 0


def command_validate(args: argparse.Namespace) -> int:
    prepared = read_json(args.prepared.resolve())
    manifest = read_json(ROOT / prepared["manifest"])
    expected = {item["node_id"] for item in manifest["nodes"]}
    seen: set[str] = set()
    checks = []
    for workflow in prepared["workflows"]:
        path = ROOT / workflow["path"]
        report = validate_workflow(path)
        if not prepared.get("pilot_only") and report.get("mode") != "nomination":
            raise ValueError("production prepared 禁止内置 SearchFetch/Verify Workflow")
        name, items = _extract_binding(path.read_text(encoding="utf-8"))
        item_nodes = {item["node_id"] for item in items}
        seen.update(item_nodes)
        checks.append({
            "path": workflow["path"],
            "mode": report["mode"],
            "binding": name,
            "items": report["items"],
            "nodes": sorted(item_nodes),
        })
    repair_record = prepared.get("production_inputs", {}).get("repair_claims")
    if repair_record:
        repair_path = _assert_frozen_record(repair_record, "production repair claims")
        repair_doc = read_json(repair_path)
        claims = repair_doc.get("claims")
        if not isinstance(claims, list) or not claims:
            raise ValueError("production repair claims 缺少非空 claims")
        item_nodes = {item["node_id"] for item in claims}
        seen.update(item_nodes)
        checks.append({
            "path": str(repair_path),
            "mode": "repair_claims",
            "binding": "CLAIMS",
            "items": len(claims),
            "nodes": sorted(item_nodes),
        })
    missing = sorted(expected - seen)
    extra = sorted(seen - expected)
    if missing or extra:
        raise ValueError(f"批次节点覆盖漂移: missing={missing} extra={extra}")
    result = {
        "protocol": {"version": BATCH_PROTOCOL, "kind": "validation-report"},
        "pilot_only": prepared["pilot_only"],
        "nodes_expected": len(expected),
        "nodes_covered": len(seen),
        "workflows": checks,
        "reviewed_upgrade_allowed": prepared["reviewed_upgrade_allowed"],
        "verdict": "PASS",
    }
    output = args.output or args.prepared.resolve().parent / "validation.json"
    if not getattr(args, "dry_run", False):
        write_json(output, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _assert_frozen_record(record: dict[str, Any], label: str) -> Path:
    path = Path(record["path"])
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists() or sha256(path.read_text(encoding="utf-8")) != record.get("sha256"):
        raise ValueError(f"{label} artifact 缺失或哈希漂移: {path}")
    return path


def committed_transaction(
    path: Path,
    *,
    expected_plan: Path | None = None,
    expected_targets: set[Path] | None = None,
) -> dict[str, Any] | None:
    """Validate a commit that finished before its release journal advanced."""
    if not path.exists():
        return None
    transaction = read_json(path)
    if transaction.get("state") != "committed":
        return None
    if (transaction.get("protocol") or {}).get("version") != "wiki-ku-transaction-v1":
        raise ValueError(f"事务协议非法: {path}")
    if expected_plan is not None:
        actual_plan = Path(str(transaction.get("plan", ""))).resolve()
        expected_plan = expected_plan.resolve()
        expected_digest = sha256(expected_plan.read_text(encoding="utf-8"))
        if (actual_plan != expected_plan
                or transaction.get("plan_sha256") != expected_digest):
            raise ValueError(f"已提交事务不属于当前 merge plan: {path}")
    targets = transaction.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError(f"已提交事务缺 targets: {path}")
    actual_targets: set[Path] = set()
    for item in targets:
        target = Path(str(item.get("target", ""))).resolve()
        digest = str(item.get("new_sha256", ""))
        if not target.exists() or sha256(target.read_text(encoding="utf-8")) != digest:
            raise ValueError(f"已提交事务 target 漂移: {target}")
        actual_targets.add(target)
    if expected_targets is not None and actual_targets != {p.resolve() for p in expected_targets}:
        raise ValueError("已提交事务 targets 与当前阶段冻结范围不一致")
    return transaction


def command_research_ready(args: argparse.Namespace) -> int:
    prepared_path = args.prepared.resolve()
    prepared = read_json(prepared_path)
    if prepared.get("pilot_only"):
        raise ValueError("research-ready 是 production 文件协议；pilot 继续使用内置 Workflow")
    batch_dir = prepared_path.parent
    if not args.evidence or len(args.evidence) != len(args.verify_workflow or []):
        raise ValueError("research-ready 必须传一一对应的 --evidence/--verify-workflow")
    manifest = read_json(ROOT / prepared["manifest"])
    manifest_nodes = {item["node_id"]: item for item in manifest["nodes"]}
    needs_nomination = any(
        item.get("recommended_mode") in {"extract", "rebuild"}
        for item in manifest_nodes.values()
    )
    nomination_usage_record: dict[str, Any] | None = None
    if needs_nomination:
        if args.nomination_usage is None:
            raise ValueError("production extract 必须传 --nomination-usage 对账实际模型/推理级别/费用")
        nomination_usage_path = args.nomination_usage.resolve()
        nomination_usage = validate_usage(nomination_usage_path, phase="nomination")
        nomination_usage_record = {
            **file_record(nomination_usage_path),
            "metrics": nomination_usage,
        }
    elif args.nomination_usage is not None:
        raise ValueError("无 empty/extract 节点的批次不得传 --nomination-usage")
    seen_claims: set[str] = set()
    by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pairs = []
    total_usage = {"network_queries": 0, "network_fetches": 0, "cost_usd": 0.0}
    if nomination_usage_record is not None:
        total_usage["cost_usd"] += float(nomination_usage_record["metrics"]["cost_usd"])
    for evidence_arg, workflow_arg in zip(args.evidence, args.verify_workflow):
        evidence_path = evidence_arg.resolve()
        workflow_path = workflow_arg.resolve()
        evidence = read_json(evidence_path)
        validate_evidence(evidence, require_payload=True, require_source_chain=True)
        version, kind = protocol_kind(evidence)
        mode = str((evidence.get("protocol") or {}).get("mode") or "")
        if version != EVIDENCE_PROTOCOL or kind != "claim-evidence" or mode not in {"extract", "repair"}:
            raise ValueError(f"evidence 协议必须是 {EVIDENCE_PROTOCOL} claim-evidence extract/repair")
        input_record = evidence.get("input_claims") or {}
        input_path = _assert_frozen_record(input_record, f"{mode} evidence input claims")
        if mode == "repair":
            expected_repair = prepared.get("production_inputs", {}).get("repair_claims") or {}
            if input_record != expected_repair:
                raise ValueError("repair evidence 输入不是 prepared 冻结的 production-claims")
        else:
            validate_nomination_result(input_path)
        if evidence.get("budget_exceeded") is not False:
            raise ValueError("evidence budget_exceeded 必须显式为 false")
        if (evidence.get("compliance") or {}).get("all_evidence_compliant") is not True:
            raise ValueError("evidence compliance.all_evidence_compliant 必须为 true")
        hard_limits = evidence.get("hard_limits")
        embedded_usage = evidence.get("usage")
        if not isinstance(hard_limits, dict) or not isinstance(embedded_usage, dict):
            raise ValueError("evidence 必须冻结 hard_limits 与 usage")
        allowed_domains = hard_limits.get("allowed_domains")
        discovery_mode = hard_limits.get("discovery_mode", "allowlist")
        if discovery_mode == "allowlist":
            if not isinstance(allowed_domains, list) or not allowed_domains:
                raise ValueError("allowlist production evidence 必须冻结非空 allowed_domains 白名单")
        elif discovery_mode == "open":
            if allowed_domains != []:
                raise ValueError("open production evidence 必须冻结空 allowed_domains")
        else:
            raise ValueError("production evidence discovery_mode 非法")
        for aliases, limit_key, total_key in (
            (("network_queries", "search_requests", "searches"), "max_searches", "network_queries"),
            (("network_fetches", "fetch_requests", "fetches"), "max_fetches", "network_fetches"),
        ):
            used = int(next((embedded_usage[key] for key in aliases if key in embedded_usage), 0) or 0)
            limit = int(hard_limits.get(limit_key, -1))
            if used < 0 or limit < 0 or used > limit:
                raise ValueError(f"evidence 预算违规: {aliases[0]}={used} > {limit_key}={limit}")
            total_usage[total_key] += used
        evidence_cost = float(embedded_usage.get("cost_usd", 0) or 0)
        if not math.isfinite(evidence_cost) or evidence_cost < 0:
            raise ValueError("evidence usage.cost_usd 必须是非负有限数")
        total_usage["cost_usd"] += evidence_cost
        workflow_report = validate_workflow(workflow_path)
        if workflow_report.get("mode") != "verify_only":
            raise ValueError("--verify-workflow 必须是 validate_workflow 认可的 verify_only Workflow")
        _, embedded_evidence = _extract_binding(workflow_path.read_text(encoding="utf-8"))
        canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if sha256(canonical(embedded_evidence)) != sha256(canonical(evidence)):
            raise ValueError("verify-only Workflow 内嵌 evidence 与 --evidence 不一致")
        for entry in evidence.get("claims", []):
            claim = entry.get("claim") if isinstance(entry, dict) else None
            if not isinstance(claim, dict):
                raise ValueError("evidence claim 结构非法")
            claim_id = str(claim.get("claim_id") or "")
            if not claim_id or claim_id in seen_claims:
                raise ValueError(f"evidence claim_id 缺失或重复: {claim_id}")
            seen_claims.add(claim_id)
            node_id = str(claim.get("node_id") or "")
            item = manifest_nodes.get(node_id)
            if item is None:
                raise ValueError(f"evidence 含批次外节点: {node_id}")
            expected_identity = {
                "display_name": item["display_name"],
                "node_type": item["node_type"],
                "facets": item["dossier"]["facets"],
                "boundary": item["dossier"]["boundary"],
            }
            if claim.get("node_identity") != expected_identity:
                raise ValueError(f"{node_id} claim.node_identity 与冻结节点档案漂移")
            expected_mode = (
                "extract"
                if item["recommended_mode"] in {"extract", "rebuild"}
                else "repair"
            )
            if mode != expected_mode:
                raise ValueError(f"{node_id} 出现在错误 evidence mode={mode}")
            by_node[node_id].append(claim)
        pairs.append({
            "mode": mode,
            "evidence": file_record(evidence_path),
            "verify_workflow": {**file_record(workflow_path), "validation": workflow_report},
            "usage": embedded_usage,
        })

    expected_nodes = set(manifest_nodes)
    if set(by_node) != expected_nodes:
        raise ValueError(f"research artifact 节点漂移: missing={sorted(expected_nodes-set(by_node))} extra={sorted(set(by_node)-expected_nodes)}")
    for node_id, claims in by_node.items():
        contract = prepared["claim_count_contracts"][node_id]
        if not claim_count_matches(contract, len(claims)):
            raise ValueError(f"{node_id} 断言数不符合冻结契约: {len(claims)} vs {contract}")
        item = manifest_nodes[node_id]
        if item["recommended_mode"] in {"extract", "rebuild"}:
            expected_requirements = item["dossier"]["claim_requirements"]
            validate_nomination_claim_slots(
                node_id, item["node_type"], claims, expected_requirements
            )

    artifact = {
        "protocol": {"version": BATCH_PROTOCOL, "kind": "research-ready"},
        "prepared": file_record(prepared_path),
        "pairs": pairs,
        "nomination_usage": nomination_usage_record,
        "usage": total_usage,
    }
    output = args.output or batch_dir / "research-ready.json"
    if not args.dry_run:
        write_json(output, artifact)
        transition_journal(
            batch_dir, "research_ready", "research-ready", resume=args.resume,
            artifacts={"research_ready": file_record(output)},
            repair_rewind=args.repair_rewind,
        )
    print(json.dumps({"research_ready": str(output), "dry_run": args.dry_run}, ensure_ascii=False))
    return 0


def validate_verify_rows(
    result_doc: dict[str, Any],
    expected_evidence: dict[str, dict[str, Any]],
    seen_claims: set[str],
) -> None:
    """Bind Verify output exactly to the hash-frozen research evidence."""
    canonical = lambda value: json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    for row in result_doc.get("claims", []):
        claim = row.get("claim") or {}
        claim_id = str(claim.get("claim_id", ""))
        if not claim_id or claim_id in seen_claims:
            raise ValueError(f"verify result claim_id 缺失或重复: {claim_id}")
        seen_claims.add(claim_id)
        frozen_entry = expected_evidence.get(claim_id)
        if frozen_entry is None:
            raise ValueError(f"verify result 含 research evidence 之外的 claim: {claim_id}")
        if canonical(claim) != canonical(frozen_entry.get("claim") or {}):
            raise ValueError(f"verify result 改写了冻结 claim: {claim_id}")
        candidates = frozen_entry.get("candidates") or []
        fetch = row.get("fetchResult") or {}
        verdict = str((row.get("verify") or {}).get("verdict", ""))
        if not candidates:
            if fetch.get("status") != "not_found" or verdict != "NOT_FOUND":
                raise ValueError(f"{claim_id} 无冻结 evidence 却返回外部核验结果")
            continue
        evidence_id = str(fetch.get("evidence_id", ""))
        selected = [c for c in candidates if c.get("evidence_id") == evidence_id]
        if len(selected) != 1:
            raise ValueError(f"{claim_id} evidence_id 不解析到唯一冻结候选")
        candidate = selected[0]
        nominated = [
            item for item in candidates
            if item.get("search_provider") in {"research_scout", "research_plan_advisory"}
        ]
        if nominated and candidate not in nominated:
            raise ValueError(f"{claim_id} Verify 选择了提名来源之外的替代证据")
        for key in ("url", "excerpt", "content_sha256", "evidence_id"):
            if fetch.get(key) != candidate.get(key):
                raise ValueError(f"{claim_id} fetchResult.{key} 与冻结 evidence 漂移")
        if fetch.get("status") != "found":
            raise ValueError(f"{claim_id} 有冻结 evidence 但 fetchResult.status 非 found")


def command_verify(args: argparse.Namespace) -> int:
    prepared_path = args.prepared.resolve()
    prepared = read_json(prepared_path)
    if prepared.get("pilot_only"):
        raise ValueError("verify 文件协议只用于 production")
    batch_dir = prepared_path.parent
    journal = read_journal(batch_dir)
    research_path = _assert_frozen_record(
        journal.get("artifacts", {}).get("research_ready", {}), "research-ready"
    )
    research = read_json(research_path)
    expected_evidence: dict[str, dict[str, Any]] = {}
    evidence_path_by_claim: dict[str, Path] = {}
    for pair in research.get("pairs", []):
        evidence_path = _assert_frozen_record(pair.get("evidence", {}), "research evidence")
        evidence = read_json(evidence_path)
        validate_evidence(evidence, require_payload=True, require_source_chain=True)
        for entry in evidence.get("claims", []):
            claim = entry.get("claim") or {}
            claim_id = str(claim.get("claim_id", ""))
            if not claim_id or claim_id in expected_evidence:
                raise ValueError(f"冻结 research evidence claim_id 缺失或重复: {claim_id}")
            expected_evidence[claim_id] = entry
            evidence_path_by_claim[claim_id] = evidence_path.resolve()
    if not args.result:
        raise ValueError("verify 必须至少传一个 --result")
    if not args.runtime_dir or len(args.runtime_dir) != len(args.result):
        raise ValueError("production verify 必须为每个 --result 提供一一对应的 --runtime-dir")
    if not args.usage or len(args.usage) != len(args.result):
        raise ValueError("production verify 必须为每个 --result 提供一一对应的 --usage")
    usage_inputs = []
    usage_total = 0.0
    for usage_arg in args.usage:
        usage_path = usage_arg.resolve()
        metrics = validate_usage(usage_path, phase="verify_only")
        usage_inputs.append({**file_record(usage_path), "metrics": metrics})
        usage_total += float(metrics["cost_usd"])
    results = []
    seen_nodes: set[str] = set()
    seen_claims: set[str] = set()
    expected = {item["node_id"] for item in read_json(ROOT / prepared["manifest"])["nodes"]}
    runtime_records = []
    from wiki_research_ready import runtime_attestation
    for path, runtime_arg, usage_entry in zip(args.result, args.runtime_dir, usage_inputs):
        resolved = path.resolve()
        validation = validate_result(resolved)
        result_doc, by_node = partition_result(resolved)
        if len(by_node) != 1:
            raise ValueError("每个 production Verify result/runtime 必须精确对应一个节点")
        validate_verify_rows(result_doc, expected_evidence, seen_claims)
        overlap = seen_nodes & set(by_node)
        if overlap:
            raise ValueError(f"verify result 节点重复: {sorted(overlap)}")
        seen_nodes.update(by_node)
        results.append({**file_record(resolved), "validation": validation})
        runtime_dir = runtime_arg.resolve()
        runtime_ok, runtime_errors = runtime_attestation(
            runtime_dir, result_doc.get("claims", [])
        )
        if not runtime_ok:
            raise ValueError(f"Verify runtime attestation 失败 {runtime_dir}: {runtime_errors}")
        usage_metrics = usage_entry["metrics"]
        if (
            usage_metrics.get("runtime_usage_sha256")
            != file_record(runtime_dir / "verify-usage.json")["sha256"]
            or usage_metrics.get("runtime_invocation_sha256")
            != file_record(runtime_dir / "verify-invocation.json")["sha256"]
        ):
            raise ValueError(f"Verify wiki-usage-v1 未绑定当前 runtime: {runtime_dir}")
        invocation = read_json(runtime_dir / "verify-invocation.json")
        invoked_evidence = Path(str(invocation.get("evidence", ""))).resolve()
        result_claim_ids = {
            str((row.get("claim") or {}).get("claim_id", ""))
            for row in result_doc.get("claims", [])
        }
        bound_paths = {evidence_path_by_claim[claim_id] for claim_id in result_claim_ids}
        if len(bound_paths) != 1 or invoked_evidence not in bound_paths:
            raise ValueError(
                f"Verify runtime evidence 未绑定当前节点冻结 evidence: {invoked_evidence} != {bound_paths}"
            )
        runtime_records.append({
            "path": str(runtime_dir),
            "node_ids": sorted(by_node),
            "invocation": file_record(runtime_dir / "verify-invocation.json"),
            "events": file_record(runtime_dir / "verify-events.jsonl"),
            "stderr": file_record(runtime_dir / "verify-stderr.log"),
            "usage": file_record(runtime_dir / "verify-usage.json"),
            "verdicts": file_record(runtime_dir / "verify-verdicts.runtime.json"),
        })
    if seen_nodes != expected:
        raise ValueError(f"verify 节点覆盖漂移: missing={sorted(expected-seen_nodes)} extra={sorted(seen_nodes-expected)}")
    if seen_claims != set(expected_evidence):
        raise ValueError(
            "verify claim 双向覆盖漂移: "
            f"missing={sorted(set(expected_evidence)-seen_claims)} "
            f"extra={sorted(seen_claims-set(expected_evidence))}"
        )
    aggregate_usage = {
        "protocol": {"version": USAGE_PROTOCOL, "kind": "usage"},
        "phase": "verify_only",
        **MODEL_POLICY["verify_only"],
        "search_requests": 0,
        "cost_usd": usage_total,
        "inputs": usage_inputs,
    }
    aggregate_usage_path = batch_dir / "verify-usage-aggregate.json"
    if not args.dry_run:
        write_json(aggregate_usage_path, aggregate_usage)
    artifact = {
        "protocol": {"version": BATCH_PROTOCOL, "kind": "verified-batch"},
        "results": results,
        "runtime_attestations": runtime_records,
        "usage_inputs": usage_inputs,
        "usage": (
            {**file_record(aggregate_usage_path), "metrics": aggregate_usage}
            if not args.dry_run else {"path": str(aggregate_usage_path), "metrics": aggregate_usage}
        ),
    }
    output = args.output or batch_dir / "verified.json"
    if not args.dry_run:
        write_json(output, artifact)
        transition_journal(
            batch_dir, "verified", "verify", resume=args.resume,
            artifacts={"verified": file_record(output)},
        )
    print(json.dumps({"verified": str(output), "nodes": len(seen_nodes), "dry_run": args.dry_run}, ensure_ascii=False))
    return 0


def partition_result(path: Path) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    validate_result(path)
    data = read_json(path)
    result = data["result"] if isinstance(data.get("result"), dict) else data
    by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in result.get("claims", []):
        by_node[row["claim"]["node_id"]].append(row)
    return result, by_node


def command_finalize(args: argparse.Namespace) -> int:
    prepared_path = args.prepared.resolve()
    prepared = read_json(prepared_path)
    manifest = read_json(ROOT / prepared["manifest"])
    batch_dir = prepared_path.parent
    content_candidates: dict[str, dict[str, Any]] = {}
    content_output_path = args.content_output.resolve() if args.content_output else None
    if content_output_path:
        content_doc = read_json(content_output_path)
        if content_doc.get("protocol") != "wiki-content-enriched-v1" or not isinstance(content_doc.get("nodes"), dict):
            raise ValueError("--content-output 不是 wiki-content-enriched-v1")
        content_candidates = content_doc["nodes"]
    output_by_mode: dict[str, Path | None] = {
        "extract": args.extract_output.resolve() if args.extract_output else None,
        "repair": args.repair_output.resolve() if args.repair_output else None,
    }
    if not prepared.get("pilot_only"):
        journal = read_journal(batch_dir)
        verified_path = _assert_frozen_record(
            journal.get("artifacts", {}).get("verified", {}), "verified"
        )
        verified_artifact = read_json(verified_path)
        for record in verified_artifact.get("results", []):
            result_path = _assert_frozen_record(record, "verify result")
            result_doc = read_json(result_path)
            unwrapped = result_doc.get("result") if isinstance(result_doc.get("result"), dict) else result_doc
            mode = (unwrapped.get("protocol") or {}).get("mode")
            if mode in output_by_mode:
                if output_by_mode[mode] is not None:
                    raise ValueError(f"verified artifact 含重复 {mode} result")
                output_by_mode[mode] = result_path
    reports = []
    node_artifacts: dict[str, dict[str, Any]] = {}
    all_repair_kus = []
    all_extract_kus = []
    missing_outputs = []
    workflows_to_finalize = prepared["workflows"]
    if not prepared.get("pilot_only"):
        workflows_to_finalize = []
        for mode in ("extract", "repair"):
            nodes = [
                item["node_id"] for item in manifest["nodes"]
                if (item["recommended_mode"] in {"extract", "rebuild"}) == (mode == "extract")
            ]
            if nodes:
                workflows_to_finalize.append({"mode": mode, "nodes": nodes})
    for workflow in workflows_to_finalize:
        mode = workflow["mode"]
        output_path = output_by_mode[mode]
        if output_path is None:
            if args.allow_partial:
                missing_outputs.append(mode)
                continue
            raise ValueError(f"缺少 --{mode}-output")
        _, by_node = partition_result(output_path)
        expected = set(workflow["nodes"])
        missing = sorted(expected - set(by_node))
        extra = sorted(set(by_node) - expected)
        if missing or extra:
            raise ValueError(f"{mode} 结果节点漂移: missing={missing} extra={extra}")
        count_drift = {}
        for node_id in sorted(expected):
            contract = prepared.get("claim_count_contracts", {}).get(node_id)
            if contract is None:  # 兼容迁移前的 prepared.json
                contract = {
                    "kind": "exact",
                    "value": prepared["sampled_claim_counts"][node_id],
                }
            actual = len(by_node[node_id])
            if not claim_count_matches(contract, actual):
                count_drift[node_id] = {
                    "expected": contract,
                    "actual": actual,
                }
        if count_drift:
            raise ValueError(f"{mode} 结果断言数漂移: {count_drift}")
        for node_id, rows in sorted(by_node.items()):
            research_rows = rows
            content_candidate = content_candidates.get(node_id) if mode == "extract" else None
            if content_candidate:
                candidate_rows = content_candidate.get("claims")
                candidate_kus = content_candidate.get("kus")
                if not isinstance(candidate_rows, list) or not isinstance(candidate_kus, list):
                    raise ValueError(f"{node_id} Content candidate claims/kus 缺失")
                research_ids = {row["claim"]["claim_id"] for row in research_rows}
                if set(content_candidate.get("research_claim_ids") or []) != research_ids:
                    raise ValueError(f"{node_id} Content candidate 研究 claim 集合漂移")
                candidate_base = {
                    row["claim"]["claim_id"]: row for row in candidate_rows
                    if row["claim"]["claim_id"] in research_ids
                }
                if candidate_base != {row["claim"]["claim_id"]: row for row in research_rows}:
                    raise ValueError(f"{node_id} Content candidate 改写了 Verify 结果")
                rows = candidate_rows
            run_dir = batch_dir / "nodes" / node_id / mode
            frozen = {
                "protocol": {"version": "wiki-ku-v1", "mode": mode},
                "claims": rows,
            }
            if not args.dry_run:
                write_json(run_dir / "claims.json", frozen)
            kus = content_candidate["kus"] if content_candidate else distill(rows)
            if content_candidate and kus != distill(rows):
                raise ValueError(f"{node_id} Content candidate KU 不可确定性重放")
            if not args.dry_run:
                write_json(run_dir / "kus.json", {"kus": kus})
                node_artifacts[node_id] = {
                    "mode": mode,
                    "claims": file_record(run_dir / "claims.json"),
                    "kus": file_record(run_dir / "kus.json"),
                }
                if content_candidate:
                    content_record = run_dir / "content-scorecard.json"
                    write_json(content_record, content_candidate.get("scorecard") or {})
                    node_artifacts[node_id]["content_scorecard"] = file_record(content_record)
            authority = defaultdict(int)
            for ku in kus:
                authority[ku["authority"]] += 1
            if mode == "extract":
                all_extract_kus.extend(kus)
                generated = batch_dir / "generated-bodies"
                if not args.dry_run:
                    generated.mkdir(parents=True, exist_ok=True)
                    (generated / f"{node_id}.ku-body.md").write_text(
                        render_node(kus), encoding="utf-8"
                    )
                    if any(ku["authority"] == "reviewed" for ku in kus):
                        (generated / f"{node_id}.ku-footnotes.md").write_text(
                            render_footnotes(kus), encoding="utf-8"
                        )
            else:
                all_repair_kus.extend(kus)
            reports.append({
                "node_id": node_id,
                "mode": mode,
                "claims": len(rows),
                "research_claims": len(research_rows),
                "content_claims": len(rows) - len(research_rows),
                "authority": dict(sorted(authority.items())),
            })

    merge_plan_path = None
    if all_repair_kus and not args.dry_run:
        combined = batch_dir / "repair-kus.json"
        write_json(combined, {"kus": all_repair_kus})
        plan = plan_merge(
            combined,
            ROOT / "wiki" / manifest["industry"],
            ROOT / "sources" / manifest["industry"] / "registry.json",
            dt.date.today().isoformat(),
        )
        merge_plan_path = batch_dir / "repair-merge-plan.json"
        write_json(merge_plan_path, plan)

    extract_merge_plan_path = None
    extract_plan = None
    if all_extract_kus and not args.dry_run:
        combined = batch_dir / "extract-kus.json"
        write_json(combined, {"kus": all_extract_kus})
        node_contracts = {
            item["node_id"]: {
                "node_type": item["node_type"],
                "write_mode": item["recommended_mode"],
                "dossier": item["dossier"],
            }
            for item in manifest["nodes"]
            if item["recommended_mode"] in {"extract", "rebuild"}
        }
        plan = plan_extract_merge(
            combined,
            ROOT / "wiki" / manifest["industry"],
            ROOT / "sources" / manifest["industry"] / "registry.json",
            dt.date.today().isoformat(),
            node_contracts=node_contracts,
            content_candidates=content_candidates,
        )
        extract_merge_plan_path = batch_dir / "extract-merge-plan.json"
        write_json(extract_merge_plan_path, plan)
        extract_plan = plan

    batch_merge_plan_path = None
    if not args.dry_run and (merge_plan_path or extract_merge_plan_path):
        component_plans = [
            value for value in (
                read_json(merge_plan_path) if merge_plan_path else None,
                extract_plan,
            ) if value is not None
        ]
        batch_kus_path = batch_dir / "batch-apply-kus.json"
        write_json(batch_kus_path, {"kus": all_repair_kus + all_extract_kus})
        entries: dict[str, Any] = {}
        files: list[dict[str, Any]] = []
        registry_hashes = {item["registry_sha256"] for item in component_plans}
        if len(registry_hashes) != 1:
            raise ValueError("extract/repair merge plan registry hash 不一致")
        for component in component_plans:
            for key, value in component.get("registry_entries", {}).items():
                if key in entries and entries[key] != value:
                    raise ValueError(f"跨计划 registry entry 冲突: {key}")
                entries[key] = value
            files.extend(component["files"])
        paths = [item["path"] for item in files]
        if len(paths) != len(set(paths)):
            raise ValueError("extract/repair merge plan 修改了同一页面")
        batch_plan = {
            "protocol": {"version": "wiki-ku-v1", "kind": "wiki-ku-merge-plan"},
            "plan_mode": "batch",
            "created_on": dt.date.today().isoformat(),
            "ku_path": str(batch_kus_path.relative_to(ROOT)),
            "ku_sha256": sha256(batch_kus_path.read_text(encoding="utf-8")),
            "wiki_root": component_plans[0]["wiki_root"],
            "registry_path": component_plans[0]["registry_path"],
            "registry_sha256": component_plans[0]["registry_sha256"],
            "registry_entries": entries,
            "files": files,
        }
        batch_merge_plan_path = batch_dir / "batch-merge-plan.json"
        write_json(batch_merge_plan_path, batch_plan)

    report = {
        "protocol": {
            "version": BATCH_PROTOCOL,
            "kind": "pilot-result" if prepared["pilot_only"] else "frozen-batch",
        },
        "pilot_only": prepared["pilot_only"],
        "reviewed_upgrade_allowed": False if prepared["pilot_only"] else True,
        "nodes": reports,
        "node_artifacts": node_artifacts,
        "repair_merge_plan": (
            str(merge_plan_path.relative_to(ROOT)) if merge_plan_path else None
        ),
        "extract_merge_plan": (
            str(extract_merge_plan_path.relative_to(ROOT)) if extract_merge_plan_path else None
        ),
        "batch_merge_plan": (
            str(batch_merge_plan_path.relative_to(ROOT)) if batch_merge_plan_path else None
        ),
        "extract_bodies": (
            str((batch_dir / "generated-bodies").relative_to(ROOT))
            if "extract" not in missing_outputs else None
        ),
        "missing_outputs": missing_outputs,
        "warning": (
            "Pilot sampling validates orchestration only; it cannot upgrade a whole page."
            if prepared["pilot_only"] else ""
        ),
        "frozen_thresholds": manifest.get("release_policy", {}),
        "manifest_sha256": sha256((ROOT / prepared["manifest"]).read_text(encoding="utf-8")),
    }
    report_path = batch_dir / ("pilot-result.json" if prepared["pilot_only"] else "frozen.json")
    if not args.dry_run:
        write_json(report_path, report)
        if prepared["pilot_only"]:
            # Pilot keeps its historical one-command finalize surface, but its
            # journal still records every formal state for resumability.
            state = read_journal(batch_dir)["state"]
            if state == "prepared":
                transition_journal(batch_dir, "research_ready", "finalize:pilot")
                transition_journal(batch_dir, "verified", "finalize:pilot")
        transition_journal(
            batch_dir, "frozen", "finalize", resume=args.resume,
            artifacts={"frozen": file_record(report_path)},
        )
    print(json.dumps(report, ensure_ascii=False))
    return 0


def load_coverage(path: Path) -> tuple[dict[str, Any], dict[str, float]]:
    coverage = read_json(path.resolve())
    version, kind = protocol_kind(coverage)
    if version != COVERAGE_PROTOCOL or kind != "claim-coverage-plan":
        raise ValueError(f"coverage 协议必须是 {COVERAGE_PROTOCOL} claim-coverage-plan")
    frozen_hash = coverage.get("artifact_sha256")
    unhashed = dict(coverage)
    unhashed.pop("artifact_sha256", None)
    canonical = json.dumps(unhashed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if frozen_hash != sha256(canonical):
        raise ValueError("coverage artifact_sha256 不匹配")
    from wiki_claim_coverage import validate_artifact
    validate_artifact(coverage)
    summary = coverage.get("summary") or {}
    total = int(summary.get("total", 0) or 0)
    pages_total = int(summary.get("pages_total", 0) or 0)
    metrics = {
        "total": float(total),
        "coverage_ratio": float(summary.get("coverage_rate", 0) or 0),
        "page_eligibility_ratio": (
            float(summary.get("pages_eligible_for_reviewed", 0) or 0) / pages_total
            if pages_total else 0.0
        ),
        "url_quote_compliance_ratio": float(summary.get("quote_compliance_rate", 0) or 0),
        "unresolved_ratio": float(summary.get("unresolved", 0) or 0) / total if total else 1.0,
        "contradicted_ratio": float(summary.get("contradicted", 0) or 0) / total if total else 1.0,
        "manual_review_ratio": float(summary.get("manual_review", 0) or 0) / total if total else 1.0,
        "hash_drift": float(summary.get("hash_drift", 0) or 0),
    }
    return coverage, metrics


def evaluate_go_no_go(
    manifest: dict[str, Any],
    metrics: dict[str, float],
    usage: dict[str, Any],
    gate_passed: bool | None,
) -> dict[str, Any]:
    policy = manifest.get("release_policy") or {}
    checks = {
        "coverage": metrics["coverage_ratio"] >= float(policy.get("min_coverage_ratio", 1)),
        "all_pages_reviewed_ready": metrics["page_eligibility_ratio"] == 1.0,
        "url_quote": metrics["url_quote_compliance_ratio"] >= float(policy.get("min_url_quote_compliance_ratio", 1)),
        "unresolved": metrics["unresolved_ratio"] <= float(policy.get("max_unresolved_ratio", 0)),
        "contradicted": metrics["contradicted_ratio"] <= float(policy.get("max_contradicted_ratio", 0)),
        "manual_review": metrics["manual_review_ratio"] <= float(policy.get("max_manual_review_ratio", 0)),
        "hash_drift": metrics["hash_drift"] == 0,
        "search_budget": int(usage.get("network_queries", 0) or 0) <= int(policy.get("max_search_requests", 0)),
        "cost_budget": float(usage.get("cost_usd", 0) or 0) <= float(policy.get("max_cost_usd", 0)),
        "gate": gate_passed if gate_passed is not None else None,
    }
    pre_apply_pass = all(value is True for key, value in checks.items() if key != "gate")
    final_pass = pre_apply_pass and gate_passed is True
    return {
        "checks": checks,
        "metrics": metrics,
        "usage": usage,
        "reviewed_apply_verdict": "GO" if pre_apply_pass else "NO_GO",
        # Compatibility alias.  This verdict is now evaluated after content
        # Apply and before the privileged reviewed-frontmatter Apply.
        "pre_apply_verdict": "GO" if pre_apply_pass else "NO_GO",
        "final_verdict": "GO" if final_pass else ("PENDING_GATE" if pre_apply_pass and gate_passed is None else "NO_GO"),
    }


def release_usage(journal: dict[str, Any]) -> dict[str, Any]:
    """Reconcile deterministic network and Verify-only runtime usage."""
    research_path = _assert_frozen_record(
        journal.get("artifacts", {}).get("research_ready", {}), "research-ready"
    )
    verified_path = _assert_frozen_record(
        journal.get("artifacts", {}).get("verified", {}), "verified"
    )
    research = read_json(research_path).get("usage") or {}
    verify_record = read_json(verified_path).get("usage") or {}
    usage_path = _assert_frozen_record(verify_record, "verify usage")
    verify = validate_usage(usage_path, phase="verify_only")
    frozen_metrics = verify_record.get("metrics") or {}
    canonical = lambda value: json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if canonical(verify) != canonical(frozen_metrics):
        raise ValueError("verified artifact 内嵌 usage.metrics 与冻结 usage 文件不一致")
    return {
        "network_queries": int(research.get("network_queries", 0) or 0)
            + int(verify.get("search_requests", verify.get("searches", 0)) or 0),
        "network_fetches": int(research.get("network_fetches", 0) or 0),
        "cost_usd": float(research.get("cost_usd", 0) or 0)
            + float(verify.get("cost_usd", 0) or 0),
        "research": research,
        "verify_only": verify,
    }


def command_go_no_go(args: argparse.Namespace) -> int:
    prepared_path = args.prepared.resolve()
    prepared = read_json(prepared_path)
    if prepared.get("pilot_only"):
        raise ValueError("pilot_only 批次永远不能进入 Apply/Publish")
    batch_dir = prepared_path.parent
    journal = read_journal(batch_dir)
    _assert_frozen_record(journal.get("artifacts", {}).get("content_apply", {}), "content apply")
    frozen_path = _assert_frozen_record(journal.get("artifacts", {}).get("frozen", {}), "frozen")
    frozen = read_json(frozen_path)
    manifest_path = ROOT / prepared["manifest"]
    manifest = read_json(manifest_path)
    if frozen.get("manifest_sha256") != sha256(manifest_path.read_text(encoding="utf-8")):
        raise ValueError("manifest 在阈值冻结后发生漂移")
    _, metrics = load_coverage(args.coverage.resolve())
    from wiki_claim_coverage import apply_plan as preflight_coverage_plan, plan_coverage
    # Reject a merely self-consistent, hand-authored coverage artifact.  This
    # is the one stage where current files still have the pre-reviewed hashes,
    # so the planner can be replayed exactly from frozen inputs.
    replayed = plan_coverage(prepared_path, ROOT)
    supplied = read_json(args.coverage.resolve())
    if replayed.get("artifact_sha256") != supplied.get("artifact_sha256"):
        raise ValueError("coverage 不是当前 prepared+BODY+冻结 claims/KU 的确定性派生物")
    preflight_coverage_plan(args.coverage.resolve(), ROOT, write=False)
    usage = release_usage(journal)
    decision = evaluate_go_no_go(manifest, metrics, usage, None)
    report = {
        "protocol": {"version": BATCH_PROTOCOL, "kind": "go-no-go"},
        "phase": "post_content_pre_reviewed_apply",
        "manifest_sha256": frozen["manifest_sha256"],
        "coverage": file_record(args.coverage.resolve()),
        **decision,
    }
    output = args.output or batch_dir / "go-no-go.json"
    if not args.dry_run:
        write_json(output, report)
        if decision["reviewed_apply_verdict"] == "GO":
            transition_journal(
                batch_dir, "apply_ready", "go-no-go", resume=args.resume,
                artifacts={"coverage": file_record(args.coverage.resolve()), "go_no_go": file_record(output)},
            )
        else:
            transition_journal(
                batch_dir, "blocked", "go-no-go", resume=args.resume,
                detail="post-content reviewed-apply NO_GO",
            )
    print(json.dumps(report, ensure_ascii=False))
    return 0 if decision["reviewed_apply_verdict"] == "GO" else 2


def command_apply(args: argparse.Namespace) -> int:
    prepared_path = args.prepared.resolve()
    prepared = read_json(prepared_path)
    if prepared.get("pilot_only"):
        raise ValueError("pilot_only 批次禁止 Apply")
    batch_dir = prepared_path.parent
    journal = read_journal(batch_dir)
    frozen = read_json(_assert_frozen_record(journal.get("artifacts", {}).get("frozen", {}), "frozen"))
    effective_state = journal["state"]
    if effective_state in TERMINAL_STATES:
        if not args.resume:
            raise ValueError(f"批次处于 {effective_state}；Apply 恢复须显式传 --resume")
        effective_state = str(journal.get("resume_from") or "")
    if effective_state == "frozen":
        plan_path = args.plan.resolve() if args.plan else (
            ROOT / frozen["batch_merge_plan"] if frozen.get("batch_merge_plan") else None
        )
        if plan_path is None:
            raise ValueError("批次没有可应用的 batch merge plan")
        prior_content = journal.get("artifacts", {}).get("content_apply")
        requested_rehydrate = bool(prior_content and args.resume and args.rehydrate)
        transaction_path = batch_dir / "apply-transaction.json"
        matching_plan_commit = False
        if requested_rehydrate and transaction_path.is_file():
            matching_plan_commit = transaction_matches_plan(
                read_json(transaction_path), plan_path
            )
        rehydrate = requested_rehydrate and matching_plan_commit
        # A recovery request for a regenerated plan is a fresh, hash-locked
        # apply.  It must never replay the old path-bound transaction.
        remediation = bool(
            prior_content and args.resume and (
                (args.plan and not requested_rehydrate)
                or (requested_rehydrate and not matching_plan_commit)
            )
        )
        if prior_content:
            prior_path = _assert_frozen_record(prior_content, "content apply")
            if not remediation and not rehydrate:
                if args.resume:
                    # A journal receipt is not proof that the materialized
                    # target still exists.  Fail closed on drift and require
                    # an explicit, hash-bound rehydrate operation.
                    plan_value = read_json(_assert_frozen_record(
                        journal.get("artifacts", {}).get("frozen", {}), "frozen"
                    )).get("batch_merge_plan")
                    plan_value_path = ROOT / str(plan_value) if plan_value else None
                    recovered = committed_transaction(
                        batch_dir / "apply-transaction.json",
                        expected_plan=plan_value_path,
                    ) if plan_value_path else None
                    if recovered is None:
                        raise ValueError(
                            "content apply materialization is missing; rerun with --rehydrate"
                        )
                    artifact = read_json(prior_path)
                    artifact["resume"] = "no-op; content 已应用，请生成 post-apply coverage 后运行 go-no-go"
                    print(json.dumps(artifact, ensure_ascii=False))
                    return 0
                print("❌ content 已应用；请生成 post-apply coverage 后运行 go-no-go（或用 --resume 查看 no-op）", file=sys.stderr)
                return 2
        draft_gate_path = (args.draft_gate.resolve() if args.draft_gate
                           else batch_dir / "draft-content-gate.json")
        if args.draft_gate:
            draft_gate = read_json(draft_gate_path)
            if (draft_gate.get("pipeline_continue", draft_gate.get("go")) is not True
                    or Path(str(draft_gate.get("plan") or "")).resolve() != plan_path
                    or draft_gate.get("plan_sha256") != sha256(plan_path.read_text(encoding="utf-8"))):
                raise ValueError("frozen draft content gate cannot continue or does not bind the current plan")
        else:
            draft_gate = gate_merge_plan(plan_path)
            if not args.dry_run:
                write_json(draft_gate_path, draft_gate)
        if not draft_gate.get("pipeline_continue", draft_gate.get("go", False)):
            artifact = {
                "protocol": {"version": BATCH_PROTOCOL, "kind": "content-apply-report"},
                "plan": file_record(plan_path),
                "draft_content_gate": (
                    file_record(draft_gate_path) if not args.dry_run else draft_gate
                ),
                "report": {"files": 0, "transaction": "not_started"},
                "disposition": "blocked_before_content_apply",
            }
            if not args.dry_run:
                transition_journal(
                    batch_dir,
                    "blocked",
                    "apply:draft-content-gate",
                    resume=args.resume,
                    artifacts={"draft_content_gate": file_record(draft_gate_path)},
                    detail="blocked_before_content_apply; repository pages unchanged",
                )
            print(json.dumps(artifact, ensure_ascii=False))
            return 2
        recovered = committed_transaction(
            batch_dir / "apply-transaction.json", expected_plan=plan_path
        ) if args.resume and not remediation and not rehydrate and not args.dry_run else None
        report = (rehydrate_committed_plan(
            plan_path, allow_partial=args.allow_partial,
            lease_seconds=args.lease_seconds,
        ) if rehydrate and not args.dry_run else {
            "files": len(recovered["targets"]),
            "dry_run": False,
            "transaction": "committed",
            "recovered_after_commit": True,
        } if recovered else apply_plan(
            plan_path,
            allow_partial=args.allow_partial,
            dry_run=args.dry_run,
            lease_seconds=args.lease_seconds,
        ))
        output = args.output or batch_dir / "content-apply-report.json"
        artifact = {
            "protocol": {"version": BATCH_PROTOCOL, "kind": "content-apply-report"},
            "plan": file_record(plan_path),
            "draft_content_gate": (
                file_record(draft_gate_path) if not args.dry_run else draft_gate
            ),
            "report": report,
            "next": "重新生成 post-apply claim coverage，再运行 go-no-go",
            "remediation": remediation,
            "rehydration": rehydrate,
            "recovery_mode": (
                "matching_plan_rehydration" if rehydrate else
                "fresh_plan_apply" if requested_rehydrate else "normal_apply"
            ),
        }
        if not args.dry_run:
            write_json(output, artifact)
            transition_journal(
                batch_dir, "frozen", "apply:content", resume=True,
                artifacts={
                    "draft_content_gate": file_record(draft_gate_path),
                    "content_apply": file_record(output),
                },
                detail="BODY/registry applied; reviewed upgrade pending",
            )
    elif effective_state == "gated":
        frozen_coverage = _assert_frozen_record(journal.get("artifacts", {}).get("coverage", {}), "coverage")
        coverage_path = args.coverage.resolve() if args.coverage else frozen_coverage
        if sha256(coverage_path.read_text(encoding="utf-8")) != sha256(frozen_coverage.read_text(encoding="utf-8")):
            raise ValueError("apply --coverage 与 journal 冻结 coverage 不一致")
        coverage, _ = load_coverage(coverage_path)
        changes: list[tuple[Path, str, str]] = []
        expected_targets: set[Path] = set()
        for node in coverage.get("nodes", []):
            if not node.get("eligible_for_reviewed"):
                continue
            path = Path(node["page"])
            if not path.is_absolute():
                path = ROOT / path
            expected_targets.add(path.resolve())
            current = path.read_text(encoding="utf-8")
            match = re.match(r"^---\n(.*?)\n---\n", current, re.S)
            if not match:
                raise ValueError(f"{path} 缺 frontmatter")
            block = match.group(1)
            for key, value in (node.get("frontmatter_updates") or {}).items():
                pattern = re.compile(rf"^{re.escape(key)}:\s*.*$", re.M)
                block = pattern.sub(f"{key}: {value}", block) if pattern.search(block) else block + f"\n{key}: {value}"
            updated = f"---\n{block}\n---\n" + current[match.end():]
            changes.append((path, updated, node["file_sha256"]))
        transaction_path = batch_dir / "coverage-apply-transaction.json"
        recovered = committed_transaction(
            transaction_path, expected_targets=expected_targets
        ) if args.resume and not args.dry_run else None
        if recovered:
            for node in coverage.get("nodes", []):
                if not node.get("eligible_for_reviewed"):
                    continue
                path = Path(node["page"])
                if not path.is_absolute():
                    path = ROOT / path
                current = path.read_text(encoding="utf-8")
                if sha256(body_of(current)) != node.get("body_sha256"):
                    raise ValueError(f"已提交 reviewed 事务 BODY 漂移: {path}")
                fm = parse_frontmatter(current)
                if any(fm.get(key) != value for key, value in (node.get("frontmatter_updates") or {}).items()):
                    raise ValueError(f"已提交 reviewed 事务 frontmatter 不完整: {path}")
            report = {
                "files": len(recovered["targets"]), "dry_run": False,
                "transaction": "committed", "recovered_after_commit": True,
            }
        else:
            report = apply_text_transaction(
                changes,
                transaction_path,
                ROOT,
                dry_run=args.dry_run,
                lease_seconds=args.lease_seconds,
            )
        output = args.output or batch_dir / "reviewed-apply-report.json"
        artifact = {
            "protocol": {"version": BATCH_PROTOCOL, "kind": "reviewed-apply-report"},
            "coverage": file_record(coverage_path),
            "report": report,
        }
        if not args.dry_run:
            write_json(output, artifact)
            transition_journal(
                batch_dir, "applied", "apply:reviewed", resume=args.resume,
                artifacts={"apply": file_record(output)},
            )
    else:
        raise ValueError(f"apply 仅接受 frozen(content) 或 gated(reviewed)，当前 {journal['state']}")
    print(json.dumps(artifact, ensure_ascii=False))
    return 0


def run_gate(name: str, command: list[str], *, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"name": name, "command": command, "returncode": None, "status": "DRY_RUN"}
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    return {
        "name": name,
        "command": command,
        "returncode": result.returncode,
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def command_gate(args: argparse.Namespace) -> int:
    prepared_path = args.prepared.resolve()
    prepared = read_json(prepared_path)
    batch_dir = prepared_path.parent
    journal = read_journal(batch_dir)
    effective_state = journal["state"]
    if effective_state in TERMINAL_STATES and args.resume:
        effective_state = str(journal.get("resume_from") or "")
    if effective_state != "apply_ready":
        raise ValueError(f"gate 要求 apply_ready 状态，当前 {journal['state']}")
    manifest = read_json(ROOT / prepared["manifest"])
    industry = manifest["industry"]
    frozen = read_json(_assert_frozen_record(
        journal.get("artifacts", {}).get("frozen", {}), "frozen"
    ))
    verified = read_json(_assert_frozen_record(
        journal.get("artifacts", {}).get("verified", {}), "verified"
    ))
    runtime_by_node: dict[str, Path] = {}
    for record in verified.get("runtime_attestations", []):
        node_ids = record.get("node_ids") or []
        if len(node_ids) != 1:
            raise ValueError("Verify runtime attestation 必须精确绑定一个节点")
        node_id = str(node_ids[0])
        if node_id in runtime_by_node:
            raise ValueError(f"重复 Verify runtime attestation: {node_id}")
        runtime_by_node[node_id] = Path(str(record.get("path", ""))).resolve()
    from wiki_research_ready import gate as page_quality_gate
    quality_nodes = []
    for item in manifest.get("nodes", []):
        node_id = str(item["node_id"])
        artifact = (frozen.get("node_artifacts") or {}).get(node_id) or {}
        claims_path = _assert_frozen_record(artifact.get("claims", {}), f"{node_id} claims")
        runtime_dir = runtime_by_node.get(node_id)
        if runtime_dir is None:
            raise ValueError(f"{node_id} 缺 Verify runtime attestation")
        page = ROOT / item["page"]
        quality_nodes.append(page_quality_gate(page, claims_path, runtime_dir))
    quality_report = {
        "protocol": {"version": "wiki-research-ready-v2", "kind": "batch-quality-gate"},
        "nodes": quality_nodes,
        "all_passed": bool(quality_nodes) and all(node.get("go") for node in quality_nodes),
    }
    quality_path = batch_dir / "quality-gate.json"
    if not args.dry_run:
        write_json(quality_path, quality_report)
    py = sys.executable
    frozen_coverage = _assert_frozen_record(journal.get("artifacts", {}).get("coverage", {}), "coverage")
    coverage_path = args.coverage.resolve() if args.coverage else frozen_coverage
    if sha256(coverage_path.read_text(encoding="utf-8")) != sha256(frozen_coverage.read_text(encoding="utf-8")):
        raise ValueError("gate --coverage 与 journal 冻结 coverage 不一致")
    commands = [
        ("validate_graph", [py, str(ROOT / "scripts/validate_graph.py"), str(ROOT / f"docs/{industry}-name-graph.json")]),
        ("wiki_lint", [py, str(ROOT / "scripts/wiki_lint.py"), str(ROOT / f"docs/{industry}-name-graph.json"), str(ROOT / f"wiki/{industry}"), str(ROOT / f"sources/{industry}/registry.json"), "--coverage", str(coverage_path)]),
        ("lca_node_search_matrix", [py, str(ROOT / "scripts/validate_lca_node_search_matrix.py")]),
        ("lca_dataset_binding", [py, str(ROOT / "scripts/validate_lca_dataset_binding.py"), "--scope", industry]),
    ]
    results = [{
        "name": "wiki_v2_quality",
        "status": "DRY_RUN" if args.dry_run else ("PASS" if quality_report["all_passed"] else "FAIL"),
        "returncode": None if args.dry_run else (0 if quality_report["all_passed"] else 1),
        "report": quality_report,
    }]
    if args.dry_run or quality_report["all_passed"]:
        results.extend(run_gate(name, command, dry_run=args.dry_run) for name, command in commands)
    gate_passed = None if args.dry_run else all(item["status"] == "PASS" for item in results)
    _, metrics = load_coverage(coverage_path)
    decision = evaluate_go_no_go(manifest, metrics, release_usage(journal), gate_passed)
    report = {
        "protocol": {"version": BATCH_PROTOCOL, "kind": "gate-report"},
        "gates": results,
        "all_passed": gate_passed,
        "go_no_go": decision,
    }
    output = args.output or batch_dir / "gate-report.json"
    if not args.dry_run:
        write_json(output, report)
        if gate_passed and decision["final_verdict"] == "GO":
            transition_journal(
                batch_dir, "gated", "gate", resume=args.resume,
                artifacts={"quality_gate": file_record(quality_path), "gate": file_record(output)},
            )
        else:
            transition_journal(batch_dir, "failed", "gate", detail="gate or Go/No-Go failed")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if args.dry_run or (gate_passed and decision["final_verdict"] == "GO") else 2


def command_publish(args: argparse.Namespace) -> int:
    prepared_path = args.prepared.resolve()
    prepared = read_json(prepared_path)
    batch_dir = prepared_path.parent
    journal = read_journal(batch_dir)
    if journal["state"] == "published":
        if not args.resume:
            raise ValueError("批次已 published；重复执行须显式传 --resume")
        prior = _assert_frozen_record(
            journal.get("artifacts", {}).get("publish", {}), "publish"
        )
        report = read_json(prior)
        report["resume"] = "no-op; bundle 已发布"
        print(json.dumps(report, ensure_ascii=False))
        return 0
    effective_state = journal["state"]
    if effective_state in TERMINAL_STATES:
        if not args.resume:
            raise ValueError(f"批次处于 {effective_state}；Publish 恢复须显式传 --resume")
        effective_state = str(journal.get("resume_from") or "")
    if effective_state != "applied":
        raise ValueError(f"publish 要求 reviewed 已原子应用的 applied 状态，当前 {journal['state']}")
    gate_path = _assert_frozen_record(journal.get("artifacts", {}).get("gate", {}), "gate")
    gate_report = read_json(gate_path)
    if gate_report.get("all_passed") is not True or (gate_report.get("go_no_go") or {}).get("final_verdict") != "GO":
        raise ValueError("publish 要求 gate 全通过且 final Go/No-Go=GO")
    manifest = read_json(ROOT / prepared["manifest"])
    industry = manifest["industry"]
    bundle_command = [
        sys.executable,
        str(ROOT / "scripts/build_wiki_bundle.py"),
        str(ROOT / f"wiki/{industry}"),
        str(ROOT / f"docs/{industry}-wiki-data.js"),
        f"{industry.upper()}_WIKI",
    ]
    bundle = run_gate("build_wiki_bundle", bundle_command, dry_run=args.dry_run)
    if not args.dry_run and bundle["status"] != "PASS":
        raise ValueError(f"bundle 重建失败: {bundle['stderr']}")
    viewer_command = [
        sys.executable,
        str(ROOT / "scripts/build_wiki_viewer.py"),
        industry,
    ]
    viewer = run_gate("build_wiki_viewer", viewer_command, dry_run=args.dry_run)
    if not args.dry_run and viewer["status"] != "PASS":
        raise ValueError(f"viewer 重建失败: {viewer['stderr']}")
    entrypoints = []
    for node in manifest.get("nodes", []):
        node_id = str(node.get("node_id") or "")
        entry_command = [
            sys.executable, str(ROOT / "scripts/build_wiki_viewer.py"),
            industry, "--start-node", node_id,
        ]
        entry = run_gate(f"build_wiki_viewer_{node_id}", entry_command, dry_run=args.dry_run)
        if not args.dry_run and entry["status"] != "PASS":
            raise ValueError(f"{node_id} viewer 入口重建失败: {entry['stderr']}")
        entrypoints.append(entry)
    output = args.output or batch_dir / "publish-report.json"
    report = {
        "protocol": {"version": BATCH_PROTOCOL, "kind": "publish-report"},
        "bundle": bundle,
        "viewer": viewer,
        "node_entrypoints": entrypoints,
    }
    if not args.dry_run:
        write_json(output, report)
        transition_journal(
            batch_dir, "published", "publish", resume=args.resume,
            artifacts={"publish": file_record(output)},
        )
    print(json.dumps(report, ensure_ascii=False))
    return 0


def command_preview(args: argparse.Namespace) -> int:
    """Build an explicitly non-publishable viewer from the current Wiki workspace."""
    industry = args.industry
    if not re.fullmatch(r"[a-z][a-z0-9_]*", industry):
        raise ValueError(f"非法 industry slug: {industry}")
    graph = ROOT / f"docs/{industry}-name-graph.json"
    wiki_root = ROOT / f"wiki/{industry}"
    registry = ROOT / f"sources/{industry}/registry.json"
    for required in (graph, wiki_root, registry):
        if not required.exists():
            raise ValueError(f"preview 缺少输入: {required}")

    py = sys.executable
    data_path = ROOT / f"docs/{industry}-wiki-preview-data.js"
    viewer_path = ROOT / f"docs/{industry}-wiki-preview.html"
    graph_preview_path = ROOT / f"docs/{industry}-name-graph-preview.html"
    commands = [
        ("validate_graph", [py, str(ROOT / "scripts/validate_graph.py"), str(graph)]),
        ("wiki_lint_base", [py, str(ROOT / "scripts/wiki_lint.py"), str(graph), str(wiki_root), str(registry)]),
        ("build_preview_bundle", [
            py, str(ROOT / "scripts/build_wiki_bundle.py"), str(wiki_root), str(data_path),
            f"{industry.upper()}_WIKI_PREVIEW", "--mode", "preview",
        ]),
        ("build_preview_viewer", [
            py, str(ROOT / "scripts/build_wiki_viewer.py"), industry,
            args.chinese_name or industry, "--preview",
        ]),
        ("build_preview_name_graph", [
            py, str(ROOT / "scripts/build_name_graph_html.py"), str(graph),
            str(graph_preview_path), "--preview",
        ]),
        ("validate_production_draft_overlay", [
            py, str(ROOT / "scripts/validate_name_graph_draft_overlay.py"), str(graph),
            str(ROOT / f"docs/{industry}-name-graph.html"),
        ]),
    ]
    results = []
    for name, command in commands:
        result = run_gate(name, command, dry_run=args.dry_run)
        results.append(result)
        if not args.dry_run and result["status"] != "PASS":
            break
    passed = None if args.dry_run else len(results) == len(commands) and all(
        item["status"] == "PASS" for item in results
    )
    report = {
        "protocol": {"version": "wiki-preview-v1", "kind": "preview-report"},
        "industry": industry,
        "mode": "preview_unpublished",
        "publish_authorized": False,
        "all_passed": passed,
        "steps": results,
        "artifacts": ({
            "data": file_record(data_path),
            "viewer": file_record(viewer_path),
            "name_graph": file_record(graph_preview_path),
        } if passed else {}),
    }
    output = args.output or ROOT / f"runs/wiki-previews/{industry}/preview-report.json"
    if not args.dry_run:
        write_json(output, report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if args.dry_run or passed else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan")
    plan.add_argument("industry")
    plan.add_argument("--nodes", action="append")
    plan.add_argument("--all", action="store_true")
    plan.add_argument("--pilot-ict10", action="store_true")
    plan.add_argument("--pilot", action="store_true")
    plan.add_argument(
        "--claim-budget",
        type=int,
        default=3,
        choices=range(1, 6),
        metavar="{1..5}",
        help="pilot 中每个节点最多处理的断言数（默认 3）",
    )
    plan.add_argument("--batch-id")
    plan.add_argument("--output", type=Path)
    plan.add_argument("--min-coverage", type=float, default=1.0)
    plan.add_argument("--min-url-quote-compliance", type=float, default=1.0)
    plan.add_argument("--max-unresolved-ratio", type=float, default=0.0)
    plan.add_argument("--max-contradicted-ratio", type=float, default=0.0)
    plan.add_argument("--max-manual-ratio", type=float, default=0.0)
    plan.add_argument("--max-search-requests", type=int, default=100)
    plan.add_argument("--max-cost-usd", type=float, default=50.0)
    plan.add_argument("--dry-run", action="store_true")
    plan.set_defaults(handler=command_plan)

    partition = sub.add_parser("partition")
    partition.add_argument("manifest", type=Path)
    partition.add_argument("--batch-size", type=int, default=8)
    partition.set_defaults(handler=command_partition)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("manifest", type=Path)
    prepare.add_argument("--resume", action="store_true")
    prepare.add_argument("--dry-run", action="store_true")
    prepare.set_defaults(handler=command_prepare)

    validate = sub.add_parser("validate")
    validate.add_argument("prepared", type=Path)
    validate.add_argument("--output", type=Path)
    validate.add_argument("--dry-run", action="store_true")
    validate.set_defaults(handler=command_validate)

    research = sub.add_parser("research-ready")
    research.add_argument("prepared", type=Path)
    research.add_argument("--evidence", type=Path, action="append", required=True)
    research.add_argument("--verify-workflow", type=Path, action="append", required=True)
    research.add_argument(
        "--nomination-usage", type=Path,
        help="含 extract 节点时必填的 wiki-usage-v1 nomination 实际模型/推理级别/费用账",
    )
    research.add_argument("--output", type=Path)
    research.add_argument("--resume", action="store_true")
    research.add_argument(
        "--repair-rewind", action="store_true",
        help="经 Orchestrator Repair Plan 授权后，将后续/失败 journal 回退到 research_ready",
    )
    research.add_argument("--dry-run", action="store_true")
    research.set_defaults(handler=command_research_ready)

    verify = sub.add_parser("verify")
    verify.add_argument("prepared", type=Path)
    verify.add_argument("--result", type=Path, action="append", required=True)
    verify.add_argument(
        "--runtime-dir", type=Path, action="append", required=True,
        help="与 --result 一一对应的 run_wiki_verify_capture.py 真实运行证据目录",
    )
    verify.add_argument(
        "--usage", type=Path, action="append", required=True,
        help="与 --result 一一对应的 wiki-usage-v1 Verify-only 费用账，可重复",
    )
    verify.add_argument("--output", type=Path)
    verify.add_argument("--resume", action="store_true")
    verify.add_argument("--dry-run", action="store_true")
    verify.set_defaults(handler=command_verify)

    finalize = sub.add_parser("finalize")
    finalize.add_argument("prepared", type=Path)
    finalize.add_argument("--extract-output", type=Path)
    finalize.add_argument("--repair-output", type=Path)
    finalize.add_argument("--content-output", type=Path,
                          help="经 Content Blueprint 校验并确定性丰富的 wiki-content-enriched-v1")
    finalize.add_argument(
        "--allow-partial",
        action="store_true",
        help="仅冻结已通过严格门的子批次；缺失模式会记录但不伪装完整完成",
    )
    finalize.add_argument("--resume", action="store_true")
    finalize.add_argument("--dry-run", action="store_true")
    finalize.set_defaults(handler=command_finalize)

    go = sub.add_parser("go-no-go")
    go.add_argument("prepared", type=Path)
    go.add_argument("--coverage", type=Path, required=True)
    go.add_argument("--output", type=Path)
    go.add_argument("--resume", action="store_true")
    go.add_argument("--dry-run", action="store_true")
    go.set_defaults(handler=command_go_no_go)

    apply_cmd = sub.add_parser("apply")
    apply_cmd.add_argument("prepared", type=Path)
    apply_cmd.add_argument("--plan", type=Path)
    apply_cmd.add_argument("--draft-gate", type=Path,
                           help="frozen GO draft-content gate bound to the current plan")
    apply_cmd.add_argument("--coverage", type=Path, help="可选复核路径；production 实际使用 journal 冻结 coverage")
    apply_cmd.add_argument("--allow-partial", action="store_true")
    apply_cmd.add_argument("--lease-seconds", type=int, default=300)
    apply_cmd.add_argument("--output", type=Path)
    apply_cmd.add_argument("--resume", action="store_true")
    apply_cmd.add_argument(
        "--rehydrate", action="store_true",
        help="仅当所有目标仍等于原事务 old_sha256 时重放冻结计划",
    )
    apply_cmd.add_argument("--dry-run", action="store_true")
    apply_cmd.set_defaults(handler=command_apply)

    gate = sub.add_parser("gate")
    gate.add_argument("prepared", type=Path)
    gate.add_argument("--coverage", type=Path, help="可选复核路径；默认使用 journal 冻结 coverage")
    gate.add_argument("--output", type=Path)
    gate.add_argument("--resume", action="store_true")
    gate.add_argument("--dry-run", action="store_true")
    gate.set_defaults(handler=command_gate)

    publish = sub.add_parser("publish")
    publish.add_argument("prepared", type=Path)
    publish.add_argument("--output", type=Path)
    publish.add_argument("--resume", action="store_true")
    publish.add_argument("--dry-run", action="store_true")
    publish.set_defaults(handler=command_publish)

    preview = sub.add_parser("preview", help="构建独立 draft 预览；不改变 journal/reviewed/publish 状态")
    preview.add_argument("industry")
    preview.add_argument("--chinese-name")
    preview.add_argument("--output", type=Path)
    preview.add_argument("--dry-run", action="store_true")
    preview.set_defaults(handler=command_preview)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.handler(args)
    except (OSError, ValueError, KeyError, MergeError, json.JSONDecodeError) as exc:
        base = None
        if hasattr(args, "prepared") and getattr(args, "prepared", None):
            base = args.prepared.resolve().parent
        elif hasattr(args, "manifest") and getattr(args, "manifest", None):
            base = args.manifest.resolve().parent
        if base is not None and not getattr(args, "dry_run", False):
            mark_journal_failure(base, args.command, exc)
        print(f"❌ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
