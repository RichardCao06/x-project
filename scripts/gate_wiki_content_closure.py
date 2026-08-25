#!/usr/bin/env python3
"""Gate Wiki content by semantic closure instead of a fixed body length."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any


KINDS = {"external_fact", "internal_graph_fact", "modeling_judgment", "evidence_gap"}


def _heading(section: dict[str, Any]) -> str:
    return str(section.get("heading") or section.get("section_id") or "")


def evaluate(blueprint: dict, content: dict, verified: dict, source_gate: dict) -> dict:
    rows = verified.get("claims") or (verified.get("result") or {}).get("claims") or []
    evidence_by_claim = {
        str((row.get("claim") or {}).get("claim_id") or ""): {
            "verdict": str((row.get("verify") or {}).get("verdict") or ""),
            "support_type": str((row.get("verify") or {}).get("support_type") or "direct_if_confirmed"),
            "scope_match": (row.get("verify") or {}).get("scope_match"),
            "requirement_id": str((row.get("claim") or {}).get("requirement_id") or ""),
        }
        for row in rows
    }
    sections = content.get("sections") or []
    expected = list((blueprint.get("sections") or {}).keys())
    actual = [_heading(section) for section in sections]
    closures: list[dict[str, Any]] = []
    invalid_kinds: list[str] = []
    unsupported_external: list[str] = []
    section_states: dict[str, set[str]] = {}
    for section in sections:
        heading = _heading(section)
        section_states.setdefault(heading, set())
        for paragraph_index, paragraph in enumerate(section.get("paragraphs") or [], 1):
            for sentence_index, sentence in enumerate(paragraph.get("sentences") or [], 1):
                kind = str(sentence.get("claim_kind") or "")
                claim_ids = [str(value) for value in sentence.get("evidence_claim_ids") or []]
                evidence = [evidence_by_claim.get(claim_id, {
                    "verdict": "MISSING", "support_type": "missing",
                    "scope_match": False, "requirement_id": "",
                }) for claim_id in claim_ids]
                verdicts = [item["verdict"] for item in evidence]
                if kind not in KINDS:
                    invalid_kinds.append(f"{heading}:{paragraph_index}:{sentence_index}")
                confirmed = any(
                    item["verdict"] == "CONFIRMED"
                    and item["scope_match"] is not False
                    and item["support_type"] in {
                        "direct", "direct_if_confirmed", "explicitly_composed", "composed",
                    }
                    for item in evidence
                )
                if kind == "external_fact" and (not claim_ids or not confirmed):
                    unsupported_external.append(f"{heading}:{paragraph_index}:{sentence_index}")
                if kind in KINDS:
                    section_states[heading].add(kind)
                closures.append({
                    "section": heading, "paragraph_index": paragraph_index,
                    "sentence_index": sentence_index, "state": kind,
                    "evidence_claim_ids": claim_ids, "evidence_verdicts": verdicts,
                    "evidence_support_types": [item["support_type"] for item in evidence],
                    "evidence_scope_matches": [item["scope_match"] for item in evidence],
                    "closed": kind in KINDS and (kind != "external_fact" or confirmed),
                })
    core_sections = expected[:5]
    core_closed = {
        heading: bool(section_states.get(heading, set()) & {"external_fact", "evidence_gap"})
        for heading in core_sections
    }
    checks = {
        "section_contract_exact": bool(expected) and actual == expected,
        "sections_have_paragraphs": bool(sections) and all(section.get("paragraphs") for section in sections),
        "all_sentences_classified": bool(closures) and not invalid_kinds,
        "external_facts_confirmed": not unsupported_external,
        "core_sections_fact_or_explicit_gap": bool(core_closed) and all(core_closed.values()),
    }
    structural = all(checks[name] for name in (
        "section_contract_exact", "sections_have_paragraphs", "all_sentences_classified",
        "external_facts_confirmed",
    ))
    source_decision = str(source_gate.get("decision") or "")
    source_limited = source_decision in {"LIMITED", "EVIDENCE_LIMITED"}
    source_candidate = (
        source_gate.get("candidate_eligible") is True
        or source_decision in {"PASS", "PASS_WITH_DEBT"}
    )
    candidate_eligible = structural and checks["core_sections_fact_or_explicit_gap"] \
        and source_candidate
    decision = (
        "PASS_WITH_DEBT" if candidate_eligible and source_decision == "PASS_WITH_DEBT"
        else "PASS" if candidate_eligible
        else "LIMITED" if source_limited and structural
        else "REPAIR"
    )
    return {
        "protocol": "wiki-content-closure-gate-v1",
        "decision": decision,
        "pipeline_continue": decision in {"PASS", "PASS_WITH_DEBT", "LIMITED"},
        "candidate_eligible": candidate_eligible,
        "maturity_ceiling": "wiki_candidate" if candidate_eligible else "evidence_limited",
        "repair_target": "content_compose" if decision == "REPAIR" else None,
        "failed_requirement_ids": source_gate.get("failed_requirement_ids") or [],
        "question_contract_sha256": source_gate.get("question_contract_sha256"),
        "question_evidence_ledger": source_gate.get("question_evidence_ledger") or {},
        "checks": checks,
        "metrics": {
            "sections": len(sections), "sentences": len(closures),
            "claim_kinds": dict(Counter(row["state"] for row in closures)),
            "unsupported_external_facts": len(unsupported_external),
            "core_sections": core_closed,
        },
        "closures": closures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("blueprint", type=Path)
    parser.add_argument("content", type=Path)
    parser.add_argument("verified", type=Path)
    parser.add_argument("source_gate", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    paths = [args.blueprint, args.content, args.verified, args.source_gate]
    values = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    report = evaluate(*values)
    report["inputs_sha256"] = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    return 0 if report["pipeline_continue"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
