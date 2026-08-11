#!/usr/bin/env python3
"""Deterministic gate for LCA dataset associations and project bindings."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from grade_lca_matches import ASSOCIATION_POLICIES, C1_ROOT_CAUSES, candidate_grade


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "registry/lca_dataset_catalog.json"
CANDIDATES = ROOT / "registry/lca_binding_candidates.json"
BINDINGS = ROOT / "registry/lca_bindings.json"
ASSOCIATIONS = ROOT / "registry/lca_dataset_associations.json"
MODEL_BINDINGS = ROOT / "registry/model_dataset_bindings.json"
MODEL_PROXY_BINDINGS = ROOT / "registry/model_proxy_bindings.json"
REJECTIONS = ROOT / "registry/lca_binding_rejections.json"
NODE_STATUS = ROOT / "registry/lca_node_match_status.json"
SOURCE_CATALOG = ROOT / "registry/lca_source_catalog.json"
C2_PROFILES = ROOT / "registry/lca_c2_profiles.json"
FORMAL_AUDIT = ROOT / "registry/ict_equipment_formal_binding_audit.json"

FORMAL_EXISTENCE = {
    "official_verified",
    "official_catalog_verified",
    "official_page_verified",
    "authoritative_aggregator_verified",
}
FORMAL_BINDING = {"exact_binding", "proxy_binding"}
CANDIDATE_ONLY = {
    "unverified_recall",
    "catalog_candidate",
    "verified_candidate",
    "mirror_seen",
    "weak",
}
PRODUCT_RELATIONSHIPS = {
    "reference_product_of",
    "background_cradle_to_gate",
    "market_supply",
    "coproduct_allocated_process",
    "waste_flow_of",
    "composed_via_activity",
    "external_product_aggregate",
}
ACTIVITY_RELATIONSHIPS = {"activity_process", "waste_treatment"}
REQUIRED_DATASET_FIELDS = {
    "dataset_key",
    "database",
    "database_version",
    "system_model",
    "dataset_id",
    "dataset_type",
    "activity_name",
    "official_locator",
    "existence_status",
    "verified_at",
    "metadata_hash",
}
REQUIRED_SOURCE_FIELDS = {
    "source_id",
    "provider",
    "source_family",
    "official_locator",
    "catalog_access_status",
    "record_level_search",
    "stable_id_support",
    "license_scope",
    "checked_at",
    "limitation",
}


def load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def wiki_spine_hashes() -> dict[str, str]:
    result: dict[str, str] = {}
    for path in ROOT.glob("wiki/*/**/*.md"):
        text = path.read_text(encoding="utf-8")
        node = re.search(r"^id:\s*([AP]\d+)\s*$", text, re.M)
        spine = re.search(r'^spine_hash:\s*"([^"]+)"\s*$', text, re.M)
        if not node or not spine:
            continue
        industry = path.parts[path.parts.index("wiki") + 1]
        result[f"{industry}::{node.group(1)}"] = spine.group(1)
    return result


def relation_ok(node_ref: str, relationship: str) -> bool:
    node_id = node_ref.rsplit("::", 1)[-1]
    if node_id.startswith("P"):
        return relationship in PRODUCT_RELATIONSHIPS
    if node_id.startswith("A"):
        return relationship in ACTIVITY_RELATIONSHIPS
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", help="Only validate node_ref values in this industry")
    parser.add_argument(
        "--node",
        help="Optional node id inside --scope (for example P001)",
    )
    args = parser.parse_args()
    if args.node and not args.scope:
        parser.error("--node requires --scope")

    catalog_doc = load(CATALOG)
    candidate_doc = load(CANDIDATES)
    binding_doc = load(BINDINGS)
    association_doc = load(ASSOCIATIONS)
    model_binding_doc = load(MODEL_BINDINGS)
    model_proxy_doc = load(MODEL_PROXY_BINDINGS)
    rejection_doc = load(REJECTIONS)
    status_doc = load(NODE_STATUS) if NODE_STATUS.exists() else {"nodes": []}
    source_doc = load(SOURCE_CATALOG) if SOURCE_CATALOG.exists() else {"sources": []}
    c2_doc = load(C2_PROFILES) if C2_PROFILES.exists() else {"profiles": []}
    audit_doc = load(FORMAL_AUDIT) if FORMAL_AUDIT.exists() else {"audits": []}

    datasets = catalog_doc.get("datasets", [])
    candidates = candidate_doc.get("candidates", [])
    bindings = binding_doc.get("bindings", [])
    associations = association_doc.get("associations", [])
    model_bindings = model_binding_doc.get("bindings", [])
    model_proxy_bindings = model_proxy_doc.get("bindings", [])
    rejections = rejection_doc.get("rejections", [])
    node_statuses = status_doc.get("nodes", [])
    sources = source_doc.get("sources", [])
    c2_profiles = c2_doc.get("profiles", [])
    if args.scope:
        prefix = f"{args.scope}::"
        candidates = [x for x in candidates if x.get("node_ref", "").startswith(prefix)]
        bindings = [x for x in bindings if x.get("node_ref", "").startswith(prefix)]
        associations = [x for x in associations if x.get("node_ref", "").startswith(prefix)]
        model_bindings = [x for x in model_bindings if x.get("node_ref", "").startswith(prefix)]
        model_proxy_bindings = [
            x for x in model_proxy_bindings
            if x.get("target_node_ref", x.get("node_ref", "")).startswith(prefix)
        ]
        rejections = [x for x in rejections if x.get("node_ref", "").startswith(prefix)]
        node_statuses = [x for x in node_statuses if x.get("node_ref", "").startswith(prefix)]
    if args.node:
        target_ref = f"{args.scope}::{args.node}"
        candidates = [x for x in candidates if x.get("node_ref") == target_ref]
        bindings = [x for x in bindings if x.get("node_ref") == target_ref]
        associations = [x for x in associations if x.get("node_ref") == target_ref]
        model_bindings = [x for x in model_bindings if x.get("node_ref") == target_ref]
        model_proxy_bindings = [
            x for x in model_proxy_bindings
            if x.get("target_node_ref", x.get("node_ref", "")) == target_ref
        ]
        rejections = [x for x in rejections if x.get("node_ref") == target_ref]
        node_statuses = [x for x in node_statuses if x.get("node_ref") == target_ref]
    retained_candidate_ids = {
        item.get("candidate_id") for item in candidates
    }
    c2_profiles = [
        item for item in c2_profiles
        if item.get("candidate_id") in retained_candidate_ids
    ]

    catalog = {x.get("dataset_key"): x for x in datasets}
    wiki_hashes = wiki_spine_hashes()
    checks: list[tuple[str, list[str]]] = []

    g1 = []
    for item in associations:
        dataset = catalog.get(item.get("dataset_key"))
        if not dataset or dataset.get("existence_status") not in FORMAL_EXISTENCE:
            g1.append(item.get("association_id", "<missing-id>"))
    checks.append(("G1 数据集关联解析到合格存在性记录", g1))

    g2 = []
    declared_dataset_count = (catalog_doc.get("_meta") or {}).get("dataset_count")
    if declared_dataset_count != len(datasets):
        g2.append(
            f"catalog-meta-dataset-count:{declared_dataset_count}!={len(datasets)}"
        )
    seen_dataset_keys: set[str] = set()
    for dataset in datasets:
        key = dataset.get("dataset_key", "<missing-key>")
        missing = sorted(
            field
            for field in REQUIRED_DATASET_FIELDS
            if dataset.get(field) in (None, "")
        )
        if key in seen_dataset_keys or missing:
            g2.append(f"{key}:missing={','.join(missing) or '-'}")
        seen_dataset_keys.add(key)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", dataset.get("metadata_hash", "")):
            g2.append(f"{key}:bad-metadata-hash")
    seen_source_ids: set[str] = set()
    for source in sources:
        source_id = source.get("source_id", "<missing-source-id>")
        missing = sorted(
            field
            for field in REQUIRED_SOURCE_FIELDS
            if source.get(field) in (None, "")
        )
        if source_id in seen_source_ids or missing:
            g2.append(f"source:{source_id}:missing={','.join(missing) or '-'}")
        seen_source_ids.add(source_id)
    if not sources:
        g2.append("source-catalog-empty")
    checks.append(("G2 数据集与来源目录的版本、标识和审计字段完整", g2))

    g3 = []
    for item in candidates + bindings + associations + rejections:
        if not relation_ok(item.get("node_ref", ""), item.get("relationship_kind", "")):
            g3.append(
                item.get("candidate_id")
                or item.get("binding_id")
                or item.get("association_id")
                or item.get("rejection_id", "<missing-id>")
            )
    checks.append(("G3 节点类型与 relationship_kind 相容", g3))

    g4 = []
    for item in candidates:
        if item.get("match_status") not in CANDIDATE_ONLY:
            g4.append(item.get("candidate_id", "<missing-id>"))
    association_locators: dict[str, set[str]] = defaultdict(set)
    associations_by_node: dict[str, list[dict]] = defaultdict(list)
    for item in associations:
        associations_by_node[item.get("node_ref", "")].append(item)
        dataset = catalog.get(item.get("dataset_key")) or {}
        locator = dataset.get("official_locator")
        if locator:
            association_locators[item.get("node_ref", "")].add(locator)
    expected_node_refs = {item["node_ref"] for item in node_statuses}
    seen_page_refs: set[str] = set()
    seen_block_refs: set[str] = set()
    for path in ROOT.glob("wiki/*/**/*.md"):
        text = path.read_text(encoding="utf-8")
        node = re.search(r"^id:\s*([AP]\d+)\s*$", text, re.M)
        if not node:
            continue
        industry = path.parts[path.parts.index("wiki") + 1]
        node_ref = f"{industry}::{node.group(1)}"
        if args.scope and not node_ref.startswith(f"{args.scope}::"):
            continue
        if args.node and node_ref != f"{args.scope}::{args.node}":
            continue
        if node_ref not in expected_node_refs:
            continue
        seen_page_refs.add(node_ref)
        if (
            re.search(r"^## 🔗 背景", text, re.M)
            or "<!-- LCA_DATASET:START -->" in text
            or "<!-- LCA_DATASET:END -->" in text
            or "<!-- LCA_BINDING:START -->" in text
            or "<!-- LCA_BINDING:END -->" in text
        ):
            g4.append(f"legacy-LCA-display-remains:{node_ref}")
        blocks = re.findall(
            r"<!-- LCA_ASSOCIATION:START -->(.*?)<!-- LCA_ASSOCIATION:END -->",
            text,
            re.S,
        )
        if len(blocks) != 1:
            g4.append(f"LCA-association-block-count:{node_ref}:{len(blocks)}")
            continue
        seen_block_refs.add(node_ref)
        block_text = blocks[0]
        shown = set(re.findall(r"\]\((https?://[^\s)]+)\)", block_text))
        expected_urls = association_locators.get(node_ref, set())
        if shown != expected_urls:
            g4.append(
                f"association-url-drift:{node_ref}:"
                f"missing={sorted(expected_urls-shown)}:extra={sorted(shown-expected_urls)}"
            )
        for association in associations_by_node.get(node_ref, []):
            required_labels = {
                association.get("association_grade", ""),
                association.get("use_label", ""),
                "仍需项目裁决",
            }
            if any(label not in block_text for label in required_labels):
                g4.append(
                    f"association-missing-grade-use-or-guard:{node_ref}:"
                    f"{association.get('association_id')}"
                )
        if associations_by_node.get(node_ref):
            if "关联不等于计算绑定" not in block_text:
                g4.append(f"missing-project-binding-separation:{node_ref}")
        elif "暂无经过语义裁决的可引用数据集" not in block_text:
            g4.append(f"missing-empty-association-status:{node_ref}")
    for node_ref in sorted(expected_node_refs - seen_page_refs):
        g4.append(f"missing-wiki-page:{node_ref}")
    for node_ref in sorted(expected_node_refs - seen_block_refs):
        if f"LCA-association-block-count:{node_ref}:0" not in g4:
            g4.append(f"missing-LCA-association-block:{node_ref}")
    checks.append(("G4 Wiki 完整展示 C0–C4 关联且不冒充项目绑定", g4))

    g5 = []
    for item in bindings:
        if item.get("binding_status") != "exact_binding":
            continue
        verdict = item.get("semantic_verdict") or {}
        required = {"product", "activity", "route", "boundary", "geography", "time"}
        if set(verdict) < required or any(verdict.get(key) != "pass" for key in ("product", "activity", "route", "boundary")):
            g5.append(item.get("binding_id", "<missing-id>"))
    checks.append(("G5 兼容层 exact_binding 身份硬门完整", g5))

    g6 = []
    seen_associations: set[tuple] = set()
    for item in associations:
        key = (item.get("node_ref"), item.get("dataset_key"), item.get("relationship_kind"))
        if key in seen_associations:
            g6.append(item.get("association_id", "<missing-id>"))
        seen_associations.add(key)
    checks.append(("G6 数据集关联无重复记录", g6))

    g7 = []
    for item in candidates + bindings + associations + rejections:
        if item.get("dataset_key") not in catalog:
            g7.append(
                item.get("candidate_id")
                or item.get("binding_id")
                or item.get("association_id")
                or item.get("rejection_id", "<missing-id>")
            )
    for item in bindings:
        if not item.get("evidence_refs") or not item.get("adjudicated_at"):
            g7.append(item.get("binding_id", "<missing-id>"))
    checks.append(("G7 目录、关联与兼容绑定 Provenance 可解析", g7))

    g8 = []
    for item in candidates + bindings + associations + rejections:
        node_ref = item.get("node_ref", "")
        if wiki_hashes.get(node_ref) != item.get("node_spine_hash"):
            g8.append(f"{node_ref}:{item.get('node_spine_hash')}!={wiki_hashes.get(node_ref)}")
    status_refs = set()
    for item in node_statuses:
        node_ref = item.get("node_ref", "")
        status_refs.add(node_ref)
        if wiki_hashes.get(node_ref) != item.get("node_spine_hash"):
            g8.append(f"status:{node_ref}:{item.get('node_spine_hash')}!={wiki_hashes.get(node_ref)}")
    expected_refs = {
        node_ref for node_ref in wiki_hashes
        if (
            (not args.scope or node_ref.startswith(f"{args.scope}::"))
            and (not args.node or node_ref == f"{args.scope}::{args.node}")
        )
    }
    if status_refs != expected_refs:
        g8.append(
            f"node-status-coverage:missing={sorted(expected_refs-status_refs)[:5]}"
            f":extra={sorted(status_refs-expected_refs)[:5]}"
        )
    checks.append(("G8 节点 spine_hash 无漂移", g8))

    g9 = []
    profile_by_candidate = {
        item.get("candidate_id"): item
        for item in c2_profiles
    }
    expected_c2_ids = set()
    for item in candidates:
        expected_grade, _ = candidate_grade(item)
        candidate_id = item.get("candidate_id", "<missing-id>")
        if item.get("match_grade") != expected_grade:
            g9.append(f"{candidate_id}:grade={item.get('match_grade')} expected={expected_grade}")
        if item.get("calculation_permission") is not False:
            g9.append(f"{candidate_id}:candidate-calculation-not-false")
        if any(value == "fail" for value in (item.get("hard_gates") or {}).values()):
            g9.append(f"{candidate_id}:hard-fail-must-be-rejection")
        if expected_grade == "C1":
            required_c1 = {
                "c1_root_cause",
                "secondary_causes",
                "failed_identity_dimensions",
                "graph_action",
                "evidence_needed",
                "terminal_c1",
                "review_note",
            }
            missing_c1 = sorted(required_c1 - set(item))
            if missing_c1:
                g9.append(f"{candidate_id}:C1-missing={','.join(missing_c1)}")
            if item.get("c1_root_cause") not in C1_ROOT_CAUSES:
                g9.append(f"{candidate_id}:bad-C1-root-cause")
            if not item.get("failed_identity_dimensions"):
                g9.append(f"{candidate_id}:C1-no-failed-dimensions")
            if not item.get("evidence_needed"):
                g9.append(f"{candidate_id}:C1-no-evidence-plan")
        if expected_grade == "C2":
            expected_c2_ids.add(candidate_id)
            profile = profile_by_candidate.get(candidate_id)
            if not profile:
                g9.append(f"{candidate_id}:missing-C2-profile")
                continue
            required = {
                "target_functional_unit",
                "source_functional_unit",
                "process_boundary",
                "china_factory_parameters",
                "promotion_requirements",
            }
            missing = sorted(required - set(profile))
            if missing:
                g9.append(f"{candidate_id}:profile-missing={','.join(missing)}")
            if profile.get("calculation_permission") is not False:
                g9.append(f"{candidate_id}:profile-calculation-not-false")
            china = profile.get("china_factory_parameters") or {}
            if (
                china.get("value_tag") != "proxy"
                or china.get("calculation_permission") is not False
                or not china.get("unresolved_target_factory_fields")
            ):
                g9.append(f"{candidate_id}:bad-China-proxy-guard")
    if set(profile_by_candidate) != expected_c2_ids:
        g9.append(
            f"C2-profile-coverage:missing={sorted(expected_c2_ids-set(profile_by_candidate))[:3]}"
            f":extra={sorted(set(profile_by_candidate)-expected_c2_ids)[:3]}"
        )
    for item in bindings:
        expected_grade = "C4" if item.get("binding_status") == "exact_binding" else "C3"
        if item.get("match_grade") != expected_grade:
            g9.append(f"{item.get('binding_id')}:grade={item.get('match_grade')} expected={expected_grade}")
    grades_by_node: dict[str, Counter[str]] = defaultdict(Counter)
    for item in candidates:
        grades_by_node[item["node_ref"]][item.get("match_grade", "missing")] += 1
    for item in bindings:
        grades_by_node[item["node_ref"]][item.get("match_grade", "missing")] += 1
    grade_order = {"C0": 0, "C1": 1, "C2": 2, "C3": 3, "C4": 4}
    for status in node_statuses:
        counts = grades_by_node.get(status["node_ref"], Counter())
        expected_counts = {
            grade: counts.get(grade, 0) for grade in ("C0", "C1", "C2")
        }
        expected_association_counts = {
            grade: counts.get(grade, 0)
            for grade in ("C0", "C1", "C2", "C3", "C4")
        }
        visible = [grade for grade, count in counts.items() if count and grade in grade_order]
        expected_highest = max(visible, key=grade_order.get) if visible else "none"
        if status.get("candidate_grade_counts") != expected_counts:
            g9.append(f"{status['node_ref']}:bad-grade-counts")
        if status.get("highest_match_grade") != expected_highest:
            g9.append(
                f"{status['node_ref']}:highest={status.get('highest_match_grade')}"
                f" expected={expected_highest}"
            )
        if status.get("association_grade_counts") != expected_association_counts:
            g9.append(f"{status['node_ref']}:bad-association-grade-counts")
        if status.get("association_count") != sum(counts.values()):
            g9.append(f"{status['node_ref']}:bad-association-count")
        if status.get("highest_association_grade") != expected_highest:
            g9.append(f"{status['node_ref']}:bad-highest-association-grade")

    source_pairs = {
        (item.get("node_ref"), item.get("dataset_key"), item.get("match_grade"))
        for item in candidates + bindings
    }
    association_pairs = {
        (
            item.get("node_ref"),
            item.get("dataset_key"),
            item.get("association_grade"),
        )
        for item in associations
    }
    if source_pairs != association_pairs:
        g9.append(
            f"association-coverage:missing={sorted(source_pairs-association_pairs)[:3]}"
            f":extra={sorted(association_pairs-source_pairs)[:3]}"
        )
    for item in associations:
        grade = item.get("association_grade")
        policy = ASSOCIATION_POLICIES.get(grade, {})
        if (
            item.get("association_strength") != policy.get("association_strength")
            or item.get("potential_use") != policy.get("potential_use")
            or item.get("use_label") != policy.get("use_label")
            or item.get("calculation_permission") != "none"
            or item.get("project_binding_required") is not True
        ):
            g9.append(f"{item.get('association_id')}:bad-association-policy")
    checks.append(("G9 C0–C4 关联、C2 档案与知识层计算防火墙一致", g9))

    g10: list[str] = []
    audits = [
        item for item in audit_doc.get("audits", [])
        if (
            (not args.scope or item.get("node_ref", "").startswith(f"{args.scope}::"))
            and (not args.node or item.get("node_ref") == f"{args.scope}::{args.node}")
        )
    ]
    audit_by_pair = {
        (item.get("node_ref"), item.get("dataset_key")): item
        for item in audits
    }
    metadata_candidates = [
        item for item in candidates
        if item.get("candidate_track") == "formal_product_metadata"
    ]
    for candidate in metadata_candidates:
        key = (candidate.get("node_ref"), candidate.get("dataset_key"))
        audit = audit_by_pair.get(key)
        if not audit:
            g10.append(f"{candidate.get('candidate_id')}:missing-source-level-audit")
            continue
        if (
            candidate.get("match_grade") != "C2"
            or candidate.get("calculation_permission") is not False
            or audit.get("final_verdict") != "C2 verified_candidate"
            or audit.get("calculation_permission") is not False
        ):
            g10.append(f"{candidate.get('candidate_id')}:bad-audit-verdict")
        unresolved = audit.get("failed_or_unresolved_gates") or {}
        if "complete_source_io" not in unresolved:
            g10.append(f"{candidate.get('candidate_id')}:missing-complete-io-gate")
        if not audit.get("evidence") or not audit.get("promotion_requirements"):
            g10.append(f"{candidate.get('candidate_id')}:incomplete-audit-evidence")
    expected_pairs = {
        (item.get("node_ref"), item.get("dataset_key"))
        for item in metadata_candidates
    }
    if set(audit_by_pair) != expected_pairs:
        g10.append(
            f"audit-coverage:missing={sorted(expected_pairs-set(audit_by_pair))[:3]}"
            f":extra={sorted(set(audit_by_pair)-expected_pairs)[:3]}"
        )
    for binding in bindings:
        dataset = catalog.get(binding.get("dataset_key"), {})
        if (
            binding.get("calculation_permission") is True
            and dataset.get("license_scope") in {"metadata_only", "public_metadata_only"}
        ):
            g10.append(f"{binding.get('binding_id')}:metadata-only-calculation-enabled")
    checks.append(("G10 来源级审计覆盖且元数据不得越权计算", g10))

    g11: list[str] = []
    for item in associations:
        if item.get("knowledge_layer_status") != "verified_association":
            g11.append(f"{item.get('association_id')}:bad-knowledge-layer-status")
    for item in model_bindings:
        missing = [
            field for field in (
                "project_id",
                "model_version",
                "node_ref",
                "dataset_key",
                "binding_status",
            )
            if item.get(field) in (None, "")
        ]
        if missing:
            g11.append(f"model-binding-missing:{item.get('binding_id')}:{missing}")
    for item in model_proxy_bindings:
        missing = [
            field for field in (
                "project_id",
                "model_version",
                "target_node_ref",
                "dataset_key",
                "proxy_grade",
                "calculation_permission",
            )
            if item.get(field) in (None, "")
        ]
        if missing:
            g11.append(f"model-proxy-missing:{item.get('proxy_binding_id')}:{missing}")
        if item.get("proxy_grade") == "P2" and item.get(
            "calculation_permission"
        ) != "scenario_only":
            g11.append(f"model-proxy-P2-overreach:{item.get('proxy_binding_id')}")
    checks.append(("G11 知识关联与项目模型绑定分层且 P 权限受控", g11))

    passed = 0
    for label, errors in checks:
        if errors:
            print(f"❌ {label} ({len(errors)}): {errors[:5]}")
        else:
            passed += 1
            print(f"✅ {label}")
    print(
        f"\nLCA dataset association gate: {passed}/{len(checks)} passed"
        f" · sources={len(sources)} catalog={len(datasets)} candidates={len(candidates)}"
        f" associations={len(associations)} project_bindings={len(model_bindings)}"
        f" project_proxies={len(model_proxy_bindings)} rejections={len(rejections)}"
    )
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
