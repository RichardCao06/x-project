#!/usr/bin/env python3
"""Apply publication-mode policy to the frozen draft-content gate report."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any


PREVIEW_QUALITY_CHECKS = frozenset({
    "cited_content_rich_enough", "source_diversity", "core_sections_source_grounded",
})


def apply_publication_policy(report: dict[str, Any], publication_mode: str,
                             source_gate: dict[str, Any] | None = None,
                             closure_gate: dict[str, Any] | None = None) -> dict[str, Any]:
    if publication_mode != "preview":
        report["decision"] = "PASS" if report.get("go") else "REPAIR"
        report["pipeline_continue"] = bool(report.get("go"))
        report["candidate_eligible"] = bool(report.get("go"))
        return report
    limited_upstream = bool(
        (source_gate or {}).get("decision") == "LIMITED"
        or (closure_gate or {}).get("decision") == "LIMITED"
    )
    hard_failure = False
    for page in report.get("pages") or []:
        checks = page.get("checks") or {}
        quality = {name: bool(checks.get(name)) for name in PREVIEW_QUALITY_CHECKS}
        page["quality_checks"] = quality
        page["quality_warnings"] = sorted(name for name, passed in quality.items() if not passed)
        page["raw_go"] = bool(checks) and all(checks.values())
        page["candidate_eligible"] = page["raw_go"] and bool(
            (closure_gate or {}).get("candidate_eligible", True)
        )
        hard_failure = hard_failure or any(
            not bool(passed) for name, passed in checks.items()
            if name not in PREVIEW_QUALITY_CHECKS
        )
    pages = report.get("pages") or []
    report["publication_mode"] = publication_mode
    candidate_eligible = bool(pages) and all(bool(page.get("candidate_eligible")) for page in pages)
    if candidate_eligible:
        decision, pipeline_continue = "PASS", True
    elif limited_upstream and not hard_failure:
        decision, pipeline_continue = "LIMITED", True
    else:
        decision, pipeline_continue = "REPAIR", False
    report.update({
        "go": candidate_eligible,
        "decision": decision,
        "pipeline_continue": pipeline_continue,
        "candidate_eligible": candidate_eligible,
        "maturity_ceiling": "wiki_candidate" if candidate_eligible else "evidence_limited",
        "disposition": ("ready_for_content_apply" if decision == "PASS"
                        else "ready_for_diagnostic_apply" if decision == "LIMITED"
                        else "blocked_before_content_apply"),
    })
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--gate-script", type=Path, required=True)
    parser.add_argument("--publication-mode", choices=("preview", "reviewed"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-gate", type=Path)
    parser.add_argument("--closure-gate", type=Path)
    args = parser.parse_args()
    gate_script = args.gate_script.resolve()
    spec = importlib.util.spec_from_file_location("frozen_wiki_draft_content_gate", gate_script)
    if not spec or not spec.loader:
        raise RuntimeError("cannot load frozen draft content gate")
    sys.path.insert(0, str(gate_script.parent))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source_gate = (json.loads(args.source_gate.read_text(encoding="utf-8"))
                   if args.source_gate else None)
    closure_gate = (json.loads(args.closure_gate.read_text(encoding="utf-8"))
                    if args.closure_gate else None)
    report = apply_publication_policy(module.gate_merge_plan(args.plan.resolve()),
                                      args.publication_mode, source_gate, closure_gate)
    report["plan_sha256"] = hashlib.sha256(args.plan.resolve().read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["pipeline_continue"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
