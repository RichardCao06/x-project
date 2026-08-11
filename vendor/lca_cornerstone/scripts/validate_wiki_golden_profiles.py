#!/usr/bin/env python3
"""Replay and validate every registered node-Wiki golden profile."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from wiki_claim_coverage import body_of, factual_sentences, frontmatter_of, normalize_claim
from wiki_quality_contract import required_external_claim_slots
from wiki_research_ready import REQUIRED_DISABLED, argv_values
from wiki_source_discovery import extract_excerpt


ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_record(record: dict, label: str) -> Path:
    path = (ROOT / str(record.get("path", ""))).resolve()
    if not path.is_file():
        raise ValueError(f"{label} 缺文件: {path}")
    if digest(path) != record.get("sha256"):
        raise ValueError(f"{label} hash 漂移: {path}")
    return path


def validate_profile(profile_id: str, profile: dict) -> dict:
    if profile.get("status") != "certified":
        raise ValueError(f"{profile_id} status 非 certified")
    page_path = resolve_record(profile.get("page") or {}, f"{profile_id} page")
    page_text = page_path.read_text(encoding="utf-8")
    fm = frontmatter_of(page_text)
    body_hash = hashlib.sha256(body_of(page_text).encode("utf-8")).hexdigest()
    if body_hash != (profile.get("page") or {}).get("body_sha256"):
        raise ValueError(f"{profile_id} BODY hash 漂移")
    expected_status = {
        "schema_version": "wiki-v2",
        "body_status": "reviewed",
        "content_maturity": "research_ready",
        "provenance_status": "claim_verified",
        "claim_verification_status": "complete",
    }
    for key, value in expected_status.items():
        if fm.get(key) != value:
            raise ValueError(f"{profile_id} {key}={fm.get(key)!r} != {value!r}")
    if fm.get("dataset_readiness") != profile.get("dataset_readiness"):
        raise ValueError(f"{profile_id} dataset_readiness 漂移")

    graph_cfg = profile.get("graph") or {}
    graph_path = ROOT / str(graph_cfg.get("path", ""))
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    nodes = [node for node in graph.get("activities", []) if node.get("id") == graph_cfg.get("node_id")]
    if len(nodes) != 1:
        raise ValueError(f"{profile_id} 图中节点不唯一")
    anchor = (nodes[0].get("facets") or {}).get("reference_product_anchor")
    if anchor != graph_cfg.get("reference_product_anchor") or fm.get("spine_hash") != graph_cfg.get("spine_hash"):
        raise ValueError(f"{profile_id} 参考产品或 spine_hash 漂移")

    certificate_path = resolve_record(profile.get("certificate") or {}, f"{profile_id} certificate")
    certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
    if (
        (certificate.get("protocol") or {}).get("version") != "wiki-golden-certificate-v1"
        or certificate.get("profile_id") != profile_id
        or (certificate.get("page") or {}).get("body_sha256") != body_hash
    ):
        raise ValueError(f"{profile_id} portable certificate 与当前 profile/BODY 不一致")
    claims = certificate.get("claims") or []
    contract = profile.get("contract") or {}
    if len(claims) != contract.get("claims_total"):
        raise ValueError(f"{profile_id} claim 总数漂移")
    body_assertions = factual_sentences(body_of(page_text))
    assertion_texts = [normalize_claim(str(item.get("text", ""))) for item in body_assertions]
    claim_texts = [normalize_claim(str(item.get("claim_text", ""))) for item in claims]
    if len(assertion_texts) != len(claim_texts) or sorted(assertion_texts) != sorted(claim_texts):
        raise ValueError(f"{profile_id} certificate claims 未双向覆盖当前 BODY")
    external = [
        row for row in claims
        if row.get("claim_kind") == "external_fact"
        and row.get("verdict") == "CONFIRMED"
        and row.get("node_alignment") == "EXACT"
    ]
    slots = {str(row.get("requirement_id", "")) for row in external}
    if len(external) != contract.get("external_confirmed_exact") or slots != required_external_claim_slots("activity"):
        raise ValueError(f"{profile_id} external claim slot 覆盖漂移")
    replay_cache: dict[tuple[str, str, str, int, str], tuple[str, str]] = {}
    for row in external:
        evidence = row.get("evidence") or {}
        excerpt = str(evidence.get("excerpt", ""))
        quote = str(evidence.get("supporting_quote", ""))
        payload_path = (ROOT / str(evidence.get("payload_path", ""))).resolve()
        if not payload_path.is_file() or digest(payload_path) != evidence.get("content_sha256"):
            raise ValueError(f"{profile_id}/{row.get('claim_id')} raw payload 缺失或 hash 漂移")
        replay_key = (
            str(payload_path), str(evidence.get("url", "")),
            str(evidence.get("content_type", "")), int(evidence.get("max_excerpt_chars", 0)),
            str(evidence.get("excerpt_locator", "")),
        )
        if replay_key not in replay_cache:
            replay_cache[replay_key] = extract_excerpt(
                replay_key[1], payload_path.read_bytes(), replay_key[2], replay_key[3], replay_key[4]
            )
        media_type, replayed_excerpt = replay_cache[replay_key]
        if (
            not str(evidence.get("url", "")).startswith("https://")
            or not quote or quote not in excerpt
            or media_type != evidence.get("content_type")
            or replayed_excerpt != excerpt
            or evidence.get("excerpt_sha256") != hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
            or evidence.get("supporting_quote_sha256") != hashlib.sha256(quote.encode("utf-8")).hexdigest()
        ):
            raise ValueError(f"{profile_id}/{row.get('claim_id')} certificate evidence 无效")
    internal = [row for row in claims if row.get("verdict") == "NOT_FOUND"]
    if len(internal) != contract.get("controlled_internal"):
        raise ValueError(f"{profile_id} controlled internal 数量漂移")
    domains = sorted({
        (urlsplit(str((row.get("evidence") or {}).get("url", ""))).hostname or "").lower()
        for row in external
    } - {""})
    if domains != contract.get("independent_domains"):
        raise ValueError(f"{profile_id} 独立来源域漂移")

    runtime = certificate.get("verify_runtime") or {}
    argv = [str(value) for value in runtime.get("argv", [])]
    disabled = runtime.get("disabled_capabilities") or []
    if (
        runtime.get("model") != "gpt-5.6-sol"
        or runtime.get("reasoning_effort") != "medium"
        or runtime.get("sandbox") != "read-only"
        or runtime.get("exit_code") != 0
        or not runtime.get("usage_records")
        or set(disabled) != REQUIRED_DISABLED
        or set(argv_values(argv, "--disable")) != REQUIRED_DISABLED
        or argv_values(argv, "-m") != ["gpt-5.6-sol"]
        or argv_values(argv, "-s") != ["read-only"]
    ):
        raise ValueError(f"{profile_id} portable Verify runtime 证明无效")
    ready = certificate.get("research_ready") or {}
    if ready.get("go") is not True or not all((ready.get("checks") or {}).values()):
        raise ValueError(f"{profile_id} research-ready certificate 非 GO")
    coverage = certificate.get("coverage") or {}
    summary = coverage.get("summary") or {}
    covered_node = coverage.get("node") or {}
    if (
        summary.get("coverage_rate") != 1.0
        or summary.get("quote_compliance_rate") != 1.0
        or summary.get("total") != contract.get("claims_total")
        or not covered_node.get("eligible_for_reviewed")
        or covered_node.get("body_sha256") != body_hash
        or covered_node.get("reasons")
    ):
        raise ValueError(f"{profile_id} reviewed coverage certificate 无效")
    return {"claims": len(claims), "external": len(external), "domains": domains, "coverage": 1.0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registry", nargs="?", type=Path, default=ROOT / "registry/wiki_golden_profiles.json")
    args = parser.parse_args()
    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    if (registry.get("protocol") or {}).get("version") != "wiki-golden-profile-v1":
        raise ValueError("golden registry 协议非 wiki-golden-profile-v1")
    reports = {key: validate_profile(key, value) for key, value in (registry.get("profiles") or {}).items()}
    print(json.dumps({"go": True, "profiles": reports}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
