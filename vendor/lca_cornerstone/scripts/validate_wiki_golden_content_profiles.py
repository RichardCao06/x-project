#!/usr/bin/env python3
"""Validate rich draft Wiki profiles without granting reviewed authority.

The content profile is deliberately separate from the reviewed Golden
certificate.  It freezes a rich, source-grounded draft as a regression
baseline while leaving final claim review and expert sign-off pending.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from wiki_claim_coverage import body_of, factual_sentences, frontmatter_of
from wiki_quality_contract import SECTIONS
from wiki_research_ready import table_rows


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "wiki-golden-content-profile-v1"
CITE_RE = re.compile(r"\[\^([a-z0-9-]+)\](?!:)")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_file(record: dict, label: str) -> Path:
    path = (ROOT / str(record.get("path", ""))).resolve()
    if not path.is_file() or not path.is_relative_to(ROOT):
        raise ValueError(f"{label} 缺文件或越界: {path}")
    expected = str(record.get("sha256", ""))
    if not expected or digest(path) != expected:
        raise ValueError(f"{label} SHA-256 漂移: {path}")
    return path


def validate_profile(profile_id: str, profile: dict) -> dict:
    if profile.get("status") != "content_candidate_pending_expert_review":
        raise ValueError(f"{profile_id} 非受控内容候选状态")

    page_cfg = profile.get("page") or {}
    page_path = resolve_file(page_cfg, f"{profile_id} page")
    page_text = page_path.read_text(encoding="utf-8")
    body = body_of(page_text)
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if body_hash != page_cfg.get("body_sha256"):
        raise ValueError(f"{profile_id} BODY SHA-256 漂移")

    fm = frontmatter_of(page_text)
    for key, expected in (profile.get("expected_frontmatter") or {}).items():
        if fm.get(key) != expected:
            raise ValueError(f"{profile_id} {key}={fm.get(key)!r} != {expected!r}")

    graph_cfg = profile.get("graph") or {}
    graph_path = resolve_file(graph_cfg, f"{profile_id} graph")
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    matches = [
        node for node in graph.get("activities", [])
        if node.get("id") == graph_cfg.get("node_id")
    ]
    if len(matches) != 1:
        raise ValueError(f"{profile_id} 图谱节点不唯一")
    node = matches[0]
    if (
        (node.get("facets") or {}).get("reference_product_anchor")
        != graph_cfg.get("reference_product_anchor")
        or fm.get("spine_hash") != graph_cfg.get("spine_hash")
    ):
        raise ValueError(f"{profile_id} 参考产品或 spine_hash 漂移")

    contract = profile.get("contract") or {}
    node_type = str(contract.get("node_type", ""))
    headings = re.findall(r"(?m)^##\s+(.+?)\s*$", body)
    if headings != SECTIONS.get(node_type):
        raise ValueError(f"{profile_id} wiki-v2 十节结构漂移")
    if "未核实·模型回忆" in body:
        raise ValueError(f"{profile_id} BODY 出现模型回忆")

    assertions = factual_sentences(body)
    cited = [row for row in assertions if row.get("citations")]
    controlled = [row for row in assertions if row.get("explicit_downgrade")]
    unclassified = [
        row for row in assertions
        if not row.get("citations") and not row.get("explicit_downgrade")
    ]
    inline_sources = set(CITE_RE.findall(body))
    checks = {
        "body_chars": len(body) >= int(contract.get("minimum_body_chars", 0)),
        "assertions": len(assertions) >= int(contract.get("minimum_assertions", 0)),
        "cited_assertions": len(cited) >= int(contract.get("minimum_cited_assertions", 0)),
        "distinct_inline_sources": len(inline_sources)
        >= int(contract.get("minimum_distinct_inline_sources", 0)),
        "unclassified_assertions": len(unclassified)
        <= int(contract.get("maximum_unclassified_assertions", 0)),
        "required_core_citations": set(contract.get("required_core_citations") or [])
        <= inline_sources,
        "required_content_tokens": all(
            str(token) in page_text for token in contract.get("required_content_tokens") or []
        ),
        "evidence_tables": all(
            table_rows(page_text, kind) >= int(minimum)
            for kind, minimum in (contract.get("evidence_table_minimum_rows") or {}).items()
        ),
    }
    failed = [key for key, value in checks.items() if not value]
    if failed:
        raise ValueError(f"{profile_id} 内容 Golden 候选失败: {failed}")

    return {
        "status": profile.get("status"),
        "body_chars": len(body),
        "assertions": len(assertions),
        "cited_assertions": len(cited),
        "controlled_assertions": len(controlled),
        "unclassified_assertions": len(unclassified),
        "distinct_inline_sources": len(inline_sources),
        "evidence_table_rows": {
            kind: table_rows(page_text, kind)
            for kind in (contract.get("evidence_table_minimum_rows") or {})
        },
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "registry",
        nargs="?",
        type=Path,
        default=ROOT / "registry/wiki_golden_content_profiles.json",
    )
    args = parser.parse_args()
    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    if (registry.get("protocol") or {}).get("version") != PROTOCOL:
        raise ValueError(f"内容 Golden registry 协议必须为 {PROTOCOL}")
    reports = {
        profile_id: validate_profile(profile_id, profile)
        for profile_id, profile in (registry.get("profiles") or {}).items()
    }
    print(json.dumps({"go": True, "profiles": reports}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
