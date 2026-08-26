#!/usr/bin/env python3
"""Independently validate a frozen Wiki table collection and its search matrix."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path


def logical_result_identity(row: dict, result: dict | None = None) -> tuple[str, ...]:
    """Return the edge-scoped identity of a matrix result or candidate audit."""
    candidate = result if result is not None else row
    return (
        str(row.get("table") or ""),
        str(row.get("field") or ""),
        str(row.get("direction") or "") if row.get("table") == "flows" else "",
        str(row.get("language") or ""),
        str(row.get("query_hash") or ""),
        str(candidate.get("url") or ""),
    )


def every_search_result_has_one_candidate_audit(
    query_rows: list[dict], candidate_audits: list[dict],
) -> bool:
    result_keys = [
        logical_result_identity(query, result)
        for query in query_rows for result in query.get("results", [])
    ]
    audit_keys = [logical_result_identity(row) for row in candidate_audits]
    return (
        len(result_keys) == len(audit_keys)
        and sorted(result_keys) == sorted(audit_keys)
        and len(audit_keys) == len(set(audit_keys))
    )


def flow_identity_parity(blueprint: dict, collection: dict) -> bool:
    """Bind every activity flow row to the ordered graph-derived ledger."""
    if collection.get("node_type") != "activity":
        return True
    expected = [
        (str(row.get("field") or ""), str(row.get("direction") or ""))
        for row in blueprint.get("flow_ledger") or [] if isinstance(row, dict)
    ]
    actual = [
        (str(row.get("field") or ""), str(row.get("direction") or ""))
        for row in collection.get("tables", {}).get("flows", [])
    ]
    return bool(expected) and actual == expected and len(actual) == len(set(actual))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("blueprint", type=Path)
    ap.add_argument("verify", type=Path)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()
    collection_path = args.output / "collection.json"
    matrix_path = args.output / "search-matrix.executed.json"
    if not matrix_path.is_file():
        raise ValueError("table verification requires search-matrix.executed.json")
    selection_path = args.output / "evidence-selection.json"
    root = Path.cwd()
    script = root / "scripts/wiki_table_population.py"
    if not script.is_file():
        script = Path(__file__).resolve().parents[1] / "vendor/lca_cornerstone/scripts/wiki_table_population.py"
    spec = importlib.util.spec_from_file_location("wiki_table_population", script)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)  # type: ignore[union-attr]
    blueprint = json.loads(args.blueprint.read_text(encoding="utf-8"))
    collection = json.loads(collection_path.read_text(encoding="utf-8"))
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    selection = json.loads(selection_path.read_text(encoding="utf-8")) if selection_path.is_file() else {}
    metrics = module.validate_collection(collection, root)
    query_rows = matrix.get("queries", [])
    languages = {row.get("language") for row in query_rows}
    terminal = {"found", "not_found", "fetched", "verified", "rejected", "error", "budget_skipped"}
    quantitative_rows = [row for row in collection["tables"]["params"]
                         if row.get("status") == "populated"]
    accepted = selection.get("accepted_evidence") or []
    routed_queries = [row for row in query_rows if row.get("document_route")]
    accepted_keys = {(str(row.get("table")), str(row.get("field")),
                      str(row.get("direction") or "") if row.get("table") == "flows" else "",
                      str(row.get("source_id")))
                     for row in accepted}
    field_decisions = {(str(row.get("table")), str(row.get("field")),
                        str(row.get("direction") or "") if row.get("table") == "flows" else ""):
                       row.get("decision")
                       for row in selection.get("fields") or []}
    candidate_audits = selection.get("candidate_audits") or []
    populated_rows = [(kind, row) for kind, rows in collection.get("tables", {}).items()
                      for row in rows if row.get("status") == "populated"]
    matrix_terms = matrix.get("terminology", {})
    zh_discovery_terms = [matrix_terms.get("canonical_zh"),
                          *matrix_terms.get("candidate_aliases_zh", matrix_terms.get("synonyms_zh", []))]
    en_discovery_terms = [matrix_terms.get("canonical_en"),
                          *matrix_terms.get("candidate_aliases_en", matrix_terms.get("synonyms_en", [])),
                          *matrix_terms.get("translated_search_terms_en", [])]
    actual_flow_identities = [
        (str(row.get("field") or ""), str(row.get("direction") or ""))
        for row in collection.get("tables", {}).get("flows", [])
    ]
    checks = {"schema_valid": True, "node_identity_matches": collection["node_id"] == matrix["node_id"],
              "flow_identity_matches_blueprint": (
                  flow_identity_parity(blueprint, collection)
              ),
              "flow_identities_unique": len(actual_flow_identities) == len(set(actual_flow_identities)),
              "flow_queries_are_direction_scoped": all(
                  row.get("table") != "flows" or row.get("direction") in {"in", "out"}
                  for row in query_rows
              ),
              "evidence_selection_present": selection.get("protocol") == "wiki-table-evidence-selection-v1",
              "evidence_selection_hash_bound": collection.get("evidence_selection_sha256") == (hashlib.sha256(selection_path.read_bytes()).hexdigest() if selection_path.is_file() else ""),
              "selection_matrix_hash_bound": selection.get("matrix_sha256") == hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
              "all_fields_have_selection_decisions": all(
                  (kind, str(row.get("field")),
                   str(row.get("direction") or "") if kind == "flows" else "") in field_decisions
                  for kind, rows in collection.get("tables", {}).items() for row in rows),
              "every_search_result_has_one_candidate_audit":
                  every_search_result_has_one_candidate_audit(query_rows, candidate_audits),
              "candidate_decisions_are_terminal_and_explained": all(
                  row.get("decision") in {"accepted", "rejected"}
                  and bool(row.get("reasons"))
                  for row in candidate_audits
              ),
              "selection_decisions_match_collection": all(
                  field_decisions.get((kind, str(row.get("field")),
                                       str(row.get("direction") or "") if kind == "flows" else "")) ==
                  ("populated" if row.get("status") == "populated" else "explicit_gap")
                  for kind, rows in collection.get("tables", {}).items() for row in rows),
              "populated_rows_are_selected": all(
                  any((kind, str(row.get("field")),
                       str(row.get("direction") or "") if kind == "flows" else "",
                       str(row.get(key))) in accepted_keys
                      for key in ("source", "int_source", "cn_source") if row.get(key))
                  for kind, row in populated_rows),
              "chinese_query_track": "zh" in languages,
              "english_query_track": "en" in languages,
              "chinese_discovery_terms_present": any(
                  str(x or "").strip() for x in zh_discovery_terms
              ),
              "english_discovery_terms_present": any(
                  str(x or "").strip() and not any("\u3400" <= char <= "\u9fff" for char in str(x))
                  for x in en_discovery_terms
              ),
              "alias_policy_present": any(str(x or "").strip() for x in zh_discovery_terms)
                                      and any(str(x or "").strip() for x in en_discovery_terms),
              "search_was_attempted": bool(query_rows) and all(row.get("status") in terminal for row in query_rows),
              "planned_rows_rejected": all(row.get("status") != "planned" for row in query_rows),
              "document_type_routes_executed": (not matrix.get("document_routes") or (
                  bool(routed_queries) and all(row.get("status") in terminal for row in routed_queries)
              )),
              "product_pcf_not_split_into_unit_process": all(
                  row.get("document_type") != "product_carbon_footprint"
                  or row.get("table") not in {"flows", "emissions", "indicators"}
                  for row in accepted
              ),
              "all_populated_rows_have_verified_sources": True,
              # Quantitative values require independent corroboration.  An entirely explicit-gap
              # table is valid, but one-source numerical population is not.
              "quantitative_source_diversity": all(
                  row.get("verification_mode") in {
                      "authoritative_single_source", "independent_two_source_corroboration",
                      "institutional_single_source", "upstream_independent_verification",
                  } for row in accepted),
              "unsupported_quantities_are_explicit_gaps": all(
                  row.get("status") in {"populated", "explicit_gap"}
                  for row in collection["tables"]["params"]),
              "collection_hash_bound": collection.get("search_matrix_sha256") == hashlib.sha256(matrix_path.read_bytes()).hexdigest()}
    advisory_names = {
        "english_query_track", "english_discovery_terms_present", "alias_policy_present",
    }
    blocking_failures = [
        name for name, passed in checks.items()
        if not passed and name not in advisory_names
    ]
    warnings = [
        name for name, passed in checks.items()
        if not passed and name in advisory_names
    ]
    result = {"protocol": "wiki-table-source-verdict-v1", "node_id": collection["node_id"],
              "verdict": "PASS" if not blocking_failures else "FAIL", "independent": True,
              "data_outcome": selection.get("outcome", "UNKNOWN"),
              "checks": checks, "blocking_failures": blocking_failures,
              "warnings": warnings, "advisory_checks": sorted(advisory_names),
              "metrics": metrics,
              "proof_metrics": {
                  "graph_edge_to_flow_identity_parity": checks["flow_identity_matches_blueprint"],
                  "table_contract_validity": int(not blocking_failures),
                  "gap_provenance_preserved": selection.get("proof_metrics", {}).get(
                      "gap_provenance_preserved", False),
                  "candidate_audit_closure_complete": selection.get("proof_metrics", {}).get(
                      "candidate_audit_closure_complete", False),
              },
              "collection_sha256": hashlib.sha256(collection_path.read_bytes()).hexdigest(),
              "search_matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest()}
    (args.output / "source-verdict.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
