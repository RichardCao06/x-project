#!/usr/bin/env python3
"""Validate that a frozen Wiki research plan can start evidence discovery.

English terminology and field translations are discovery-quality signals.  They
must remain visible, but an incomplete optional English track cannot prevent the
Chinese track from executing.  Actual bilingual coverage is proven by the
executed-search and source-diversity gates later in the workflow.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


REQUIRED_QUESTIONS = {
    "identity_and_terminology", "process_origin_and_boundary", "collection_and_handoff",
    "composition_and_quantity", "recovery_and_destination", "representativeness_and_quality",
}
REQUIRED_SOURCE_ROLES = {
    "identity", "process_boundary", "adjacent_distinction",
    "regional_representativeness", "quantitative",
}
CJK = re.compile(r"[\u3400-\u9fff]")
INTERNAL_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9])[AP]\d{3}(?!\d)", re.I)


def evaluate(plan: dict) -> dict:
    terminology = plan.get("terminology") or {}
    english_terms = [
        terminology.get("canonical_en"),
        *terminology.get("candidate_aliases_en", []),
        *terminology.get("translated_search_terms_en", []),
    ]
    english_terms = [str(value).strip() for value in english_terms if str(value or "").strip()]
    translation = terminology.get("query_translation") or {}
    translated = [str(value) for value in translation.get("translated_terms", [])]
    translation_audited = bool(
        terminology.get("canonical_en")
        or (translated and translation.get("authority") == "discovery_only"
            and translation.get("method") != "bilingual_passthrough_no_glossary_match"
            and not translation.get("unmatched_fragments"))
    )
    field_contract = plan.get("field_translation_contract") or {}
    field_translations = plan.get("field_translations") or {}
    required_fields = {
        str(field)
        for fields in (field_contract.get("required_fields_by_table") or {}).values()
        for field in fields
    }
    translated_fields = {str(field) for field in field_translations}
    complete_field_contract = bool(
        field_contract
        and field_contract.get("scope") == "node_and_table_schema"
        and str(field_contract.get("node_id")) == str(plan.get("node_id"))
        and field_contract.get("complete_english_coverage_required") is True
        and int(field_contract.get("required_field_count") or -1) == len(required_fields)
        and required_fields == translated_fields
        and required_fields
        and all(
            str(field_translations.get(field) or "").strip()
            and not CJK.search(str(field_translations[field]))
            and not INTERNAL_IDENTIFIER.search(str(field_translations[field]))
            for field in required_fields
        )
    )
    checks = {
        "canonical_chinese_present": bool(str(terminology.get("canonical_zh") or "").strip()),
        "english_discovery_terms_present": bool(english_terms),
        "english_translation_audited": translation_audited,
        "english_terms_are_actually_english": bool(english_terms) and all(
            not CJK.search(value) for value in english_terms
        ),
        "bilingual_tracks_declared": {"zh", "en"} <= set(plan.get("languages") or []),
        "research_questions_complete": REQUIRED_QUESTIONS <= set(plan.get("research_questions") or []),
        "source_role_contract_complete": REQUIRED_SOURCE_ROLES <= set(
            (plan.get("source_role_contract") or {}).keys()
        ),
    }
    # Static field translations make English queries more precise, but unknown
    # activity schemas are expected to expand this track at table-collection
    # time.  Keep the coverage signal without turning it into a G1 false block.
    if str(plan.get("node_id") or "").startswith("A") or field_contract:
        checks["english_field_translation_coverage_complete"] = complete_field_contract
    advisory_names = {
        "english_discovery_terms_present",
        "english_translation_audited",
        "english_terms_are_actually_english",
        "english_field_translation_coverage_complete",
    }
    failures = [
        name for name, passed in checks.items()
        if not passed and name not in advisory_names
    ]
    warnings = [
        name for name, passed in checks.items()
        if not passed and name in advisory_names
    ]
    return {
        "protocol": "wiki-research-plan-gate-v1",
        "decision": "PASS" if not failures else "REPAIR",
        "pipeline_continue": not failures,
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "advisory_checks": sorted(advisory_names & set(checks)),
        "translation_policy": (
            "execute_available_queries_and_expand_english_terms_from_runtime_results"
        ),
        "repair_target": "research_plan" if failures else None,
        "maturity_ceiling": (
            "evidence_limited" if not failures and warnings
            else "wiki_candidate" if not failures
            else "diagnostic_preview"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    report = evaluate(plan)
    report["plan_sha256"] = hashlib.sha256(args.plan.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    return 0 if report["decision"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
