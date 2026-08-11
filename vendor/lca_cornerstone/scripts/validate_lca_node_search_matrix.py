#!/usr/bin/env python3
"""Deterministic gate for the per-node LCA source search matrix."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "registry/lca_node_search_matrix.json"
SOURCE_CATALOG = ROOT / "registry/lca_source_catalog.json"
WIKI_ROOT = ROOT / "wiki/ict_equipment"

SEARCHED_STATUSES = {
    "searched_full_public_catalog",
    "searched_full_public_metadata_catalog",
    "searched_complete_official_public_sample",
    "searched_discovery_index",
}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def wiki_hashes() -> dict[str, str]:
    result = {}
    for path in WIKI_ROOT.glob("**/*.md"):
        text = path.read_text(encoding="utf-8")
        node = re.search(r"^id:\s*([AP]\d+)\s*$", text, re.M)
        spine = re.search(r'^spine_hash:\s*"([^"]+)"\s*$', text, re.M)
        if node and spine:
            result[f"ict_equipment::{node.group(1)}"] = spine.group(1)
    return result


def main() -> int:
    matrix = load(MATRIX)
    source_catalog = load(SOURCE_CATALOG)
    rows = matrix.get("nodes", [])
    expected_hashes = wiki_hashes()
    expected_sources = {item["source_id"] for item in source_catalog.get("sources", [])}
    checks: list[tuple[str, list[str]]] = []

    refs = [item.get("node_ref") for item in rows]
    expected_node_count = len(expected_hashes)
    expected_cell_count = expected_node_count * len(expected_sources)
    g1 = []
    if (
        len(rows) != expected_node_count
        or set(refs) != set(expected_hashes)
        or len(refs) != len(set(refs))
    ):
        g1.append(
            f"rows={len(rows)} unique={len(set(refs))} "
            f"missing={sorted(set(expected_hashes)-set(refs))[:5]}"
        )
    checks.append((f"M1 {expected_node_count} 个节点双向覆盖且唯一", g1))

    g2 = []
    for row in rows:
        if row.get("node_spine_hash") != expected_hashes.get(row.get("node_ref")):
            g2.append(row.get("node_ref", "<missing>"))
    checks.append(("M2 节点 spine_hash 无漂移", g2))

    g3 = []
    for row in rows:
        sources = row.get("sources", [])
        ids = [item.get("source_id") for item in sources]
        if set(ids) != expected_sources or len(ids) != len(set(ids)):
            g3.append(
                f"{row.get('node_ref')}:missing={sorted(expected_sources-set(ids))}"
                f":extra={sorted(set(ids)-expected_sources)}"
            )
        if not row.get("identity_queries"):
            g3.append(f"{row.get('node_ref')}:no-query")
    checks.append(("M3 每节点覆盖全部来源并带身份查询词", g3))

    g4 = []
    for row in rows:
        for source in row.get("sources", []):
            status = source.get("search_status", "")
            hit_count = source.get("hit_count")
            if status in SEARCHED_STATUSES and not isinstance(hit_count, int):
                g4.append(f"{row.get('node_ref')}:{source.get('source_id')}:missing-count")
            if status not in SEARCHED_STATUSES and hit_count == 0:
                g4.append(f"{row.get('node_ref')}:{source.get('source_id')}:false-zero")
    checks.append(("M4 未执行/受阻来源不得伪装零结果", g4))

    g5 = []
    for row in rows:
        for source in row.get("sources", []):
            for hit in source.get("top_hits", []):
                if hit.get("adjudication_status") not in {
                    "discovery_only",
                    "discovery_only_original_provider_verification_required",
                }:
                    g5.append(
                        f"{row.get('node_ref')}:{source.get('source_id')}:"
                        f"{hit.get('dataset_id')}"
                    )
    checks.append(("M5 词面/Nexus 命中只能是 discovery_only", g5))

    g6 = []
    matrix_meta = matrix.get("_meta") or {}
    source_meta = source_catalog.get("_meta") or {}
    meta_catalogs = matrix_meta.get("catalogs") or {}
    minimums = {
        "ecoinvent_3_12_cutoff_records": 26000,
        "sphera_2026_1_records": 19000,
        "aist_idea_3_4_public_sample_records": 9000,
    }
    for key, minimum in minimums.items():
        if int(meta_catalogs.get(key) or 0) < minimum:
            g6.append(f"{key}:{meta_catalogs.get(key)}<{minimum}")
    if int(matrix_meta.get("matrix_cell_count") or 0) != expected_cell_count:
        g6.append("matrix-cell-count")
    if matrix_meta.get("node_count") != expected_node_count:
        g6.append(
            f"matrix-node-count:{matrix_meta.get('node_count')}!={expected_node_count}"
        )
    if matrix_meta.get("source_count_per_node") != len(expected_sources):
        g6.append(
            "matrix-source-count:"
            f"{matrix_meta.get('source_count_per_node')}!={len(expected_sources)}"
        )
    if matrix_meta.get("checked_at") != source_meta.get("checked_at"):
        g6.append(
            "matrix-source-freeze-date:"
            f"{matrix_meta.get('checked_at')}!={source_meta.get('checked_at')}"
        )
    checks.append((f"M6 本地公开目录为完整遍历且矩阵 {expected_cell_count} 格", g6))

    passed = 0
    for label, errors in checks:
        if errors:
            print(f"❌ {label} ({len(errors)}): {errors[:5]}")
        else:
            passed += 1
            print(f"✅ {label}")
    print(f"\nLCA node search matrix gate: {passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
