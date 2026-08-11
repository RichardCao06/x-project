#!/usr/bin/env python3
"""Freeze a portable, reviewable certificate from a complete golden audit run."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from wiki_claim_coverage import body_of, validate_artifact
from wiki_research_ready import runtime_attestation
from wiki_source_discovery import validate_evidence


ROOT = Path(__file__).resolve().parents[1]


def sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--page", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--claims", required=True, type=Path)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--research-ready", required=True, type=Path)
    parser.add_argument("--coverage", required=True, type=Path)
    parser.add_argument("--portable-payload-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    validate_evidence(evidence, require_payload=True, require_source_chain=True)
    rows = json.loads(args.claims.read_text(encoding="utf-8")).get("claims", [])
    runtime_ok, runtime_errors = runtime_attestation(args.runtime_dir, rows)
    if not runtime_ok:
        raise ValueError(f"Verify runtime attestation 失败: {runtime_errors}")
    ready = json.loads(args.research_ready.read_text(encoding="utf-8"))
    if ready.get("go") is not True:
        raise ValueError("research-ready 非 GO")
    coverage = json.loads(args.coverage.read_text(encoding="utf-8"))
    validate_artifact(coverage)
    page_text = args.page.read_text(encoding="utf-8")
    body_hash = sha_text(body_of(page_text))
    node_id = args.profile_id.split("::", 1)[-1]
    matched = [node for node in coverage.get("nodes", []) if node.get("node_id") == node_id]
    if len(matched) != 1 or not matched[0].get("eligible_for_reviewed") or matched[0].get("body_sha256") != body_hash:
        raise ValueError("coverage 未授权当前 BODY reviewed")

    portable_payloads = {
        hashlib.sha256(path.read_bytes()).hexdigest(): path.resolve()
        for path in args.portable_payload_dir.iterdir() if path.is_file()
    }
    evidence_by_claim = {
        str((entry.get("claim") or {}).get("claim_id", "")): entry
        for entry in evidence.get("claims", [])
    }
    frozen_claims = []
    for row in rows:
        claim = row.get("claim") or {}
        fetch = row.get("fetchResult") or {}
        verify = row.get("verify") or {}
        item = {
            "claim_id": claim.get("claim_id"),
            "requirement_id": claim.get("requirement_id"),
            "section": claim.get("section"),
            "claim_kind": claim.get("claim_kind"),
            "claim_text": claim.get("claim_text"),
            "verdict": verify.get("verdict"),
            "node_alignment": verify.get("node_alignment"),
        }
        if verify.get("verdict") == "CONFIRMED":
            excerpt = str(fetch.get("excerpt", ""))
            quote = str(verify.get("supporting_quote", ""))
            if not quote or quote not in excerpt:
                raise ValueError(f"{claim.get('claim_id')} decisive quote 不在 excerpt")
            source_candidates = [
                candidate for candidate in evidence_by_claim[str(claim.get("claim_id", ""))].get("candidates", [])
                if candidate.get("evidence_id") == fetch.get("evidence_id")
            ]
            if len(source_candidates) != 1:
                raise ValueError(f"{claim.get('claim_id')} evidence_id 不解析到唯一 raw candidate")
            source_candidate = source_candidates[0]
            content_hash = str(source_candidate.get("content_sha256", ""))
            payload_path = portable_payloads.get(content_hash)
            if payload_path is None or not payload_path.is_relative_to(ROOT):
                raise ValueError(f"{claim.get('claim_id')} 缺可提交 raw payload")
            item["evidence"] = {
                "evidence_id": fetch.get("evidence_id"),
                "url": fetch.get("url"),
                "content_sha256": content_hash,
                "content_type": source_candidate.get("content_type"),
                "payload_path": str(payload_path.relative_to(ROOT)),
                "excerpt_locator": source_candidate.get("excerpt_locator"),
                "max_excerpt_chars": int((evidence.get("hard_limits") or {}).get("max_excerpt_chars", 0)),
                "excerpt": excerpt,
                "excerpt_sha256": sha_text(excerpt),
                "supporting_quote": quote,
                "supporting_quote_sha256": sha_text(quote),
            }
        frozen_claims.append(item)

    invocation = json.loads((args.runtime_dir / "verify-invocation.json").read_text(encoding="utf-8"))
    usage = json.loads((args.runtime_dir / "verify-usage.json").read_text(encoding="utf-8"))
    certificate = {
        "protocol": {"version": "wiki-golden-certificate-v1", "kind": "wiki-golden-certificate"},
        "profile_id": args.profile_id,
        "node_id": node_id,
        "page": {"body_sha256": body_hash},
        "claims": frozen_claims,
        "verify_runtime": {
            "model": invocation.get("model"),
            "reasoning_effort": invocation.get("reasoning_effort"),
            "sandbox": invocation.get("sandbox"),
            "disabled_capabilities": invocation.get("disabled_capabilities"),
            "argv": invocation.get("argv"),
            "exit_code": usage.get("exit_code"),
            "usage_records": usage.get("usage_records"),
            "artifact_hashes": usage.get("artifacts"),
        },
        "research_ready": {"go": True, "checks": ready.get("checks"), "metrics": ready.get("metrics")},
        "coverage": {"summary": coverage.get("summary"), "node": matched[0]},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(certificate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "claims": len(frozen_claims), "body_sha256": body_hash}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
