#!/usr/bin/env python3
"""Freeze no-Web Verify runtime decisions against deterministic evidence.

This is the local production bridge between ``run_wiki_verify_capture.py``
and ``ku_distill.py``.  It never upgrades a decision: missing, duplicate,
misaligned, wrong-evidence or non-verbatim decisions become INSUFFICIENT.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def freeze(evidence: dict, verdicts: dict) -> dict:
    decisions = verdicts.get("items")
    if not isinstance(decisions, list):
        raise ValueError("Verify verdicts 缺少 items 数组")
    by_id: dict[str, dict] = {}
    duplicates: set[str] = set()
    for item in decisions:
        claim_id = str((item or {}).get("claim_id", ""))
        if claim_id in by_id:
            duplicates.add(claim_id)
        by_id[claim_id] = item

    rows = []
    external_ids = {
        str(row["claim"]["claim_id"])
        for row in evidence.get("claims", []) if row.get("candidates")
    }
    if set(by_id) != external_ids or duplicates:
        raise ValueError(
            f"Verify scope 漂移: missing={sorted(external_ids-set(by_id))} "
            f"extra={sorted(set(by_id)-external_ids)} duplicate={sorted(duplicates)}"
        )

    for row in evidence.get("claims", []):
        claim = row["claim"]
        candidates = row.get("candidates") or []
        if not candidates:
            rows.append({
                "claim": claim,
                "fetchResult": {"status": "not_found"},
                "verify": {
                    "verdict": "NOT_FOUND", "node_alignment": "EXACT",
                    "supporting_quote": "", "reasoning": "受控内部 claim，不进入外部核验。",
                },
                "verification_protocol": {
                    "independent": True,
                    "search_label": f"search:deterministic:{claim['node_id']}",
                    "verify_label": "",
                    "verify_skipped_reason": "internal_modeling_judgment",
                },
            })
            continue
        decision = by_id[str(claim["claim_id"])]
        candidates_by_id = {str(item["evidence_id"]): item for item in candidates}
        candidate = candidates_by_id.get(str(decision.get("evidence_id", "")))
        verdict = str(decision.get("verdict", "INSUFFICIENT"))
        alignment = str(decision.get("node_alignment", "UNRELATED"))
        quote = str(decision.get("supporting_quote", ""))
        valid = (
            candidate is not None
            and alignment in {"EXACT", "ADJACENT", "UNRELATED"}
            and verdict in {"CONFIRMED", "CONTRADICTED", "INSUFFICIENT"}
            and (not quote or quote in str(candidate.get("excerpt", "")))
            and (verdict not in {"CONFIRMED", "CONTRADICTED"} or bool(quote))
            and (verdict != "CONFIRMED" or alignment == "EXACT")
        )
        if not valid:
            verdict, alignment, quote = "INSUFFICIENT", "UNRELATED", ""
        rows.append({
            "claim": claim,
            "fetchResult": (
                {**candidate, "status": "found"} if candidate
                else {"status": "not_found"}
            ),
            "verify": {
                "verdict": verdict, "node_alignment": alignment,
                "supporting_quote": quote,
                "reasoning": str(decision.get("reasoning", "")),
            },
            "verification_protocol": {
                "independent": True,
                "search_label": f"search:deterministic:{claim['node_id']}",
                "verify_label": f"verify:{claim['node_id']}",
                "web_search_allowed": False,
                "evidence_protocol": (evidence.get("protocol") or {}).get("version"),
            },
        })
    return {
        "protocol": {"version": "wiki-ku-v1", "mode": "extract"},
        "claims": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("verdicts", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = freeze(
        json.loads(args.evidence.read_text(encoding="utf-8")),
        json.loads(args.verdicts.read_text(encoding="utf-8")),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "claims": len(result["claims"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
