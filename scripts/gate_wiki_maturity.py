#!/usr/bin/env python3
"""Aggregate immutable Wiki quality reports into an honest maturity ceiling."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate(batch: Path) -> dict[str, Any]:
    paths = {
        "research_plan_gate": batch / "research-plan-gate.json",
        "source_diversity_gate": batch / "source-diversity-gate.json",
        "content_blueprint": batch / "content-blueprint.json",
        "content_closure_gate": batch / "content-closure-gate.json",
        "editorial_policy": batch / "editorial-loop/editorial-policy-decision.json",
        "draft_content_gate": batch / "draft-content-gate.json",
        "table_verdict": batch / "table-data/source-verdict.json",
        "table_selection": batch / "table-data/evidence-selection.json",
        "table_collection": batch / "table-data/collection.json",
        "table_population": batch / "table-data/table-population-gate.json",
        "verified": batch / "verify-output.json",
    }
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        raise ValueError(f"maturity gate missing artifacts: {missing}")
    docs = {name: load(path) for name, path in paths.items()}
    verified_rows = docs["verified"].get("claims") or []
    confirmed_requirements = {
        str((row.get("claim") or {}).get("requirement_id") or "").lower()
        for row in verified_rows if (row.get("verify") or {}).get("verdict") == "CONFIRMED"
    }
    conflicts = docs["content_blueprint"].get("semantic_conflicts") or []
    mapping_confirmed = any(
        any(token in requirement for token in ("bom", "configuration_mapping", "flow_identity"))
        for requirement in confirmed_requirements
    )
    unresolved_conflicts = [] if mapping_confirmed else conflicts
    selection = docs["table_selection"]
    collection = docs["table_collection"]
    population = docs["table_population"]
    gap_fields = [row for row in selection.get("fields") or []
                  if row.get("decision") == "explicit_gap"]
    gap_cells: list[dict[str, Any]] = []
    for rows in (collection.get("tables") or {}).values():
        for row in rows:
            by_track = row.get("gap_evidence_by_track") or {}
            for source_key, value_key, track in (
                ("source", "value", "value"),
                ("int_source", "int_value", "int"),
                ("cn_source", "cn_value", "cn"),
            ):
                value = str(row.get(value_key) or "")
                if source_key in row and value.startswith(("缺口", "缺失", "未获取", "未公开")):
                    gap_cells.append(by_track.get(track) or row.get("gap_evidence") or {})
    reported_gaps = [
        gap
        for field in selection.get("fields") or []
        for gap in ([field.get("gap_evidence") or {}]
                    + list((field.get("gap_tracks") or {}).values()))
        if field.get("decision") == "explicit_gap" or field.get("gap_tracks")
    ]
    gap_records = gap_cells + reported_gaps
    gap_provenance_ok = all(
        gap.get("protocol") == "wiki-table-gap-evidence-v1"
        and bool(gap.get("reason")) and bool(gap.get("matrix_sha256"))
        and bool(gap.get("query_hashes"))
        for gap in gap_records
    )
    accepted = selection.get("accepted_evidence") or []
    populated_fields = int(
        ((population.get("goal_readiness") or {}).get("populated_fields") or 0)
    )
    if populated_fields == 0:
        populated_fields = sum(
            1 for rows in (collection.get("tables") or {}).values()
            for row in rows if row.get("status") == "populated"
        )
    checks = {
        "research_plan_candidate_ready": docs["research_plan_gate"].get("decision") == "PASS",
        "source_roles_candidate_ready": (
            docs["source_diversity_gate"].get("candidate_eligible") is True
            or docs["source_diversity_gate"].get("decision") in {"PASS", "PASS_WITH_DEBT"}
        ),
        "graph_semantic_conflicts_resolved": not unresolved_conflicts,
        "content_semantically_closed": docs["content_closure_gate"].get("candidate_eligible") is True,
        "draft_candidate_ready": docs["draft_content_gate"].get("candidate_eligible") is True,
        "editorial_candidate_ready": docs["editorial_policy"].get("decision") in {
            "accept", "accept_with_advisories",
        },
        "table_contract_valid": docs["table_verdict"].get("verdict") == "PASS",
        "table_population_contract_valid": population.get("verdict") in {"GO", "INCOMPLETE"},
        "accepted_field_evidence_nonzero": bool(accepted),
        "populated_model_fields_nonzero": populated_fields > 0,
        "explicit_gaps_have_search_provenance": gap_provenance_ok,
    }
    candidate_eligible = all(checks.values())
    reasons = [name for name, passed in checks.items() if not passed]
    if candidate_eligible:
        maturity = "wiki_candidate"
    elif unresolved_conflicts or not checks["editorial_candidate_ready"]:
        maturity = "diagnostic_preview"
    else:
        maturity = "evidence_limited"
    data_readiness = ("data_ready" if accepted
                      else "no_eligible_public_data" if selection.get("outcome") == "NO_ELIGIBLE_PUBLIC_DATA"
                      else "partial_data")
    recoverable_reason_codes = {
        "payload_not_fetched", "public_extraction_rule_missing",
        "fetch_failed", "extraction_failed", "document_route_missing",
        "FIELD_EXTRACTION_ZERO_YIELD", "MISSING_DOCUMENT_ROUTES",
    }
    observed_reason_codes = {
        str(reason)
        for audit in selection.get("candidate_audits") or []
        for reason in (audit.get("reason_codes") or audit.get("reasons") or [])
    } | {
        str(reason) for reason, count in (selection.get("reason_counts") or {}).items()
        if int(count or 0) > 0
    }
    non_research_repairs_remain = any(not checks[name] for name in (
        "graph_semantic_conflicts_resolved", "content_semantically_closed",
        "draft_candidate_ready", "editorial_candidate_ready",
    ))
    source_repair_remains = (
        docs["source_diversity_gate"].get("decision") not in {"PASS", "PASS_WITH_DEBT"}
        and docs["source_diversity_gate"].get("pipeline_continue") is not False
    )
    pipeline_continue = bool(
        not candidate_eligible and (
            non_research_repairs_remain or source_repair_remains
            or bool(observed_reason_codes & recoverable_reason_codes)
        )
    )
    return {
        "protocol": "wiki-maturity-gate-v1",
        "decision": "PASS" if candidate_eligible else "LIMITED",
        "pipeline_continue": pipeline_continue,
        "candidate_eligible": candidate_eligible,
        "maturity": maturity,
        "data_readiness": data_readiness,
        "checks": checks,
        "reason_codes": reasons,
        "quality_debt": {
            "source_warnings": (
                (docs["source_diversity_gate"].get("quality_assessment") or {}).get("warnings")
                or docs["source_diversity_gate"].get("warnings") or []
            ),
            "question_evidence_metrics": (
                (docs["source_diversity_gate"].get("question_evidence_ledger") or {}).get("metrics")
                or {}
            ),
            "draft_warnings": [warning for page in docs["draft_content_gate"].get("pages") or []
                               for warning in page.get("quality_warnings") or []],
            "unresolved_graph_conflicts": unresolved_conflicts,
            "explicit_gap_fields": len(gap_fields),
            "explicit_gap_cells": len(gap_cells),
            "accepted_field_evidence": len(accepted),
            "populated_model_fields": populated_fields,
            "recoverable_reason_codes": sorted(
                observed_reason_codes & recoverable_reason_codes
            ),
        },
        "inputs_sha256": {
            name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.batch.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
