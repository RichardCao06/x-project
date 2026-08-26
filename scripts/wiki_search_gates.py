#!/usr/bin/env python3
"""Deterministic gates for executed Search and current-job source diversity."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from wiki_research_contract import match_question, validate_question_contracts


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


def _verified_rows(verified: dict) -> list[dict]:
    rows = verified.get("claims") or verified.get("result", {}).get("claims") or []
    return [row for row in rows if isinstance(row, dict)]


def question_evidence_ledger(verified: dict, plan: dict) -> dict:
    """Close evidence per explicit research question instead of URL counts."""
    rows = _verified_rows(verified)
    contracts = plan.get("research_question_contracts") or []
    question_rows: list[dict] = []
    evidence_by_question: dict[str, list[dict]] = {}
    unmapped: list[dict] = []
    for row in rows:
        claim = row.get("claim") or {}
        requirement_id = str(claim.get("requirement_id") or "")
        binding = match_question(plan, requirement_id)
        record = {
            "claim_id": str(claim.get("claim_id") or ""),
            "requirement_id": requirement_id,
            "verdict": str((row.get("verify") or {}).get("verdict") or "UNRESOLVED"),
            "claim_kind": str(claim.get("claim_kind") or ""),
            "url": str((row.get("fetchResult") or {}).get("url") or ""),
            "support_type": str((row.get("verify") or {}).get("support_type") or "direct_if_confirmed"),
        }
        if binding is None:
            unmapped.append(record)
            continue
        record.update({
            "question_id": binding["question_id"],
            "dimension": binding["dimension"],
            "source_role_requirements": binding["source_role_requirements"],
        })
        evidence_by_question.setdefault(str(binding["question_id"]), []).append(record)

    required_ids: list[str] = []
    for contract in contracts:
        if contract.get("criticality") == "required_for_model":
            required_ids.extend(str(value) for value in contract.get("required_question_ids") or [])
        for question in contract.get("subquestions") or []:
            question_id = str(question.get("question_id") or "")
            evidence = evidence_by_question.get(question_id, [])
            verdicts = {item["verdict"] for item in evidence}
            claim_kinds = {item["claim_kind"] for item in evidence}
            bound_requirements = {str(value) for value in question.get("requirement_ids") or []}
            confirmed_requirements = {
                item["requirement_id"] for item in evidence if item["verdict"] == "CONFIRMED"
            }
            if "CONTRADICTED" in verdicts:
                status = "contradicted"
            elif (bound_requirements and
                  bound_requirements <= confirmed_requirements):
                status = "confirmed"
            elif not bound_requirements and "CONFIRMED" in verdicts:
                status = "confirmed"
            elif "evidence_gap" in claim_kinds:
                status = "explicit_gap"
            elif "CONFIRMED" in verdicts or verdicts & {
                "INSUFFICIENT", "INSUFFICIENT_EVIDENCE", "NOT_FOUND", "NOT_CONFIRMED"
            }:
                status = "partially_supported" if evidence else "unresolved"
            else:
                status = "unresolved"
            question_rows.append({
                "question_id": question_id,
                "dimension": contract.get("dimension"),
                "criticality": contract.get("criticality"),
                "required_for_stage": question_id in set(contract.get("required_question_ids") or []),
                "question": question.get("question") or {},
                "status": status,
                "closure_rule": question.get("closure_rule") or "any_direct_confirmation",
                "bound_requirement_ids": sorted(bound_requirements),
                "confirmed_requirement_ids": sorted(confirmed_requirements),
                "missing_requirement_ids": sorted(bound_requirements - confirmed_requirements),
                "evidence": evidence,
                "source_role_requirements": contract.get("source_role_requirements") or [],
            })
    critical_status = {
        item["question_id"]: item["status"] for item in question_rows
        if item["question_id"] in set(required_ids)
    }
    closed = all(critical_status.get(question_id) == "confirmed" for question_id in required_ids)
    return {
        "protocol": "wiki-question-evidence-ledger-v1",
        "question_contract_sha256": plan.get("question_contract_sha256"),
        "questions": question_rows,
        "critical_question_ids": required_ids,
        "critical_question_status": critical_status,
        "critical_questions_closed": bool(required_ids) and closed,
        "unmapped_claims": unmapped,
        "metrics": {
            "questions_total": len(question_rows),
            "questions_confirmed": sum(item["status"] == "confirmed" for item in question_rows),
            "critical_questions_total": len(required_ids),
            "critical_questions_confirmed": sum(value == "confirmed" for value in critical_status.values()),
            "unmapped_claims": len(unmapped),
        },
    }


def diversity_gate(verified: dict, plan: dict, *, reviewed: bool,
                   attempt: int = 0, repair_budget: int = 2) -> dict:
    rows = _verified_rows(verified)
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
    ledger = question_evidence_ledger(verified, plan)
    question_status = {
        str(item.get("question_id") or ""): str(item.get("status") or "")
        for item in ledger["questions"]
    }
    role_checks = {
        "identity_source_role": question_status.get("identity.activity_definition") == "confirmed",
        "process_boundary_source_role": question_status.get("process.origin_boundary") == "confirmed",
        "adjacent_distinction_source_role": question_status.get("identity.adjacent_distinction") == "confirmed",
    }
    quality_checks = {
        "preview_primary_sources": len(urls) >= int(required.get("preview_primary_sources", 3)),
        "preview_distinct_domains": len(domains) >= int(required.get("preview_distinct_domains", 3)),
        "preview_technical_source": len(technical) >= int(required.get("preview_technical_sources", 1)),
        "preview_language_tracks": len(languages) >= int(required.get("preview_language_tracks", 2)),
        **role_checks,
    }
    candidate_ready = bool(confirmed) and all(quality_checks.values())
    contract_v2 = (
        plan.get("schema_version") == "wiki-research-plan-v2"
        and validate_question_contracts(plan)["valid"]
    )
    if contract_v2:
        hard_checks = {"critical_questions_closed": ledger["critical_questions_closed"]}
        warnings = [name for name, passed in quality_checks.items() if not passed]
        if ledger["critical_questions_closed"]:
            decision = "PASS_WITH_DEBT" if warnings else "PASS"
            pipeline_continue = True
            candidate_eligible = True
        elif attempt < repair_budget:
            decision = "RESEARCH_MORE"
            pipeline_continue = False
            candidate_eligible = False
        else:
            decision = "EVIDENCE_LIMITED"
            pipeline_continue = True
            candidate_eligible = False
        strategy_signal = [{
            "requirement_id": str((row.get("claim") or {}).get("requirement_id") or ""),
            "locator": str((row.get("claim") or {}).get("believed_locator") or ""),
            "source": str((row.get("claim") or {}).get("believed_source") or ""),
            "url": str((row.get("fetchResult") or {}).get("url") or ""),
        } for row in rows]
        strategy_hash = hashlib.sha256(json.dumps(
            strategy_signal, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        failed_ids = [
            question_id for question_id, status in
            ledger["critical_question_status"].items() if status != "confirmed"
        ]
        return {
            "protocol": "wiki-source-diversity-gate-v2",
            "gate_id": "question_evidence_sufficiency_gate",
            "gate_version": "question-evidence-governance-v2",
            "decision": decision,
            "pipeline_continue": pipeline_continue,
            "candidate_eligible": candidate_eligible,
            "checks": hard_checks,
            "failed_requirement_ids": failed_ids,
            "quality_assessment": {
                "protocol": "wiki-source-portfolio-quality-v1",
                "constraint_class": "quality_target",
                "default_effect": "warn_and_expand",
                "checks": quality_checks,
                "warnings": warnings,
            },
            "quality_checks": quality_checks,
            "warnings": warnings,
            "question_evidence_ledger": ledger,
            "repair_target": "research_ready" if decision == "RESEARCH_MORE" else None,
            "maturity_ceiling": (
                "wiki_candidate" if candidate_eligible else "evidence_limited"
                if decision == "EVIDENCE_LIMITED" else "diagnostic_preview"
            ),
            "attempt": attempt,
            "repair_budget": repair_budget,
            "metrics": {
                "confirmed_urls": len(urls),
                "confirmed_domains": len(domains),
                "technical_domains": len(technical),
                "confirmed_language_tracks": sorted(languages),
                **ledger["metrics"],
                "language_detection": [
                    {"claim_id": str((row.get("claim") or {}).get("claim_id") or ""),
                     "language": language, "method": method}
                    for row, (language, method) in zip(confirmed, detected)
                ],
            },
            "question_contract_sha256": plan.get("question_contract_sha256"),
            "strategy_hash": strategy_hash,
            "reviewed": reviewed,
            "materialization_branch": ({
                "kind": "explicit_gap_evidence_limited",
                "release_prohibited": True,
                "failed_question_ids": failed_ids,
                "gap_provenance_sha256": hashlib.sha256(json.dumps(
                    ledger, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ).encode()).hexdigest(),
            } if decision == "EVIDENCE_LIMITED" else None),
        }
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
        "gate_id": "legacy_source_diversity_gate",
        "gate_version": "source-diversity-v1",
        "decision": decision,
        "pipeline_continue": decision in {"PASS", "LIMITED"},
        "candidate_eligible": decision == "PASS",
        "checks": hard_checks,
        "quality_checks": quality_checks,
        "warnings": warnings,
        "failed_requirement_ids": [name for name, passed in hard_checks.items() if not passed],
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
    return 0 if result.get("pipeline_continue", result["decision"] in {
        "PASS", "PARTIAL", "LIMITED", "PASS_WITH_DEBT",
    }) else 2


if __name__ == "__main__":
    raise SystemExit(main())
