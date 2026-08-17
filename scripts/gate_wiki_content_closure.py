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
    verdict_by_claim = {
        str((row.get("claim") or {}).get("claim_id") or ""):
        str((row.get("verify") or {}).get("verdict") or "")
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
                verdicts = [verdict_by_claim.get(claim_id, "MISSING") for claim_id in claim_ids]
                if kind not in KINDS:
                    invalid_kinds.append(f"{heading}:{paragraph_index}:{sentence_index}")
                confirmed = any(value == "CONFIRMED" for value in verdicts)
                if kind == "external_fact" and (not claim_ids or not confirmed):
                    unsupported_external.append(f"{heading}:{paragraph_index}:{sentence_index}")
                if kind in KINDS:
                    section_states[heading].add(kind)
                closures.append({
                    "section": heading, "paragraph_index": paragraph_index,
                    "sentence_index": sentence_index, "state": kind,
                    "evidence_claim_ids": claim_ids, "evidence_verdicts": verdicts,
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
    source_limited = source_gate.get("decision") == "LIMITED"
    candidate_eligible = structural and checks["core_sections_fact_or_explicit_gap"] \
        and source_gate.get("decision") == "PASS"
    decision = ("PASS" if candidate_eligible else "LIMITED" if source_limited and structural
                else "REPAIR")
    return {
        "protocol": "wiki-content-closure-gate-v1",
        "decision": decision,
        "pipeline_continue": decision in {"PASS", "LIMITED"},
        "candidate_eligible": candidate_eligible,
        "maturity_ceiling": "wiki_candidate" if candidate_eligible else "evidence_limited",
        "repair_target": "content_compose" if decision == "REPAIR" else None,
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
