#!/usr/bin/env python3
"""Deterministic gates for executed Search and current-job source diversity."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


EXECUTED = {"found", "not_found"}
FAILED = {"error", "budget_skipped"}
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN = re.compile(r"[A-Za-z]")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def search_gate(evidence: dict, *, allow_partial: bool) -> dict:
    external = [item for item in evidence.get("claims", []) if item.get("query") is not None]
    statuses = [item.get("search_status") for item in external]
    checks = {
        "has_external_queries": bool(external),
        "no_planned_queries": all(status != "planned" for status in statuses),
        "all_queries_reached_terminal_search_state": all(
            status in EXECUTED | FAILED for status in statuses
        ),
        "at_least_one_query_executed": any(status in EXECUTED for status in statuses),
    }
    complete = bool(external) and all(status in EXECUTED for status in statuses)
    partial = bool(external) and checks["at_least_one_query_executed"] and not complete
    decision = "PASS" if complete else ("PARTIAL" if allow_partial and partial else "BLOCKED")
    return {
        "protocol": "wiki-search-execution-gate-v1",
        "decision": decision,
        "checks": checks,
        "counts": {
            "queries": len(statuses),
            "found": statuses.count("found"),
            "not_found": statuses.count("not_found"),
            "failed": sum(status in FAILED for status in statuses),
        },
        "allow_partial": allow_partial,
    }


def _normalized_language(value: object) -> str | None:
    text = str(value or "").strip().lower().replace("_", "-")
    if text.startswith("zh") or text in {"cn", "chinese"}:
        return "zh"
    if text.startswith("en") or text == "english":
        return "en"
    return None


def source_language(row: dict) -> tuple[str, str]:
    """Determine source language from provenance first, fetched text second.

    Claim text is deliberately excluded because the nomination ledger is
    Chinese even when it points at an English source.
    """
    claim = row.get("claim") or {}
    fetched = row.get("fetchResult") or {}
    for owner, label in ((row, "row"), (fetched, "fetch"), (claim, "claim")):
        for key in ("source_language", "content_language", "query_language", "language", "lang"):
            language = _normalized_language(owner.get(key))
            if language:
                return language, f"{label}.{key}"
    sample = " ".join((
        str(claim.get("believed_source") or ""),
        str(fetched.get("title") or ""),
        str(fetched.get("excerpt") or "")[:12000],
    ))
    cjk = len(_CJK.findall(sample))
    latin = len(_LATIN.findall(sample))
    if cjk >= 20 and cjk / max(1, cjk + latin) >= 0.15:
        return "zh", "content_script_ratio"
    if latin >= 20:
        return "en", "content_script_ratio"
    return "unknown", "insufficient_language_signal"


def diversity_gate(verified: dict, plan: dict, *, reviewed: bool,
                   attempt: int = 0, repair_budget: int = 2) -> dict:
    rows = verified.get("claims") or verified.get("result", {}).get("claims") or []
    confirmed = [
        row for row in rows
        if (row.get("verify") or {}).get("verdict") == "CONFIRMED"
        and (row.get("fetchResult") or {}).get("url")
    ]
    urls = {str(row["fetchResult"]["url"]) for row in confirmed}
    domains = {urlsplit(url).hostname for url in urls if urlsplit(url).hostname}
    technical = {
        domain for domain in domains
        if domain and not domain.endswith("gov.cn") and not domain.endswith("gov.cn.")
    }
    detected = [source_language(row) for row in confirmed]
    languages = {language for language, _method in detected if language != "unknown"}
    required = plan.get("minimum_source_diversity", {})
    requirement_ids = {
        str((row.get("claim") or {}).get("requirement_id") or "").lower()
        for row in confirmed
    }
    role_checks = {
        "identity_source_role": any("identity" in value or "reference.product" in value
                                    for value in requirement_ids),
        "process_boundary_source_role": any(
            any(token in value for token in ("boundary", "process", "origin", "route"))
            for value in requirement_ids
        ),
        "adjacent_distinction_source_role": any(
            "adjacent" in value or "distinction" in value for value in requirement_ids
        ),
    }
    quality_checks = {
        "preview_primary_sources": len(urls) >= int(required.get("preview_primary_sources", 3)),
        "preview_distinct_domains": len(domains) >= int(required.get("preview_distinct_domains", 3)),
        "preview_technical_source": len(technical) >= int(required.get("preview_technical_sources", 1)),
        "preview_language_tracks": len(languages) >= int(required.get("preview_language_tracks", 2)),
        **role_checks,
    }
    candidate_ready = bool(confirmed) and all(quality_checks.values())
    hard_checks = {"candidate_source_roles_and_diversity": candidate_ready}
    if reviewed:
        hard_checks.update({
            "reviewed_confirmed_urls": len(urls) >= int(
                required.get("reviewed_primary_sources", required.get("preview_primary_sources", 3))
            ),
            "reviewed_distinct_domains": len(domains) >= int(
                required.get("reviewed_distinct_domains", required.get("preview_distinct_domains", 3))
            ),
            "reviewed_technical_sources": len(technical) >= int(
                required.get("reviewed_technical_sources", 2)
            ),
            "reviewed_language_tracks": len(languages) >= int(
                required.get("reviewed_language_tracks", required.get("preview_language_tracks", 2))
            ),
        })
    warnings = [name for name, passed in quality_checks.items() if not passed]
    if all(hard_checks.values()):
        decision = "PASS"
    elif reviewed:
        decision = "BLOCKED"
    elif attempt < repair_budget:
        decision = "REPAIR"
    else:
        decision = "LIMITED"
    return {
        "protocol": "wiki-source-diversity-gate-v1",
        "decision": decision,
        "pipeline_continue": decision in {"PASS", "LIMITED"},
        "candidate_eligible": decision == "PASS",
        "checks": hard_checks,
        "quality_checks": quality_checks,
        "warnings": warnings,
        "repair_target": "research_ready" if decision == "REPAIR" else None,
        "maturity_ceiling": ("wiki_candidate" if decision == "PASS"
                             else "evidence_limited" if decision == "LIMITED"
                             else "diagnostic_preview"),
        "attempt": attempt,
        "repair_budget": repair_budget,
        "metrics": {
            "confirmed_urls": len(urls),
            "confirmed_domains": len(domains),
            "technical_domains": len(technical),
            "confirmed_language_tracks": sorted(languages),
            "language_detection": [
                {"claim_id": str((row.get("claim") or {}).get("claim_id") or ""),
                 "language": language, "method": method}
                for row, (language, method) in zip(confirmed, detected)
            ],
        },
        "reviewed": reviewed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    search = sub.add_parser("search")
    search.add_argument("evidence", type=Path)
    search.add_argument("output", type=Path)
    search.add_argument("--allow-partial", action="store_true")
    diversity = sub.add_parser("diversity")
    diversity.add_argument("verified", type=Path)
    diversity.add_argument("plan", type=Path)
    diversity.add_argument("output", type=Path)
    diversity.add_argument("--reviewed", action="store_true")
    diversity.add_argument("--attempt", type=int, default=0)
    diversity.add_argument("--repair-budget", type=int, default=2)
    args = parser.parse_args()
    if args.cmd == "search":
        result = search_gate(load(args.evidence), allow_partial=args.allow_partial)
        inputs = [args.evidence]
    else:
        result = diversity_gate(load(args.verified), load(args.plan), reviewed=args.reviewed,
                                attempt=args.attempt, repair_budget=args.repair_budget)
        inputs = [args.verified, args.plan]
    result["inputs_sha256"] = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs
    }
    dump(args.output, result)
    return 0 if result["decision"] in {"PASS", "PARTIAL", "LIMITED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
