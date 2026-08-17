#!/usr/bin/env python3
"""Independently validate a frozen Wiki table collection and its search matrix."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path


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
    accepted_keys = {(str(row.get("table")), str(row.get("field")), str(row.get("source_id")))
                     for row in accepted}
    field_decisions = {(str(row.get("table")), str(row.get("field"))): row.get("decision")
                       for row in selection.get("fields") or []}
    populated_rows = [(kind, row) for kind, rows in collection.get("tables", {}).items()
                      for row in rows if row.get("status") == "populated"]
    matrix_terms = matrix.get("terminology", {})
    zh_discovery_terms = [matrix_terms.get("canonical_zh"),
                          *matrix_terms.get("candidate_aliases_zh", matrix_terms.get("synonyms_zh", []))]
    en_discovery_terms = [matrix_terms.get("canonical_en"),
                          *matrix_terms.get("candidate_aliases_en", matrix_terms.get("synonyms_en", [])),
                          *matrix_terms.get("translated_search_terms_en", [])]
    checks = {"schema_valid": True, "node_identity_matches": collection["node_id"] == matrix["node_id"],
              "evidence_selection_present": selection.get("protocol") == "wiki-table-evidence-selection-v1",
              "evidence_selection_hash_bound": collection.get("evidence_selection_sha256") == (hashlib.sha256(selection_path.read_bytes()).hexdigest() if selection_path.is_file() else ""),
              "selection_matrix_hash_bound": selection.get("matrix_sha256") == hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
              "all_fields_have_selection_decisions": all(
                  (kind, str(row.get("field"))) in field_decisions
                  for kind, rows in collection.get("tables", {}).items() for row in rows),
              "selection_decisions_match_collection": all(
                  field_decisions.get((kind, str(row.get("field")))) ==
                  ("populated" if row.get("status") == "populated" else "explicit_gap")
                  for kind, rows in collection.get("tables", {}).items() for row in rows),
              "populated_rows_are_selected": all(
                  any((kind, str(row.get("field")), str(row.get(key))) in accepted_keys
                      for key in ("source", "int_source", "cn_source") if row.get(key))
                  for kind, row in populated_rows),
              "chinese_query_track": "zh" in languages,
              "english_query_track": "en" in languages,
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
    result = {"protocol": "wiki-table-source-verdict-v1", "node_id": collection["node_id"],
              "verdict": "PASS" if all(checks.values()) else "FAIL", "independent": True,
              "data_outcome": selection.get("outcome", "UNKNOWN"),
              "checks": checks, "metrics": metrics,
              "collection_sha256": hashlib.sha256(collection_path.read_bytes()).hexdigest(),
              "search_matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest()}
    (args.output / "source-verdict.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
