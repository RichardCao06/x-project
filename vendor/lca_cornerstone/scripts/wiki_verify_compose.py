#!/usr/bin/env python3
"""Compose strict wiki-ku-v1 results from frozen evidence and Verify verdicts.

The Verify model returns decisions only.  This deterministic boundary restores
the exact frozen claims/fetch records, validates evidence IDs and verbatim
quotes, and creates NOT_FOUND rows without invoking a model when no candidate
bytes exist.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from validate_wiki_workflow import validate_result
from wiki_source_discovery import read_json, validate_evidence, write_json


VERDICTS = {"CONFIRMED", "CONTRADICTED", "INSUFFICIENT"}


def compose(evidence: dict[str, Any], verdicts: dict[str, Any]) -> dict[str, Any]:
    validate_evidence(evidence, require_payload=True, require_source_chain=True)
    rows = verdicts.get("items")
    if not isinstance(rows, list):
        raise ValueError("verify verdicts.items 必须是数组")
    expected = {
        str(entry["claim"]["claim_id"]): entry
        for entry in evidence["claims"]
        if entry.get("candidates")
    }
    decisions: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"verify verdicts.items[{index}] 非法")
        claim_id = str(row.get("claim_id", ""))
        if not claim_id or claim_id in decisions or claim_id not in expected:
            raise ValueError(f"verify claim_id 缺失、重复或越界: {claim_id!r}")
        verdict = row.get("verdict")
        if verdict not in VERDICTS:
            raise ValueError(f"{claim_id} verdict 非法")
        alignment = row.get("node_alignment")
        if alignment not in {"EXACT", "ADJACENT", "UNRELATED"}:
            raise ValueError(f"{claim_id} node_alignment 非法")
        if verdict == "CONFIRMED" and alignment != "EXACT":
            raise ValueError(f"{claim_id} 非 EXACT 节点证据不得 CONFIRMED")
        candidates = expected[claim_id]["candidates"]
        selected = [x for x in candidates if x.get("evidence_id") == row.get("evidence_id")]
        if len(selected) != 1:
            raise ValueError(f"{claim_id} evidence_id 不解析到唯一冻结候选")
        quote = str(row.get("supporting_quote", ""))
        reasoning = str(row.get("reasoning", "")).strip()
        if not reasoning:
            raise ValueError(f"{claim_id} 缺 Verify reasoning")
        if verdict in {"CONFIRMED", "CONTRADICTED"} and (
            not quote or quote not in str(selected[0].get("excerpt", ""))
        ):
            raise ValueError(f"{claim_id} decisive verdict 缺冻结原文逐字 quote")
        decisions[claim_id] = {**row, "_candidate": selected[0]}
    if set(decisions) != set(expected):
        raise ValueError(
            "Verify verdict 双向覆盖漂移: "
            f"missing={sorted(set(expected)-set(decisions))} "
            f"extra={sorted(set(decisions)-set(expected))}"
        )

    output: list[dict[str, Any]] = []
    for entry in evidence["claims"]:
        claim = entry["claim"]
        claim_id = str(claim["claim_id"])
        candidates = entry.get("candidates") or []
        if not candidates:
            internal = entry.get("disposition") == "internal_modeling_judgment"
            output.append({
                "claim": claim,
                "fetchResult": {"status": "not_found"},
                "verify": {
                    "verdict": "NOT_FOUND",
                    "node_alignment": "EXACT",
                    "supporting_quote": "",
                    "reasoning": (
                        "该项是 INTERNAL_MODELING_JUDGMENT，不属于外部检索断言，确定性安全降级为 draft"
                        if internal else "确定性 Search/Fetch evidence 中无可核验外部原文"
                    ),
                },
                "verification_protocol": {
                    "independent": True,
                    "search_label": f"search:deterministic:{claim['node_id']}",
                    "verify_label": "",
                    "verify_skipped_reason": (
                        "internal_modeling_judgment" if internal else "no_retrievable_external_source"
                    ),
                },
            })
            continue
        decision = decisions[claim_id]
        candidate = decision.pop("_candidate")
        output.append({
            "claim": claim,
            "fetchResult": {
                "status": "found", "url": candidate["url"],
                "excerpt": candidate["excerpt"],
                "content_sha256": candidate["content_sha256"],
                "evidence_id": candidate["evidence_id"],
            },
            "verify": {
                "verdict": decision["verdict"],
                "node_alignment": decision["node_alignment"],
                "supporting_quote": str(decision.get("supporting_quote", "")),
                "reasoning": str(decision["reasoning"]),
            },
            "verification_protocol": {
                "independent": True,
                "search_label": f"search:deterministic:{claim['node_id']}",
                "verify_label": f"verify:{claim['node_id']}",
                "web_search_allowed": False,
                "evidence_protocol": evidence["protocol"]["version"],
            },
        })
    return {
        "protocol": {"version": "wiki-ku-v1", "mode": evidence["protocol"]["mode"]},
        "claims": output,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("verdicts", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = compose(read_json(args.evidence), read_json(args.verdicts))
        write_json(args.output, result)
        report = validate_result(args.output)
        print(json.dumps({"output": str(args.output.resolve()), **report}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
